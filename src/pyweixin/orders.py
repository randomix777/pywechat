"""Tenant-scoped order workflow with demo and optional schema-validated parsers.

## 两个容易被误读的名字

**``plans.ai_quota`` / ``plans.ai_used`` 是死列。**
只写不读 —— 从未被 SELECT，从未参与任何判断。真正的余额在 product.db 的
subscriptions 里（``quota_micro_usd`` 等，整数微美元）。

它的默认值 10000 看起来很像"额度"，而实际余额可能已经是 0。
**不要把它当成余额读** —— 照它判断会得出「商户额度充足」，
而商户其实已经欠费。

**``parse_runs.usage_units`` 是 token 数**（input + output），
不是金额、也不是次数。名字里的 "units" 指的是 token 用量，
别当成"额度"改掉。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .wechat_time import sent_epoch

#: 商品条目的字段清单**从契约取**，不在这儿另写一份 ——
#: 两处各写各的，契约一改这边就静默地把全部模型输出拒掉。
#: 契约的唯一出处：商品条目的字段清单、整份 JSON Schema、提示词、
#: 意图枚举。这里一律引用，不再各写一份。
#:
#: 之前这个文件里整份 schema（连 intent 枚举都是抄的）硬编码过。契约一改 ——
#: 为了“客户不报 SKU”给 items 加 said/brand/category —— 它就和契约不一致了，
#: 校验失败时只说“结构不符”，没人看得出是哪一边过时了。
try:
    from order_agent.contract import INTENTS, ITEM_KEYS, OUTPUT_SCHEMA
    from order_agent.plugin import SYSTEM_PROMPT as PARSER_SYSTEM_PROMPT
except ImportError:  # pragma: no cover
    # 降级兜底必须和契约**一致** —— 不一致就等于在降级时制造一个
    # 永远不能用的契约。此分支被 tests/test_contract_single_source.py
    # 的静态检查监督，改字面量时必须同时改 contract 里那一份。
    ITEM_KEYS = ("said", "brand", "category", "sku", "quantity", "unit", "evidence")
    OUTPUT_SCHEMA = {"type": "object", "required": []}
    PARSER_SYSTEM_PROMPT = ""


SCHEMA_VERSION = 4

ALLOWED_INTENTS = ("new_order", "append", "modify", "cancel", "inquiry", "follow_up", "payment_claim", "chitchat", "unclear")

#: Adapter name recorded in ``parse_runs`` for suggestions produced by the cloud
#: gateway, so a cloud suggestion and a local model run stay distinguishable.
CLOUD_ADAPTER = "cloud-parse"

#: 微信导出的 ``消息类型`` 是中文，而解析器只认内部英文标识。以前直接把
#: 中文存进去会让**每一条文字消息**都被当成"不支持的媒体"丢进人工核对 ——
#: 结果就是永远不自动记单。这里在入库这一道关口统一归一化。
KIND_ALIASES = {
    "文本": "text", "文字": "text", "text": "text", "text_message": "text",
    "图片": "image", "图像": "image", "image": "image",
    "语音": "voice", "voice": "voice", "audio": "voice",
    "视频": "video", "video": "video",
    "文件": "file", "file": "file",
    "链接": "link", "link": "link",
    "引用": "quote", "quote": "quote",
    "名片": "card", "card": "card",
    "位置": "location", "location": "location",
    "转账": "transfer", "transfer": "transfer",
    "红包": "red_packet", "red_packet": "red_packet",
    "聊天记录": "history", "history": "history",
    "系统消息": "other", "other": "other", "unknown": "other",
}

#: 解析器可以处理的消息类型。其余一律转人工，不静默丢弃。
PARSABLE_KINDS = ("text", "text_message")


def normalize_message_kind(value: Any) -> str:
    """把微信的中文消息类型归一化成内部英文标识。"""
    text = str(value or "").strip()
    if not text:
        return "text"
    if text in KIND_ALIASES:
        return KIND_ALIASES[text]
    lowered = text.lower()
    return KIND_ALIASES.get(lowered, "other")


def _sender_needs_upgrade(old: Any, new: Any) -> bool:
    """localdb 重读时，旧发送人值得被新名字替换吗。

    只升不降：旧的是空/占位/wxid/群名/XML 碎片，且新的是干干净净的人名，
    才换。其它情况一律不动 —— 认错人比没认出来严重。
    """
    old_text, new_text = str(old or "").strip(), str(new or "").strip()
    if not new_text or new_text == old_text:
        return False
    if "</" in new_text or ">" in new_text:
        return False
    if not old_text or "未识别" in old_text:
        return True
    if old_text.endswith("@chatroom") or old_text.startswith(("wxid_", "gh_")):
        return True
    return False


class OpenAICompatibleParser:
    """Schema-constrained adapter for OpenAI-compatible Chat Completions APIs."""

    #: **整份 schema 从契约取，不在这儿抄一份。**
    #:
    #: 这里以前硬编码了整份 JSON Schema（连 intent 枚举都是抄的）。
    #: 契约一改 —— 比如为「客户不报 SKU」给 items 加上 said/brand/category ——
    #: 这里就静默地和契约不一致，而校验失败时只说「结构不符」，
    #: 没人看得出是哪一边过时了。三处各写各的，错的必然是最后抄的那个。
    schema = OUTPUT_SCHEMA

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float=45):
        self.url=base_url.rstrip("/")+"/chat/completions";self.api_key=api_key;self.model=model;self.timeout=timeout
    def parse(self, context: Mapping[str,Any]) -> tuple[dict[str,Any],dict[str,int]]:
        body={"model":self.model,"temperature":0,"messages":[{"role":"system","content":PARSER_SYSTEM_PROMPT},{"role":"user","content":json.dumps(dict(context),ensure_ascii=False,default=str)}],"response_format":{"type":"json_schema","json_schema":{"name":"order_parse","strict":True,"schema":self.schema}}}
        request=urllib.request.Request(self.url,data=json.dumps(body,ensure_ascii=False).encode(),headers={"Authorization":"Bearer "+self.api_key,"Content-Type":"application/json"},method="POST")
        try:
            with urllib.request.urlopen(request,timeout=self.timeout) as response:payload=json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError,TimeoutError,OSError,json.JSONDecodeError) as exc:raise RuntimeError(f"model request failed: {exc}") from exc
        try:
            result=json.loads(payload["choices"][0]["message"]["content"]);usage=payload.get("usage",{});tokens={"input":int(usage.get("prompt_tokens",0)),"output":int(usage.get("completion_tokens",0))}
        except (KeyError,IndexError,TypeError,ValueError) as exc:raise ValueError("model returned invalid structured response") from exc
        if not isinstance(result,dict) or set(result)!=set(self.schema["required"]):raise ValueError("model response does not match required schema")
        if result["intent"] not in set(INTENTS):raise ValueError("unsupported model intent")
        for field in ("customer_alias","target_order","delivery_date"):
            if result[field] is not None and not isinstance(result[field],str):raise ValueError(f"model field {field} must be a string or null")
        for field in ("evidence","missing_fields","ambiguities"):
            if not isinstance(result[field],list) or any(not isinstance(item,str) for item in result[field]):raise ValueError(f"model field {field} must be a string list")
        if not isinstance(result["items"],list):raise ValueError("model field items must be a list")
        for item in result["items"]:
            if not isinstance(item,dict) or set(item)!={"sku","quantity","unit","evidence"}:raise ValueError("model item does not match required schema")
            if item["sku"] is not None and not isinstance(item["sku"],str):raise ValueError("model item SKU must be a string or null")
            if item["unit"] is not None and not isinstance(item["unit"],str):raise ValueError("model item unit must be a string or null")
            if item["quantity"] is not None and (isinstance(item["quantity"],bool) or not isinstance(item["quantity"],(int,float))):raise ValueError("model item quantity must be numeric or null")
            if not isinstance(item["evidence"],list) or any(not isinstance(value,str) for value in item["evidence"]):raise ValueError("model item evidence must be a string list")
        if result["suggested_change"] is not None and not isinstance(result["suggested_change"],dict):raise ValueError("model suggested_change must be an object or null")
        return result,tokens


class OrderParseResultInvalid(ValueError):
    """A parse suggestion does not match the required structure."""


def validate_order_parse_result(result: Any) -> dict[str, Any]:
    """Canonical structural check for the fixed order-parse output shape.

    Lives next to :attr:`OpenAICompatibleParser.schema` so the cloud gateway and
    the local business layer can never disagree about what a valid suggestion is.
    Raises :class:`OrderParseResultInvalid`; callers translate that into their
    own error type.
    """
    required = set(OpenAICompatibleParser.schema["required"])
    if not isinstance(result, dict) or set(result) != required:
        raise OrderParseResultInvalid("parse result does not match the required schema")
    if result["intent"] not in ALLOWED_INTENTS:
        raise OrderParseResultInvalid("unsupported parse intent")
    for field_name in ("customer_alias", "target_order", "delivery_date"):
        if result[field_name] is not None and not isinstance(result[field_name], str):
            raise OrderParseResultInvalid(f"{field_name} must be a string or null")
    for field_name in ("evidence", "missing_fields", "ambiguities"):
        value = result[field_name]
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise OrderParseResultInvalid(f"{field_name} must be a list of strings")
        if len(value) > 64 or any(len(item) > 500 for item in value):
            raise OrderParseResultInvalid(f"{field_name} is unreasonably large")
    if not isinstance(result["items"], list) or len(result["items"]) > 100:
        raise OrderParseResultInvalid("items must be a bounded list")
    for item in result["items"]:
        if not isinstance(item, dict):
            raise OrderParseResultInvalid("item must be an object")
        # 字段清单**从契约取**，不在这儿另写一份。
        #
        # 以前这里硬编码了 `{"sku","quantity","unit","evidence"}`，和
        # order_agent.contract 各写各的。契约一改（比如为「客户不报 SKU」
        # 加上 said/brand/category），这里就静默地把**全部**模型输出拒掉，
        # 而报错只说"不符合要求的结构"，没人看得出是哪边过时了。
        if set(item) != set(ITEM_KEYS):
            raise OrderParseResultInvalid(
                "item does not match the required schema: "
                f"期望 {sorted(ITEM_KEYS)}，收到 {sorted(item)}"
            )
        if not isinstance(item["said"], str) or not item["said"].strip():
            raise OrderParseResultInvalid(
                "said 必须记录客户原话里对商品的描述，否则匹配不上时商户无从辨认"
            )
        if item["brand"] is not None and not isinstance(item["brand"], str):
            raise OrderParseResultInvalid("item brand must be a string or null")
        if item["category"] is not None and not isinstance(item["category"], str):
            raise OrderParseResultInvalid("item category must be a string or null")
        if item["sku"] is not None and not isinstance(item["sku"], str):
            raise OrderParseResultInvalid("item sku must be a string or null")
        if item["unit"] is not None and not isinstance(item["unit"], str):
            raise OrderParseResultInvalid("item unit must be a string or null")
        if item["quantity"] is not None and (isinstance(item["quantity"], bool) or not isinstance(item["quantity"], (int, float))):
            raise OrderParseResultInvalid("item quantity must be numeric or null")
        if not isinstance(item["evidence"], list) or any(not isinstance(value, str) for value in item["evidence"]):
            raise OrderParseResultInvalid("item evidence must be a list of strings")
    if result["suggested_change"] is not None and not isinstance(result["suggested_change"], dict):
        raise OrderParseResultInvalid("suggested_change must be an object or null")
    return result


def _same_quantity(left: Any, right: Any) -> bool:
    """数量相等判定。

    订单库里是 REAL（2.0），模型 JSON 里可能是 int（2）。按字符串比会把
    "2.0" 和 "2" 判成不一样 —— 那样每次重新分析都会报一堆假差异，商户
    看着全是噪声，反而看不出真正变了的那一项。
    """
    if left is None or right is None:
        return left is right
    try:
        return abs(float(left) - float(right)) < 1e-9
    except (TypeError, ValueError):
        return str(left) == str(right)


def _customer_differs(current: Mapping[str, Any] | None, suggestion: Mapping[str, Any]) -> bool | None:
    """新结果指认的客户是不是**换了个人**。

    没有当前订单时无从比较，返回 None（不算差异）。名字对得上、或新结果
    压根没提客户，都不算差异。
    """
    if current is None:
        return None
    named = str(suggestion.get("customer_alias") or "").strip()
    if not named:
        return False
    known = {str(current.get("customer") or "").strip(), str(current.get("customer_id") or "").strip()}
    known.discard("")
    return named not in known


def _match_items_to_products(items: Sequence[Mapping[str, Any]], products: Sequence[Mapping[str, Any]]) -> list[str]:
    """把「客户说了什么」匹配到商户目录里的商品，返回每条匹配到的 sku。

    ## 为什么需要这一步

    群里客户**不报 SKU**，只说「李记酱油」「上次那种」「那种大瓶的」。模型
    按契约把原话放在 ``said``、拆出的品牌品类放在 ``brand``/``category``，
    **sku 一律留 null** —— 因为猜错的代价比留空大得多：猜错了商户会照着发错货。

    所以 sku 必须由本地按目录匹配后填上。匹配不上就留空，走「转人工确认」。

    ## 匹配顺序

    先按 ``said``（客户原话，最具体），再按 ``category``（品类，较宽）。
    先精确等于别名，再退到"别名出现在原话里"。

    ## 什么时候**不**填

    * 匹配到**多于一个**商品 —— 猜一个就可能发错货，留空让商户选。
    * 一个都没匹配上 —— 留空并在 ambiguities 里说明，商户在确认卡上补。
    * 商品**已经有 sku** —— 不覆盖。契约要求模型留 null，但别的来源
      （本地规则、历史草稿）可能已经填好了。

    任何一种情况都保留 ``said``：那是客户原话，商户补 sku 时要看着它认。
    """
    resolved: list[str] = []
    for item in items:
        if item.get("sku"):
            resolved.append(str(item["sku"]))
            continue
        aliases_by_sku: dict[str, list[str]] = {}
        for product in products:
            # products 通常是 ``dict(SELECT * FROM products)`` —— **没有 aliases 列**，
            # 别名在 product_aliases 里（调用方用 group_concat 拼进来）。
            # 但也可能有人直接传 ``sqlite3.Row`` 过来，而 Row **没有 .get**。
            # 两种都按「用键取值」来读 —— dict 和 Row 都支持下标。
            def field(key: str) -> Any:
                try:
                    return product[key]
                except (KeyError, IndexError, TypeError):
                    return None
            raw = field("aliases") or ""
            names = [a for a in raw.split("|") if a] if raw else []
            name = field("name")
            sku = field("sku")
            if name:
                names.append(str(name))
            if sku:
                names.append(str(sku))
            aliases_by_sku.setdefault(str(sku), []).extend(names)

        matches: list[str] = []
        for probe in (str(item.get("said") or "").strip(),
                      str(item.get("category") or "").strip()):
            if not probe:
                continue
            exact = [sku for sku, names in aliases_by_sku.items() if probe in names]
            if exact:
                matches = sorted(exact)
                break
            loose = sorted(sku for sku, names in aliases_by_sku.items()
                           if any(name in probe for name in names))
            if loose:
                matches = loose
                break
        # \u591a\u4e2a\u5019\u9009\u65f6\u4e0d\u731c\u3002\u7559\u7a7a\u624d\u662f\u5b89\u5168\u7684\u9009\u62e9\uff1a\u5546\u6237\u8865\u624b\u7ea6\u4e00\u4e0b\u5c31\u884c\u3002
        resolved.append(matches[0] if len(matches) == 1 else "")
    return resolved


class OrderStore:
    def __init__(self, path: str | Path = "orders.db", parser: OpenAICompatibleParser | None = None):
        self.path = str(path)
        self.parser = parser
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        # 必须在建表脚本**之前**：v2 的 parse_runs 带着表级 UNIQUE 且没有
        # run_key 列，脚本里的 CREATE INDEX 会因为列不存在而直接失败。
        self._migrate_parse_runs()
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
        INSERT INTO schema_version SELECT 2 WHERE NOT EXISTS(SELECT 1 FROM schema_version);
        CREATE TABLE IF NOT EXISTS merchants(id TEXT PRIMARY KEY,name TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS merchant_settings(merchant_id TEXT PRIMARY KEY REFERENCES merchants(id),parser_mode TEXT NOT NULL CHECK(parser_mode IN ('demo-rules','model')));
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,merchant_id TEXT NOT NULL REFERENCES merchants(id),name TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('owner','operator')),token_hash TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY,merchant_id TEXT NOT NULL REFERENCES merchants(id),name TEXT NOT NULL,token_hash TEXT UNIQUE NOT NULL,revoked INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS device_groups(merchant_id TEXT NOT NULL,device_id TEXT NOT NULL,group_id TEXT NOT NULL,account_id TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(merchant_id,device_id,group_id,account_id));
        CREATE TABLE IF NOT EXISTS plans(merchant_id TEXT PRIMARY KEY REFERENCES merchants(id),plan TEXT NOT NULL,max_users INTEGER NOT NULL,max_groups INTEGER NOT NULL,ai_quota INTEGER NOT NULL,ai_used INTEGER NOT NULL DEFAULT 0,active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS customers(id TEXT PRIMARY KEY,merchant_id TEXT NOT NULL REFERENCES merchants(id),name TEXT NOT NULL,address TEXT NOT NULL DEFAULT '',preference TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,UNIQUE(merchant_id,name));
        CREATE TABLE IF NOT EXISTS aliases(id TEXT PRIMARY KEY,merchant_id TEXT NOT NULL,customer_id TEXT NOT NULL REFERENCES customers(id),group_id TEXT NOT NULL,alias TEXT NOT NULL,valid_from TEXT NOT NULL,valid_to TEXT);
        CREATE TABLE IF NOT EXISTS products(id TEXT PRIMARY KEY,merchant_id TEXT NOT NULL REFERENCES merchants(id),sku TEXT NOT NULL,name TEXT NOT NULL,unit TEXT NOT NULL,price_cents INTEGER NOT NULL,UNIQUE(merchant_id,sku));
        CREATE TABLE IF NOT EXISTS product_aliases(id TEXT PRIMARY KEY,product_id TEXT NOT NULL REFERENCES products(id),alias TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS imports(id TEXT PRIMARY KEY,merchant_id TEXT NOT NULL,group_id TEXT NOT NULL,source TEXT NOT NULL,status TEXT NOT NULL,message_count INTEGER NOT NULL DEFAULT 0,min_sent_at TEXT,max_sent_at TEXT,known_gap TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,UNIQUE(merchant_id,source));
        CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,merchant_id TEXT NOT NULL,import_id TEXT NOT NULL REFERENCES imports(id),group_id TEXT NOT NULL,sender TEXT,customer_id TEXT REFERENCES customers(id),content TEXT NOT NULL,kind TEXT NOT NULL,sent_at TEXT,sent_at_epoch REAL,observed_at TEXT NOT NULL,time_precision TEXT NOT NULL,source_id TEXT,match_confidence REAL NOT NULL,media_json TEXT NOT NULL DEFAULT '{}',UNIQUE(merchant_id,group_id,source_id));
        CREATE TABLE IF NOT EXISTS message_matches(merchant_id TEXT NOT NULL,message_id TEXT NOT NULL REFERENCES messages(id),candidate_message_id TEXT NOT NULL REFERENCES messages(id),match_basis TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',created_at TEXT NOT NULL,PRIMARY KEY(merchant_id,message_id,candidate_message_id));
        CREATE INDEX IF NOT EXISTS messages_tenant_time ON messages(merchant_id,group_id,sent_at);
        CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,merchant_id TEXT NOT NULL,customer_id TEXT REFERENCES customers(id),group_id TEXT NOT NULL,status TEXT NOT NULL,delivery_date TEXT,source_import_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,history_order_id TEXT,claim_payment INTEGER NOT NULL DEFAULT 0,change_type TEXT NOT NULL DEFAULT 'new',business_at_epoch REAL,parse_run_id TEXT);
        CREATE TABLE IF NOT EXISTS order_items(id TEXT PRIMARY KEY,order_id TEXT NOT NULL REFERENCES orders(id),product_id TEXT NOT NULL REFERENCES products(id),quantity REAL NOT NULL,unit TEXT NOT NULL,unit_price_cents INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY,merchant_id TEXT NOT NULL,order_id TEXT NOT NULL REFERENCES orders(id),message_id TEXT NOT NULL REFERENCES messages(id),role TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS memory_facts(id TEXT PRIMARY KEY,merchant_id TEXT NOT NULL,customer_id TEXT NOT NULL REFERENCES customers(id),kind TEXT NOT NULL,value TEXT NOT NULL,source_order_id TEXT REFERENCES orders(id),confirmed INTEGER NOT NULL,effective_from TEXT NOT NULL,effective_to TEXT,replaced_by TEXT);
        CREATE TABLE IF NOT EXISTS review_tasks(id TEXT PRIMARY KEY,merchant_id TEXT NOT NULL,order_id TEXT NOT NULL REFERENCES orders(id),reason TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'open');
        CREATE TABLE IF NOT EXISTS parse_runs(id TEXT PRIMARY KEY,merchant_id TEXT NOT NULL,message_id TEXT NOT NULL REFERENCES messages(id),adapter TEXT NOT NULL,status TEXT NOT NULL,usage_units INTEGER NOT NULL DEFAULT 0,error TEXT,created_at TEXT NOT NULL DEFAULT '',result_json TEXT,run_key TEXT NOT NULL DEFAULT '',model TEXT NOT NULL DEFAULT '',prompt_version TEXT NOT NULL DEFAULT '',requested_by TEXT NOT NULL DEFAULT 'auto');
        -- 一次运行 = 一条 parse_run。同一条消息可以有**多次**运行：换模型
        -- 重新分析就是新的一次，用 run_key 区分（自动运行 run_key='' ，
        -- 仍然保持"一条消息一次自动运行"的旧约束）。
        CREATE UNIQUE INDEX IF NOT EXISTS parse_runs_identity ON parse_runs(merchant_id,message_id,adapter,run_key);
        CREATE TABLE IF NOT EXISTS audit(id TEXT PRIMARY KEY,merchant_id TEXT NOT NULL,user_id TEXT,action TEXT NOT NULL,entity_id TEXT NOT NULL,before_json TEXT,after_json TEXT,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS model_usage(id TEXT PRIMARY KEY,merchant_id TEXT NOT NULL,parse_key TEXT NOT NULL UNIQUE,model TEXT NOT NULL,input_tokens INTEGER NOT NULL,output_tokens INTEGER NOT NULL,created_at TEXT NOT NULL);
        -- 独立澄清任务：没有任何订单明细、也没建空订单占位时，问客户的那一句。
        -- 与 review_tasks 的区别：review_tasks 挂在**真实订单**上；这里的
        -- order_id 可空，而且**不计入**订单数、待核对数与备货汇总。
        CREATE TABLE IF NOT EXISTS clarification_tasks(id TEXT PRIMARY KEY,merchant_id TEXT NOT NULL,message_id TEXT NOT NULL REFERENCES messages(id),group_id TEXT NOT NULL,customer_id TEXT,customer_candidates TEXT NOT NULL DEFAULT '[]',parse_run_id TEXT,order_id TEXT,reason TEXT NOT NULL,missing_fields TEXT NOT NULL DEFAULT '[]',suggested_question TEXT NOT NULL DEFAULT '',evidence TEXT NOT NULL DEFAULT '[]',status TEXT NOT NULL DEFAULT 'open',resolution TEXT NOT NULL DEFAULT '',resolved_by_message_id TEXT,resolved_order_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS clarification_open ON clarification_tasks(merchant_id,status,group_id);
        -- 同一消息 + 同一原因只留一个未关闭的澄清任务：重试、重复导入、回放
        -- 都不会把待办翻倍。
        CREATE UNIQUE INDEX IF NOT EXISTS clarification_open_unique ON clarification_tasks(merchant_id,message_id,reason) WHERE status='open';
        -- 换模型重新分析的结果：**建议**，不是订单。应用前要比对基础订单版本，
        -- 版本变了就标 stale，绝不拿旧的比较结果覆盖新订单。
        CREATE TABLE IF NOT EXISTS reanalysis_suggestions(id TEXT PRIMARY KEY,merchant_id TEXT NOT NULL,message_id TEXT NOT NULL,parse_run_id TEXT NOT NULL,model_alias TEXT NOT NULL DEFAULT '',prompt_version TEXT NOT NULL DEFAULT '',base_order_id TEXT NOT NULL DEFAULT '',base_version INTEGER NOT NULL DEFAULT 0,diff_json TEXT NOT NULL DEFAULT '{}',status TEXT NOT NULL DEFAULT 'pending',applied_order_id TEXT NOT NULL DEFAULT '',applied_at TEXT,outcome TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(merchant_id,parse_run_id));
        CREATE INDEX IF NOT EXISTS reanalysis_pending ON reanalysis_suggestions(merchant_id,status);
        """)
        try:self.db.execute("ALTER TABLE orders ADD COLUMN change_type TEXT NOT NULL DEFAULT 'new'")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():raise
        try:self.db.execute("ALTER TABLE parse_runs ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():raise
        try:self.db.execute("ALTER TABLE messages ADD COLUMN sent_at_epoch REAL")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():raise
        try:self.db.execute("ALTER TABLE messages ADD COLUMN media_json TEXT NOT NULL DEFAULT '{}'")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():raise
        try:self.db.execute("ALTER TABLE orders ADD COLUMN business_at_epoch REAL")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():raise
        try:self.db.execute("ALTER TABLE parse_runs ADD COLUMN result_json TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():raise
        try:self.db.execute("ALTER TABLE orders ADD COLUMN parse_run_id TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():raise
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS orders_parse_run_unique ON orders(merchant_id,parse_run_id) WHERE parse_run_id IS NOT NULL")
        self.db.execute("INSERT OR IGNORE INTO merchant_settings(merchant_id,parser_mode) SELECT id,? FROM merchants",("model" if parser else "demo-rules",))
        self.db.execute("UPDATE orders SET business_at_epoch=(SELECT m.sent_at_epoch FROM evidence e JOIN messages m ON m.id=e.message_id WHERE e.order_id=orders.id AND m.sent_at_epoch IS NOT NULL ORDER BY e.rowid LIMIT 1) WHERE business_at_epoch IS NULL")
        self.db.execute("UPDATE schema_version SET version=? WHERE version<?",(SCHEMA_VERSION,SCHEMA_VERSION))
        self.db.commit()

    def _migrate_parse_runs(self) -> None:
        """让一条消息可以有多次解析运行（v2 → v3）。

        v2 的 ``parse_runs`` 上有表级 ``UNIQUE(merchant_id,message_id,adapter)``，
        SQLite 改不了表级约束，只能重建表。重建前先备份一份到
        ``parse_runs_backup_v2``，万一重建后读回来不对，可以照原样还原。

        整个过程在**一个事务**里，并且以"新列是否已存在"作为幂等判据 ——
        重复打开同一个库不会重建第二次。
        """
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(parse_runs)").fetchall()}
        if not columns or "run_key" in columns:
            return
        self.db.execute("DROP TABLE IF EXISTS parse_runs_backup_v2")
        self.db.execute("CREATE TABLE parse_runs_backup_v2 AS SELECT * FROM parse_runs")
        self.db.execute("ALTER TABLE parse_runs RENAME TO parse_runs_v2")
        # ``usage_units`` 是 **token 数**（input + output），不是金额、不是次数。
        # 名字里的 "units" 指的是 token 用量，别当成"额度"改掉 ——
        # 真正的计费在 product.db 的 subscriptions，单位是整数微美元。
        self.db.executescript(
            """
            CREATE TABLE parse_runs(id TEXT PRIMARY KEY,merchant_id TEXT NOT NULL,message_id TEXT NOT NULL REFERENCES messages(id),adapter TEXT NOT NULL,status TEXT NOT NULL,usage_units INTEGER NOT NULL DEFAULT 0,error TEXT,created_at TEXT NOT NULL DEFAULT '',result_json TEXT,run_key TEXT NOT NULL DEFAULT '',model TEXT NOT NULL DEFAULT '',prompt_version TEXT NOT NULL DEFAULT '',requested_by TEXT NOT NULL DEFAULT 'auto');
            INSERT INTO parse_runs(id,merchant_id,message_id,adapter,status,usage_units,error,created_at,result_json,run_key,model,prompt_version,requested_by)
                SELECT id,merchant_id,message_id,adapter,status,usage_units,error,created_at,result_json,'' AS run_key,'' AS model,'' AS prompt_version,'auto' AS requested_by
                FROM parse_runs_v2;
            DROP TABLE parse_runs_v2;
            CREATE UNIQUE INDEX IF NOT EXISTS parse_runs_identity ON parse_runs(merchant_id,message_id,adapter,run_key);
            """
        )

    def migrate_clarification_placeholders(self, dry_run: bool = False) -> dict[str, Any]:
        """把**只作为澄清占位**的空订单，转成独立澄清任务。

        识别条件（四个都满足才算，缺一不可）：

        1. 没有任何订单明细；
        2. 状态是 ``review``（待核对占位），不是草稿也不是已生效订单；
        3. 挂着一个未解决的审核任务，且原因属于"要问客户/要商户看一眼"那几类；
        4. 从未被客户确认过（``version=1`` 且没有后续版本）。

        **不碰**：已确认订单、取消/取消待核对、任何带明细的单、以及原因属于
        "系统失败待重试"的单 —— 那些是另一种待办，不是澄清。

        幂等：已经转过的订单 ``status`` 变成 ``superseded``，第二次跑不会再选中它。
        ``dry_run=True`` 只报告不落库。
        """
        reasons = sorted(set(self.CUSTOMER_QUESTION_REASONS) | set(self.MERCHANT_ONLY_REASONS))
        placeholders = ",".join("?" for _ in reasons)
        rows = self.db.execute(
            f"""
            SELECT o.id,o.merchant_id,o.customer_id,o.group_id,o.version,
                   (SELECT e.message_id FROM evidence e WHERE e.order_id=o.id ORDER BY e.rowid LIMIT 1) AS message_id,
                   (SELECT r.reason FROM review_tasks r WHERE r.order_id=o.id AND r.status='open' ORDER BY r.rowid LIMIT 1) AS reason
            FROM orders o
            WHERE o.status='review' AND o.version=1
              AND NOT EXISTS(SELECT 1 FROM order_items i WHERE i.order_id=o.id)
              AND EXISTS(SELECT 1 FROM review_tasks r WHERE r.order_id=o.id AND r.status='open'
                         AND (r.reason IN ({placeholders}) OR r.reason LIKE '%{reasons[0]}%'
                              OR r.reason LIKE '%intent_unclear%' OR r.reason LIKE '%product_or_quantity_unresolved%'
                              OR r.reason LIKE '%target_order_unresolved%' OR r.reason LIKE '%no_eligible_historical_order_at_message_time%'
                              OR r.reason LIKE '%unidentified_customer%' OR r.reason LIKE '%unsupported_media%'
                              OR r.reason LIKE '%possible_cross_batch_duplicate%'))
            """,
            tuple(reasons),
        ).fetchall()
        moved: list[dict[str, Any]] = []
        for row in rows:
            primary = str(row["reason"] or "").split(";")[0]
            if primary not in reasons:
                # 复合原因（多个歧义用 ; 连起来）取第一个认得的。
                primary = next((r for r in reasons if r in str(row["reason"] or "")), primary)
            task = None
            if not dry_run:
                msg = self.db.execute(
                    "SELECT * FROM messages WHERE merchant_id=? AND id=?", (row["merchant_id"], row["message_id"])
                ).fetchone()
                if msg is None:
                    continue
                task = self.open_clarification(
                    row["merchant_id"], msg, {}, [primary], order_id=str(row["id"]),
                    customer_candidates=self._order_customer_candidates(row["merchant_id"], msg),
                )
                # 订单本身不删：标成 superseded，审计链、证据、来源消息都还在。
                with self.db:
                    self.db.execute(
                        "UPDATE orders SET status='superseded',updated_at=? WHERE merchant_id=? AND id=?",
                        (self.now(), row["merchant_id"], row["id"]),
                    )
                    self.db.execute(
                        "UPDATE review_tasks SET status='resolved' WHERE order_id=? AND status='open'",
                        (row["id"],),
                    )
            moved.append({
                "order_id": str(row["id"]), "merchant_id": str(row["merchant_id"]),
                "message_id": str(row["message_id"] or ""), "reason": primary,
                "clarification_id": (task or {}).get("clarification_id", ""),
            })
        return {
            "dry_run": bool(dry_run),
            "moved": len(moved),
            "details": moved,
            "note": "订单被标记为 superseded 而不是删除：证据、来源消息与审计链都保留。",
        }

    #: 系统解析失败的原因。它们是"系统没跑成"，**不是**"要问客户"，
    #: 也不是"订单需要人工改业务"。
    SYSTEM_FAILURE_REASONS = frozenset({
        "model_parse_failed",
        "cloud_suggestion_failed",
        "model_unavailable_retry_required",
    })

    def migrate_system_failure_placeholders(self, dry_run: bool = False) -> dict[str, Any]:
        """把旧版本用**空订单**承载的系统失败，迁到解析运行上。

        识别条件（四条全中）：没有任何明细、状态 ``review``、版本 1、
        挂着一个未解决的系统失败审核任务。

        **不碰**：有明细的、已确认的、取消待核对的，以及非系统失败原因
        （业务澄清、订单业务例外）的单。

        迁移是"把待办搬走"而不是删数据：订单、原消息、失败原因、解析运行、
        审计全部留痕。``dry_run=True`` 只报告不落库。幂等：处理过之后
        ``review_tasks.reason`` 不再是系统失败原因，第二次跑不会重复处理。
        """
        reasons = ",".join("?" for _ in sorted(self.SYSTEM_FAILURE_REASONS))
        rows = self.db.execute(
            f"""
            SELECT o.id,o.merchant_id,o.version,o.parse_run_id,
                   (SELECT r.reason FROM review_tasks r WHERE r.order_id=o.id AND r.status='open' ORDER BY r.rowid LIMIT 1) AS reason,
                   (SELECT e.message_id FROM evidence e WHERE e.order_id=o.id ORDER BY e.rowid LIMIT 1) AS message_id
            FROM orders o
            WHERE o.status='review' AND o.version=1
              AND NOT EXISTS(SELECT 1 FROM order_items i WHERE i.order_id=o.id)
              AND EXISTS(SELECT 1 FROM review_tasks r WHERE r.order_id=o.id AND r.status='open'
                         AND r.reason IN ({reasons}))
            """,
            tuple(sorted(self.SYSTEM_FAILURE_REASONS)),
        ).fetchall()
        moved: list[dict[str, Any]] = []
        for row in rows:
            reason = str(row["reason"] or "")
            if reason in self.CUSTOMER_QUESTION_REASONS or reason in self.MERCHANT_ONLY_REASONS:
                continue
            record = {
                "order_id": str(row["id"]),
                "merchant_id": str(row["merchant_id"]),
                "message_id": str(row["message_id"] or ""),
                "reason": reason,
                "parse_run_id": str(row["parse_run_id"] or ""),
            }
            if not dry_run:
                with self.db:
                    # 待办搬到解析运行上，审核任务就地关闭。订单不删不改：
                    # 它可能引用真实订单，删掉会断掉证据链。
                    self.db.execute(
                        "UPDATE review_tasks SET status='resolved' WHERE order_id=? AND status='open' AND reason=?",
                        (row["id"], reason),
                    )
                record["action"] = "review_task_resolved"
            moved.append(record)
        return {
            "dry_run": bool(dry_run),
            "moved": len(moved),
            "details": moved,
            "note": (
                "只把审核任务标为已解决并保留解析运行；订单本身不删不改，"
                "原消息、失败原因与审计都留痕。"
            ),
        }

    # -------------------------------------------------------- re-analysis

    #: 重新分析建议的状态。``stale`` = 基础订单版本已经变了，不能应用。
    REANALYSIS_STATES = ("pending", "applied", "dismissed", "stale")

    def start_reanalysis(self, tenant: str, message_id: str, model_alias: str = "",
                         prompt_version: str = "") -> dict[str, Any]:
        """商户主动换模型重新分析：**新建**一次明确的运行。

        与自动运行分开：``run_key`` 不同，``requested_by='merchant'``。
        同一条消息的自动运行不受影响，也不会被覆盖。
        """
        msg = self.db.execute(
            "SELECT id FROM messages WHERE merchant_id=? AND id=?", (tenant, message_id)
        ).fetchone()
        if msg is None:
            raise ValueError("message is not in this merchant's store")
        run_key = "re:" + uuid.uuid4().hex[:12]
        run_id = uuid.uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO parse_runs(id,merchant_id,message_id,adapter,status,usage_units,error,"
                "created_at,result_json,run_key,model,prompt_version,requested_by)"
                " VALUES(?,?,?,?,'running',0,NULL,?,NULL,?,?,?,'merchant')",
                (run_id, tenant, message_id, CLOUD_ADAPTER, self.now(), run_key,
                 model_alias, prompt_version),
            )
        return {"parse_run_id": run_id, "run_key": run_key, "message_id": message_id,
                "model_alias": model_alias, "requested_by": "merchant"}

    def finish_reanalysis(self, tenant: str, run_id: str, suggestion: Mapping[str, Any]) -> str:
        """把云端结果写进这次运行，并算出与当前有效订单的**差异**。

        **不改任何订单，也不发回执。** 差异只是一条待商户决定要不要应用的建议。
        """
        row = self.db.execute(
            "SELECT * FROM parse_runs WHERE merchant_id=? AND id=?", (tenant, run_id)
        ).fetchone()
        if row is None:
            raise ValueError("parse run not found")
        if row["run_key"] == "":
            raise ValueError("this is an automatic run, not a re-analysis")
        with self.db:
            self.db.execute(
                "UPDATE parse_runs SET status='complete',error=NULL,result_json=? WHERE id=?",
                (json.dumps(dict(suggestion), ensure_ascii=False), run_id),
            )
        return self.record_reanalysis_diff(tenant, run_id, suggestion)

    def current_effective_order(self, tenant: str, message_id: str) -> dict[str, Any] | None:
        """这条消息当前对应的**有效**订单版本（被替代的旧版本不算）。"""
        row = self.db.execute(
            "SELECT o.id,o.version,o.customer_id,o.delivery_date,o.status FROM orders o"
            " JOIN evidence e ON e.order_id=o.id"
            " WHERE o.merchant_id=? AND e.message_id=? AND o.status!='superseded'"
            " ORDER BY o.created_at DESC LIMIT 1",
            (tenant, message_id),
        ).fetchone()
        if row is None:
            return None
        detail = self.order_detail(tenant, row["id"])
        customer = self.db.execute(
            "SELECT name FROM customers WHERE id=? AND merchant_id=?", (row["customer_id"], tenant)
        ).fetchone() if row["customer_id"] else None
        return {"order_id": str(row["id"]), "version": int(row["version"]),
                "customer_id": str(row["customer_id"] or ""),
                "customer": str(customer["name"] or "") if customer else "",
                "delivery_date": str(row["delivery_date"] or ""),
                "status": str(row["status"]),
                "items": [{"sku": str(i.get("sku") or ""), "product_name": str(i.get("product_name") or ""),
                           "quantity": i.get("quantity"), "unit": str(i.get("unit") or "")}
                          for i in detail.get("items", [])]}

    def diff_against_effective(self, tenant: str, suggestion: Mapping[str, Any], message_id: str = "") -> dict[str, Any]:
        """新结果与当前有效订单版本的逐字段差异。

        比的是**业务字段**：商品、数量、单位、送货日期、客户归属。
        订单 id、版本号、内部时间戳都不算差异 —— 换了模型重跑一次就
        多一列 id，那不是商户关心的区别。
        """
        current = self.current_effective_order(tenant, message_id)
        proposed_items = {
            str(item.get("sku") or ""): {
                "quantity": item.get("quantity"), "unit": str(item.get("unit") or ""),
            }
            for item in suggestion.get("items", []) if item.get("sku")
        }
        current_items = {
            item["sku"]: {"quantity": item["quantity"], "unit": item["unit"]}
            for item in (current or {}).get("items", [])
        }
        added = sorted(set(proposed_items) - set(current_items))
        removed = sorted(set(current_items) - set(proposed_items))
        changed = []
        for sku in sorted(set(proposed_items) & set(current_items)):
            new, old = proposed_items[sku], current_items[sku]
            if new["unit"] != old["unit"] or not _same_quantity(new["quantity"], old["quantity"]):
                changed.append({"sku": sku, "from": old, "to": new})
        return {
            "base_order_id": (current or {}).get("order_id", ""),
            "base_version": (current or {}).get("version", 0),
            "has_current_order": current is not None,
            "items_added": added,
            "items_removed": removed,
            "items_changed": changed,
            "delivery_date_changed": (
                None if current is None
                else str(suggestion.get("delivery_date") or "") != str(current.get("delivery_date") or "")
            ),
            "delivery_date": str(suggestion.get("delivery_date") or ""),
            "current_delivery_date": str((current or {}).get("delivery_date") or ""),
            # 客户归属：新结果指认的客户与订单上的客户**不是同一个人**才算变。
            # 名字对得上是常态，不是差异 —— 否则每次重跑都被报成"客户变了"。
            "customer_changed": _customer_differs(current, suggestion),
            "suggested_customer": str(suggestion.get("customer_alias") or ""),
            "current_customer": str((current or {}).get("customer") or ""),
            "intent": str(suggestion.get("intent") or ""),
            "evidence": [str(e) for e in suggestion.get("evidence", []) or []],
        }

    @staticmethod
    def diff_is_empty(diff: Mapping[str, Any]) -> bool:
        return not (
            diff.get("items_added") or diff.get("items_removed") or diff.get("items_changed")
            or diff.get("delivery_date_changed") or diff.get("customer_changed")
        )

    def record_reanalysis_diff(self, tenant: str, run_id: str, suggestion: Mapping[str, Any]) -> str:
        """保存一次差异建议。同一运行只存一条。

        消息身份来自**运行**（parse_run），不是模型输出 —— 模型的 JSON 里
        根本没有 message_id，从那里取会永远取到空，差异就算成了"从无到有"。
        """
        run = self.db.execute(
            "SELECT message_id,model,prompt_version FROM parse_runs WHERE merchant_id=? AND id=?",
            (tenant, run_id),
        ).fetchone()
        if run is None:
            raise ValueError("parse run not found")
        message_id = str(run["message_id"] or "")
        diff = self.diff_against_effective(tenant, suggestion, message_id)
        suggestion_id = "rs_" + uuid.uuid4().hex[:14]
        now = self.now()
        with self.db:
            self.db.execute(
                "INSERT INTO reanalysis_suggestions(id,merchant_id,message_id,parse_run_id,model_alias,"
                "prompt_version,base_order_id,base_version,diff_json,status,outcome,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (suggestion_id, tenant, message_id, run_id,
                 str(run["model"] or ""), str(run["prompt_version"] or ""),
                 str(diff.get("base_order_id") or ""), int(diff.get("base_version") or 0),
                 json.dumps(diff, ensure_ascii=False, default=str),
                 "applied" if self.diff_is_empty(diff) else "pending",
                 "no_difference" if self.diff_is_empty(diff) else "",
                 now, now),
            )
        return suggestion_id

    def reanalysis_suggestion(self, tenant: str, suggestion_id: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM reanalysis_suggestions WHERE merchant_id=? AND id=?", (tenant, suggestion_id)
        ).fetchone()
        if row is None:
            raise ValueError("reanalysis suggestion not found")
        view = dict(row)
        try:
            view["diff"] = json.loads(row["diff_json"] or "{}")
        except (TypeError, ValueError):
            view["diff"] = {}
        view["has_difference"] = not self.diff_is_empty(view["diff"])
        return view

    def apply_reanalysis_suggestion(self, tenant: str, suggestion_id: str) -> dict[str, Any]:
        """应用一条差异建议。

        三条硬规则：
        1. **先比版本。** 基础订单版本已经变了就拒绝，标成 ``stale`` ——
           用旧比较结果覆盖新订单，是比不给结果更糟的事。
        2. **不直接改已生效的订单**，而是建一个新的待核对版本。
        3. **重复应用同一个建议不重复改**（状态不是 pending 就直接返回）。
        """
        row = self.reanalysis_suggestion(tenant, suggestion_id)
        if row["status"] == "applied":
            return {"applied": False, "reason": "already_applied", "suggestion": row}
        if row["status"] == "dismissed":
            return {"applied": False, "reason": "dismissed", "suggestion": row}
        if row["status"] == "stale":
            return {"applied": False, "reason": "stale_base_version", "suggestion": row}
        base_order_id = str(row["base_order_id"] or "")
        base_version = int(row["base_version"] or 0)
        if base_order_id:
            current = self.db.execute(
                "SELECT version,status FROM orders WHERE merchant_id=? AND id=?", (tenant, base_order_id)
            ).fetchone()
            newer = self.db.execute(
                "SELECT COUNT(*) AS c FROM orders WHERE merchant_id=? AND history_order_id=? AND id!=?",
                (tenant, base_order_id, base_order_id),
            ).fetchone()["c"]
            # 订单行"往前走"了：版本号变了、被替代了，或者已经有一个更新的
            # 待核对版本挂上去了。三种都要重新比较 —— 拿旧的比较结果覆盖
            # 新订单，比不给结果更糟。
            if (
                current is None
                or int(current["version"]) != base_version
                or current["status"] == "superseded"
                or int(newer) > 0
            ):
                with self.db:
                    self.db.execute(
                        "UPDATE reanalysis_suggestions SET status='stale',outcome=?,updated_at=? WHERE id=?",
                        ("基础订单已经变化，需要重新比较", self.now(), suggestion_id),
                    )
                return {"applied": False, "reason": "stale_base_version",
                        "suggestion": self.reanalysis_suggestion(tenant, suggestion_id)}
        suggestion = json.loads(
            self.db.execute("SELECT result_json FROM parse_runs WHERE id=?", (row["parse_run_id"],)).fetchone()["result_json"] or "{}"
        )
        message_id = str(row["message_id"])
        msg = self.db.execute("SELECT * FROM messages WHERE merchant_id=? AND id=?", (tenant, message_id)).fetchone()
        products = [dict(r) for r in self.db.execute("SELECT * FROM products WHERE merchant_id=?", (tenant,))]
        applied = self._apply_parse_suggestion(
            tenant, str(msg["group_id"]), msg, suggestion, str(row["parse_run_id"]),
            {"input": 0, "output": 0}, products, None, [],
        )
        with self.db:
            self.db.execute(
                "UPDATE reanalysis_suggestions SET status='applied',applied_order_id=?,applied_at=?,"
                "outcome=?,updated_at=? WHERE id=? AND status='pending'",
                (applied.get("order_id") or "", self.now(), "已生成新的待核对版本", self.now(), suggestion_id),
            )
        return {"applied": True, "reason": "ok", "result": applied,
                "suggestion": self.reanalysis_suggestion(tenant, suggestion_id)}

    def dismiss_reanalysis_suggestion(self, tenant: str, suggestion_id: str, reason: str = "") -> dict[str, Any]:
        """拒绝一条建议。**留痕**，不更新任何长期事实（记忆、别名、习惯）。"""
        with self.db:
            self.db.execute(
                "UPDATE reanalysis_suggestions SET status='dismissed',outcome=?,updated_at=?"
                " WHERE id=? AND merchant_id=? AND status='pending'",
                (reason or "商户拒绝", self.now(), suggestion_id, tenant),
            )
        return self.reanalysis_suggestion(tenant, suggestion_id)

    def pending_reanalysis_suggestions(self, tenant: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT id FROM reanalysis_suggestions WHERE merchant_id=? AND status='pending'"
            " ORDER BY created_at DESC LIMIT ?", (tenant, max(1, min(int(limit), 200)))
        ).fetchall()
        return [self.reanalysis_suggestion(tenant, row["id"]) for row in rows]

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _sent_epoch(value: str | None) -> float | None:
        """消息发送时刻。解析规则见 :mod:`pyweixin.wechat_time`。

        原来这里用 ``datetime.fromisoformat`` 且**显式拒绝**无时区输入，于是真实
        微信导出的每一条消息都是 ``NULL``：日不补零的写法直接抛异常，带时区的又
        恰好能用，合成数据（永远带 ``+08:00``）永远发现不了这个问题。
        """
        return sent_epoch(value)

    def create_merchant(self, name: str, token: str | None = None, plan: str = "demo", max_users: int = 5, max_groups: int = 3, ai_quota: int = 10000) -> dict[str, str]:
        merchant_id, token = "m_" + uuid.uuid4().hex[:12], token or uuid.uuid4().hex
        user_id = "u_" + uuid.uuid4().hex[:12]
        with self.db:
            self.db.execute("INSERT INTO merchants VALUES(?,?,?)", (merchant_id, name, self.now()))
            self.db.execute("INSERT INTO users VALUES(?,?,?,?,?)", (user_id, merchant_id, name + "老板", "owner", self._token_hash(token)))
            self.db.execute("INSERT INTO plans(merchant_id,plan,max_users,max_groups,ai_quota) VALUES(?,?,?,?,?)", (merchant_id, plan, max_users, max_groups, ai_quota))
            self.db.execute("INSERT INTO merchant_settings VALUES(?,?)",(merchant_id,"model" if self.parser else "demo-rules"))
        return {"merchant_id": merchant_id, "user_id": user_id, "token": token}

    def authenticate(self, token: str) -> dict[str, str] | None:
        h = self._token_hash(token)
        row = self.db.execute("SELECT id,merchant_id,role FROM users WHERE token_hash=? UNION ALL SELECT id,merchant_id,'device' FROM devices WHERE token_hash=? AND revoked=0 LIMIT 1", (h,h)).fetchone()
        return dict(row) if row else None

    def create_device(self, merchant_id: str, name: str, token: str | None = None) -> dict[str, str]:
        token, device_id = token or uuid.uuid4().hex, "d_" + uuid.uuid4().hex[:12]
        with self.db: self.db.execute("INSERT INTO devices VALUES(?,?,?,?,0)", (device_id,merchant_id,name,self._token_hash(token)))
        return {"device_id":device_id,"token":token}

    def bind_device_group(self, merchant_id: str, device_id: str, group_id: str, account_id: str = "default") -> None:
        with self.db:
            if not self.db.execute("SELECT 1 FROM devices WHERE merchant_id=? AND id=? AND revoked=0",(merchant_id,device_id)).fetchone():raise ValueError("device is not active for this merchant")
            self.db.execute("INSERT OR REPLACE INTO device_groups VALUES(?,?,?,?,1)",(merchant_id,device_id,group_id,account_id))

    def ingest_agent_events(self, merchant_id: str, device_id: str, agent_id: str, events: list[Mapping[str,Any]]) -> dict[str,int]:
        inserted=0; review=0; rejected=0; accepted=0
        for event in events:
            chat=str(event.get("chat_id") or "");account=str(event.get("account_id") or "default")
            if not self.db.execute("SELECT 1 FROM device_groups WHERE merchant_id=? AND device_id=? AND group_id=? AND account_id=? AND active=1",(merchant_id,device_id,chat,account)).fetchone():rejected+=1;continue
            accepted+=1
            if str(event.get("direction","")).lower() in {"outgoing","system"}:continue
            payload=dict(event);raw=payload.get("metadata",{});raw=raw if isinstance(raw,Mapping) else {};raw=raw.get("raw",{}) if isinstance(raw.get("raw",{}),Mapping) else {}
            sent=raw.get("消息发送时间") or payload.get("sent_at");kind=normalize_message_kind(payload.get("kind") or raw.get("消息类型"));content=str(payload.get("content") or raw.get("消息内容") or "")
            sid=str(payload.get("stable_id") or payload.get("event_id") or "");source="device:"+account+":"+sid if sid else "device-weak:"+hashlib.sha256(json.dumps([chat,payload.get("sender"),sent,content,payload.get("event_id")],ensure_ascii=False).encode()).hexdigest()
            source_tag=f"agent:{agent_id}:{device_id}:{account}:{chat}"
            # Always retain message type. Unsupported media is queued by parser.
            stable=bool(payload.get("stable_id")) and payload.get("identity_quality")=="stable" and not str(payload.get("stable_id")).startswith("history:")
            result=self.add_import(merchant_id,chat,[{"sender":payload.get("sender"),"content":content,"kind":kind,"sent_at":sent,"source_id":source,"stable_source_id":stable,"source_confidence":.95 if stable else .55}],source_tag,"agent_sync","Agent 上传来源 ID 可信度见消息记录")
            inserted+=result["inserted"]
            if result["inserted"]:
                try:self.parse_import(merchant_id,chat,result["import_id"])
                except Exception:review+=1
        return {"inserted":inserted,"review":review,"accepted":accepted,"rejected":rejected}

    def _plan(self, tenant: str) -> sqlite3.Row:
        row=self.db.execute("SELECT * FROM plans WHERE merchant_id=?",(tenant,)).fetchone()
        if not row or not row["active"]: raise PermissionError("merchant plan inactive")
        return row

    def add_customer(self, tenant: str, name: str, alias: str, group: str, address: str="") -> str:
        self._plan(tenant); cid="c_"+uuid.uuid4().hex[:12]
        with self.db:
            row=self.db.execute("SELECT id FROM customers WHERE merchant_id=? AND name=?",(tenant,name)).fetchone()
            if row: cid=row["id"]
            else: self.db.execute("INSERT INTO customers(id,merchant_id,name,address,created_at) VALUES(?,?,?,?,?)",(cid,tenant,name,address,self.now()))
            matches=self.db.execute("SELECT customer_id FROM aliases WHERE merchant_id=? AND group_id=? AND alias=? AND valid_to IS NULL",(tenant,group,alias)).fetchall()
            if matches and cid not in {r["customer_id"] for r in matches}:
                # Keep all competing mappings active; multiple active owners force manual resolution.
                self.db.execute("INSERT INTO aliases VALUES(?,?,?,?,?,?,NULL)",(uuid.uuid4().hex,tenant,cid,group,alias,self.now()))
            elif not matches:self.db.execute("INSERT INTO aliases VALUES(?,?,?,?,?,?,NULL)",(uuid.uuid4().hex,tenant,cid,group,alias,self.now()))
        return cid

    def bind_message_customer(self,tenant: str,message_id: str,customer_id: str,user_id: str) -> None:
        msg=self.db.execute("SELECT * FROM messages WHERE merchant_id=? AND id=?",(tenant,message_id)).fetchone()
        if not msg:raise ValueError("message not found in merchant scope")
        if not self.db.execute("SELECT 1 FROM customers WHERE merchant_id=? AND id=?",(tenant,customer_id)).fetchone():raise ValueError("customer is outside merchant")
        with self.db:
            self.db.execute("UPDATE messages SET customer_id=?,match_confidence=1.0 WHERE id=? AND merchant_id=?",(customer_id,message_id,tenant))
            if msg["sender"]:
                # **纠正必须真的改掉绑定。**
                #
                # 原来写的是「没有任何绑定时才插入」。于是发送人一旦被绑到
                # 错的人身上，纠正就什么也不写：`current` 非空，插入被跳过，
                # 下一条同一个人发的消息照样认错 —— **同一个错误永远重犯**，
                # 而那正是「记记忆用于减少错误」要消灭的东西。
                #
                # aliases 表本来就有 valid_from / valid_to，就是为此设计的：
                # 把指向**别人**的旧绑定关掉（保留历史，不删），再写新的。
                #
                # 只动同一个群里、同一个称呼、且指向别人的那些 ——
                # 别的群的叫法互不相干，不能一起关掉。
                self.db.execute(
                    "UPDATE aliases SET valid_to=? WHERE merchant_id=? AND group_id=?"
                    " AND alias=? AND valid_to IS NULL AND customer_id<>?",
                    (self.now(), tenant, msg["group_id"], msg["sender"], customer_id))
                already = self.db.execute(
                    "SELECT 1 FROM aliases WHERE merchant_id=? AND group_id=? AND alias=?"
                    " AND customer_id=? AND valid_to IS NULL",
                    (tenant, msg["group_id"], msg["sender"], customer_id)).fetchone()
                if not already:
                    self.db.execute("INSERT INTO aliases VALUES(?,?,?,?,?,?,NULL)",
                                    (uuid.uuid4().hex, tenant, customer_id, msg["group_id"],
                                     msg["sender"], self.now()))
            # Identity correction invalidates only prior parse attempts for this
            # message. Any empty identity review card is closed before replay.
            self.db.execute("UPDATE parse_runs SET status='superseded',error='customer identity corrected; reparse required' WHERE merchant_id=? AND message_id=?",(tenant,message_id))
            review_orders=self.db.execute("SELECT o.id FROM orders o JOIN evidence e ON e.order_id=o.id WHERE o.merchant_id=? AND e.message_id=? AND o.status='review' AND NOT EXISTS(SELECT 1 FROM order_items i WHERE i.order_id=o.id)",(tenant,message_id)).fetchall()
            for order in review_orders:
                self.db.execute("UPDATE orders SET status='superseded' WHERE id=?",(order["id"],))
                self.db.execute("UPDATE review_tasks SET status='resolved' WHERE merchant_id=? AND order_id=?",(tenant,order["id"]))
            self.db.execute("INSERT INTO audit VALUES(?,?,?,?,?,?,?,?)",(uuid.uuid4().hex,tenant,user_id,"bind_customer",message_id,json.dumps({"customer_id":msg["customer_id"]}),json.dumps({"customer_id":customer_id}),self.now()))

    def add_product(self, tenant: str, sku: str, name: str, unit: str, price_cents: int, aliases: Iterable[str]=()) -> str:
        self._plan(tenant); pid="p_"+uuid.uuid4().hex[:12]
        with self.db:
            self.db.execute("INSERT INTO products VALUES(?,?,?,?,?,?)",(pid,tenant,sku,name,unit,int(price_cents)))
            for alias in set(aliases)|{name,sku}: self.db.execute("INSERT INTO product_aliases VALUES(?,?,?)",(uuid.uuid4().hex,pid,alias))
        return pid

    def import_customers(self, tenant: str, rows: Iterable[Mapping[str,Any]]) -> dict[str,Any]:
        """批量导入客户数据。

        ## 为什么客户名单比商品名单更要紧

        群里说话的是**人**，而商户认人靠的是「张姐」「老李」这些称呼，
        同一个客户在不同群里可能叫法还不一样。

        名单进来了，模型才能把「张姐」对上商户心里的那个人；
        对不上时也能说「这个称呼我不认识」，而不是自己编一个人。

        ## 去重与别名

        按 **name** 去重（商户心里的那个正式称呼）：已存在则**更新地址与偏好**。

        别名用「别名」列给（逗号、顿号分隔），并进 ``aliases`` 表 ——
        那正是消息里认人用的地方。所以别名可以多给几个：
        「张姐, 张大姐, 老张家的」。

        ## 群

        别名是**按群**生效的：同一个人在 A 群叫「张姐」、在 B 群叫「小张」，
        这是两条别名记录。``group`` 列留空表示「所有群」。
        留空时写入通配群，消息里任意群都能认出来。

        ## 不抛异常

        一行看不懂就记进 ``skipped`` 并说明原因，其余照常导入 ——
        一个脏单元格不该让整批失败。
        """
        self._plan(tenant)
        created = updated = skipped = 0
        problems: list[str] = []
        for index, row in enumerate(rows, 1):
            name = str(row.get("name") or row.get("\u540d\u5b57") or row.get("\u5ba2\u6237")
                       or row.get("\u79f0\u547c") or "").strip()
            if not name:
                skipped += 1
                problems.append(f"\u7b2c {index} \u884c\uff1a\u7f3a\u5c11\u5ba2\u6237\u79f0\u547c")
                continue
            group = str(row.get("group") or row.get("\u7fa4") or "").strip()
            address = str(row.get("address") or row.get("\u5730\u5740") or "").strip()
            preference = str(row.get("preference") or row.get("\u504f\u597d")
                             or row.get("\u53e3\u5473") or "").strip()
            aliases: list[str] = []
            for key in ("aliases", "\u522b\u540d", "\u522b\u540d1"):
                raw = row.get(key)
                if not raw:
                    continue
                for piece in str(raw).replace("\uff0c", ",").replace("\u3001", ",").split(","):
                    piece = piece.strip()
                    if piece and piece not in aliases:
                        aliases.append(piece)
            existing = self.db.execute(
                "SELECT id FROM customers WHERE merchant_id=? AND name=?", (tenant, name)
            ).fetchone()
            if existing is not None:
                with self.db:
                    self.db.execute(
                        "UPDATE customers SET address=?, preference=? WHERE id=?",
                        (address or "", preference or "", existing["id"]))
                cid = existing["id"]
                updated += 1
            else:
                cid = "c_" + uuid.uuid4().hex[:12]
                with self.db:
                    self.db.execute(
                        "INSERT INTO customers(id,merchant_id,name,address,preference,created_at)"
                        " VALUES(?,?,?,?,?,?)", (cid, tenant, name, address, preference, self.now()))
                created += 1
            # \u79f0\u547c\u672c\u8eab\u4e5f\u662f\u4e00\u4e2a\u522b\u540d \u2014\u2014 \u5426\u5219\u6d88\u606f\u91cc\u53eb\u300c\u5f20\u59d0\u300d\u53cd\u800c\u8ba4\u4e0d\u51fa\u3002
            for alias in [name, *aliases]:
                self._bind_customer_alias(tenant, cid, group, alias)
        return {"created": created, "updated": updated, "skipped": skipped,
                "problems": problems}

    def _bind_customer_alias(self, tenant: str, customer_id: str, group: str, alias: str) -> None:
        """把一个称呼绑到客户身上。**已经绑过就不再写**。

        ``aliases`` 表有 ``valid_from/valid_to``，查询用的是 ``valid_to IS NULL``。
        重复写入会让同一个称呼在同一个群里出现多条有效记录，
        认人时就成了「多个候选」—— 那会让本来能自动认出的消息转人工。
        """
        if not alias:
            return
        row = self.db.execute(
            "SELECT id FROM aliases WHERE merchant_id=? AND customer_id=? AND group_id=?"
            " AND alias=? AND valid_to IS NULL", (tenant, customer_id, group, alias)).fetchone()
        if row:
            return
        with self.db:
            self.db.execute("INSERT INTO aliases VALUES(?,?,?,?,?,?,NULL)",
                            (uuid.uuid4().hex, tenant, customer_id, group, alias, self.now()))

    def import_customers_csv(self, tenant: str, text: str) -> dict[str,Any]:
        """从 CSV 文本导入客户。

        表头认中文常用名：名字/客户/称呼、群、地址、偏好、别名。
        """
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
        return self.import_customers(tenant, [dict(row) for row in reader])

    def import_products(self, tenant: str, rows: Iterable[Mapping[str,Any]]) -> dict[str,Any]:
        """批量导入商品数据。

        模型是从商户目录里匹配的，所以目录里什么，
        决定了自动匹配的命中率。手工逐条录入太慢，商户也不会手录。

        ## 重复的商品如何处理

        按 **sku** 去重：已存在则**更新价格与名称**，不再新增一条。
        重复导入不应让目录膨胀 —— 那会让同一个商品在目录里出现
        多次，模型匹配时会拿不到应该哪一个。

        ## 别名

        可以用「别名」或「别名1」列给多个（逗号、台号、等号分隔）。
        它们会一并存进 product_aliases，模型匹配时用的就是它们。

        ## 返回

        不抛异常。一行看不懂就记在 skipped 里并说明原因，
        其余的照常导入—— 一个脏单元格不应让整批失败。
        """
        self._plan(tenant)
        created = updated = skipped = 0
        problems: list[str] = []
        for index, row in enumerate(rows, 1):
            sku = str(row.get("sku") or row.get("编码") or "").strip()
            name = str(row.get("name") or row.get("名称") or row.get("商品") or "").strip()
            unit = str(row.get("unit") or row.get("单位") or "").strip()
            raw_price = row.get("price_cents")
            if raw_price in (None, ""):
                yuan = row.get("price") or row.get("价格") or row.get("单价")
                try:
                    raw_price = int(round(float(yuan) * 100)) if yuan not in (None, "") else None
                except (TypeError, ValueError):
                    raw_price = None
            try:
                price = int(raw_price) if raw_price not in (None, "") else None
            except (TypeError, ValueError):
                price = None
            aliases = []
            for key in ("aliases", "别名"):
                raw = row.get(key)
                if not raw:
                    continue
                for piece in str(raw).replace("，", ",").replace("、", ",").split(","):
                    piece = piece.strip()
                    if piece:
                        aliases.append(piece)
            if not sku and not name:
                skipped += 1
                problems.append(f"第 {index} 行：既没有编码也没有名称")
                continue
            if not name:
                skipped += 1
                problems.append(f"第 {index} 行：缺少商品名称")
                continue
            if not sku:
                # 既没编码又没名称的行进来了，那就不是商品。
                # 不能拿名称充当 sku 存进去：会在目录里留下一个
                # “编码就是中文名”的冒充商品，模型匹配时会很难很难分辨。
                sku = "SKU-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:10].upper()
            if price is None or price < 0:
                skipped += 1
                problems.append(f"第 {index} 行（{name}）：价格不合法，需要数字（单价可以留空）")
                continue
            existing = self.db.execute(
                "SELECT id FROM products WHERE merchant_id=? AND sku=?", (tenant, sku)
            ).fetchone() if sku else None
            if existing is not None:
                with self.db:
                    self.db.execute("UPDATE products SET name=?,unit=?,price_cents=? WHERE id=?",
                                    (name, unit, price, existing["id"]))
                pid = existing["id"]
                updated += 1
            else:
                pid = "p_" + uuid.uuid4().hex[:12]
                with self.db:
                    self.db.execute("INSERT INTO products VALUES(?,?,?,?,?,?)",
                                    (pid, tenant, sku or name, name, unit, price))
                created += 1
            for alias in set(aliases) | {name, sku or name}:
                if alias:
                    with self.db:
                        self.db.execute("INSERT OR IGNORE INTO product_aliases VALUES(?,?,?)",
                                        (uuid.uuid4().hex, pid, alias))
        return {"created": created, "updated": updated, "skipped": skipped, "problems": problems}

    def import_products_csv(self, tenant: str, text: str) -> dict[str,Any]:
        """从 CSV 文本导入商品。

        表头认中文常用名：编码/SKU、名称/商品、单位、
        价格/单价、别名。价格写元（如 12.50）自动换算到分。
        """
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
        return self.import_products(tenant, [dict(row) for row in reader])

    #: 交给模型的商品条数上限。目录很大时不把整本塞进去。
    CATALOG_LIMIT = 500
    #: 交给模型的客户条数上限。
    ROSTER_LIMIT = 500

    def _catalog_for_model(self, tenant: str) -> list[dict[str, Any]]:
        """给模型的商品目录，**带别名**。

        ## 为什么必须带别名

        别名是商家自己教会系统的（导入时给的、纠正时学的）。
        不带的话，商家费劲教会了「李记酱油 → 海天生抽」，模型看到的却只有
        「海天生抽」四个字 —— 它依然匹配不上，商家会觉得教了也没用。

        别名用 ``|`` 拼成一串，和 ``_match_items_to_products`` 的格式一致，
        两边看的是同一份东西。
        """
        rows = self.db.execute(
            "SELECT p.sku, p.name, p.unit, p.price_cents,"
            " group_concat(pa.alias, '|') AS aliases"
            " FROM products p LEFT JOIN product_aliases pa ON pa.product_id = p.id"
            " WHERE p.merchant_id=? GROUP BY p.id ORDER BY p.name LIMIT ?",
            (tenant, self.CATALOG_LIMIT)).fetchall()
        catalog: list[dict[str, Any]] = []
        for row in rows:
            entry: dict[str, Any] = {"sku": row["sku"], "name": row["name"], "unit": row["unit"]}
            # name 和 sku 在**上一层的字段里已经有了**，别名里再出现一次纯属浪费
            # token。add_product / import_products 会把两者都塞进别名表，
            # 那是给本地匹配用的（本地只看别名串），对模型则是重复。
            redundant = {str(row["name"]), str(row["sku"])}
            aliases = [a for a in str(row["aliases"] or "").split("|") if a and a not in redundant]
            if aliases:
                entry["aliases"] = aliases
            catalog.append(entry)
        return catalog

    def _roster_for_model(self, tenant: str, group: str) -> list[dict[str, Any]]:
        """给模型的客户名单（商家上传的那份）。

        ## 为什么需要

        商家上传了客户数据，但解析时模型只看得到**当前这一条消息已经绑定的**
        那个客户。客户身份没认出来的时候，模型手里没有任何候选，
        它只能编一个名字，或者干脆不填。

        把名单给它，它才有得选；选不出来时也能明确说「这个称呼我不认识」，
        而不是编一个。

        ## 按群过滤，但不止于本群

        ``group_id`` 为空的别名是通配的（导入时没指定群），本群也能用。
        所以查的是「本群的 + 通配的」。
        """
        rows = self.db.execute(
            "SELECT c.id, c.name, group_concat(a.alias, '|') AS aliases"
            " FROM customers c LEFT JOIN aliases a ON a.customer_id = c.id"
            " AND a.merchant_id = c.merchant_id AND a.valid_to IS NULL"
            " AND (a.group_id = ? OR a.group_id = '')"
            " WHERE c.merchant_id=? GROUP BY c.id ORDER BY c.name LIMIT ?",
            (group, tenant, self.ROSTER_LIMIT)).fetchall()
        roster: list[dict[str, Any]] = []
        for row in rows:
            entry: dict[str, Any] = {"id": row["id"], "name": row["name"]}
            aliases = [a for a in str(row["aliases"] or "").split("|") if a and a != row["name"]]
            if aliases:
                entry["aliases"] = aliases
            roster.append(entry)
        return roster

    def add_import(self, tenant: str, group: str, messages: list[Mapping[str,Any]], source: str, source_type: str="demo", known_gap: str="") -> dict[str,Any]:
        self._plan(tenant); iid="i_"+hashlib.sha256(f"{tenant}:{source}".encode()).hexdigest()[:16]
        normalized=[];overlap_candidates=0;weak_identity_count=0
        with self.db:
            existing=self.db.execute("SELECT id FROM imports WHERE merchant_id=? AND source=?",(tenant,source)).fetchone()
            if existing: iid=existing["id"]
            else: self.db.execute("INSERT INTO imports(id,merchant_id,group_id,source,status,known_gap,created_at) VALUES(?,?,?,?,?,?,?)",(iid,tenant,group,source,source_type,known_gap,self.now()))
            for n,m in enumerate(messages):
                content=str(m.get("content",m.get("消息内容",""))).strip(); sender=str(m.get("sender",m.get("消息发送人",""))).strip() or None
                sent=str(m.get("sent_at",m.get("消息发送时间",""))).strip() or None
                kind=normalize_message_kind(m.get("kind",m.get("消息类型","text")))
                media_json=json.dumps(m.get("media") or {},ensure_ascii=False,separators=(",",":"))
                source_id=str(m.get("source_id",m.get("message_id",""))).strip() or None
                if not source_id: source_id="import:"+hashlib.sha256(f"{iid}:{n}:{sender}:{sent}:{content}".encode()).hexdigest()
                stable_source=bool(m.get("stable_source_id",False))
                if not stable_source:weak_identity_count+=1
                customer=self.db.execute("SELECT a.customer_id FROM aliases a WHERE a.merchant_id=? AND a.group_id=? AND a.alias=? AND a.valid_to IS NULL",(tenant,group,sender)).fetchall()
                cid=customer[0]["customer_id"] if len({r["customer_id"] for r in customer})==1 and customer else None
                mid="msg_"+hashlib.sha256(f"{tenant}:{group}:{source_id}".encode()).hexdigest()[:20]
                epoch=self._sent_epoch(sent)
                cursor=self.db.execute("INSERT OR IGNORE INTO messages(id,merchant_id,import_id,group_id,sender,customer_id,content,kind,sent_at,sent_at_epoch,observed_at,time_precision,source_id,match_confidence,media_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(mid,tenant,iid,group,sender,cid,content,kind,sent,epoch,self.now(),"exact" if epoch is not None else ("unknown" if not sent else "timezone_unknown"),source_id,float(m.get("source_confidence",.95 if m.get("stable_source_id") else .55)),media_json))
                if cursor.rowcount:
                    normalized.append(mid)
                    if not stable_source and epoch is not None and sender:
                        batch_ids=set(normalized[:-1])
                        matches=[match for match in self.db.execute("SELECT id FROM messages WHERE merchant_id=? AND group_id=? AND sender=? AND content=? AND kind=? AND sent_at_epoch BETWEEN ? AND ? AND id<>?",(tenant,group,sender,content,kind,epoch-0.5,epoch+0.5,mid)).fetchall() if match["id"] not in batch_ids]
                        if matches:
                            for match in matches:self.db.execute("INSERT OR IGNORE INTO message_matches VALUES(?,?,?,'sender_time_content_kind','pending',?)",(tenant,mid,match["id"],self.now()))
                            overlap_candidates+=1
                elif source_id.startswith(("localdb-", "wcdb-")):
                    # 稳定 ID 重读：后认出来的信息合到同一条上，而不是多一条。
                    # localdb 的 source_id 不带发送人，wcdb 早期的解不出压缩正文，
                    # 都会留下"空壳行"（有 ID、无正文/发送人是群名）。
                    # 只升级，不降级 —— 原发送人已经是名字的、或新名字带 XML
                    # 碎片的，都不动；正文只填空不覆盖（同 ID 即同消息）。
                    old=self.db.execute(
                        "SELECT sender,sent_at,sent_at_epoch,content,media_json,kind FROM messages WHERE id=?",
                        (mid,)).fetchone()
                    if old is not None:
                        updates={}
                        if sender and "未识别" not in sender \
                                and (_sender_needs_upgrade(old["sender"], sender)
                                     or (str(old["sender"] or "").strip() == group
                                         and sender != group)):
                            updates["sender"]=sender
                        if not str(old["content"] or "").strip() and content:
                            updates["content"]=content
                        if old["media_json"] in (None, "", "{}") and media_json != "{}":
                            updates["media_json"]=media_json
                        if old["kind"] == "other" and kind == "file":
                            updates["kind"] = "file"
                        if epoch is not None and old["sent_at_epoch"] is None \
                                and (not old["sent_at"] or "未识别" in str(old["sent_at"])):
                            updates["sent_at"]=sent
                            updates["sent_at_epoch"]=epoch
                            updates["time_precision"]="exact"
                        if updates:
                            self.db.execute(
                                "UPDATE messages SET {} WHERE id=?".format(
                                    ",".join(f"{key}=?" for key in updates)),
                                (*updates.values(),mid))
            rows=self.db.execute("SELECT sent_at FROM messages WHERE import_id=? AND sent_at IS NOT NULL ORDER BY COALESCE(sent_at_epoch,strftime('%s',sent_at)),sent_at",(iid,)).fetchall()
            count=self.db.execute("SELECT COUNT(*) n FROM messages WHERE import_id=?",(iid,)).fetchone()["n"]
            gaps=[known_gap.strip()] if known_gap.strip() else []
            if weak_identity_count:gaps.append(f"{weak_identity_count} 条消息没有稳定来源 ID；精确重叠候选只比较发送人、发送时间、正文和类型，其他跨批次对齐未验证")
            if overlap_candidates:gaps.append(f"发现 {overlap_candidates} 条可能与既有批次重叠的消息，已阻止自动建单并转人工裁决")
            known_gap="；".join(gaps)
            self.db.execute("UPDATE imports SET message_count=?,min_sent_at=?,max_sent_at=?,status='complete',known_gap=? WHERE id=?",(count,rows[0][0] if rows else None,rows[-1][0] if rows else None,known_gap,iid))
        return {"import_id":iid,"inserted":len(normalized),"message_ids":list(normalized),"message_count":count,"min_sent_at":rows[0][0] if rows else None,"max_sent_at":rows[-1][0] if rows else None,"known_gap":known_gap,"overlap_candidates":overlap_candidates,"weak_identity_messages":weak_identity_count}

    def _messages(self, tenant: str, group: str, import_id: str | None = None) -> list[sqlite3.Row]:
        sql="SELECT * FROM messages WHERE merchant_id=? AND group_id=?"; args=[tenant,group]
        if import_id: sql+=" AND import_id=?"; args.append(import_id)
        return self.db.execute(sql+" ORDER BY COALESCE(sent_at_epoch,strftime('%s',observed_at)),observed_at,id",args).fetchall()

    def recent_messages(self, tenant: str, groups: Iterable[str] | None, limit: int = 200) -> list[dict[str, Any]]:
        """所选会话的最新消息（核对页用）。只读，不碰解析、不触发任何任务。

        按时间倒序，时间未知的沉底。调用方只拿来展示与导出。
        """
        names = [str(name) for name in (groups or []) if str(name or "").strip()]
        if not names:
            return []
        try:
            count = max(1, int(limit))
        except (TypeError, ValueError):
            count = 200
        placeholders = ",".join("?" for _ in names)
        rows = self.db.execute(
            "SELECT id,group_id,sender,content,kind,sent_at,sent_at_epoch,observed_at FROM messages"
            f" WHERE merchant_id=? AND group_id IN ({placeholders})"
            " ORDER BY COALESCE(sent_at_epoch,strftime('%s',observed_at)) DESC, id DESC"
            f" LIMIT {count}",
            [tenant, *names],
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            sent = row["sent_at"] or ""
            if not sent and row["observed_at"]:
                sent = str(row["observed_at"])[:16]
            out.append({
                "id": row["id"] or "",
                "group": row["group_id"] or "",
                "sender": row["sender"] or "",
                "content": row["content"] or "",
                "kind": row["kind"] or "",
                "sent_at": sent or "时间未知",
            })
        return out

    def messages_media(self, tenant: str, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """按 message_id 取 ``media_json``（导出附件时用来关联本机缓存文件）。

        核对页的 ``recent_messages`` 不带 media（页面上用不到），导出要按
        md5 找微信缓存，所以单独查一次。拿不到的记空字典，调用方照常走。
        """
        clean = [str(i) for i in (ids or []) if str(i or "").strip()]
        out: dict[str, dict[str, Any]] = {}
        for start in range(0, len(clean), 500):
            chunk = clean[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            try:
                rows = self.db.execute(
                    "SELECT id, media_json FROM messages"
                    f" WHERE merchant_id=? AND id IN ({marks})",
                    [tenant, *chunk]).fetchall()
            except Exception:
                continue
            for row in rows:
                try:
                    media = json.loads(row["media_json"] or "{}")
                except Exception:
                    media = {}
                out[str(row["id"])] = media if isinstance(media, dict) else {}
        return out

    def parse_import(self, tenant: str, group: str, import_id: str | None = None, timezone_name: str="America/Toronto") -> dict[str,int]:
        """Use configured model with deterministic validation, or explicit demo rules."""
        self._plan(tenant); rows=self._messages(tenant,group,import_id)
        mode=self.db.execute("SELECT parser_mode FROM merchant_settings WHERE merchant_id=?",(tenant,)).fetchone()
        parser_mode=mode["parser_mode"] if mode else ("model" if self.parser else "demo-rules")
        if parser_mode=="model":
            if self.parser is not None:
                result=self._parse_with_model(tenant,group,rows)
                return self._parse_result_summary(tenant,group,import_id,rows,result)
            result={"drafts":0,"review":0,"ignored":0,"duplicates":0,"failed":0}
            for msg in rows:
                prior=self.db.execute("SELECT status FROM parse_runs WHERE merchant_id=? AND message_id=? AND adapter='model-v1'",(tenant,msg["id"])).fetchone()
                if prior and prior["status"]=="complete":result["duplicates"]+=1;continue
                with self.db:
                    if prior:self.db.execute("UPDATE parse_runs SET status='failed',error='configured model unavailable; demo parser fallback is disabled' WHERE merchant_id=? AND message_id=? AND adapter='model-v1'",(tenant,msg["id"]))
                    else:self.db.execute("INSERT INTO parse_runs(id,merchant_id,message_id,adapter,status,usage_units,error,created_at) VALUES(?,?,?,'model-v1','failed',0,'configured model unavailable; demo parser fallback is disabled',?)",(uuid.uuid4().hex,tenant,msg["id"],self.now()))
                self.open_system_failure(tenant,msg,"model_unavailable_retry_required","configured model unavailable");result["failed"]+=1;result["review"]+=1
            return self._parse_result_summary(tenant,group,import_id,rows,result)
        result={"drafts":0,"review":0,"ignored":0,"duplicates":0}
        products=self.db.execute("SELECT p.*,group_concat(pa.alias,'|') aliases FROM products p LEFT JOIN product_aliases pa ON pa.product_id=p.id WHERE p.merchant_id=? GROUP BY p.id",(tenant,)).fetchall()
        def match_products(text: str):
            found=[]
            for p in products:
                if any(a and a in text for a in p["aliases"].split("|")): found.append(p)
            return found
        last_order={}
        for msg in rows:
            overlap=self._message_overlap_state(tenant,msg["id"])
            if overlap=="pending":self._review(tenant,msg,"possible_cross_batch_duplicate");result["review"]+=1;continue
            if overlap=="duplicate":result["duplicates"]+=1;continue
            mid=msg["id"]; model_run=self.db.execute("SELECT status FROM parse_runs WHERE merchant_id=? AND message_id=? AND adapter='model-v1'",(tenant,mid)).fetchone()
            if model_run:
                if model_run["status"]=="complete":result["duplicates"]+=1;continue
                with self.db:self.db.execute("UPDATE parse_runs SET status='failed',error='configured model unavailable; demo parser fallback is disabled' WHERE merchant_id=? AND message_id=? AND adapter='model-v1'",(tenant,mid))
                self.open_system_failure(tenant,msg,"model_unavailable_retry_required","configured model unavailable");result["failed"]=result.get("failed",0)+1;result["review"]+=1;continue
            parsed=self.db.execute("SELECT id,status FROM parse_runs WHERE merchant_id=? AND message_id=? AND adapter='demo-rules-v1'",(tenant,mid)).fetchone()
            if parsed and parsed["status"]!="superseded": result["duplicates"]+=1; continue
            if parsed:
                with self.db:self.db.execute("DELETE FROM parse_runs WHERE id=?",(parsed["id"],))
            with self.db: self.db.execute("INSERT INTO parse_runs(id,merchant_id,message_id,adapter,status,usage_units,error,created_at) VALUES(?,?,?,'demo-rules-v1','complete',0,NULL,?)",(uuid.uuid4().hex,tenant,mid,self.now()))
            text=msg["content"]; cid=msg["customer_id"]
            if msg["kind"] not in ("text","text_message"):
                self._review(tenant,msg,"unsupported_media:"+msg["kind"]);result["review"]+=1;continue
            if not cid:
                if any(k in text for k in ("箱","袋","斤","+1","老样子","一样","不要","取消")): self._review(tenant,msg,"unidentified_customer");result["review"]+=1
                else: result["ignored"]+=1
                continue
            normalized="".join(text.split()); products_found=match_products(normalized)
            if any(word in normalized for word in ("不要了","取消")):
                candidates=self.db.execute("SELECT id FROM orders WHERE merchant_id=? AND customer_id=? AND status IN ('draft','review','confirmed') ORDER BY created_at DESC LIMIT 2",(tenant,cid)).fetchall()
                if len(candidates)==1:
                    oid=candidates[0]["id"]; self._suggest_status(tenant,oid,"cancelled",mid);result["drafts"]+=1
                else: self._review(tenant,msg,"ambiguous_cancel");result["review"]+=1
                continue
            if "钱转了" in normalized or "已转" in normalized:
                self._review(tenant,msg,"payment_claim_requires_manual_receipt_check");result["review"]+=1;continue
            prior=self.db.execute("SELECT id FROM orders WHERE merchant_id=? AND customer_id=? AND status='confirmed' AND business_at_epoch<=? ORDER BY business_at_epoch DESC,created_at DESC LIMIT 1",(tenant,cid,msg["sent_at_epoch"])).fetchone() if msg["sent_at_epoch"] is not None else None
            is_classic="老样子" in normalized or "跟昨天一样" in normalized or "跟上次一样" in normalized
            if is_classic:
                if msg["sent_at_epoch"] is None:self._review(tenant,msg,"message_time_zone_unknown");result["review"]+=1;continue
                if not prior: self._review(tenant,msg,"no_confirmed_history");result["review"]+=1;continue
                order_id=self._new_order(tenant,cid,group,mid,history=prior["id"])
                with self.db:self.db.execute("INSERT INTO evidence VALUES(?,?,?,?,'history_reference')",(uuid.uuid4().hex,tenant,order_id,mid))
                for item in self.db.execute("SELECT * FROM order_items WHERE order_id=?",(prior["id"],)):
                    with self.db:self.db.execute("INSERT INTO order_items VALUES(?,?,?,?,?,?)",(uuid.uuid4().hex,order_id,item["product_id"],item["quantity"],item["unit"],self.db.execute("SELECT price_cents FROM products WHERE id=?",(item["product_id"],)).fetchone()[0]))
                if "不要鸡蛋" in normalized:
                    egg=self.db.execute("SELECT id FROM products WHERE merchant_id=? AND (name LIKE '%蛋%' OR sku LIKE '%egg%')",(tenant,)).fetchall()
                    for p in egg:self.db.execute("DELETE FROM order_items WHERE order_id=? AND product_id=?",(order_id,p["id"]))
                if "明天" in normalized and msg["sent_at_epoch"] is not None:
                    # Keep the sender's explicit offset when deriving a calendar date.
                    sent_at=datetime.fromisoformat(msg["sent_at"].replace("Z","+00:00"))
                    delivery=(sent_at+timedelta(days=1)).date().isoformat()
                    with self.db:self.db.execute("UPDATE orders SET delivery_date=? WHERE id=?",(delivery,order_id))
                self._attach(tenant,order_id,mid); result["drafts"]+=1; continue
            if normalized in {"+1","+1!","1"} or "再加" in normalized:
                oid=last_order.get(cid)
                if not oid:
                    cand=self.db.execute("SELECT id FROM orders WHERE merchant_id=? AND customer_id=? AND status IN ('draft','review') ORDER BY created_at DESC LIMIT 1",(tenant,cid)).fetchone();oid=cand["id"] if cand else None
                if oid and len(products_found)==1:
                    product=products_found[0]; qty=self._quantity(normalized,product["unit"])
                    with self.db:self.db.execute("INSERT INTO order_items VALUES(?,?,?,?,?,?)",(uuid.uuid4().hex,oid,product["id"],qty,product["unit"],product["price_cents"]))
                    self._attach(tenant,oid,mid);result["drafts"]+=1
                else:self._review(tenant,msg,"ambiguous_append_product_or_target");result["review"]+=1
                continue
            if not products_found:
                if any(x in normalized for x in ("箱","袋","斤","个","两")):self._review(tenant,msg,"product_not_matched");result["review"]+=1
                else:result["ignored"]+=1
                continue
            if len(products_found)>1:self._review(tenant,msg,"ambiguous_multiple_products");result["review"]+=1;continue
            product=products_found[0];qty=self._quantity(normalized,product["unit"])
            if qty is None:self._review(tenant,msg,"quantity_missing_or_ambiguous");result["review"]+=1;continue
            # “改三箱” changes an existing line; it does not append three boxes.
            change=any(w in normalized for w in ("改","改成"))
            target=last_order.get(cid)
            if change and target:
                self._change_quantity(tenant,target,product["id"],qty,mid);result["drafts"]+=1;continue
            oid=self._new_order(tenant,cid,group,mid)
            with self.db:self.db.execute("INSERT INTO order_items VALUES(?,?,?,?,?,?)",(uuid.uuid4().hex,oid,product["id"],qty,product["unit"],product["price_cents"]))
            self._attach(tenant,oid,mid);last_order[cid]=oid;result["drafts"]+=1
        return self._parse_result_summary(tenant,group,import_id,rows,result)

    def _parse_result_summary(self,tenant,group,import_id,rows,result):
        """Keep legacy message-action counters while exposing disjoint message/order counts."""
        enriched=dict(result)
        enriched.update({
            "counter_scope":"message actions and order versions are separate counts",
            "messages_total":len(rows),
            "order_action_messages":result.get("drafts",0),
            "manual_review_messages":result.get("review",0),
            "ignored_messages":result.get("ignored",0),
            "duplicate_messages":result.get("duplicates",0),
        })
        if import_id:
            order_rows=self.db.execute("SELECT status FROM orders WHERE merchant_id=? AND source_import_id=?",(tenant,import_id)).fetchall()
            enriched["order_versions_in_batch"]=len(order_rows)
            enriched["draft_orders_in_batch"]=sum(r["status"]=="draft" for r in order_rows)
            enriched["review_orders_in_batch"]=sum(r["status"] in ("review","cancel_review") for r in order_rows)
        else:
            enriched["order_versions_in_batch"]=None
            enriched["draft_orders_in_batch"]=None
            enriched["review_orders_in_batch"]=None
        return enriched

    def apply_cloud_suggestion(self,tenant,group,message_id,suggestion,job_id=""):
        """Apply a suggestion returned by the cloud gateway to local order state.

        The cloud proposes; this store decides. Prices come from the local
        catalogue, the evidence and ambiguity rules are the same ones the local
        model path uses, and a confirmed order is only ever superseded by a new
        reviewable version -- never silently rewritten. Applying the same
        suggestion twice is a no-op, so a retried upload cannot double-order.

        Returns a summary dict with ``outcome`` in ``drafts``/``review``/
        ``ignored``/``duplicate``/``rejected``.
        """
        validate_order_parse_result(suggestion)
        msg=self.db.execute("SELECT * FROM messages WHERE merchant_id=? AND id=?",(tenant,message_id)).fetchone()
        if not msg:raise ValueError("message is not in this merchant's store")
        if not str(group or msg["group_id"]):raise ValueError("group is required")
        # 下面这几种"这条消息变不成订单"的情况一律开**独立澄清任务**，
        # 不再用一张空订单占位 —— 空订单会进订单数、进待核对数，让商户以为
        # 有一笔生意要做。原始消息和解析记录都留着。
        if msg["kind"] not in ("text","text_message"):
            return self._reject_as_clarification(tenant,msg,"unsupported_media",suggestion)
        if not msg["customer_id"]:
            return self._reject_as_clarification(tenant,msg,"unidentified_customer",suggestion,
                                                 candidates=self._order_customer_candidates(tenant,msg))
        overlap=self._message_overlap_state(tenant,msg["id"])
        if overlap=="pending":
            return self._reject_as_clarification(tenant,msg,"possible_cross_batch_duplicate",suggestion)
        if overlap=="duplicate":return {"outcome":"duplicate","order_id":None,"reason":"already_duplicate","ambiguities":[]}
        prior_run=self.db.execute("SELECT id,status FROM parse_runs WHERE merchant_id=? AND message_id=? AND adapter=?",(tenant,msg["id"],CLOUD_ADAPTER)).fetchone()
        if prior_run and prior_run["status"]=="complete":
            row=self.db.execute("SELECT o.id FROM orders o JOIN evidence e ON e.order_id=o.id WHERE o.merchant_id=? AND e.message_id=? AND o.status!='superseded' AND o.parse_run_id=? LIMIT 1",(tenant,msg["id"],prior_run["id"])).fetchone()
            if row:return {"outcome":"duplicate","order_id":row["id"],"reason":"already_applied","ambiguities":[]}
            return {"outcome":"duplicate","order_id":None,"reason":"already_applied","ambiguities":[]}
        if prior_run:
            with self.db:self.db.execute("DELETE FROM parse_runs WHERE id=?",(prior_run["id"],))
        run_id=uuid.uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO parse_runs(id,merchant_id,message_id,adapter,status,usage_units,error,created_at,result_json) VALUES(?,?,?,?,'running',0,NULL,?,?)",
                            (run_id,tenant,msg["id"],CLOUD_ADAPTER,self.now(),json.dumps(dict(suggestion),ensure_ascii=False)))
        products=[dict(r) for r in self.db.execute("SELECT * FROM products WHERE merchant_id=?",(tenant,))]
        prior=self.db.execute("SELECT id,status,created_at FROM orders WHERE merchant_id=? AND customer_id=? AND status='confirmed' AND business_at_epoch<=? ORDER BY business_at_epoch DESC,created_at DESC LIMIT 1",(tenant,msg["customer_id"],msg["sent_at_epoch"])).fetchone() if msg["sent_at_epoch"] is not None else None
        recent=[]
        if msg["sent_at_epoch"] is not None:
            recent=[dict(r) for r in self.db.execute("SELECT id,sender,content,sent_at FROM messages WHERE merchant_id=? AND group_id=? AND customer_id=? AND sent_at_epoch<=? AND sent_at_epoch>=? ORDER BY sent_at_epoch",(tenant,group,msg["customer_id"],msg["sent_at_epoch"],msg["sent_at_epoch"]-90))]
        try:
            applied=self._apply_parse_suggestion(tenant,group,msg,suggestion,run_id,{"input":0,"output":0},products,prior,recent)
        except Exception as exc:
            with self.db:self.db.execute("UPDATE parse_runs SET status='failed',error=? WHERE id=?",(str(exc)[:500],run_id))
            # 系统失败记在**这次解析运行**上，不建订单。
            # 早先的做法是建一张空订单挂个 review_task —— 商户于是看到一个
            # 没有任何明细的"待核对订单"，分不清是客户没说清还是系统没跑成，
            # 还把它算进订单数与备货基数。
            return {"outcome":"failed","order_id":None,"reason":"cloud_suggestion_failed",
                    "error":str(exc)[:200],"parse_run_id":run_id,"ambiguities":[],
                    "retryable":True}
        applied["parse_run_id"]=run_id;applied["job_id"]=job_id
        return applied

    def open_system_failure(self,tenant,msg,reason,error="",parse_run_id=""):
        """记一次**系统解析失败**，挂在已有解析运行上。**不建任何订单。**

        ``parse_run_id`` 为空时，先找这条消息**已经**失败的运行挂上去 ——
        调用方通常刚把某次运行标成 failed，再建一条新记录只会让"失败次数"
        翻倍，看起来像失败了两遍。找不到才新建一条。

        失败记录**可以**引用已有的真实订单（``order_id``），但绝不修改它的
        内容或状态 —— 系统失败不等于业务变更。
        """
        run_id=parse_run_id
        if not run_id:
            row=self.db.execute(
                "SELECT id FROM parse_runs WHERE merchant_id=? AND message_id=? AND status='failed'"
                " ORDER BY created_at DESC, rowid DESC LIMIT 1",(tenant,msg["id"])).fetchone()
            run_id=str(row["id"]) if row else ""
        if not run_id:
            run_id=uuid.uuid4().hex
            with self.db:
                self.db.execute(
                    "INSERT INTO parse_runs(id,merchant_id,message_id,adapter,status,usage_units,error,created_at,run_key,requested_by)"
                    " VALUES(?,?,?,?,'failed',0,?,?,'','auto')",
                    (run_id,tenant,msg["id"],CLOUD_ADAPTER,str(error)[:500],self.now()))
        else:
            row=self.db.execute("SELECT status FROM parse_runs WHERE id=?",(run_id,)).fetchone()
            if row is None:
                with self.db:
                    self.db.execute(
                        "INSERT INTO parse_runs(id,merchant_id,message_id,adapter,status,usage_units,error,created_at,run_key,requested_by)"
                        " VALUES(?,?,?,?,'failed',0,?,?,'','auto')",
                        (run_id,tenant,msg["id"],CLOUD_ADAPTER,str(error)[:500],self.now()))
            elif row["status"]!="failed":
                with self.db:
                    self.db.execute("UPDATE parse_runs SET status='failed',error=? WHERE id=?",(str(error)[:500],run_id))
        order_id=self.db.execute(
            "SELECT o.id FROM orders o JOIN evidence e ON e.order_id=o.id"
            " WHERE o.merchant_id=? AND e.message_id=? AND o.status!='superseded'"
            " ORDER BY o.created_at DESC LIMIT 1",(tenant,msg["id"])).fetchone()
        order_id=str(order_id["id"]) if order_id else ""
        if order_id:
            # 只做记录：订单内容与状态一个字都不改。
            with self.db:
                self.db.execute(
                    "INSERT INTO review_tasks(id,merchant_id,order_id,reason,status) VALUES(?,?,?,?,'open')",
                    (uuid.uuid4().hex,tenant,order_id,reason))
                self.db.execute(
                    "INSERT OR IGNORE INTO evidence(id,merchant_id,order_id,message_id,role) VALUES(?,?,?,?,'parse_failure')",
                    (uuid.uuid4().hex,tenant,order_id,msg["id"]))
        return {"outcome":"failed","order_id":order_id or None,"reason":reason,
                "error":str(error)[:200],"parse_run_id":run_id,"retryable":True}

    def system_failures(self,tenant,limit=100):
        """未解决的系统失败。按**解析任务**定位，不依赖"有一张空订单"。

        有真实订单可引用的就引用（``order_id``），没有的 ``order_id`` 为空 ——
        失败记录与订单是两件事，不该靠建一张空订单把两者绑在一起。
        """
        rows=self.db.execute(
            "SELECT r.id run_id,r.message_id,r.error,r.created_at,r.adapter,"
            "  (SELECT t.id FROM review_tasks t WHERE t.order_id IS NOT NULL AND t.status='open'"
            "     AND t.reason IN ('model_parse_failed','cloud_suggestion_failed','model_unavailable_retry_required')"
            "     AND t.order_id IN (SELECT o.id FROM orders o JOIN evidence e ON e.order_id=o.id"
            "                         WHERE o.merchant_id=r.merchant_id AND e.message_id=r.message_id)"
            "   ORDER BY t.rowid DESC LIMIT 1) AS task_id,"
            "  (SELECT t.order_id FROM review_tasks t WHERE t.order_id IS NOT NULL AND t.status='open'"
            "     AND t.reason IN ('model_parse_failed','cloud_suggestion_failed','model_unavailable_retry_required')"
            "     AND t.order_id IN (SELECT o.id FROM orders o JOIN evidence e ON e.order_id=o.id"
            "                         WHERE o.merchant_id=r.merchant_id AND e.message_id=r.message_id)"
            "   ORDER BY t.rowid DESC LIMIT 1) AS order_id"
            " FROM parse_runs r WHERE r.merchant_id=? AND r.status='failed'"
            " ORDER BY r.created_at DESC LIMIT ?",(tenant,max(1,min(int(limit),500)))).fetchall()
        result=[]
        for r in rows:
            result.append({"task_id":r["task_id"] or "","parse_run_id":r["run_id"],
                           "message_id":r["message_id"],"order_id":r["order_id"] or "",
                           "reason":"parse_failed","error":r["error"] or "",
                           "status":"failed","created_at":r["created_at"],
                           "adapter":r["adapter"],"retryable":True,
                           "is_clarification":False,"is_system_failure":True})
        return result

    def _reject_as_clarification(self,tenant,msg,reason,suggestion,candidates=()):
        """开澄清任务并把这次解析运行记为完成。**不建订单，不改任何订单。**"""
        run_id=uuid.uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO parse_runs(id,merchant_id,message_id,adapter,status,usage_units,error,created_at,result_json)"
                " VALUES(?,?,?,?,'complete',0,NULL,?,?)",
                (run_id,tenant,msg["id"],CLOUD_ADAPTER,self.now(),json.dumps(dict(suggestion),ensure_ascii=False)))
        task=self.open_clarification(tenant,msg,suggestion,[reason],parse_run_id=run_id,customer_candidates=candidates)
        return {"outcome":"clarify","order_id":None,"clarification_id":task["clarification_id"],
                "clarification":task,"reason":reason,"parse_run_id":run_id,"ambiguities":[reason]}

    def _parse_with_model(self,tenant,group,rows):
        result={"drafts":0,"review":0,"ignored":0,"duplicates":0,"failed":0}
        products=[dict(r) for r in self.db.execute("SELECT * FROM products WHERE merchant_id=?",(tenant,))]
        for msg in rows:
            overlap=self._message_overlap_state(tenant,msg["id"])
            if overlap=="pending":self._review(tenant,msg,"possible_cross_batch_duplicate");result["review"]+=1;continue
            if overlap=="duplicate":result["duplicates"]+=1;continue
            parse_key=f"{tenant}:{msg['id']}:model-v1"
            prior_run=self.db.execute("SELECT id,status,created_at,result_json FROM parse_runs WHERE merchant_id=? AND message_id=? AND adapter='model-v1'",(tenant,msg["id"])).fetchone()
            if prior_run and prior_run["status"]=="complete":result["duplicates"]+=1;continue
            is_running=bool(prior_run and prior_run["status"]=="running")
            if is_running and prior_run["created_at"] and (datetime.now(timezone.utc)-datetime.fromisoformat(prior_run["created_at"])).total_seconds()<600:
                result["duplicates"]+=1;continue
            if is_running and prior_run["result_json"]:
                committed=self.db.execute("SELECT o.id FROM orders o JOIN evidence e ON e.order_id=o.id WHERE o.merchant_id=? AND e.message_id=? AND o.status!='superseded' LIMIT 1",(tenant,msg["id"])).fetchone()
                if committed:
                    with self.db:self.db.execute("UPDATE parse_runs SET status='complete' WHERE id=?",(prior_run["id"],))
                    result["duplicates"]+=1;continue
            if prior_run and not (is_running and prior_run["result_json"]):
                with self.db:self.db.execute("DELETE FROM parse_runs WHERE id=?",(prior_run["id"],))
            if msg["kind"] not in ("text","text_message"):
                with self.db:self.db.execute("INSERT OR IGNORE INTO parse_runs(id,merchant_id,message_id,adapter,status,usage_units,error,created_at) VALUES(?,?,?,'model-v1','complete',0,NULL,?)",(uuid.uuid4().hex,tenant,msg["id"],self.now()))
                self._review(tenant,msg,"unsupported_media:"+msg["kind"]);result["review"]+=1;continue
            if not msg["customer_id"]:
                with self.db:self.db.execute("INSERT OR IGNORE INTO parse_runs(id,merchant_id,message_id,adapter,status,usage_units,error,created_at) VALUES(?,?,?,'model-v1','complete',0,NULL,?)",(uuid.uuid4().hex,tenant,msg["id"],self.now()))
                self._review(tenant,msg,"unidentified_customer");result["review"]+=1;continue
            run_id=prior_run["id"] if is_running and prior_run["result_json"] else uuid.uuid4().hex
            if not (is_running and prior_run["result_json"]):
                with self.db:
                    cursor=self.db.execute("INSERT OR IGNORE INTO parse_runs(id,merchant_id,message_id,adapter,status,usage_units,error,created_at) VALUES(?,?,?,'model-v1','running',0,NULL,?)",(run_id,tenant,msg["id"],self.now()))
                if not cursor.rowcount:result["duplicates"]+=1;continue
            prior=self.db.execute("SELECT id,status,created_at FROM orders WHERE merchant_id=? AND customer_id=? AND status='confirmed' AND business_at_epoch<=? ORDER BY business_at_epoch DESC,created_at DESC LIMIT 1",(tenant,msg["customer_id"],msg["sent_at_epoch"])).fetchone() if msg["sent_at_epoch"] is not None else None
            recent=[]
            if msg["sent_at_epoch"] is not None:
                recent=[dict(r) for r in self.db.execute("SELECT id,sender,content,sent_at FROM messages WHERE merchant_id=? AND group_id=? AND customer_id=? AND sent_at_epoch<=? AND sent_at_epoch>=? ORDER BY sent_at_epoch",(tenant,group,msg["customer_id"],msg["sent_at_epoch"],msg["sent_at_epoch"]-90))]
            open_orders=[self.order_detail(tenant,r["id"]) for r in self.db.execute("SELECT id FROM orders WHERE merchant_id=? AND customer_id=? AND status IN ('draft','review') ORDER BY created_at DESC LIMIT 5",(tenant,msg["customer_id"]))]
            context={"message":{"id":msg["id"],"sender":msg["sender"],"content":msg["content"],"sent_at":msg["sent_at"],"group_id":group},"recent_customer_messages":recent,"open_orders":open_orders,"customer":{"id":msg["customer_id"],"name":self.db.execute("SELECT name FROM customers WHERE id=?",(msg["customer_id"],)).fetchone()[0]},"products":self._catalog_for_model(tenant),"customers":self._roster_for_model(tenant,group),"last_confirmed_order":self.order_detail(tenant,prior["id"]) if prior else None,"rules":{"prices_are_server_owned":True,"history_cutoff":msg["sent_at"] or msg["observed_at"],"untrusted_message_data":True}}
            try:
                if is_running and prior_run["result_json"]:
                    parsed=json.loads(prior_run["result_json"])
                    usage=self.db.execute("SELECT input_tokens,output_tokens FROM model_usage WHERE merchant_id=? AND parse_key=?",(tenant,parse_key)).fetchone()
                    if not usage:raise ValueError("saved model result has no usage record")
                    tokens={"input":usage["input_tokens"],"output":usage["output_tokens"]}
                else:
                    parsed,tokens=self.parser.parse(context)
                    with self.db:
                        self.db.execute("UPDATE parse_runs SET result_json=?,usage_units=?,error=NULL WHERE id=?",(json.dumps(parsed,ensure_ascii=False),tokens["input"]+tokens["output"],run_id))
                        self.db.execute("INSERT OR IGNORE INTO model_usage VALUES(?,?,?,?,?,?,?)",(uuid.uuid4().hex,tenant,parse_key,self.parser.model,tokens["input"],tokens["output"],self.now()))
            except Exception as exc:
                with self.db:self.db.execute("UPDATE parse_runs SET status='failed',error=? WHERE id=?",(str(exc)[:500],run_id))
                self.open_system_failure(tenant,msg,"model_parse_failed","model output unusable");result["failed"]+=1;result["review"]+=1;continue
            self._resolve_parse_failure_review(tenant,msg["id"])
            applied=self._apply_parse_suggestion(tenant,group,msg,parsed,run_id,tokens,products,prior,recent)
            result[applied["outcome"]]+=1
        return result

    def analyzed_message_ids(self, tenant: str) -> set[str]:
        """已经被云端解析**并落地**过的消息。

        这些消息再次提交会真的产生一次模型调用和一次扣费，而结果只会因为
        "已处理过"被判成 duplicate —— 花了钱没有任何产出。调用方必须先拿
        这个集合挡掉，才能谈"换模型重新分析"。

        只认**完成**的云端运行：失败或待确认的消息仍然要重试，不能被这里
        判定成"已分析"。
        """
        rows = self.db.execute(
            "SELECT message_id FROM parse_runs WHERE merchant_id=? AND adapter=? AND status='complete'",
            (tenant, CLOUD_ADAPTER),
        ).fetchall()
        return {str(row["message_id"]) for row in rows}

    #: 角色整理的运行标记前缀。角色整理**不占**"已整理"状态（换角色可重跑，
    #: 见 ``analyzed_message_ids`` 只认 CLOUD_ADAPTER），但它算"过了一遍 AI"，
    #: 导出认它 —— 不然角色用户永远导不出东西。
    ROLE_ADAPTER_PREFIX = "role-"

    def mark_role_organized(self, tenant: str, message_ids: list[str], role_id: str) -> int:
        """记下这批消息被某个角色整理过。返回记下条数，失败整批跳过。

        只记"整理过"这个事实，不记结论 —— 结论在报告里，换角色重跑会覆盖。
        同一角色同一批重复记是幂等的（UNIQUE 约束顶掉）。
        """
        ids = [str(mid) for mid in (message_ids or []) if str(mid or "").strip()]
        if not ids or not str(role_id or "").strip():
            return 0
        adapter = f"{self.ROLE_ADAPTER_PREFIX}{role_id}"
        now = self.now()
        done = 0
        with self.db:
            for mid in dict.fromkeys(ids):
                try:
                    self.db.execute(
                        "INSERT OR IGNORE INTO parse_runs(id,merchant_id,message_id,adapter,"
                        "status,usage_units,error,created_at,run_key,requested_by)"
                        " VALUES(?,?,?,?,'complete',0,NULL,?,?,?)",
                        (uuid.uuid4().hex, tenant, mid, adapter, now,
                         f"role-{role_id}", "role"),
                    )
                    done += 1
                except Exception:
                    continue
        return done

    def processed_message_ids(self, tenant: str) -> set[str]:
        """过了一遍 AI 的消息：订单解析完成，或任一角色整理过。

        导出只放这些 —— 没过 AI 的导出去等于绕开整理，与其计费不如直接卡住。
        注意和 ``analyzed_message_ids`` 的区别：那个只认订单链路（防重复扣费），
        这个认所有"处理过"（导出放行），两者不能混用。
        """
        rows = self.db.execute(
            "SELECT message_id FROM parse_runs WHERE merchant_id=? AND status='complete'"
            f" AND (adapter=? OR adapter LIKE '{self.ROLE_ADAPTER_PREFIX}%')",
            (tenant, CLOUD_ADAPTER),
        ).fetchall()
        return {str(row["message_id"]) for row in rows}

    # ------------------------------------------------------- clarification

    #: 付款声称但定位不到是哪一单：问客户，不猜。
    PAYMENT_UNLINKED_QUESTION = "您说的这笔钱是付哪一单？方便说下是哪一批吗？"
    #: 缺信息时**问客户**的原因。会生成建议提问文案。
    CUSTOMER_QUESTION_REASONS = {
        "intent_unclear": "这句话的意思还没看懂，方便再说一次要什么吗？",
        "unidentified_customer": "这条消息还不知道是哪位客户的，方便留个称呼吗？",
        "product_or_quantity_unresolved": "要哪个商品、几件还没说清，方便补一下吗？",
        "target_order_unresolved": "要给哪一单加/改？方便说下是哪一批吗？",
        "no_eligible_historical_order_at_message_time": "暂时找不到可以照着来的历史订单，方便再说一次吗？",
        "payment_claim_unlinked": PAYMENT_UNLINKED_QUESTION,
    }
    #: 内部待办类原因：不问客户，是商户自己要看。
    MERCHANT_ONLY_REASONS = {
        "unsupported_media": "这条不是文字消息，本软件还不能解析，请人工看一下。",
        "possible_cross_batch_duplicate": "这条和另一条可能是重复的，请确认是不是新消息。",
        # 模型给了一个目录里没有的编码 —— 已经按"没填过"处理了。
        # 这是**商户**要看的：不是客户没说清，是软件不该编一个编码出来。
        # 不列在这里的话 _clarify 会拿到空 reasons，整条任务变成"失败"而
        # 不是"待人工确认" —— 失败会让人以为软件坏了，而实际只是要人看一眼。
        "model_sku_not_in_merchant_catalog": "软件没能确定这是哪个商品，请确认一下。",
        # 按「客户说了什么」本地匹配也没匹配上。同样是商户看一眼的事。
        "product_not_resolved_locally": "商品还没对上目录，请确认一下是哪个。",
    }

    def open_clarification(
        self,
        tenant: str,
        msg: Any,
        parsed: Mapping[str, Any] | None = None,
        reasons: Iterable[str] = (),
        parse_run_id: str = "",
        order_id: str = "",
        customer_candidates: Iterable[str] = (),
    ) -> dict[str, Any]:
        """开一条**独立**澄清任务，不建任何订单。

        幂等：同一消息 + 同一原因已经有未关闭的澄清时直接返回那一条，
        所以重试、重复导入、回放都不会把待办翻倍。原始消息和解析运行都留着，
        不会因为"没有订单"就丢掉。
        """
        reasons = [r for r in dict.fromkeys(reasons) if r] or ["intent_unclear"]
        primary = reasons[0]
        existing = self.db.execute(
            "SELECT * FROM clarification_tasks WHERE merchant_id=? AND message_id=? AND reason=? AND status='open'",
            (tenant, msg["id"], primary),
        ).fetchone()
        if existing is not None:
            return self._clarification_view(existing)
        missing = [
            str(field) for field in (parsed or {}).get("missing_fields", []) or []
        ] or list(reasons)
        question = self._suggested_question(primary, parsed, msg)
        evidence = [str(e) for e in (parsed or {}).get("evidence", []) or []]
        task_id = "ct_" + uuid.uuid4().hex[:14]
        now = self.now()
        with self.db:
            self.db.execute(
                "INSERT INTO clarification_tasks(id,merchant_id,message_id,group_id,customer_id,"
                "customer_candidates,parse_run_id,order_id,reason,missing_fields,suggested_question,"
                "evidence,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?)",
                (
                    task_id, tenant, msg["id"], msg["group_id"],
                    str(msg["customer_id"]) if msg["customer_id"] else None,
                    json.dumps([str(c) for c in customer_candidates], ensure_ascii=False),
                    parse_run_id or None, order_id or None, primary,
                    json.dumps(missing, ensure_ascii=False), question,
                    json.dumps(evidence, ensure_ascii=False), now, now,
                ),
            )
        return self.clarification(tenant, task_id)

    def _suggested_question(self, reason: str, parsed: Mapping[str, Any] | None, msg: Any) -> str:
        """问客户的那一句。只对**问客户**的原因生成。"""
        base = self.CUSTOMER_QUESTION_REASONS.get(reason)
        if not base:
            return self.MERCHANT_ONLY_REASONS.get(reason, "这条消息需要人工确认。")
        items = (parsed or {}).get("items") or []
        if reason == "product_or_quantity_unresolved" and items:
            missing = [f"{item.get('sku') or '商品'} {item.get('unit') or ''}".strip() for item in items]
            return base + f"（当前识别到：{'、'.join(missing)}）"
        return base

    def clarification(self, tenant: str, task_id: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM clarification_tasks WHERE merchant_id=? AND id=?", (tenant, task_id)
        ).fetchone()
        if row is None:
            raise ValueError("clarification task not found")
        return self._clarification_view(row)

    def open_clarifications(self, tenant: str, limit: int = 100) -> list[dict[str, Any]]:
        """未关闭的澄清任务。**不含**任何订单。"""
        rows = self.db.execute(
            "SELECT * FROM clarification_tasks WHERE merchant_id=? AND status='open'"
            " ORDER BY COALESCE((SELECT m.sent_at_epoch FROM messages m WHERE m.id=clarification_tasks.message_id),0), id"
            " LIMIT ?",
            (tenant, max(1, min(int(limit), 500))),
        ).fetchall()
        return [self._clarification_view(row) for row in rows]

    def _clarification_view(self, row: sqlite3.Row) -> dict[str, Any]:
        """澄清任务视图。

        刻意**不含** order_items / 金额：不进订单数、不进待核对数、不进备货。
        """
        def load(name: str) -> Any:
            try:
                return json.loads(row[name] or "[]")
            except (TypeError, ValueError):
                return []

        customer = ""
        if row["customer_id"]:
            found = self.db.execute(
                "SELECT name FROM customers WHERE id=? AND merchant_id=?", (row["customer_id"], self._tenant_of(row))
            ).fetchone()
            customer = str(found["name"]) if found else ""
        candidates = []
        for cid in load("customer_candidates"):
            found = self.db.execute(
                "SELECT name FROM customers WHERE id=? AND merchant_id=?", (cid, self._tenant_of(row))
            ).fetchone()
            candidates.append({"customer_id": cid, "name": str(found["name"]) if found else cid})
        return {
            "clarification_id": row["id"],
            "merchant_id": row["merchant_id"],
            "message_id": row["message_id"],
            "group_id": row["group_id"],
            "customer_id": row["customer_id"] or "",
            "customer": customer,
            "customer_candidates": candidates,
            "parse_run_id": row["parse_run_id"] or "",
            "order_id": row["order_id"] or "",
            "reason": row["reason"],
            "missing_fields": load("missing_fields"),
            "suggested_question": row["suggested_question"],
            "evidence": load("evidence"),
            "status": row["status"],
            "resolution": row["resolution"],
            "resolved_by_message_id": row["resolved_by_message_id"] or "",
            "resolved_order_id": row["resolved_order_id"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "counts_as_order": False,
        }

    @staticmethod
    def _tenant_of(row: sqlite3.Row) -> str:
        return str(row["merchant_id"])

    def _apply_parse_suggestion(self,tenant,group,msg,parsed,run_id,tokens,products,prior,recent):
        """Turn a validated parse suggestion into local order state.

        Shared by the in-process model parser and by a suggestion that came back
        from the cloud gateway. Everything that decides a price, rejects a bad
        SKU, flags a weak evidence quote or leaves an order awaiting human
        review lives here, so the two sources can never drift apart. The
        suggestion is a proposal: a confirmed order is never modified in place,
        only superseded by a new reviewable version.
        """
        self._resolve_parse_failure_review(tenant,msg["id"])
        intent=parsed["intent"]
        if intent in ("chitchat","inquiry","follow_up"):
            self._finish_model_run(run_id,tokens);return {"outcome":"ignored","order_id":None,"ambiguities":[]}
        if intent=="payment_claim":
            # 必须**先于**缺商品的判断：付款声称本来就没有商品明细，若按
            # "没有明细"处理，会被当成"客户没说清要什么"去追问商品。
            return self._record_payment_claim(tenant,group,msg,parsed,run_id,tokens,prior,
                                             list(parsed["ambiguities"])+list(parsed["missing_fields"])+["customer_payment_claim_not_receipt"])
        ambiguities=list(parsed["ambiguities"])+list(parsed["missing_fields"])
        # 模型会尝试从商户目录里匹配。先验一道：
        # **它填的 sku 必须真的在目录里。** 不在就当没填过——
        # 一个编造出来的编码比空值更危险。
        _catalog_skus={str(p["sku"]) for p in products}
        for _item in parsed["items"]:
            _claimed=str(_item.get("sku") or "")
            if _claimed and _claimed not in _catalog_skus:
                _item["sku"]=""
                ambiguities.append("model_sku_not_in_merchant_catalog")
        # 目录里找不到的，按「客户说了什么」再匹配一次。
        # 模型只是转述者，最后一道不能靠它。
        _resolved = _match_items_to_products(parsed["items"], products)
        for _item, _sku in zip(parsed["items"], _resolved):
            if _sku:
                _item["sku"] = _sku
            elif not _item.get("sku"):
                # \u7559\u7a7a = \u5339\u914d\u4e0d\u4e0a\u6216\u5339\u914d\u5230\u591a\u4e2a\uff0c\u4e24\u79cd\u90fd\u8be5\u8f6c\u4eba\u5de5\u786e\u8ba4\u3002
                ambiguities.append("product_not_resolved_locally")
        if intent=="payment_claim":ambiguities.append("customer_payment_claim_not_receipt")
        if any(term in msg["content"] for term in ("老样子","跟昨天一样","跟上次一样")) and prior is None:ambiguities.append("no_eligible_historical_order_at_message_time")
        candidate=next((p for p in products if p["sku"] in {item.get("sku") for item in parsed["items"]}),None)
        # 模型自拟的编码已在上面被剔除（并记下 model_sku_not_in_merchant_catalog），
        # 所以这里的判定标准是「有商品没对上任何商品」——
        # 而不是「商品的 sku 不在目录里」。两者都要转澄清，
        # 但只有前者能做到“问客户还是问商家”的判断。
        if any(not item.get("sku") for item in parsed["items"]):ambiguities.append("sku_outside_merchant_catalog")
        evidence_text="\n".join([msg["content"]]+[r["content"] for r in recent])
        if parsed["evidence"] and not any(str(ev) in evidence_text for ev in parsed["evidence"]):ambiguities.append("evidence_not_in_source_message")
        for item in parsed["items"]:
            p=next((x for x in products if x["sku"]==item.get("sku")),None)
            if p and item.get("unit")!=p["unit"]:ambiguities.append("unit_mismatch")
            if p:
                try:valid_quantity=item.get("quantity") is not None and float(item["quantity"])>0
                except (TypeError,ValueError):valid_quantity=False
                if not valid_quantity:ambiguities.append("invalid_quantity")
        if intent in ("cancel","modify","append"):
            target=parsed.get("target_order")
            existing=self.db.execute("SELECT id FROM orders WHERE merchant_id=? AND id=? AND customer_id=? AND status IN ('draft','review','confirmed')",(tenant,target,msg["customer_id"])).fetchone() if target else None
            if not existing:
                # 不知道是给哪一单加/改：问客户，**不**新建一张订单占位。
                ambiguities.append("target_order_unresolved")
                return self._clarify(
                    tenant, group, msg, parsed, run_id, tokens, ambiguities, order_id="",
                    candidates=self._order_customer_candidates(tenant, msg),
                )
            oid=self._change_status_copy(tenant,existing["id"],"cancelled" if intent=="cancel" else "review",msg["id"],run_id)
            if intent!="cancel" and parsed["items"]:
                for item in parsed["items"]:
                    p=next((x for x in products if x["sku"]==item.get("sku")),None)
                    if p and item.get("quantity") is not None and item.get("unit")==p["unit"]:
                        if intent=="modify":
                            # 改量是**替换**：先删掉这一行再写。
                            with self.db:self.db.execute("DELETE FROM order_items WHERE order_id=? AND product_id=?",(oid,p["id"]))
                            with self.db:self.db.execute("INSERT INTO order_items VALUES(?,?,?,?,?,?)",(uuid.uuid4().hex,oid,p["id"],float(item["quantity"]),p["unit"],p["price_cents"]))
                        else:
                            # 追加是**累加到同一行**：原来 2 箱、再加 1 箱，
                            # 商户看到的应该是一行 3 箱，而不是"2 箱"和"1 箱"
                            # 两行 —— 备货汇总会加，但订单页看起来像两笔。
                            line=self.db.execute("SELECT * FROM order_items WHERE order_id=? AND product_id=?",(oid,p["id"])).fetchone()
                            if line is not None:
                                with self.db:self.db.execute("UPDATE order_items SET quantity=? WHERE id=?",(float(line["quantity"])+float(item["quantity"]),line["id"]))
                            else:
                                with self.db:self.db.execute("INSERT INTO order_items VALUES(?,?,?,?,?,?)",(uuid.uuid4().hex,oid,p["id"],float(item["quantity"]),p["unit"],p["price_cents"]))
            # 取消建议保持在 ``cancel_review``：``_review_task`` 不该把它按成普通的
            # "待核对"，否则商户在界面上分不出"这一单要取消"和"数量要改"。
            with self.db:self.db.execute("UPDATE orders SET status=? WHERE id=?",("cancel_review" if intent=="cancel" else "review",oid))
            self._review_task(tenant,oid,";".join(ambiguities) or intent+"_suggestion",keep_status=intent=="cancel")
            self._finish_model_run(run_id,tokens)
            self._auto_resolve_clarifications(tenant,msg,oid)
            return {"outcome":"drafts","order_id":oid,"ambiguities":ambiguities}
        if intent=="unclear":
            # 意图不明是**主因**，"没有商品明细"只是它的表现；先问意图，
            # 免得商户看到的是"商品没说清"而实际是整句话没看懂。
            ambiguities.append("intent_unclear")
            return self._clarify(tenant, group, msg, parsed, run_id, tokens, ambiguities)
        if not candidate or not parsed["items"]:
            ambiguities.append("product_or_quantity_unresolved")
            # 没有可用明细：建独立澄清任务，**不建空订单**。
            return self._clarify(tenant, group, msg, parsed, run_id, tokens, ambiguities)
        if "no_eligible_historical_order_at_message_time" in ambiguities:
            # 「老样子」但消息时间点之前没有可照着的订单。模型给出的明细没有
            # 依据：建澄清任务问客户要哪一批，**不**拿它当成一张新单建出去。
            return self._clarify(tenant, group, msg, parsed, run_id, tokens, ambiguities,
                                 candidates=self._order_customer_candidates(tenant, msg))
        if ambiguities:
            # 有明细、但需要人工核对：仍然是订单 + 订单关联的审核任务。
            # **明细要写进去** —— 只建一张没有明细的壳，商户既看不到软件理解
            # 成了什么，也没法在核对界面上改。
            oid=self._new_order(tenant,msg["customer_id"],group,msg["id"],parse_run_id=run_id)
            with self.db:
                for item in parsed["items"]:
                    p=next((x for x in products if x["sku"]==item.get("sku") and item.get("unit")==x["unit"]),None)
                    if p is None or item.get("quantity") is None:continue
                    try:
                        if float(item["quantity"])<=0:continue
                    except (TypeError,ValueError):continue
                    self.db.execute("INSERT INTO order_items VALUES(?,?,?,?,?,?)",(uuid.uuid4().hex,oid,p["id"],float(item["quantity"]),p["unit"],p["price_cents"]))
            self._attach(tenant,oid,msg["id"]);self._review_task(tenant,oid,";".join(dict.fromkeys(ambiguities)));self._finish_model_run(run_id,tokens)
            return {"outcome":"review","order_id":oid,"ambiguities":list(dict.fromkeys(ambiguities))}
        if intent in ("new_order","append","modify"):
            oid=self._new_order(tenant,msg["customer_id"],group,msg["id"],history=parsed.get("target_order"),change_type="modify" if intent in ("modify","append") else "new",parse_run_id=run_id)
            with self.db:
                for item in parsed["items"]:
                    p=next(x for x in products if x["sku"]==item["sku"])
                    self.db.execute("INSERT INTO order_items VALUES(?,?,?,?,?,?)",(uuid.uuid4().hex,oid,p["id"],float(item["quantity"]),p["unit"],p["price_cents"]))
                self.db.execute("UPDATE orders SET delivery_date=? WHERE id=?",(parsed["delivery_date"],oid))
            self._attach(tenant,oid,msg["id"]);self._finish_model_run(run_id,tokens)
            self._auto_resolve_clarifications(tenant,msg,oid)
            return {"outcome":"drafts","order_id":oid,"ambiguities":[]}
        if intent=="cancel":self._finish_model_run(run_id,tokens);return {"outcome":"review","order_id":None,"ambiguities":ambiguities}
        self._finish_model_run(run_id,tokens);return {"outcome":"ignored","order_id":None,"ambiguities":[]}

    def _record_payment_claim(self,tenant,group,msg,parsed,run_id,tokens,prior,ambiguities):
        """「钱转了」：只记录客户**声称**已付款，不建新单，不认定到账。

        能定位到是哪一单就挂在那张单上（``claim_payment=1``），商户在首页
        单独看到"客户声称已付款"。定位不到就问客户是哪一单 —— 猜一个就是
        把别人的单标成已付款。
        """
        target=parsed.get("target_order")
        order=None
        if target:
            order=self.db.execute(
                "SELECT id FROM orders WHERE merchant_id=? AND id=? AND customer_id=? AND status!='superseded'",
                (tenant,target,msg["customer_id"])).fetchone()
        if order is None and prior is not None:
            # 同一客户、消息时间之前、最近一张有效订单。只有**唯一**候选才行。
            rows=self.db.execute(
                "SELECT id FROM orders WHERE merchant_id=? AND customer_id=? AND status!='superseded'"
                " AND COALESCE(business_at_epoch,0)<=COALESCE(?,0) ORDER BY COALESCE(business_at_epoch,0) DESC LIMIT 2",
                (tenant,msg["customer_id"],msg["sent_at_epoch"])).fetchall()
            if len(rows)==1:order=rows[0]
        if order is None:
            return self._clarify(tenant,group,msg,parsed,run_id,tokens,
                                 ambiguities+["payment_claim_unlinked"])
        with self.db:
            self.db.execute("UPDATE orders SET claim_payment=1,updated_at=? WHERE merchant_id=? AND id=?",
                            (self.now(),tenant,order["id"]))
        self._finish_model_run(run_id,tokens)
        return {"outcome":"payment_claim","order_id":str(order["id"]),"acknowledged":False,
                "ambiguities":list(dict.fromkeys(ambiguities))}

    def _clarify(self,tenant,group,msg,parsed,run_id,tokens,ambiguities,order_id="",candidates=()):
        """开独立澄清任务并结束本次运行。**返回时没有任何订单被创建或改动。**"""
        reasons=[a for a in dict.fromkeys(ambiguities) if a in self.CUSTOMER_QUESTION_REASONS or a in self.MERCHANT_ONLY_REASONS]
        if not reasons:reasons=["intent_unclear"]
        task=self.open_clarification(tenant,msg,parsed,reasons,parse_run_id=run_id,order_id=order_id,customer_candidates=candidates)
        self._finish_model_run(run_id,tokens)
        return {"outcome":"clarify","order_id":None,"clarification_id":task["clarification_id"],
                "clarification":task,"ambiguities":list(dict.fromkeys(ambiguities))}

    def _order_customer_candidates(self,tenant,msg):
        """没有绑定客户时，给出**可能**是哪位。多个候选绝不替客户选。

        群昵称与客户的对应关系在 ``aliases`` 里，``customers`` 表本身没有
        group_id —— 早期这里写错过，SQLite 直接报 no such column。
        """
        return [row["customer_id"] for row in self.db.execute(
            "SELECT DISTINCT a.customer_id FROM aliases a WHERE a.merchant_id=? AND a.group_id=?"
            " AND (a.valid_to IS NULL OR a.valid_to>?) ORDER BY a.customer_id LIMIT 5",
            (tenant,msg["group_id"],self.now()))]

    def _auto_resolve_clarifications(self,tenant,msg,order_id):
        """客户补了信息、订单建好之后，关掉对应的澄清任务。

        **多个候选时不关联**：同一客户同一会话下若仍有两条以上未关闭的澄清，
        哪条才是"这一条"无法确定，就都不关，留给商户判断。猜错等于把
        别的客户的待办抹掉。
        """
        epoch=msg["sent_at_epoch"]
        rows=self.db.execute(
            "SELECT c.id,c.message_id,c.customer_id FROM clarification_tasks c JOIN messages m ON m.id=c.message_id"
            " WHERE c.merchant_id=? AND c.status='open' AND c.group_id=? AND c.order_id IS NULL"
            "   AND COALESCE(m.sent_at_epoch,0) <= COALESCE(?,0)"
            "   AND (c.customer_id=? OR c.customer_id IS NULL)"
            " ORDER BY COALESCE(m.sent_at_epoch,0) DESC, c.id",
            (tenant,msg["group_id"],epoch,msg["customer_id"])).fetchall()
        if not rows:return []
        same_customer=[r for r in rows if r["customer_id"] and r["customer_id"]==msg["customer_id"]]
        if len(same_customer)!=1:return []
        target=same_customer[0]
        now=self.now()
        with self.db:
            self.db.execute(
                "UPDATE clarification_tasks SET status='resolved',resolution=?,resolved_by_message_id=?,"
                "resolved_order_id=?,updated_at=? WHERE merchant_id=? AND id=? AND status='open'",
                (f"客户补充信息后已生成订单 {order_id}",msg["id"],order_id,now,tenant,target["id"]))
        return [str(target["id"])]

    def _finish_model_run(self,run_id,tokens):
        with self.db:self.db.execute("UPDATE parse_runs SET status='complete',usage_units=?,error=NULL WHERE id=?",(tokens["input"]+tokens["output"],run_id))

    def _resolve_parse_failure_review(self,tenant,message_id):
        with self.db:
            self.db.execute("UPDATE orders SET status='superseded' WHERE merchant_id=? AND status='review' AND id IN (SELECT o.id FROM orders o JOIN evidence e ON e.order_id=o.id JOIN review_tasks r ON r.order_id=o.id WHERE e.message_id=? AND r.merchant_id=? AND r.status='open' AND r.reason IN ('model_parse_failed','model_unavailable_retry_required') AND NOT EXISTS(SELECT 1 FROM order_items i WHERE i.order_id=o.id))",(tenant,message_id,tenant))
            self.db.execute("UPDATE review_tasks SET status='resolved' WHERE merchant_id=? AND status='open' AND reason IN ('model_parse_failed','model_unavailable_retry_required') AND order_id IN (SELECT o.id FROM orders o JOIN evidence e ON e.order_id=o.id WHERE e.message_id=?)",(tenant,message_id))

    def _review_task(self,tenant,oid,reason,keep_status=False):
        """挂一条订单关联的审核任务，把订单置为 ``review``。

        ``keep_status=True`` 时**不**改状态：取消建议必须留在
        ``cancel_review``，否则商户在界面上分不出"要取消"和"要改量"。
        """
        with self.db:
            if not keep_status:
                self.db.execute("UPDATE orders SET status='review' WHERE id=?",(oid,))
            self.db.execute("INSERT INTO review_tasks VALUES(?,?,?,?,'open')",(uuid.uuid4().hex,tenant,oid,reason))

    def _message_overlap_state(self,tenant,message_id):
        rows=self.db.execute("SELECT DISTINCT status FROM message_matches WHERE merchant_id=? AND message_id=?",(tenant,message_id)).fetchall()
        states={r["status"] for r in rows}
        if "pending" in states:return "pending"
        if states and states=={"duplicate"}:return "duplicate"
        return "distinct" if states else "none"

    def resolve_message_match(self,tenant,message_id,is_duplicate,user_id):
        message=self.db.execute("SELECT * FROM messages WHERE merchant_id=? AND id=?",(tenant,message_id)).fetchone()
        if not message:raise ValueError("message not found in merchant scope")
        if self._message_overlap_state(tenant,message_id)!="pending":raise ValueError("message has no pending overlap candidate")
        resolution="duplicate" if is_duplicate else "distinct"
        with self.db:
            self.db.execute("UPDATE message_matches SET status=? WHERE merchant_id=? AND message_id=? AND status='pending'",(resolution,tenant,message_id))
            orders=self.db.execute("SELECT DISTINCT o.id FROM orders o JOIN evidence e ON e.order_id=o.id WHERE o.merchant_id=? AND e.message_id=? AND o.status='review' AND NOT EXISTS(SELECT 1 FROM order_items i WHERE i.order_id=o.id)",(tenant,message_id)).fetchall()
            for order in orders:self.db.execute("UPDATE orders SET status=? WHERE merchant_id=? AND id=?",("ignored_duplicate" if is_duplicate else "superseded",tenant,order["id"]))
            self.db.execute("UPDATE review_tasks SET status='resolved' WHERE merchant_id=? AND status='open' AND order_id IN (SELECT o.id FROM orders o JOIN evidence e ON e.order_id=o.id WHERE o.merchant_id=? AND e.message_id=?)",(tenant,tenant,message_id))
            self.db.execute("UPDATE parse_runs SET status='superseded',error='message overlap manually resolved; parse again if distinct' WHERE merchant_id=? AND message_id=?",(tenant,message_id))
            self.db.execute("INSERT INTO audit VALUES(?,?,?,?,?,?,?,?)",(uuid.uuid4().hex,tenant,user_id,"resolve_message_overlap",message_id,json.dumps({"status":"pending"}),json.dumps({"status":resolution}),self.now()))
        if is_duplicate:return {"resolution":resolution,"parse":None}
        return {"resolution":resolution,"parse":self.parse_import(tenant,message["group_id"],message["import_id"])}

    @staticmethod
    def _quantity(text: str, unit: str) -> float | None:
        if text.strip() in ("+1","+1!","1"): return 1
        nums=re.findall(r"(\d+(?:\.\d+)?|[一二两三四五六七八九十]+)\s*(?:箱|袋|斤|个|份|件)?",text)
        if not nums:return None
        cn={"一":1,"两":2,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10}
        raw=nums[-1]; return float(raw) if raw[0].isdigit() else float(cn.get(raw,0)) or None

    def _new_order(self,tenant,cid,group,mid,history=None,change_type="new",parse_run_id=None):
        oid="o_"+uuid.uuid4().hex[:14]; now=self.now()
        source=self.db.execute("SELECT import_id,sent_at_epoch FROM messages WHERE merchant_id=? AND group_id=? AND id=?",(tenant,group,mid)).fetchone()
        existing=self.db.execute("SELECT id FROM orders WHERE merchant_id=? AND parse_run_id=?",(tenant,parse_run_id)).fetchone() if parse_run_id else None
        if existing:
            oid=existing["id"]
            with self.db:
                self.db.execute("DELETE FROM order_items WHERE order_id=?",(oid,));self.db.execute("DELETE FROM evidence WHERE order_id=?",(oid,));self.db.execute("DELETE FROM review_tasks WHERE order_id=?",(oid,))
                self.db.execute("UPDATE orders SET customer_id=?,group_id=?,status='draft',delivery_date=NULL,source_import_id=?,updated_at=?,history_order_id=?,change_type=?,business_at_epoch=? WHERE merchant_id=? AND id=?",(cid,group,source["import_id"] if source else None,now,history,change_type,source["sent_at_epoch"] if source else None,tenant,oid))
            return oid
        with self.db:self.db.execute("INSERT INTO orders(id,merchant_id,customer_id,group_id,status,delivery_date,source_import_id,created_at,updated_at,version,history_order_id,claim_payment,change_type,business_at_epoch,parse_run_id) VALUES(?,?,?,?,?,?,?,?,?,1,?,0,?,?,?)",(oid,tenant,cid,group,"draft",None,source["import_id"] if source else None,now,now,history,change_type,source["sent_at_epoch"] if source else None,parse_run_id))
        return oid
    def _attach(self,tenant,oid,mid):
        with self.db:self.db.execute("INSERT OR IGNORE INTO evidence VALUES(?,?,?,?, 'source')",(uuid.uuid4().hex,tenant,oid,mid))
    def _review(self,tenant,msg,reason):
        """挂一条**订单关联**的审核任务（会建一张空订单当载体）。

        只留给"系统侧待办"用：解析失败待重试、cloud_suggestion_failed。
        这些不是要问客户的问题，而且 `console.py` 的「重试解析」按钮就是按
        订单上的 ``review_tasks.reason`` 找它们的。

        **"缺信息要问客户"的情况已经不再走这里** —— 那些现在开独立澄清任务
        （见 :meth:`open_clarification`）。空订单会进订单数、进待核对数，
        商户会以为有一笔生意要做，而实际上连要什么都没说清。
        """
        existing=self.db.execute("SELECT o.id FROM orders o JOIN evidence e ON e.order_id=o.id WHERE o.merchant_id=? AND e.message_id=? AND o.status='review' ORDER BY o.created_at DESC LIMIT 1",(tenant,msg["id"])).fetchone()
        if existing:
            with self.db:self.db.execute("UPDATE review_tasks SET reason=?,status='open' WHERE merchant_id=? AND order_id=?",(reason,tenant,existing["id"]))
            return existing["id"]
        oid=self._new_order(tenant,msg["customer_id"],msg["group_id"],msg["id"])
        with self.db:
            self.db.execute("UPDATE orders SET status='review' WHERE id=?",(oid,));self._attach(tenant,oid,msg["id"])
            self.db.execute("INSERT INTO review_tasks VALUES(?,?,?,?,'open')",(uuid.uuid4().hex,tenant,oid,reason))
    def _suggest_status(self,tenant,oid,status,mid):
        # A cancellation is represented as a reviewable versioned order suggestion.
        new=self._change_status_copy(tenant,oid,status,mid)
        if new:
            with self.db:self.db.execute("INSERT INTO review_tasks VALUES(?,?,?,'cancellation_suggestion','open')",(uuid.uuid4().hex,tenant,new))
    def _change_quantity(self,tenant,oid,pid,qty,mid):
        original=self.db.execute("SELECT * FROM orders WHERE id=? AND merchant_id=?",(oid,tenant)).fetchone()
        if not original:return
        new=self._new_order(tenant,original["customer_id"],original["group_id"],mid,history=oid,change_type="modify")
        with self.db:
            for it in self.db.execute("SELECT * FROM order_items WHERE order_id=?",(oid,)).fetchall():
                q=qty if it["product_id"]==pid else it["quantity"]
                self.db.execute("INSERT INTO order_items VALUES(?,?,?,?,?,?)",(uuid.uuid4().hex,new,it["product_id"],q,it["unit"],it["unit_price_cents"]))
            if not self.db.execute("SELECT 1 FROM order_items WHERE order_id=? AND product_id=?",(new,pid)).fetchone():
                p=self.db.execute("SELECT * FROM products WHERE id=?",(pid,)).fetchone();self.db.execute("INSERT INTO order_items VALUES(?,?,?,?,?,?)",(uuid.uuid4().hex,new,pid,qty,p["unit"],p["price_cents"]))
            self.db.execute("UPDATE orders SET status='review' WHERE id=?",(new,));self._attach(tenant,new,mid)
            self.db.execute("INSERT INTO review_tasks VALUES(?,?,?,'quantity_change','open')",(uuid.uuid4().hex,tenant,new))
    def _change_status_copy(self,tenant,oid,status,mid,parse_run_id=None):
        old=self.db.execute("SELECT * FROM orders WHERE id=? AND merchant_id=?",(oid,tenant)).fetchone()
        if not old:return
        new=self._new_order(tenant,old["customer_id"],old["group_id"],mid,history=oid,change_type="cancel" if status=="cancelled" else "modify",parse_run_id=parse_run_id)
        with self.db:
            self.db.execute("UPDATE orders SET status=? WHERE id=?",("cancel_review" if status=="cancelled" else "review",new))
            for item in self.db.execute("SELECT * FROM order_items WHERE order_id=?",(oid,)).fetchall():self.db.execute("INSERT INTO order_items VALUES(?,?,?,?,?,?)",(uuid.uuid4().hex,new,item["product_id"],item["quantity"],item["unit"],item["unit_price_cents"]))
            self.db.execute("INSERT INTO evidence VALUES(?,?,?,?,'change_suggestion')",(uuid.uuid4().hex,tenant,new,mid))
        return new

    def list_orders(self,tenant,status=""):
        sql="SELECT o.*,c.name customer_name FROM orders o LEFT JOIN customers c ON c.id=o.customer_id WHERE o.merchant_id=?";args=[tenant]
        if status:sql+=" AND o.status=?";args.append(status)
        return [dict(r) for r in self.db.execute(sql+" ORDER BY o.created_at DESC",args)]
    def order_detail(self,tenant,oid):
        row=self.db.execute("SELECT o.*,c.name customer_name FROM orders o LEFT JOIN customers c ON c.id=o.customer_id WHERE o.merchant_id=? AND o.id=?",(tenant,oid)).fetchone()
        if not row:return None
        result=dict(row);result["items"]=[dict(x) for x in self.db.execute("SELECT i.*,p.sku,p.name product_name,p.unit FROM order_items i JOIN products p ON p.id=i.product_id WHERE i.order_id=?",(oid,))]
        result["evidence"]=[dict(x) for x in self.db.execute("SELECT m.id message_id,m.content,m.sender,m.sent_at,e.role FROM evidence e JOIN messages m ON m.id=e.message_id WHERE e.order_id=?",(oid,))]
        result["review_tasks"]=[dict(x) for x in self.db.execute("SELECT id,reason,status FROM review_tasks WHERE merchant_id=? AND order_id=? AND status='open'",(tenant,oid))]
        result["match_candidates"]=[dict(x) for x in self.db.execute("SELECT mm.candidate_message_id,m.sender,m.content,m.sent_at,mm.match_basis FROM message_matches mm JOIN messages m ON m.id=mm.candidate_message_id WHERE mm.merchant_id=? AND mm.message_id IN (SELECT message_id FROM evidence WHERE order_id=?) AND mm.status='pending'",(tenant,oid))]
        result["previous_order"]=self.order_detail(tenant,row["history_order_id"]) if row["history_order_id"] and row["history_order_id"]!=oid else None
        return result
    def confirm(self,tenant,oid,user_id):
        detail=self.order_detail(tenant,oid)
        if not detail:raise KeyError("order not found")
        if detail["status"] not in ("draft","review","cancel_review"):raise ValueError("only drafts and review tasks can be confirmed")
        if not detail["items"] and detail["status"]!="cancel_review":raise ValueError("order has no items")
        target_status="cancelled" if detail["status"]=="cancel_review" else "confirmed"
        with self.db:
            self.db.execute("UPDATE orders SET status=?,updated_at=?,version=version+1 WHERE id=? AND merchant_id=?",(target_status,self.now(),oid,tenant))
            if detail["change_type"] in ("modify","cancel") and detail["history_order_id"]:
                # Supersede the previous version whether it was confirmed or
                # still an unreviewed draft. Leaving a draft alive would show
                # the merchant two live rows for the same order, and an
                # unreviewed draft that was never correct must not linger as
                # if it were still an option.
                self.db.execute(
                    "UPDATE orders SET status='superseded',updated_at=? WHERE id=? AND merchant_id=? AND status IN ('draft','review','cancel_review','confirmed')",
                    (self.now(), detail["history_order_id"], tenant),
                )
            for item in detail["items"] if target_status=="confirmed" else []:
                self.db.execute("INSERT INTO memory_facts VALUES(?,?,?,?,?,?,1,?,NULL,NULL)",(uuid.uuid4().hex,tenant,detail["customer_id"],"confirmed_order",json.dumps({"sku":item["sku"],"quantity":item["quantity"],"unit":item["unit"]},ensure_ascii=False),oid,self.now()))
            self.db.execute("UPDATE review_tasks SET status='resolved' WHERE merchant_id=? AND order_id=?",(tenant,oid))
            self.db.execute("INSERT INTO audit VALUES(?,?,?,?,?,?,?,?)",(uuid.uuid4().hex,tenant,user_id,"confirm_order" if target_status=="confirmed" else "confirm_cancellation",oid,json.dumps({"status":detail["status"]}),json.dumps({"status":target_status}),self.now()))
    #: 学别名时，客户原话最长允许多少字。
    #:
    #: 「李记酱油」这种短语值得记 —— 客户下次还会这么说。
    #: 「明天下午要3箱红富士苹果」这种整句不值得记 —— 它不会一字不差地重现，
    #: 记下来只会让别名表变脏，反而干扰匹配。
    LEARNABLE_ALIAS_MAX = 12

    def _learn_from_correction(self, tenant: str, order_id: str,
                               selected: list[Mapping[str, Any]]) -> list[str]:
        """商家纠正之后，把「客户当时怎么说的」记成商品别名。

        ## 为什么值得做

        人工纠正是**质量最高的信号**：商家亲口确认了这句客户话对应哪个商品。
        只用来修一张单子、然后就丢掉，等于同一个客户明天再说一遍
        「李记酱油」，还要再问一次商家。

        ## 只在无歧义时学

        一张单子只有**一条**商品时才学。多条的时候，客户说的几个短语
        分别对应哪条商品无从知道 —— 猜错就把别名记到错的商品上，
        以后每次都会认错，比不学更糟。

        ## 学什么

        学客户原话（``said``），但**只学短的**。整句不学：它不会一字不差地
        重现，记下来只会让别名表变脏。

        ``said`` 太长时退一步学 ``category`` / ``brand`` —— 它们本来就是
        从原话里拆出来的品类与品牌，比整句可靠。

        返回实际记下的别名，方便界面告诉商家「以后会自动认出来」。
        """
        if len(selected) != 1:
            return []
        product_id = str(selected[0].get("product_id") or "")
        if not product_id:
            return []
        product = self.db.execute(
            "SELECT id, sku, name FROM products WHERE merchant_id=? AND id=?",
            (tenant, product_id)).fetchone()
        if product is None:
            return []
        phrases = self._phrases_for_order(tenant, order_id)
        learned: list[str] = []
        for phrase in phrases:
            if not phrase or len(phrase) > self.LEARNABLE_ALIAS_MAX:
                continue
            if phrase == product["name"] or phrase == product["sku"]:
                continue
            # **先把这个称呼从别的商品上摘掉。**
            #
            # 只加不撤会留下两个候选：原本「李记酱油」错绑在 P-A 上（唯一候选 →
            # 自动匹配，商家拿到错商品还不知道），纠正后变成 P-A 和 P-B 都有 ——
            # 匹配结果从「错」变成「认不出」，商家每次都要手动确认。
            #
            # 那不叫减少错误，只是把错换成了烦。纠正要**改掉**绑定，
            # 不只是加一条新的。product_aliases 没有时序列，所以只能删；
            # 删掉的是错的绑定，改动记在 audit 里。
            with self.db:
                self.db.execute(
                    "DELETE FROM product_aliases WHERE alias=? AND product_id<>?"
                    " AND product_id IN (SELECT id FROM products WHERE merchant_id=?)",
                    (phrase, product_id, tenant))
            exists = self.db.execute(
                "SELECT 1 FROM product_aliases WHERE product_id=? AND alias=?",
                (product_id, phrase)).fetchone()
            if exists:
                continue
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO product_aliases VALUES(?,?,?)",
                                (uuid.uuid4().hex, product_id, phrase))
            learned.append(phrase)
        return learned

    def _phrases_for_order(self, tenant: str, order_id: str) -> list[str]:
        """这张单子对应的解析结果里，客户是怎么说商品的。

        优先用 ``said``（客户原话），拿不到再退到 ``category`` / ``brand``。
        解析结果读不到就返回空 —— 学不到东西是可以接受的，
        拿别的东西冒充客户原话不行。
        """
        row = self.db.execute(
            "SELECT parse_run_id FROM orders WHERE merchant_id=? AND id=?",
            (tenant, order_id)).fetchone()
        if row is None or not row["parse_run_id"]:
            return []
        run = self.db.execute(
            "SELECT result_json FROM parse_runs WHERE merchant_id=? AND id=?",
            (tenant, row["parse_run_id"])).fetchone()
        if run is None or not run["result_json"]:
            return []
        try:
            parsed = json.loads(run["result_json"])
        except (TypeError, ValueError):
            return []
        phrases: list[str] = []
        for item in (parsed or {}).get("items") or []:
            if not isinstance(item, Mapping):
                continue
            for key in ("said", "category", "brand"):
                value = str(item.get(key) or "").strip()
                if value and value not in phrases:
                    phrases.append(value)
        return phrases

    def correct_order(self,tenant,oid,user_id,customer_id=None,items=None):
        old=self.order_detail(tenant,oid)
        if not old:raise ValueError("order not found in merchant scope")
        if old["status"] not in ("draft","review"):raise ValueError("only drafts and review tasks can be corrected")
        target_customer=customer_id or old["customer_id"]
        if target_customer and not self.db.execute("SELECT 1 FROM customers WHERE merchant_id=? AND id=?",(tenant,target_customer)).fetchone():raise ValueError("customer is outside merchant")
        evidence=self.db.execute("SELECT message_id FROM evidence WHERE order_id=? ORDER BY rowid LIMIT 1",(oid,)).fetchone()
        if not evidence:raise ValueError("order has no source evidence")
        new=self._new_order(tenant,target_customer,old["group_id"],evidence["message_id"],history=old["history_order_id"] or oid,change_type="modify")
        with self.db:
            if old["status"] in ("draft","review"):
                self.db.execute("UPDATE orders SET status='superseded' WHERE id=? AND merchant_id=?",(oid,tenant))
            selected=items if items is not None else [{"product_id":x["product_id"],"quantity":x["quantity"]} for x in old["items"]]
            for entry in selected:
                product=self.db.execute("SELECT * FROM products WHERE merchant_id=? AND id=?",(tenant,entry["product_id"])).fetchone()
                if not product:raise ValueError("product is outside merchant")
                self.db.execute("INSERT INTO order_items VALUES(?,?,?,?,?,?)",(uuid.uuid4().hex,new,product["id"],float(entry["quantity"]),product["unit"],product["price_cents"]))
            self.db.execute("UPDATE orders SET status='draft' WHERE id=?",(new,))
            for e in old["evidence"]:
                msg=self.db.execute("SELECT id FROM messages WHERE merchant_id=? AND group_id=? AND content=? AND COALESCE(sender,'')=COALESCE(?,'') ORDER BY observed_at LIMIT 1",(tenant,old["group_id"],e["content"],e["sender"])).fetchone()
                if msg:self._attach(tenant,new,msg["id"])
            self.db.execute("INSERT INTO audit VALUES(?,?,?,?,?,?,?,?)",(uuid.uuid4().hex,tenant,user_id,"correct_order",new,json.dumps({"order_id":oid,"status":old["status"]}),json.dumps({"order_id":new,"customer_id":target_customer,"items":selected},ensure_ascii=False),self.now()))
            # **纠正之后要学。**
            #
            # 人工纠正是质量最高的信号：商家亲口确认了这句客户话对应哪个商品。
            # 只用来修一张单子然后就丢掉，等于同一个客户明天再说一遍
            # 「李记酱油」还要再问一次商家。
            #
            # 只学无歧义的情况 —— 见 _learn_from_correction。
            learned=self._learn_from_correction(tenant,oid,list(selected))
            if learned:
                self.db.execute("INSERT INTO audit VALUES(?,?,?,?,?,?,?,?)",(uuid.uuid4().hex,tenant,user_id,"learn_aliases",new,json.dumps({"from_order":oid}),json.dumps({"aliases":learned},ensure_ascii=False),self.now()))
        # 返回值**始终**是新订单号，不因为「学到了别名」而变成字典 ——
        # 同一个方法按情况返回两种类型，调用方会在某一次突然崩掉，
        # 而且只在恰好学过别名的那些单子上崩。
        # 学到的别名记在 audit 里，界面要展示就自己回读。
        return new

    def learned_aliases(self, tenant: str, order_id: str) -> list[str]:
        """这张单子纠正时学到了哪些别名。界面用它告诉商家「以后会自动认出来」。

        审计表的列是 ``entity_id``（这里放订单号）和 ``after_json``（学到的别名）。
        """
        rows = self.db.execute(
            "SELECT after_json FROM audit WHERE merchant_id=? AND entity_id=?"
            " AND action='learn_aliases' ORDER BY rowid DESC",
            (tenant, order_id)).fetchall()
        learned: list[str] = []
        for row in rows:
            try:
                data = json.loads(row["after_json"] or "{}")
            except (TypeError, ValueError):
                continue
            for alias in data.get("aliases") or []:
                if alias not in learned:
                    learned.append(alias)
        return learned
    def dashboard(self,tenant):
        counts={r["status"]:r["n"] for r in self.db.execute("SELECT status,count(*) n FROM orders WHERE merchant_id=? GROUP BY status",(tenant,))}
        mode=self.db.execute("SELECT parser_mode FROM merchant_settings WHERE merchant_id=?",(tenant,)).fetchone()
        return {"orders":sum(counts.values()),"draft_orders":counts.get("draft",0),"pending_review":counts.get("review",0)+counts.get("cancel_review",0),"open_review_tasks":self.db.execute("SELECT count(DISTINCT order_id) FROM review_tasks WHERE merchant_id=? AND status='open'",(tenant,)).fetchone()[0],"parse_failures":self.db.execute("SELECT count(*) FROM parse_runs WHERE merchant_id=? AND status='failed'",(tenant,)).fetchone()[0],"parser_mode":mode["parser_mode"] if mode else "demo-rules","imports":[dict(r) for r in self.db.execute("SELECT * FROM imports WHERE merchant_id=? ORDER BY created_at DESC LIMIT 10",(tenant,))]}
    def export_csv(self,tenant,path):
        with open(path,"w",newline="",encoding="utf-8-sig") as f:
            writer=csv.writer(f);writer.writerow(["order_id","customer","status","SKU","商品","数量","单位","单价","金额"])
            for order in self.list_orders(tenant,"confirmed"):
                for item in self.order_detail(tenant,order["id"])["items"]:writer.writerow([order["id"],order["customer_name"],order["status"],item["sku"],item["product_name"],item["quantity"],item["unit"],item["unit_price_cents"]/100,item["quantity"]*item["unit_price_cents"]/100])
    def close(self):self.db.close()
