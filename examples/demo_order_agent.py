"""Load a clearly synthetic merchant dataset into the order business DB."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from pyweixin.orders import OrderStore


def main() -> None:
    if hasattr(sys.stdout,"reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    path = os.environ.get("PYWEIXIN_BUSINESS_DB", "orders.db")
    store = OrderStore(path)
    try:
        tenant = store.db.execute("SELECT id FROM merchants WHERE name='【演示】丰收果蔬' LIMIT 1").fetchone()
        if tenant:
            print(json.dumps({"merchant_id": tenant["id"], "message": "演示租户已存在"}, ensure_ascii=False))
            return
        merchant = store.create_merchant("【演示】丰收果蔬", token=os.environ.get("PYWEIXIN_DEMO_TOKEN") or None)
        tid, token = merchant["merchant_id"], merchant["token"]
        group = "演示·鲜果群"
        for name, alias in [("张姐", "张姐"), ("王老板", "老王"), ("李女士", "小李"), ("张小姐", "张姐"), ("陈先生", "陈先生"), ("赵女士", "赵女士"), ("孙先生", "孙先生")]:
            store.add_customer(tid, name, alias, group)
        apples = store.add_product(tid, "APL-BOX", "苹果", "箱", 2600, ["苹果", "A"])
        eggs = store.add_product(tid, "EGG-BOX", "鸡蛋", "箱", 3400, ["鸡蛋"])
        oranges = store.add_product(tid, "ORG-BOX", "橙子", "箱", 2200, ["橙子", "橙"])
        rows = [
            {"sender":"张姐","content":"苹果两箱","sent_at":"2026-09-20T09:00:00-04:00","source_id":"demo-001"},
            {"sender":"王老板","content":"橙子一箱","sent_at":"2026-09-20T09:01:00-04:00","source_id":"demo-002"},
            {"sender":"小李","content":"鸡蛋一箱","sent_at":"2026-09-20T09:02:00-04:00","source_id":"demo-003"},
            {"sender":"张姐","content":"苹果两箱","sent_at":"2026-09-20T09:03:00-04:00","source_id":"demo-004"},
            {"sender":"张姐","content":"苹果两箱","sent_at":"2026-09-20T09:04:00-04:00","source_id":"demo-005"},
            {"sender":"张姐","content":"苹果两箱 鸡蛋一箱","sent_at":"2026-09-20T09:05:00-04:00","source_id":"demo-006"},
            {"sender":"王老板","content":"图片","kind":"图片","sent_at":"2026-09-20T09:06:00-04:00","source_id":"demo-007"},
            {"sender":"老王","content":"苹果两箱","sent_at":"2026-09-20T09:07:00-04:00","source_id":"demo-008"},
            {"sender":"陈先生","content":"苹果两箱","sent_at":"2026-09-20T09:08:00-04:00","source_id":"demo-009"},
            {"sender":"陈先生","content":"苹果两箱","sent_at":"2026-09-20T09:09:00-04:00","source_id":"demo-010"},
            {"sender":"陈先生","content":"苹果两箱 鸡蛋一箱","sent_at":"2026-09-20T09:10:00-04:00","source_id":"demo-011"},
            {"sender":"王老板","content":"再加一箱橙子","sent_at":"2026-09-20T09:11:00-04:00","source_id":"demo-012"},
            {"sender":"赵女士","content":"A两箱","sent_at":"2026-09-20T09:12:00-04:00","source_id":"demo-013"},
            {"sender":"赵女士","content":"A改成三箱","sent_at":"2026-09-20T09:13:00-04:00","source_id":"demo-014"},
            {"sender":"孙先生","content":"橙子一箱","sent_at":"2026-09-20T09:14:00-04:00","source_id":"demo-015"},
        ]
        rows=[{**row,"stable_source_id":True} for row in rows]
        imported = store.add_import(tid, group, rows, "demo-history-2026-09", "synthetic_demo", "仅有 15 条合成消息，不代表真实群覆盖")
        parsed = store.parse_import(tid, group, imported["import_id"])
        # The second batch demonstrates quoted customer memory and a cancellation
        # suggestion. Only the explicitly confirmed first order becomes memory.
        chen=store.db.execute("SELECT id FROM customers WHERE merchant_id=? AND name='陈先生'",(tid,)).fetchone()["id"]
        seed=next(o for o in store.list_orders(tid,"draft") if o["customer_id"]==chen)
        store.confirm(tid,seed["id"],merchant["user_id"])
        followup_rows=[
            {"sender":"陈先生","content":"老样子，明天送","sent_at":"2026-09-21T09:00:00-04:00","source_id":"demo-followup-001"},
            {"sender":"孙先生","content":"不要了","sent_at":"2026-09-21T09:01:00-04:00","source_id":"demo-followup-002"},
        ]
        followup_rows=[{**row,"stable_source_id":True} for row in followup_rows]
        followup=store.add_import(tid,group,followup_rows,"demo-followup-2026-09","synthetic_demo","合成后续消息")
        followup_parse=store.parse_import(tid,group,followup["import_id"])
        duplicate=store.add_import(tid,group,rows,"demo-history-2026-09","synthetic_demo","重复导入演示")
        replay=store.parse_import(tid,group,imported["import_id"])
        other=store.create_merchant("【演示】隔离验证商家")
        print(json.dumps({"merchant_id": tid, "demo_token": token, "group": group, "product_ids": [apples, eggs, oranges], "import": imported, "parse": parsed, "followup_import":followup, "followup_parse":followup_parse, "duplicate_import_inserted":duplicate["inserted"], "replay_parse":replay, "other_merchant_visible_orders":len(store.list_orders(other["merchant_id"]))}, ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
