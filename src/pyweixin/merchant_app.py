"""商家本地软件的启动入口。

    python -m pyweixin.merchant_app

双击即可运行。商户不需要命令行、不需要环境变量、不需要填群名、不需要填
API Key —— 全部在界面上点。

**本轮的变化**：不再要求 ``--group``。会话在界面上选；配置存在
``%LOCALAPPDATA%\\pyweixin\\settings.json``，下次启动自动恢复。
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path
from typing import Any, Callable

from ._packages import ensure_packages_on_path  # noqa: E402
from . import diagnostics  # noqa: E402

ensure_packages_on_path()

from .application import MerchantApplication  # noqa: E402
from .client_login import (  # noqa: E402
    CredentialStore,
    DeviceLoginFlow,
    MerchantClient,
    PendingQueue,
    Session,
    restore_session,
)
from .conversations import RealConversationSource  # noqa: E402
from .local_parse import LocalParseService  # noqa: E402
from .orders import OrderStore  # noqa: E402
from .wechat_state import RealWeChatLauncher, RealWeChatProbe  # noqa: E402

#: 没有配置时使用的占位地址。商户在界面上点「登录」时会被提示替换。
DEFAULT_GATEWAY = "https://gateway.example.com"


def default_data_dir() -> Path:
    base = os.environ.get("PYWEIXIN_CLIENT_DATA_DIR")
    if base:
        return Path(base)
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".pyweixin"))


def default_device_name() -> str:
    try:
        return socket.gethostname() or "本机电脑"
    except OSError:  # pragma: no cover - platform dependent
        return "本机电脑"


def ensure_local_store(path: Path) -> tuple[OrderStore, str]:
    """Open (or create) the merchant's local order database.

    The merchant's orders, customers, memory and review queue live here and only
    here. Losing the gateway does not lose any of it.
    """
    store = OrderStore(str(path))
    row = store.db.execute("SELECT id FROM merchants ORDER BY created_at LIMIT 1").fetchone()
    if row is None:
        created = store.create_merchant(os.environ.get("PYWEIXIN_MERCHANT_NAME") or "我的店铺", plan="demo", ai_quota=0)
        return store, created["merchant_id"]
    return store, str(row["id"])


def build_login_runner(client: MerchantClient, credentials: CredentialStore, device_name: str) -> Callable[[Callable[[str, str], None]], Session]:
    def run(progress: Callable[[str, str], None]) -> Session:
        flow = DeviceLoginFlow(
            client=client,
            credentials=credentials,
            device_name=device_name,
            on_progress=lambda p: progress(p.status, p.detail),
        )
        return flow.run()

    return run


def build_service_factory(
    orders: OrderStore, tenant: str, gateway_url: str, queue: PendingQueue
) -> Callable[[Session], LocalParseService]:
    """Build the local parse service for a logged-in session.

    ``group`` is deliberately absent: which conversation to work on is decided in
    the interface, not on the command line.
    """

    def factory(session: Session) -> LocalParseService:
        return LocalParseService(
            orders=orders,
            client=MerchantClient(gateway_url, session=session),
            queue=queue,
            tenant=tenant,
        )

    return factory


def build_application(
    *,
    data_dir: Path,
    orders: OrderStore,
    tenant: str,
    gateway_url: str,
    queue: PendingQueue,
    credentials: CredentialStore,
    device_name: str,
    sender: Callable[[str, str], Any] | None = None,
) -> MerchantApplication:
    """Assemble the whole application. Shared by the window and the tests."""
    app = MerchantApplication(
        data_dir=data_dir,
        orders=orders,
        tenant=tenant,
        probe=RealWeChatProbe(),
        launcher=RealWeChatLauncher(),
        conversation_source=RealConversationSource(ui_lock=None),
        history_reader_fn=None,
        gateway_url=gateway_url,
    )
    # The UI queue is created by the application; wire the conversation source to
    # it now that it exists so history reading, listing and sending serialise.
    app.conversation_source = RealConversationSource(ui_lock=app.ui_lock)
    app.history._ui_lock = app.ui_lock

    client = MerchantClient(gateway_url, session=restore_session(credentials))
    service_factory = build_service_factory(orders, tenant, gateway_url, queue)
    app.login_runner = build_login_runner(client, credentials, device_name)  # type: ignore[attr-defined]
    app.pending_queue = queue  # type: ignore[attr-defined]

    def attach_session(session: Session) -> None:
        app.attach(session, client, service_factory(session))

    app.attach_session = attach_session  # type: ignore[attr-defined]
    app._pending_attach = attach_session  # type: ignore[attr-defined]
    if client.session is not None:
        attach_session(client.session)
    if sender is not None:
        app.outbox.sender = sender
    return app


def _force_utf8_stdout() -> None:
    """Windows 控制台默认不是 UTF-8。

    ``--check`` 和启动过程都要打印中文，不重配会抛 UnicodeEncodeError ——
    商户看到的是"程序崩溃了"，而不是缺什么、怎么办。重配放在 ``main`` 里而不是
    ``__main__`` 块里：被别的入口直接调用 ``main()`` 时同样需要它。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _self_check(app: MerchantApplication, data_dir: Path, device_name: str) -> int:
    print(f"数据目录：{data_dir}")
    print(f"本机名称：{device_name}")
    print(f"本地订单库：已就绪（{app.tenant}）")
    print(f"微信状态：{app.monitor.check_once().state}")
    print(f"已登录产品账号：{'是' if app.session is not None else '否'}")
    print(f"待发送回执：{app.outbox.pending_count()}　结果待确认：{app.outbox.unknown_count()}")
    print(f"当前模式：{app.auto.label()}（{app.auto.describe()}）")
    print("本地订单查看、人工修改和导出不需要网络。")
    return 0


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(prog="pyweixin-merchant", description="微信群接单助手")
    parser.add_argument("--gateway", default=os.environ.get("PYWEIXIN_GATEWAY_URL", DEFAULT_GATEWAY), help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", default="", help=argparse.SUPPRESS)
    parser.add_argument("--device-name", default=os.environ.get("PYWEIXIN_DEVICE_NAME", ""), help=argparse.SUPPRESS)
    parser.add_argument("--check", action="store_true", help="只做自检，不打开界面")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    # 必须**先于**建任何组件开日志。这个应用驱动微信窗口，任何一环出问题
    # ��户只会看到「读取失败」四个字。没有落盘日志，出了事谁也查不了 ——
    # 以前这个应用一处 logging 都没有，而它跑在 pythonw 下，连 stderr 都丢。
    log_path = diagnostics.install(data_dir)
    diagnostics.quiet_noisy_modules()
    device_name = args.device_name or default_device_name()

    orders, tenant = ensure_local_store(data_dir / "orders.db")
    credentials = CredentialStore(data_dir / "credentials.bin")
    queue = PendingQueue(data_dir / "pending.db")

    app = build_application(
        data_dir=data_dir,
        orders=orders,
        tenant=tenant,
        gateway_url=args.gateway,
        queue=queue,
        credentials=credentials,
        device_name=device_name,
    )
    try:
        if args.check:
            return _self_check(app, data_dir, device_name)
        from .merchant_ui import MerchantWindow

        MerchantWindow(app).run()
    finally:
        app.close()
        orders.close()
        queue.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    _force_utf8_stdout()
    sys.exit(main())
