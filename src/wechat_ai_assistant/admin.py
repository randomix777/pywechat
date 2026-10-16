"""Operator CLI: first account, subscriptions, device and job inspection.

Run against the gateway's SQLite file. This is the only supported way to seed
the first operator and merchant accounts, so there is no default password and no
public registration endpoint.

    python -m wechat_ai_assistant.admin --db /data/product.db create-operator
    python -m wechat_ai_assistant.admin --db /data/product.db create-merchant --username demo --quota 100
    python -m wechat_ai_assistant.admin --db /data/product.db list-accounts
    python -m wechat_ai_assistant.admin --db /data/product.db set-plan --username demo --quota 200 --period-days 60
    python -m wechat_ai_assistant.admin --db /data/product.db disable-device --device dv_ab12
    python -m wechat_ai_assistant.admin --db /data/product.db list-devices --username demo
    python -m wechat_ai_assistant.admin --db /data/product.db list-jobs --state outcome_unknown
    python -m wechat_ai_assistant.admin --db /data/product.db purge --older-than-days 7
"""
from __future__ import annotations

import argparse
import math
import getpass
import json
import sys
from typing import Any

from .product import ProductStore


def _prompt_password(confirm: bool = True) -> str:
    if not sys.stdin or not sys.stdin.isatty():
        raise SystemExit("需要交互式输入密码；请在终端中运行，或改用 --password-stdin。")
    password = getpass.getpass("请输入密码（至少 8 位）: ")
    if len(password) < 8:
        raise SystemExit("密码至少 8 位")
    if confirm and password != getpass.getpass("请再次输入密码: "):
        raise SystemExit("两次输入不一致")
    return password


def _read_password_stdin() -> str:
    password = sys.stdin.readline().strip()
    if len(password) < 8:
        raise SystemExit("密码至少 8 位")
    return password


def _emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))



def usd_to_micro(amount: float | int) -> int:
    """美元 -> 微美元（整数）。

    命令行面向人，所以输入是美元；存储里是整数微美元。两者差 100 万倍，
    所以转换只能在这一个边界做一次。

    返回 int：价钱不能用浮点存。向上取整——少多一美分应认为向上取整，
    小数部分让利等于每次都在向平台多拿一点。
    """
    return int(math.ceil(max(0.0, float(amount or 0.0)) * 1_000_000))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wechat-ai-assistant-admin")
    parser.add_argument("--db", default="product.db", help="网关 SQLite 文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    operator = sub.add_parser("create-operator", help="创建运营账号（仅首次）")
    operator.add_argument("--username", required=True)
    operator.add_argument("--password-stdin", action="store_true")
    operator.add_argument("--display-name", default="")

    merchant = sub.add_parser("create-merchant", help="开通商家账号")
    merchant.add_argument("--username", required=True)
    merchant.add_argument("--display-name", default="")
    merchant.add_argument("--plan-code", default="trial")
    # 命令行收**美元**。存储用微美元（整数），但操作员手输 --quota 100
    # 期望的是 $100。把微美元直接暴露给人，一定会少写三个零。
    merchant.add_argument("--quota-usd", type=float, default=0.0,
                        help="额度（美元），储存时换算为微美元")
    merchant.add_argument("--max-concurrent-jobs", type=int, default=2)
    merchant.add_argument("--period-days", type=int, default=30)
    merchant.add_argument("--password-stdin", action="store_true")

    sub.add_parser("list-accounts", help="列出全部账号")

    plan = sub.add_parser("set-plan", help="调整套餐、额度或有效期")
    plan.add_argument("--username", required=True)
    plan.add_argument("--quota-usd", type=float,
                     help="额度（美元），存储时换算为微美元")
    plan.add_argument("--plan-code")
    plan.add_argument("--max-concurrent-jobs", type=int)
    plan.add_argument("--period-days", type=int)
    plan.add_argument("--status", choices=["active", "suspended"])

    devices = sub.add_parser("list-devices", help="列出某账号的设备")
    devices.add_argument("--username", required=True)

    disable = sub.add_parser("disable-device", help="吊销设备")
    disable.add_argument("--device", required=True)

    jobs = sub.add_parser("list-jobs", help="查看任务（不显示消息正文）")
    jobs.add_argument("--state", default="", help="逗号分隔的状态筛选")
    jobs.add_argument("--limit", type=int, default=50)

    reconcile = sub.add_parser("reconcile", help="对结果未知的任务做人工对账")
    reconcile.add_argument("--job", required=True)

    purge = sub.add_parser("purge", help="清理过期的会话、令牌和授权码")
    purge.add_argument("--older-than-days", type=int, default=7)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = ProductStore(args.db)
    try:
        return _dispatch(store, args)
    finally:
        store.close()

def _dispatch(store: ProductStore, args: Any) -> int:
    if args.command == "create-operator":
        password = _read_password_stdin() if args.password_stdin else _prompt_password()
        account = store.create_account(
            args.username,
            password,
            display_name=args.display_name or "运营",
            role="operator",
            plan_code="internal",
            quota_micro_usd=0,
            period_days=3650,
            must_change_password=True,
        )
        _emit({"created": account["username"], "account_id": account["account_id"], "note": "初始密码仅显示一次"})
        return 0

    if args.command == "create-merchant":
        password = _read_password_stdin() if args.password_stdin else _prompt_password()
        account = store.create_account(
            args.username,
            password,
            display_name=args.display_name,
            plan_code=args.plan_code,
            quota_micro_usd=usd_to_micro(args.quota_usd),
            max_concurrent_jobs=args.max_concurrent_jobs,
            period_days=args.period_days,
        )
        _emit(
            {
                "created": account["username"],
                "account_id": account["account_id"],
                "merchant_id": account["merchant_id"],
                "initial_password": account["password"],
                "note": "初始密码仅显示一次；请通过安全渠道交给商家，并要求首次登录时修改",
            }
        )
        return 0

    if args.command == "list-accounts":
        rows = store.list_accounts()
        _emit(rows)
        return 0

    if args.command == "set-plan":
        account = store.find_account(args.username)
        if account is None:
            raise SystemExit("账号不存在")
        if args.status:
            store.set_account_status(account["id"], args.status)
        subscription = store.update_subscription(
            account["id"],
            plan_code=args.plan_code,
            quota_micro_usd=usd_to_micro(args.quota_usd),
            max_concurrent_jobs=args.max_concurrent_jobs,
            period_days=args.period_days,
        )
        _emit(subscription)
        return 0

    if args.command == "list-devices":
        account = store.find_account(args.username)
        if account is None:
            raise SystemExit("账号不存在")
        _emit(store.list_devices(account["id"]))
        return 0

    if args.command == "disable-device":
        device = store.disable_device_by_id(args.device)
        _emit(device)
        return 0

    if args.command == "list-jobs":
        states = [state for state in args.state.split(",") if state]
        _emit(store.list_all_jobs(states, limit=args.limit))
        return 0

    if args.command == "reconcile":
        _emit({"job_id": args.job, "note": "请通过 POST /v1/operator/jobs/{id}/reconcile 触发对账"})
        return 0

    if args.command == "purge":
        _emit(store.purge_expired(max(0, args.older_than_days) * 86400))
        return 0

    raise SystemExit("未知命令")


__all__ = ["main", "build_parser"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
