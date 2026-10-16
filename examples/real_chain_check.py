"""真实模型订单链路验收：逐条记录 + 分段延迟。

    python examples\real_chain_check.py

走的是完整真实路径：
    本地消息 → 本地上下文 → 网关任务 → 真实 OpenCode → 真实模型
    → 订单插件与业务规则 → 本地订单与备货汇总

**消息来源必须说明**：本脚本用的是**合成**订单消息（见 ``CASES``），
因为本机微信未登录，拿不到真实聊天数据。合成数据与真实数据在输出里分开标注，
不混为一谈。

不发送任何微信消息。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
for _name in ("business_agent", "order_agent"):
    _src = ROOT / "packages" / _name / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

NOW = datetime.now(timezone.utc)
GROUP = "合成验收群"   # 合成数据，不碰任何真实群

#: 合成用例。刻意覆盖用户点名要看的每一种行为。
CASES = [
    ("c1", "张姐", "苹果两箱", "新单，数量必须是数字 2"),
    ("c2", "张姐", "再加一箱", "追加到刚才那单"),
    ("c3", "张姐", "改成三箱", "改量，不是追加"),
    ("c4", "陈先生", "老样子，明天送", "引用同客户历史；无历史时应转澄清"),
    ("c5", "李女士", "苹果多少钱", "询价不建单"),
    ("c6", "王老板", "钱转了", "只记录付款声称"),
    ("c7", "赵女士", "来两箱那个", "商品不明确，应转澄清"),
]


@dataclass
class Finding:
    case_id: str
    sender: str
    raw: str
    note: str
    normalized: dict = field(default_factory=dict)
    customer: str = ""
    model: str = ""
    request_id: str = ""
    job_id: str = ""
    run_id: str = ""
    intent: str = ""
    items: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    ambiguities: list = field(default_factory=list)
    outcome: str = ""
    order_id: str = ""
    clarification_id: str = ""
    order_status: str = ""
    effective_version: str = ""
    stock_impact: dict = field(default_factory=dict)
    latency: dict = field(default_factory=dict)
    review_reasons: list = field(default_factory=list)
    correct: bool = False
    verdict: str = ""


def http(method: str, url: str, body=None, headers=None, timeout=600):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="real-chain-check")
    parser.add_argument("--gateway", default=os.environ.get("WECHAT_AI_GATEWAY_URL", "http://127.0.0.1:8790"))
    parser.add_argument("--model", default=os.environ.get("PROBE_MODEL", "qwen3.6-plus"))
    parser.add_argument("--provider", default=os.environ.get("PROBE_PROVIDER", "opencode-go"))
    parser.add_argument("--alias", default=os.environ.get("PROBE_ALIAS", "free-standard"))
    parser.add_argument("--max-calls", type=int, default=7, help="最多发多少个真实请求，防止无上限探测")
    parser.add_argument(
        "--workdir", default="",
        help="凭据与本地订单库所在目录。默认临时目录下的 realgw3/pyw_real_chain；"
             "换目录可以重复跑而不覆盖上一次的验收数据。",
    )
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from wechat_ai_assistant.local_parse import LocalParseService
    from wechat_ai_assistant.orders import OrderStore
    from wechat_ai_assistant.client_login import CredentialStore, MerchantClient, PendingQueue, Session
    from wechat_ai_assistant.dashboard import DashboardBuilder
    from wechat_ai_assistant.runmode import SettingsStore

    workdir = Path(args.workdir) if args.workdir else Path(os.environ.get("TEMP", ".")) / "realgw3" / "pyw_real_chain"
    workdir.mkdir(parents=True, exist_ok=True)
    orders = OrderStore(str(workdir / "orders.db"))
    tenant = orders.create_merchant("合成验收商家")["merchant_id"]
    for name in {case[1] for case in CASES}:
        orders.add_customer(tenant, name, name, GROUP)
    orders.add_product(tenant, "APL", "苹果", "箱", 2600, ["苹果", "A"])
    orders.add_product(tenant, "ORG", "橙子", "箱", 2200, ["橙子"])

    creds = json.loads((workdir / "gateway-creds.json").read_text(encoding="utf-8"))
    session = Session(access_token=creds["access_token"], refresh_token=creds.get("refresh_token", ""),
                      device_id=creds["device_id"], merchant_name="合成验收商家")
    client = MerchantClient(args.gateway, session=session, allow_insecure_remote=True)
    queue = PendingQueue(str(workdir / "pending.db"))
    service = LocalParseService(orders=orders, client=client, queue=queue, tenant=tenant,
                                group=GROUP, model_alias=args.alias)
    settings = SettingsStore(workdir / "settings.json")

    print("=" * 78)
    print("  消息来源：合成数据（本机微信未登录，无真实聊天数据可用）")
    print(f"  群：{GROUP}（合成，非真实群）　不会发送任何微信消息")
    print(f"  真实模型：{args.provider}/{args.model}　别名：{args.alias}")
    print(f"  最多真实调用 {args.max_calls} 次")
    print("=" * 78)

    findings: list[Finding] = []
    calls = 0
    builder: DashboardBuilder | None = None
    for case_id, sender, text, note in CASES[: args.max_calls]:
        if calls >= args.max_calls:
            break
        finding = Finding(case_id=case_id, sender=sender, raw=text, note=note)
        t_local = time.time()
        import_id = f"chain-{case_id}"
        orders.add_import(tenant, GROUP, [{
            "sender": sender, "content": text, "kind": "text",
            "sent_at": (NOW - timedelta(hours=1)).isoformat(),
            "source_id": f"chain-{case_id}", "stable_source_id": True,
        }], import_id, "synthetic_loopback", "合成验收数据，非真实聊天")
        row = orders.db.execute(
            "SELECT * FROM messages WHERE merchant_id=? AND source_id=?", (tenant, f"chain-{case_id}")
        ).fetchone()
        finding.normalized = {"id": row["id"], "content": row["content"], "kind": row["kind"],
                             "sent_at": row["sent_at"], "customer_id": row["customer_id"] or ""}
        customer = orders.db.execute(
            "SELECT name FROM customers WHERE merchant_id=? AND id=?", (tenant, row["customer_id"])
        ).fetchone()
        finding.customer = customer["name"] if customer else ""
        local_done = time.time() - t_local

        t_submit = time.time()
        calls += 1
        progress = service.submit(row["id"])
        job_id = progress.job_id or ""
        submit_seconds = time.time() - t_submit

        t_wait = time.time()
        while True:
            settled = service.settle()
            if settled.order_id or settled.needs_review or settled.state in {"done", "blocked", "needs_retry"}:
                break
            if time.time() - t_wait > 420:
                break
            time.sleep(1.0)
        wait_seconds = time.time() - t_wait

        t_apply = time.time()
        orders.db.commit()
        apply_seconds = time.time() - t_apply

        job_row = orders.db  # noqa: F841 - 仅为可读性保留
        finding.job_id = job_id
        finding.run_id = f"run-{job_id}" if job_id else ""
        finding.request_id = f"req-{row['id']}"
        finding.model = f"{args.provider}/{args.model}"
        finding.latency = {
            "本地持久化": round(local_done, 3),
            "网关提交": round(submit_seconds, 3),
            "排队+模型执行": round(wait_seconds, 3),
            "结果落地": round(apply_seconds, 3),
            "端到端": round(local_done + submit_seconds + wait_seconds + apply_seconds, 3),
        }

        # 云端返回的原始建议
        raw_result = None
        if job_id:
            with_orders = orders
            del with_orders
            raw_result = _last_suggestion(orders, tenant, row["id"])

        if raw_result:
            finding.intent = str(raw_result.get("intent", ""))
            finding.items = list(raw_result.get("items") or [])
            finding.evidence = list(raw_result.get("evidence") or [])
            finding.ambiguities = list(raw_result.get("ambiguities") or [])

        order_id = settled.order_id or ""
        finding.order_id = order_id
        finding.clarification_id = settled.clarification_id or ""
        if order_id:
            detail = orders.order_detail(tenant, order_id)
            finding.order_status = detail.get("status", "")
            finding.items = finding.items or [
                {"sku": i.get("sku"), "quantity": i.get("quantity"), "unit": i.get("unit")}
                for i in detail.get("items", [])
            ]
            finding.review_reasons = [t["reason"] for t in detail.get("review_tasks", [])]
            finding.effective_version = f"v{detail.get('version')} ({detail.get('status')})"
        finding.outcome = settled.state
        _judge(finding)
        findings.append(finding)
        _print_finding(finding)

    print()
    print("=" * 78)
    print("  正确性汇总")
    ok = sum(1 for f in findings if f.correct)
    print(f"  符合预期 {ok}/{len(findings)}　"
          f"（空明细、缺意图、无证据一律记为不符合预期；数量多不代表对）")
    for finding in findings:
        mark = "OK " if finding.correct else "NG "
        print(f"    {mark}{finding.case_id} 「{finding.raw}」 -> {finding.verdict}")
    builder = builder or DashboardBuilder(orders, tenant)
    print()
    print("  澄清任务（不是订单，不进备货）")
    clarifications = builder.clarifications()
    for task in clarifications:
        print(f"    {task['reason']}　{task.get('customer') or '（未绑定客户）'}　"
              f"{task.get('suggested_question', '')}")
    if not clarifications:
        print("    （无）")
    print()
    print("  待核对付款声称（不认定到账）")
    claims = builder.payment_claims()
    for claim in claims:
        print(f"    {claim['order_id']}　{claim['customer']}　{claim['note']}")
    if not claims:
        print("    （无）")
    print()
    print(f"  订单数：{len(builder.effective_orders())}　"
          f"备货行数：{len(builder.stock_summary())}　"
          f"未关闭澄清：{len(clarifications)}")
    print()
    print("  备货汇总（只统计有效版本）")
    for row in builder.stock_summary():
        print(f"    {row.name} {row.quantity:g}{row.unit}　（{row.order_count} 单，{len(row.customers)} 位客户）")
    latencies = [f.latency["端到端"] for f in findings if f.latency]
    if latencies:
        print()
        print(f"  端到端耗时：最小 {min(latencies):.1f}s　最大 {max(latencies):.1f}s　"
              f"均值 {sum(latencies) / len(latencies):.1f}s（{len(latencies)} 个样本，**不代表生产 P95**）")
    print(f"  真实模型调用次数：{calls}")
    print()
    print("  提示：上面这份结果来自" + ("真实模型" if args.provider != "local-rules" else "本地规则适配器（不是模型）"))
    if args.provider == "local-rules":
        print("  本地规则适配器只会返回 intent=unclear，因此**不能**用它判断订单正确性；")
        print("  它只用来验证链路是否跑得通、失败状态是否明确、以及是否会产生空订单。")
    orders.close()
    queue.close()
    return 0


def _last_suggestion(orders, tenant, message_id):
    row = orders.db.execute(
        "SELECT result_json FROM parse_runs WHERE merchant_id=? AND message_id=? AND result_json IS NOT NULL"
        " ORDER BY created_at DESC LIMIT 1", (tenant, message_id)
    ).fetchone()
    if not row or not row["result_json"]:
        return None
    try:
        return json.loads(row["result_json"])
    except ValueError:
        return None


def _judge(f: Finding) -> None:
    """逐条判断是否符合预期。**空卡片不算成功**。

    判据分两层，缺一不可：

    1. **没有做错的事**：不该建的单没建、不该认定的到账没认定。
    2. **做了该做的事**：该建的单建了、该问的问了、该记录的记了。

    只看第 1 层会把"什么都没做"判成通过 —— 那正是上一版 5/7 假绿灯的成因。
    数量也必须先有**非空**明细，`all([])` 恒为真，不能当成功判据。
    """
    quantities = [i.get("quantity") for i in f.items if isinstance(i, dict)]
    has_items = bool(f.items)
    numeric = has_items and all(
        isinstance(q, (int, float)) and not isinstance(q, bool) for q in quantities
    )
    evidence_ok = all(str(e) in f.raw for e in f.evidence)
    detail = f"数量={quantities}　商品数={len(f.items)}　意图={f.intent or '(无)'}"

    if "多少钱" in f.raw:
        # 询价：不建单，**并且**要留下商品咨询这条记录。
        no_order = not f.order_id
        consulted = f.intent == "inquiry" and not f.items
        f.correct = no_order and consulted
        missing = []
        if not no_order:
            missing.append("不应建单却建了单")
        if not consulted:
            missing.append("没有识别成商品咨询")
        f.verdict = f"{detail}　建单={'是' if f.order_id else '否'}　" + (
            "符合预期" if not missing else "、".join(missing)
        )
    elif "钱转了" in f.raw:
        # 付款声称：不建新单，**并且**要么挂在某张单上等核对，要么问是哪一单。
        no_new_order = bool(f.order_id or f.clarification_id) or f.intent == "payment_claim"
        recorded = bool(f.clarification_id) or bool(f.review_reasons) or f.intent == "payment_claim"
        f.correct = no_new_order and recorded
        f.verdict = (
            f"{detail}　新单={'否' if f.correct else '是'}　"
            f"挂到订单={'是' if f.order_id else ('待澄清' if f.clarification_id else '否')}"
        )
    elif "老样子" in f.raw:
        # 「老样子」必须有历史可引用；没有历史时要的是澄清任务，不是订单。
        referenced = bool(f.order_id) and has_items and "no_eligible_historical_order" not in f.review_reasons
        clarified = bool(f.clarification_id) and not f.order_id
        f.correct = referenced or clarified
        f.verdict = (
            f"{detail}　引用历史并建单" if referenced
            else (f"{detail}　无历史 → 独立澄清任务，未建单" if clarified
                  else f"{detail}　既没有引用到历史明细，也没有落到澄清任务")
        )
    elif "那个" in f.raw:
        clarified = bool(f.clarification_id) and not has_items
        f.correct = clarified
        f.verdict = f"{detail}　商品不明确 → 独立澄清任务，未硬猜" if clarified else f"{detail}　商品不明确却没有转澄清"
    else:
        # 普通下单：数值数量 + 非空明细 + 有订单 + 证据逐字命中。
        f.correct = numeric and bool(f.order_id) and evidence_ok
        f.verdict = f"{detail}　订单={f.order_id or '（无）'}　证据逐字命中={evidence_ok}"


def _print_finding(f: Finding) -> None:
    print()
    print(f"  [{f.case_id}] {f.sender}：{f.raw}    （预期：{f.note}）")
    print(f"      规范化    : {f.normalized.get('content')!r}　类型={f.normalized.get('kind')}　发送时间={f.normalized.get('sent_at')}")
    print(f"      客户归属  : {f.customer}（{f.normalized.get('customer_id')}）")
    print(f"      实际模型  : {f.model}")
    print(f"      链路标识  : request={f.request_id} job={f.job_id} run={f.run_id}")
    print(f"      意图      : {f.intent or '(未取到)'}")
    print(f"      商品      : {json.dumps(f.items, ensure_ascii=False)}")
    print(f"      证据      : {json.dumps(f.evidence, ensure_ascii=False)}")
    if f.ambiguities:
        print(f"      模型歧义  : {json.dumps(f.ambiguities, ensure_ascii=False)}")
    print(f"      实际动作  : {f.outcome}　订单={f.order_id or '（无）'}　状态={f.order_status or '-'}")
    if f.clarification_id:
        print(f"      澄清任务: {f.clarification_id}（不是订单，不进备货）")
    if f.review_reasons:
        print(f"      转人工原因: {'、'.join(f.review_reasons)}")
    print(f"      有效版本  : {f.effective_version or '-'}")
    print(f"      延迟      : " + "　".join(f"{k} {v}s" for k, v in f.latency.items()))
    print(f"      判定      : {'符合预期' if f.correct else '不符合预期'}　{f.verdict}")


if __name__ == "__main__":
    sys.exit(main())
