from __future__ import annotations

import argparse
import json
import os
import signal
import threading

from .agent import AgentConfig, AgentStore, LocalAgent
from .cloud import CloudClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pyweixin-agent")
    parser.add_argument("--group", default=os.environ.get("PYWEIXIN_GROUP", ""))
    parser.add_argument("--db", default=os.environ.get("PYWEIXIN_AGENT_DB", "agent.db"))
    parser.add_argument("--account-id", default=os.environ.get("PYWEIXIN_ACCOUNT_ID", "default"))
    parser.add_argument("--agent-id", default=os.environ.get("PYWEIXIN_AGENT_ID", "default"))
    parser.add_argument("--history", type=int, default=50)
    parser.add_argument("--listen", default="30s")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--group-member", action="append", default=[])
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--no-history", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--cloud-url", default=os.environ.get("PYWEIXIN_CLOUD_URL", ""))
    parser.add_argument("--device-token", default=os.environ.get("PYWEIXIN_DEVICE_TOKEN", ""))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.group:
        build_parser().error("--group or PYWEIXIN_GROUP is required")
    config = AgentConfig(
        group=args.group,
        db_path=args.db,
        account_id=args.account_id,
        agent_id=args.agent_id,
        history_limit=args.history,
        bootstrap_history=not args.no_history,
        listen_duration=args.listen,
        poll_interval=args.poll_interval,
        group_members=tuple(args.group_member),
        background=args.background,
        cloud_url=args.cloud_url,
        device_token=args.device_token,
    )
    cloud = CloudClient(args.cloud_url, args.device_token, agent_id=args.agent_id) if args.cloud_url else None
    agent = LocalAgent(config, cloud=cloud)
    stop_event = threading.Event()

    def stop_handler(signum, frame):
        del signum, frame
        stop_event.set()

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    try:
        if args.once:
            print(json.dumps(agent.run_once(), ensure_ascii=False, default=str), flush=True)
        else:
            agent.run_forever(stop_event)
    finally:
        agent.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
