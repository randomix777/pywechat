"""参考信息模块的端到端验证 —— 真实模型调用，不是模拟。

## 这条链路要证明的事

    商家上传名单 → 模型匹配 → 商家纠正 → 写入记忆 → **模型下次匹配对**

前面几段是分开验证的（上传写库、目录进上下文、纠正写别名）。
这个脚本把**整条链路合起来**跑一遍真实调用 —— 只有它才能证明
「纠正之后模型真的会选对」。

## 为什么必须有这一步

分段都对、合起来不对，是很常见的事。比如纠正只往正确商品上加别名、
却没有从错误商品上撤掉：分段看「别名写进去了」✓，合起来却是
两个候选、模型反而选不出来。

## 跑法

    $env:PYTHONPATH="src;packages/business_agent/src;packages/order_agent/src"
    .\.venv\Scripts\python.exe -X utf8 examples/reference_loop_probe.py

需要 `~/.dsh/.credentials.yaml` 里有 OPENROUTER_API_KEY。
**密钥只在内存里用，不打印。** 每次运行 2 次真实调用，成本约 $0.0004。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
for extra in ("src", "packages/business_agent/src", "packages/order_agent/src"):
    sys.path.insert(0, str(ROOT / extra))

from order_agent.contract import OUTPUT_SCHEMA  # noqa: E402
from order_agent.plugin import SYSTEM_PROMPT  # noqa: E402
from wechat_ai_assistant.orders import OrderStore  # noqa: E402

MODEL = "deepseek/deepseek-v4.1-flash"
GROUP = "鲜果群"


def _key() -> str:
    text = pathlib.Path(os.path.expanduser("~/.dsh/.credentials.yaml")).read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip().startswith("OPENROUTER_API_KEY:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    raise SystemExit("凭据文件里没有 OPENROUTER_API_KEY")


def ask(context: dict, key: str) -> dict:
    """真实调用，要求它按契约输出。"""
    body = {
        "model": MODEL,
        "temperature": 0,
        "max_tokens": 1200,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "order_parse", "strict": True,
                                            "schema": OUTPUT_SCHEMA}},
    }
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return json.loads(payload["choices"][0]["message"]["content"])


def main() -> int:
    key = _key()
    workdir = tempfile.TemporaryDirectory()
    store = OrderStore(str(pathlib.Path(workdir.name) / "o.db"))
    tenant = store.create_merchant("验证商家")["merchant_id"]
    customer = store.add_customer(tenant, "李叔", "李叔", GROUP)

    # 「李记酱油」**先错绑**在 P-A 上。不先造这个错误，就证明不了纠正改掉了什么。
    wrong = store.add_product(tenant, "P-A", "老酱油", "瓶", 1000, aliases=["李记酱油"])
    right = store.add_product(tenant, "P-B", "海天生抽", "瓶", 1200)

    message = {"sender": "李叔", "content": "李记酱油 2 瓶",
               "sent_at": "2026-10-14T09:00", "group_id": GROUP}
    item = {"said": "李记酱油", "brand": None, "category": None, "sku": "",
            "quantity": 2, "unit": "瓶", "evidence": ["李记酱油"]}

    def context() -> dict:
        return {"message": message,
                "products": store._catalog_for_model(tenant),
                "customers": store._roster_for_model(tenant, GROUP)}

    def chosen() -> list:
        return [i.get("sku") for i in (ask(context(), key).get("items") or [])]

    print("=" * 72)
    print("参考信息模块 —— 端到端验证（真实模型调用）")
    print("=" * 72)
    before = chosen()
    print(f"\n纠正前，模型选: {before}")

    # 走一次真实的纠正
    store.add_import(tenant, GROUP, [
        {"sender": "李叔", "content": message["content"],
         "sent_at": "2026-10-14T09:00:00-04:00", "source_id": "s1"}], "probe", "demo")
    mid = store.db.execute("SELECT id FROM messages WHERE merchant_id=?",
                           (tenant,)).fetchone()["id"]
    order = store._new_order(tenant, customer, GROUP, mid)
    with store.db:
        store.db.execute("INSERT INTO evidence VALUES(?,?,?,?,'source')",
                         ("e1", tenant, order, mid))
        store.db.execute(
            "INSERT INTO parse_runs(id,merchant_id,message_id,adapter,status,usage_units,"
            "error,created_at,result_json) VALUES(?,?,?,'model-v1','complete',0,NULL,?,?)",
            ("r1", tenant, mid, store.now(),
             json.dumps({"items": [item]}, ensure_ascii=False)))
        store.db.execute("UPDATE orders SET parse_run_id='r1', status='review' WHERE id=?",
                         (order,))
    corrected = store.correct_order(tenant, order, "u1",
                                    items=[{"product_id": right, "quantity": 2}])
    learned = store.learned_aliases(tenant, corrected)
    after = chosen()

    print(f"学到别名: {learned}")
    print(f"纠正后，模型选: {after}")
    print(f"\n给模型的目录: {json.dumps(store._catalog_for_model(tenant), ensure_ascii=False)}")
    print()

    ok = True
    if before != ["P-A"]:
        print(f"✗ 纠正前应当选中错的那个 P-A，实际 {before}（这个脚本证明不了纠正的效果）")
        ok = False
    if after != ["P-B"]:
        print(f"✗ 纠正后模型没选对，实际 {after}")
        ok = False
    if "李记酱油" not in learned:
        print(f"✗ 没有学到别名，实际 {learned}")
        ok = False
    print("端到端通过：纠正之后模型自动选对了。" if ok else "端到端**未**通过。")

    store.close()
    workdir.cleanup()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())