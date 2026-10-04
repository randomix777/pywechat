"""真实执行后端验证：本地消息 → 网关 → OpenCode → 订单插件 → 本地订单。

不经过界面、不用假后端。全程打真实 HTTP：
本地 OrderStore → MerchantClient → 网关队列 → OpenCode(真实模型) → 校验 → 落地。

运行前需要：
  * opencode serve 已在 127.0.0.1:4096 监听，并已配置 order-parse agent
  * pyweixin.gateway_server 已在 127.0.0.1:8790 监听

    python examples\real_backend_check.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
for _name in ("business_agent", "order_agent"):
    _src = ROOT / "packages" / _name / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from pyweixin.client_login import MerchantClient, PendingQueue, Session  # noqa: E402
from pyweixin.local_parse import LocalParseService  # noqa: E402
from pyweixin.orders import OrderStore  # noqa: E402
from pyweixin.service import OrderPipeline  # noqa: E402

NOW = datetime.now(timezone.utc)

#: 一小批、明显是测试用的消息。**不是**真实客户群内容。
BATCH = [
    ("张姐", "苹果两箱", 30),
    ("张姐", "再加一箱苹果", 28),
    ("王老板", "鸡蛋三箱", 26),
    ("陈先生", "老样子，明天送", 25),
    ("李女士", "苹果多少钱", 24),
    ("陈先生", "钱转了", 23),
]


def seed(orders: OrderStore, tenant: str) -> dict:
    group = "回环验证群"
    for name, _content, _hours in BATCH:
        orders.add_customer(tenant, name, name, group)
    orders.add_product(tenant, "APL", "苹果", "箱", 2600, ["苹果", "A"])
    orders.add_product(tenant, "EGG", "鸡蛋", "箱", 3400, ["鸡蛋"])
    rows = [
        {"sender": sender, "content": content, "kind": "text",
         "sent_at": (NOW - timedelta(hours=hours)).isoformat(),
         "source_id": f"rb-{index}", "stable_source_id": True}
        for index, (sender, content, hours) in enumerate(BATCH)
    ]
    imported = orders.add_import(tenant, group, rows, "real-backend-check", "loopback")
    return {"group": group, "imported": imported}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="real-backend-check")
    parser.add_argument("--gateway", default=os.environ.get("PYWEIXIN_GATEWAY_URL", "http://127.0.0.1:8790"))
    args = parser.parse_args(argv)

    workdir = Path(os.environ.get("TEMP", ".")) / "pyw_real_backend"
    workdir.mkdir(parents=True, exist_ok=True)
    db = workdir / "orders.db"
    if db.exists():
        db.unlink()
    for suffix in ("-wal", "-shm"):
        if Path(str(db) + suffix).exists():
            Path(str(db) + suffix).unlink()

    orders = OrderStore(str(db))
    merchant = orders.create_merchant("回环验证商家")
    tenant = merchant["merchant_id"]
    info = seed(orders, tenant)
    group = info["group"]
    # 队列也必须清空：留着上一次的待办会让所有消息指向同一个旧 job_id。
    queue_db = workdir / "pending.db"
    for path in (queue_db, Path(str(queue_db) + "-wal"), Path(str(queue_db) + "-shm")):
        if path.exists():
            path.unlink()
    print(f"① 本地消息已入库：{info['imported']['inserted']} 条（群：{group}，租户：{tenant}）")

    # 真实令牌由网关自己的账号库签发；本地 OrderStore 里的设备令牌网关不认。
    creds_path = Path(os.environ.get("TEMP", ".")) / "pyw_real_backend" / "creds.json"
    if not creds_path.exists():
        print("缺少网关签发的令牌文件：", creds_path, file=sys.stderr)
        print("请先在网关库中创建账号与设备并签发令牌。", file=sys.stderr)
        return 2
    creds = json.loads(creds_path.read_text(encoding="utf-8"))
    session = Session(
        access_token=creds["access_token"],
        refresh_token=creds.get("refresh_token", ""),
        device_id=creds.get("device_id", ""),
        merchant_name="回环验证商家",
        device_name="回环验证电脑",
        subscription={"active": True, "remaining_micro_usd": 500, "plan_code": "验证", "period_end": ""},
    )
    print(f"② 网关签发的设备令牌：{creds.get('device_id')}")
    client = MerchantClient(args.gateway, session=session, allow_insecure_remote=True)
    queue = PendingQueue(str(workdir / "pending.db"))
    parse = LocalParseService(orders=orders, client=client, queue=queue, tenant=tenant, group=group)
    print(f"③ 网关：{args.gateway}")

    # 先证明后端确实是真模型
    try:
        report = _operator_report(args.gateway)
    except Exception:
        report = {}
    print(f"④ 执行后端报告：ok={report.get('ok')} version={report.get('version')} "
          f"model={report.get('checks', {}).get('agent', {}).get('model')}")

    pipeline = OrderPipeline(orders=orders, tenant=tenant, group=group, parse_service=parse)
    print("⑤ 逐条送入真实执行路径：\n")
    rows = orders.db.execute(
        "SELECT id,sender,content,sent_at FROM messages WHERE merchant_id=? ORDER BY COALESCE(sent_at_epoch,0), id",
        (tenant,),
    ).fetchall()

    started = time.time()
    parse_service = pipeline.parse_service
    for row in rows:
        outcome = pipeline.process(row["id"])
        trace = outcome.trace
        # 网关任务是异步的：真实模型要花时间。这里只**轮询 settle**，
        # 不能再 process() 一次 —— 那会用同一个幂等键重复提交。
        outcome, waited = _await_result(parse_service, row["id"], outcome, timeout=240.0)
        trace = outcome.trace
        print(f"  ── {row['sender']}：{row['content']}")
        print(f"     request_id={trace.request_id}  job_id={trace.job_id or '（无）'}  "
              f"run_id={trace.run_id or '（无）'}  等待={waited:.1f}s")
        print(f"     动作={outcome.outcome}  订单={outcome.order_id or '（无）'}  "
              f"需核对={outcome.needs_review}")
        if outcome.ambiguities:
            print(f"     歧义：{sorted({a.code for a in outcome.ambiguities})}")
        print(f"     结果文案：{outcome.message}")
        print()
    elapsed = time.time() - started

    print(f"⑥ 全部完成，用时 {elapsed:.1f}s")
    print_summary(orders, tenant, group)
    orders.close()
    queue.close()
    return 0


def _await_result(parse_service, message_id: str, outcome, timeout: float = 240.0):
    """只轮询 settle，不重新提交。

    真实模型比桩后端慢得多；提交完就打印等于什么都没验。但重复调用
    process() 会用同一个幂等键再提交一次，所以这里只驱动 settle。
    """
    start = time.time()
    deadline = start + timeout
    while time.time() < deadline:
        message = outcome.message or ""
        pending = outcome.outcome == "ignored" and ("排队" in message or "处理中" in message)
        if not pending:
            return outcome, time.time() - start
        time.sleep(2.0)
        settled = parse_service.settle()
        outcome.order_id = getattr(settled, "order_id", "") or outcome.order_id
        outcome.message = getattr(settled, "message", "") or outcome.message
        outcome.needs_review = bool(getattr(settled, "needs_review", False))
        if settled.order_id:
            outcome.outcome = "recorded"
    return outcome, time.time() - start


def _operator_report(gateway: str) -> dict:
    import urllib.request

    token = os.environ.get("PYWEIXIN_OPERATOR_TOKEN", "")
    if not token:
        return {}
    request = urllib.request.Request(
        gateway.rstrip("/") + "/v1/operator/execution-backend",
        headers={"X-Operator-Token": token, "Accept": "application/json"},
    )
    return json.loads(urllib.request.urlopen(request, timeout=20).read())


def print_summary(orders: OrderStore, tenant: str, group: str) -> None:
    """只统计**真正成功的订单**，空审核卡片不算。"""
    from pyweixin.dashboard import EFFECTIVE_STATUSES

    print("⑦ 结果核对（空审核卡片不计入成功订单）")
    orders_list = orders.list_orders(tenant)
    counted: list[str] = []
    empty_cards: list[str] = []
    for row in orders_list:
        detail = orders.order_detail(tenant, row["id"])
        if not detail:
            continue
        if detail["items"]:
            counted.append(row["id"])
        else:
            empty_cards.append(row["id"])
    print(f"   订单版本记录 {len(orders_list)} 条")
    print(f"   有商品明细的 {len(counted)} 条")
    print(f"   空的待核对卡片 {len(empty_cards)} 条（**不计为成功订单**）")
    print()
    for order_id in counted:
        detail = orders.order_detail(tenant, order_id)
        items = "、".join(
            f"{i.get('product_name') or i.get('sku')} {i.get('quantity')}{i.get('unit')} "
            f"@{i.get('unit_price_cents', 0) / 100:.2f}"
            for i in detail["items"]
        )
        evidence = "；".join(
            f"[{e['sender']}]{e['content'][:24]}" for e in detail.get("evidence", [])[:3]
        )
        print(f"   ✔ {order_id[:12]} 客户={detail.get('customer_name') or '未绑定'} 状态={detail['status']}")
        print(f"      商品：{items}")
        print(f"      交付：{detail.get('delivery_date') or '（未指定）'}  金额：{sum(float(i.get('quantity') or 0) * int(i.get('unit_price_cents') or 0) for i in detail['items']) / 100:.2f} 元")
        print(f"      证据：{evidence}")
    if empty_cards:
        print()
        print("   待人工核对的卡片：")
        for order_id in empty_cards:
            detail = orders.order_detail(tenant, order_id)
            reasons = "、".join(t["reason"] for t in detail.get("review_tasks", []))
            print(f"      · {order_id[:12]} 原因：{reasons or '（未记录）'}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
