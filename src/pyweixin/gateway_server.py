"""Standalone runner for the product gateway.

    python -m pyweixin.gateway --host 127.0.0.1 --port 8080

The gateway is one process. It is intended to sit behind a TLS-terminating
reverse proxy; see ``docs/product-gateway.md`` for the deployment contract.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .gateway import GatewayResponse, ProductGateway
from .opencode import PINNED_OPENCODE_VERSION, LocalRuleAdapter, OpenCodeAdapter
from .product import PlatformBudget, ProductStore

#: DeepSeek 官方 base_url（见 business_agent.backends.deepseek 模块文档）。
DEFAULT_BASE_URL = "https://api.deepseek.com"

#: 订单解析的输出结构。它会被写进提示词（DeepSeek 只支持 json_object），
#: 同时也在解析后做结构校验 —— 提示与校验用**同一份**，不各写一套。
ORDER_PARSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["intent", "customer_alias", "target_order", "items",
                 "delivery_date", "evidence", "missing_fields", "ambiguities",
                 "suggested_change", "payment_claim"],
    "properties": {
        "intent": {"type": "string",
                   "enum": ["new_order", "append", "modify", "cancel", "inquiry",
                            "follow_up", "payment_claim", "chitchat", "unclear"]},
        "customer_alias": {"type": ["string", "null"]},
        "target_order": {"type": ["string", "null"]},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["sku", "quantity", "unit", "evidence"],
                "properties": {
                    "sku": {"type": ["string", "null"]},
                    "quantity": {"type": ["number", "null"]},
                    "unit": {"type": ["string", "null"]},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "delivery_date": {"type": ["string", "null"]},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "missing_fields": {"type": "array", "items": {"type": "string"}},
        "ambiguities": {"type": "array", "items": {"type": "string"}},
        "suggested_change": {"type": ["object", "null"]},
        "payment_claim": {"type": "boolean"},
    },
}

LOGGER = logging.getLogger("pyweixin.gateway")

#: Upper bound on bytes read-and-discarded from an oversized request before the
#: 413 is sent. Large enough to unblock a realistic client, small enough that a
#: hostile sender cannot turn the gateway into a bandwidth sink.
DRAIN_LIMIT_BYTES = 8 * 1024 * 1024

#: Wall-clock ceiling for draining an oversized body. Without this, a client
#: that announces a huge length and then stops sending would pin a worker
#: thread and an open socket for as long as it liked.
DRAIN_TIMEOUT_SECONDS = 5.0


class GatewayHTTPRequestHandler(BaseHTTPRequestHandler):
    gateway: ProductGateway
    server_version = "pyweixin-gateway/1"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    _close_connection_after = False

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        # Default logging records the request line, which can contain a query
        # string. Route bodies and credentials are never logged.
        LOGGER.debug("%s %s", self.address_string(), format % args)

    def _headers(self) -> dict[str, str]:
        return {key: value for key, value in self.headers.items()}

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            return b""
        if length <= 0:
            return b""
        if length > self.gateway.max_body_bytes:
            # Drain what the client is still writing *before* answering.
            # Responding and closing first makes the client fail with a
            # connection abort (WSAECONNABORTED on Windows) instead of reading
            # the 413 -- the merchant then sees a network error rather than
            # "your message is too long".
            self._drain(length)
            raise _BodyTooLarge()
        return self.rfile.read(length)

    def _drain(self, length: int) -> None:
        """Read and discard an oversized body, bounded in **both** size and time.

        Bounding size alone is not enough. A client that announces 4 GB and
        then trickles bytes would hold a worker thread — and the socket —
        indefinitely, turning an error response into a denial of service. Both
        caps are enforced, and hitting either still produces the 413.
        """
        remaining = min(length, DRAIN_LIMIT_BYTES)
        deadline = time.monotonic() + DRAIN_TIMEOUT_SECONDS
        try:
            while remaining > 0:
                if time.monotonic() >= deadline:
                    LOGGER.info(
                        "abandoning drain of oversized body: %d bytes remaining", remaining
                    )
                    return
                # A short socket timeout keeps the loop responsive to the
                # deadline even when the peer stops sending mid-body.
                self.connection.settimeout(min(1.0, max(0.05, deadline - time.monotonic())))
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    return
                remaining -= len(chunk)
        except OSError:
            # The client gave up first, or the socket timed out. Either way the
            # 413 still goes out below.
            return
        finally:
            try:
                self.connection.settimeout(None)
            except OSError:
                pass

    def _send(self, response: GatewayResponse) -> None:
        if response.html:
            body = response.html.encode("utf-8")
        elif response.raw:
            body = response.raw
        else:
            body = json.dumps(response.payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if getattr(self, "_close_connection_after", False):
            # The unread request body would otherwise be interpreted as the next
            # request on a keep-alive connection, corrupting the stream.
            self.send_header("Connection", "close")
            self.close_connection = True
        for name, value in response.headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _dispatch(self) -> None:
        try:
            body = self._read_body()
        except _BodyTooLarge:
            self._close_connection_after = True
            self._send(GatewayResponse(status=413, payload={"error": "payload_too_large", "message": "提交内容过大"}))
            return
        response = self.gateway.handle(
            self.command, self.path, self._headers(), body, read_json=self.gateway.decode_body
        )
        self._send(response)

    do_GET = _dispatch
    do_POST = _dispatch
    do_PATCH = _dispatch


class _BodyTooLarge(Exception):
    pass


def build_gateway(
    db_path: str,
    public_base_url: str = "",
    secure_cookies: bool = True,
    operator_token: str = "",
    opencode_url: str = "",
    opencode_user: str = "opencode",
    opencode_password: str = "",
    opencode_provider: str = "",
    opencode_model: str = "",
    expected_version: str = PINNED_OPENCODE_VERSION,
    workers: int = 1,
    platform_budget: Any = None,
) -> ProductGateway:
    store = ProductStore(db_path)
    adapter, report = _build_adapter(
        opencode_url, opencode_user, opencode_password, opencode_provider, opencode_model, expected_version
    )
    gateway = ProductGateway(
        store=store,
        adapter=adapter,
        public_base_url=public_base_url,
        secure_cookies=secure_cookies,
        operator_token=operator_token,
        adapter_report=report,
        platform_budget=platform_budget,
    )
    del workers
    return gateway


#: 密钥从**哪个文件**读。这是配置里唯一该出现的字段 —— 路径不是秘密。
OPENCODE_PASSWORD_FILE_ENV = "PYWEIXIN_OPENCODE_PASSWORD_FILE"
#: 密钥**内容**直接写在环境变量里。保留是为了兼容，但会告警：值会进部署日志。
OPENCODE_PASSWORD_ENV = "PYWEIXIN_OPENCODE_PASSWORD"

#: DeepSeek 密钥同样**只从文件读**。环境变量和命令行都不行：前者会进部署日志，
#: 后者会进 ``ps`` 输出。内容不进仓库、不进配置文件本身。
DEEPSEEK_KEY_FILE_ENV = "PYWEIXIN_DEEPSEEK_API_KEY_FILE"


def _resolve_deepseek_key(environ: Any = None) -> str:
    """读取 DeepSeek 密钥。只认文件，不认环境变量里的明文。"""
    env = os.environ if environ is None else environ
    path = str(env.get(DEEPSEEK_KEY_FILE_ENV, "") or "").strip()
    if not path:
        return ""
    try:
        content = Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        LOGGER.error("%s points at a file that does not exist", DEEPSEEK_KEY_FILE_ENV)
        return ""
    except OSError as exc:
        LOGGER.error("%s could not be read (%s)", DEEPSEEK_KEY_FILE_ENV, type(exc).__name__)
        return ""
    if not content:
        LOGGER.error("%s is empty", DEEPSEEK_KEY_FILE_ENV)
    return content


def resolve_opencode_password(environ: Any = None) -> str:
    """解析 OpenCode 的 Basic 认证口令。

    优先用**文件**（``PYWEIXIN_OPENCODE_PASSWORD_FILE``）：路径不是秘密，
    可以进配置；密钥本身只存在于那个文件里，不进环境变量、不进命令行，
    也就不会出现在部署日志和 ``ps`` 输出里。

    只有在**没有**配置文件时才回落到环境变量字面量，并打一条不含密钥的
    告警。两种情况都不会把口令写进日志或异常消息。
    """
    env = os.environ if environ is None else environ
    path = str(env.get(OPENCODE_PASSWORD_FILE_ENV, "") or "").strip()
    if path:
        file = Path(path)
        if not file.is_file():
            LOGGER.error("%s points at a file that does not exist; no password loaded", OPENCODE_PASSWORD_FILE_ENV)
            return ""
        try:
            text = file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            LOGGER.error("%s could not be read (%s); no password loaded", OPENCODE_PASSWORD_FILE_ENV, type(exc).__name__)
            return ""
        if not text:
            LOGGER.error("%s is empty; no password loaded", OPENCODE_PASSWORD_FILE_ENV)
            return ""
        return text.splitlines()[0].strip()
    literal = str(env.get(OPENCODE_PASSWORD_ENV, "") or "")
    if literal:
        LOGGER.warning(
            "%s holds the secret inline; prefer %s so the value stays out of deploy logs",
            OPENCODE_PASSWORD_ENV, OPENCODE_PASSWORD_FILE_ENV,
        )
    return literal


def write_secret_file(path: Path, secret: str) -> str:
    """把口令写进本地文件并尽量收紧权限。**从命令行参数取密钥是反模式。**

    优先给交互式入口用（``getpass`` 隐藏输入），所以口令不会进 shell 历史。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows 上 ACL 才是真正的边界
        pass
    if os.name == "nt":  # pragma: no cover - 仅 Windows
        # 文件系统 ACL 比 mode 位有用：只留当前用户与 SYSTEM。
        user = os.environ.get("USERNAME", "")
        try:
            import subprocess

            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W)", "*S-1-5-18:(F)"],
                capture_output=True, check=False, timeout=15,
            )
        except Exception as exc:
            LOGGER.warning("could not restrict ACL on %s (%s); the file is still git-ignored", path, type(exc).__name__)
    return str(path)


def secret_from_local_file(value: str, environ: Any = None) -> str:
    """兼容入口：``value`` 既可以是密钥文件路径，也可以是密钥字面量。

    新代码请用 :func:`resolve_opencode_password`，它把"内容"和"路径"分成
    两个字段，不会混淆。
    """
    env = os.environ if environ is None else environ
    del env
    if not value:
        return ""
    path = Path(value)
    if not path.is_file():
        # 看起来更像一个密钥本身而不是路径：按字面量处理，保持兼容。
        return value
    try:
        return path.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except (OSError, IndexError) as exc:  # pragma: no cover - 读文件失败要说清
        LOGGER.error("secret file %s could not be read: %s", path, type(exc).__name__)
        return ""


def platform_budget_from_env(environ: Any = None) -> Any:
    """从环境变量读平台总预算与全局并发。

    全部不设 = 不限制，这是安全的默认值。设了任一项就按该项执行 ——
    平台自己的请求数 / 并发上限，**不依赖客户端**自觉。

    订阅费用无法可靠按请求折算，所以这里默认**不填**金额：没单价时
    ``cost_cents_per_paid_job`` 留空，运营视图里 ``cost_known`` 就是 False，
    而不是编一个人民币成本出来。
    """
    env = os.environ if environ is None else environ

    def number(name: str) -> int:
        try:
            return max(0, int(str(env.get(name, "") or 0)))
        except (TypeError, ValueError):
            LOGGER.warning("ignoring non-numeric %s", name)
            return 0

    def optional_int(name: str) -> int | None:
        raw = str(env.get(name, "") or "").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            LOGGER.warning("ignoring non-numeric %s", name)
            return None

    daily_paid_jobs = number("PYWEIXIN_PLATFORM_DAILY_JOBS")
    max_paid_concurrency = number("PYWEIXIN_PLATFORM_MAX_CONCURRENCY")
    cost_cents = optional_int("PYWEIXIN_PLATFORM_COST_CENTS_PER_JOB")
    daily_cost_cents = optional_int("PYWEIXIN_PLATFORM_DAILY_COST_CENTS")
    if not any((daily_paid_jobs, max_paid_concurrency, cost_cents, daily_cost_cents)):
        return None
    return PlatformBudget(
        daily_paid_jobs=daily_paid_jobs,
        max_paid_concurrency=max_paid_concurrency,
        cost_cents_per_paid_job=cost_cents,
        daily_cost_cents=daily_cost_cents,
    )


def _build_adapter(
    url: str, username: str, password: str, provider: str, model: str, expected_version: str
) -> tuple[Any, dict[str, Any]]:
    """按优先级选执行后端。

    **DeepSeek 是正式默认**。OpenCode 保留为可选的遗留后端：配了地址就用它，
    没配就用 DeepSeek。两者都不需要时退回本地规则适配器，并**明说那不是模型
    结果** —— 悄悄用一个"看起来能跑"的桩冒充模型解析，比明确不可用更糟。
    """
    from .deepseek_adapter import DeepSeekAdapter

    if url:
        # 显式配了 OpenCode 地址：尊重配置，但把"这是遗留路径"写进报告。
        adapter = OpenCodeAdapter(
            url,
            username=username,
            password=password,
            provider_id=provider,
            model_id=model,
            expected_version=expected_version,
        )
        report = adapter.probe()
        report["legacy_opencode"] = True
        return adapter, report

    key = _resolve_deepseek_key()
    if key:
        adapter = DeepSeekAdapter(
            api_key=key,
            model=os.environ.get("PYWEIXIN_DEEPSEEK_MODEL", "deepseek-flash"),
            base_url=os.environ.get("PYWEIXIN_DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
            timeout=float(os.environ.get("PYWEIXIN_DEEPSEEK_TIMEOUT", "60") or 60),
            response_schema=ORDER_PARSE_SCHEMA,
        )
        return adapter, adapter.probe()

    report = LocalRuleAdapter().probe()
    report["warning"] = (
        "未配置 DeepSeek 密钥，当前使用本地规则适配器；这不是模型解析结果"
    )
    return LocalRuleAdapter(), report


def _bootstrap_catalog(gateway: Any, db_path: str, policy_path: str) -> None:
    """启动时装配模型目录。

    三件事：读回上次保存的目录 → 载入运营策略 → 绑定上游来源并拉一次。

    没有策略文件就**只有历史目录，没有可选模型**，网关会如实拒绝任务。
    这比悄悄退回一个固定模型要好：后者会让商户在不知情的情况下被计费。
    """
    from pathlib import Path

    saved = gateway.store.load_catalog()
    if len(saved):
        gateway.catalog.catalog = saved
    if policy_path and Path(policy_path).is_file():
        try:
            policies = json.loads(Path(policy_path).read_text(encoding="utf-8"))
            gateway.catalog.load_policies(policies.get("models", policies) if isinstance(policies, dict) else policies)
            gateway.store.save_model_policies(gateway.catalog.policies())
        except Exception as exc:  # pragma: no cover - 配置坏了要看得见
            LOGGER.error("model policy file could not be read: %s", exc)
    lister = getattr(gateway.adapter, "list_providers", None)
    if callable(lister):
        gateway.catalog.bind(lambda: lister(), {p.alias: p for p in gateway.catalog.policies()})
        try:
            gateway.catalog.refresh(force=True)
            gateway.store.save_catalog(gateway.catalog.catalog)
        except Exception as exc:  # pragma: no cover - 上游不可达要看得见
            LOGGER.warning("model catalog refresh at startup failed: %s", exc)
        return
    # 没有上游可拉（离线/演练/无 OpenCode）：目录只能来自运营声明。
    # 这时模型**不会**自动变成可用 —— 除非策略里显式写了 assumed_verified，
    # 并且会打一条警告，因为那等于跳过了真实调用验证。
    from pyweixin.model_catalog import CatalogStatus, ModelEntry, ModelCatalog

    entries = {}
    for policy in gateway.catalog.policies():
        entries[policy.alias] = ModelEntry(
            alias=policy.alias,
            display_name=policy.alias,
            provider_id=policy.provider_id,
            model_id=policy.model_id,
            policy=policy,
            status=CatalogStatus.SELECTABLE,
            status_reason="",
            verified_at=1.0 if getattr(policy, "assumed_verified", False) else 0.0,
        )
        if not getattr(policy, "assumed_verified", False):
            entries[policy.alias].status = CatalogStatus.DISCOVERED
            entries[policy.alias].status_reason = "尚未通过订单能力验证"
    if entries:
        LOGGER.warning(
            "no upstream catalog source; using operator-declared models "
            "(%d declared, %d assumed verified without a real call)",
            len(entries),
            sum(1 for e in entries.values() if e.verified_at),
        )
    gateway.catalog.catalog = ModelCatalog(entries=entries)
    gateway.store.save_catalog(gateway.catalog.catalog)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pyweixin-gateway")
    parser.add_argument("--host", default=os.environ.get("PYWEIXIN_GATEWAY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PYWEIXIN_GATEWAY_PORT", "8080")))
    parser.add_argument("--db", default=os.environ.get("PYWEIXIN_PRODUCT_DB", "product.db"))
    parser.add_argument("--public-base-url", default=os.environ.get("PYWEIXIN_PUBLIC_BASE_URL", ""))
    parser.add_argument("--operator-token", default=os.environ.get("PYWEIXIN_OPERATOR_TOKEN", ""))
    parser.add_argument("--workers", type=int, default=int(os.environ.get("PYWEIXIN_PARSE_WORKERS", "1")))
    parser.add_argument(
        "--insecure-cookies",
        action="store_true",
        default=os.environ.get("PYWEIXIN_SECURE_COOKIES", "1") not in {"1", "true", "yes"},
        help="仅用于本机 HTTP 调试；公网部署必须保持 Secure Cookie",
    )
    parser.add_argument("--log-level", default=os.environ.get("PYWEIXIN_LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    gateway = build_gateway(
        args.db,
        public_base_url=args.public_base_url,
        secure_cookies=not args.insecure_cookies,
        operator_token=args.operator_token,
        opencode_url=os.environ.get("PYWEIXIN_OPENCODE_URL", ""),
        opencode_user=os.environ.get("PYWEIXIN_OPENCODE_USER", "opencode"),
        opencode_password=resolve_opencode_password(),
        opencode_provider=os.environ.get("PYWEIXIN_OPENCODE_PROVIDER", ""),
        opencode_model=os.environ.get("PYWEIXIN_OPENCODE_MODEL", ""),
        workers=args.workers,
        platform_budget=platform_budget_from_env(),
    )
    gateway.start(args.workers)
    _bootstrap_catalog(gateway, args.db, os.environ.get("PYWEIXIN_MODEL_POLICY", ""))
    report = gateway.execution_backend_report()
    LOGGER.info("execution backend ok=%s %s", report.get("ok"), report.get("version", report.get("error", "")))
    if not report.get("ok"):
        LOGGER.error("execution backend NOT ready: %s", json.dumps(report, ensure_ascii=False))
    LOGGER.info(
        "model catalog: %d entries, default=%s, free_available=%s",
        len(gateway.catalog),
        gateway.catalog.default_entry().alias if gateway.catalog.default_entry() else "(无)",
        bool(gateway.catalog.default_entry() and gateway.catalog.default_entry().policy.is_free),
    )
    LOGGER.info("platform budget: %s", json.dumps(gateway.platform_budget_status(), ensure_ascii=False))

    class Handler(GatewayHTTPRequestHandler):
        pass

    Handler.gateway = gateway
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True

    def shutdown(*_: Any) -> None:
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for signal_name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, signal_name):
            try:
                signal.signal(getattr(signal, signal_name), shutdown)
            except ValueError:  # pragma: no cover - non-main thread
                pass

    LOGGER.info("product gateway listening on http://%s:%s", args.host, args.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        gateway.stop()
        httpd.server_close()
        gateway.store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
