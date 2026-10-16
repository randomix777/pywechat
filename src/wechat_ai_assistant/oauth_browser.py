"""浏览器跳转登录（OAuth 授权码 + 本机回环）。

## 为什么不是现在的设备码

商户的原话：**「登录跳转是自动开浏览器」**。

现在这套（`client_login.DeviceLoginFlow`）也要开浏览器，但商户得**手动把
验证码敲进去**。对每天要用的软件来说，多这一步就是一次流失。

授权码流程是浏览器自己跳转回来，商户只管在网页上登录：

    点「登录产品账号」
      → 浏览器自动打开 JYI 的登录页
      → 商户在网页上登录（JYI 负责账号、密码、验证码）
      → JYI 把浏览器重定向回本机 127.0.0.1
      → 客户端拿授权码换到令牌，登录完成

## 为什么本机回环而不是让 JYI 直接下发令牌

桌面客户端没有稳定的公网回调地址。把回调开到 `127.0.0.1` 上，
**令牌永远不经过第三方**，也不用为每个商户在 JYI 那边登记回调地址。

## JYI 生产接入

端点路径全部可配置；生产配置由 ``jyi_client`` 提供：

    GET  {authorize_base}/connect/authorize?client_id&redirect_uri&state&response_type=code
    →  重定向到 {redirect_uri}?code=...&state=...
    POST {token_base}/oauth/token   {client_id, code, redirect_uri, code_verifier}

JYI 返回连接设备凭据与网关 Key，由客户端使用 DPAPI 保存。

## 两件不能省的事

* **state 必须校验** —— 不校验的话，攻击者能把自己的授权码塞进来，
  把商户的客户端绑到他自己的账号上。
* **只监听 127.0.0.1** —— 监听 `0.0.0.0` 等于把令牌接口开到局域网。
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import secrets
import socket
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)

#: 回环回调的固定路径。JYI 那边要登记的也是这个。
CALLBACK_PATH = "/oauth/callback"

#: 找不到空闲端口时依次尝试的范围。0 表示让系统分配。
PORT_RANGE = range(0, 20)

#: 商户放弃等待的上限。到点就收摊，不让人对着窗口干等。
DEFAULT_TIMEOUT = 300.0


class OAuthError(RuntimeError):
    """登录流程本身出了问题（不是商户填错，也不是网络抖）。"""


@dataclass(frozen=True)
class OAuthConfig:
    """JYI 侧的登录端点。

    默认值指向本项目自带的网关，方便本地开发；JYI 端点完成后改配置即可。
    """

    #: 授权页的**基址**（不含 /authorize）。通常是 JYI 网站的根。
    authorize_base: str
    #: 换令牌的基址（不含路径）。
    token_base: str
    # This OAuth client is registered under the old id on the JYI server.
    client_id: str = "pyweixin-desktop"
    #: 授权页路径。JYI 可以按自己的路由改。
    authorize_path: str = "/authorize"
    #: 换令牌路径。
    token_path: str = "/v1/auth/exchange"
    #: 固定端口。0 = 每次让系统分配（推荐，避开端口冲突）。
    redirect_port: int = 0
    scope: str = ""
    #: 关掉 PKCE。桌面客户端用不上就别开，开了 JYI 也得支持。
    use_pkce: bool = True

    def __post_init__(self) -> None:
        for name in ("authorize_base", "token_base"):
            value = getattr(self, name)
            if not value.startswith("https://") and not _is_loopback(value):
                raise ValueError(f"{name} 必须是 https（回环地址除外）")
        if not self.client_id:
            raise ValueError("client_id 不能为空")


def _is_loopback(url: str) -> bool:
    host = url.split("://", 1)[-1].split("/")[0].split(":")[0]
    return host in ("127.0.0.1", "localhost", "::1")


@dataclass
class CallbackResult:
    """浏览器跳回来的那一下。"""

    code: str = ""
    state: str = ""
    error: str = ""
    error_description: str = ""


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """只处理一次回调，然后自己关掉。

    页面内容给商户看：**说清楚发生了什么**，不能让他对着白屏猜。
    """

    server_version = "wechat-ai-assistant"
    #: 由 :class:`LoopbackReceiver` 注入。
    sink: Callable[[CallbackResult], None] | None = None

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # 默认实现会把每个请求打到 stderr —— 客户端是 pythonw，没有 stderr，
        # 而且这里本来也没人看。
        # BaseHTTPRequestHandler's request line contains the authorization code.
        # Never put the callback query into logs, including debug logs.
        LOGGER.debug("oauth callback handled")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self._reply(404, "页面不存在", "这个地址不是登录回调。")
            return
        query = urllib.parse.parse_qs(parsed.query)
        result = CallbackResult(
            code=(query.get("code") or [""])[0],
            state=(query.get("state") or [""])[0],
            error=(query.get("error") or [""])[0],
            error_description=(query.get("error_description") or [""])[0],
        )
        if self.sink is not None:
            self.sink(result)
        if result.error:
            self._reply(400, "登录没有成功", _friendly_error(result))
        else:
            self._reply(
                200,
                "登录成功",
                "可以关闭这个浏览器标签页，回到微信AI智能助理继续。",
            )

    def _reply(self, code: int, title: str, body: str) -> None:
        page = (
            "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
            "<title>" + title + "</title>"
            "<body style=\"font-family:system-ui,'Microsoft YaHei',sans-serif;"
            "display:flex;align-items:center;justify-content:center;height:100vh;"
            "margin:0;background:#f6f7f9;color:#1f2933\">"
            "<div style=\"text-align:center;max-width:32rem;padding:2rem\">"
            f"<h1 style=\"font-size:1.25rem;margin:0 0 .75rem\">{title}</h1>"
            f"<p style=\"margin:0;color:#5a6572;line-height:1.7\">{body}</p>"
            "</div></body></html>"
        ).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(page)
        except OSError:
            # 商户可能已经关掉标签页了。那不是错误。
            pass


def _friendly_error(result: CallbackResult) -> str:
    """把 JYI 的错误码翻成人话。原文只进日志。"""
    known = {
        "access_denied": "您取消了授权。",
        "invalid_request": "登录请求有问题，请重新点一次登录。",
        "unauthorized_client": "这个客户端没有登录权限，请联系客服。",
        "server_error": "登录服务暂时不可用，请稍后再试。",
        "temporarily_unavailable": "登录服务暂时不可用，请稍后再试。",
    }
    text = known.get(result.error)
    if text:
        return text
    # 不认识的错误码**不往商户屏幕上放**：可能是内部标识。
    LOGGER.warning("oauth authorize failed: %s / %s",
                   result.error, result.error_description)
    return "登录没有成功，请重新点一次登录。"


def _state_matches(received: str, expected: str) -> bool:
    """恒定时间比对 state。

    回调里的 state 是**不可信输入**：它可能不是 ASCII，也可能根本不是字符串。
    直接丢给 ``compare_digest`` 会抛 ``TypeError`` —— 也就是说，一个乱来的
    请求能让登录流程崩在一个看不懂的异常上，而不是干净地说"校验没过"。

    所以先统一成字节再比。
    """
    try:
        left = (received or "").encode("utf-8")
        right = (expected or "").encode("utf-8")
    except (AttributeError, UnicodeError):
        return False
    return secrets.compare_digest(left, right)


class LoopbackReceiver:
    """在本机开一个只听 127.0.0.1 的临时端口，等一次回调。"""

    def __init__(self, port: int = 0) -> None:
        self._wanted_port = port
        self._server: http.server.HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.received = CallbackResult()
        self._done = threading.Event()

    @property
    def port(self) -> int:
        if self._server is None:
            raise OAuthError("回环服务还没启动")
        return int(self._server.server_address[1])

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}{CALLBACK_PATH}"

    def start(self) -> None:
        handler = type("_H", (_CallbackHandler,), {"sink": self._sink})
        last: Exception | None = None
        for candidate in ([self._wanted_port] if self._wanted_port else list(PORT_RANGE)):
            try:
                # 关键：绑 127.0.0.1，不绑 0.0.0.0。
                self._server = http.server.HTTPServer(
                    ("127.0.0.1", candidate), handler
                )
                self._wanted_port = candidate
                break
            except OSError as exc:
                last = exc
                continue
        if self._server is None:
            raise OAuthError(f"本机回环端口无法监听：{last}")
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="oauth-loopback", daemon=True
        )
        self._thread.start()

    def _sink(self, result: CallbackResult) -> None:
        self.received = result
        self._done.set()

    def wait(self, timeout: float) -> CallbackResult:
        if not self._done.wait(timeout):
            raise OAuthError("等待浏览器跳转超时")
        return self.received

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except OSError:
                pass
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def __enter__(self) -> "LoopbackReceiver":
        self.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.stop()


def build_authorize_url(config: OAuthConfig, redirect_uri: str, state: str,
                        code_challenge: str = "") -> str:
    """拼出要交给浏览器打开的地址。"""
    query = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    if config.scope:
        query["scope"] = config.scope
    if code_challenge:
        query["code_challenge"] = code_challenge
        query["code_challenge_method"] = "S256"
    base = config.authorize_base.rstrip("/")
    path = config.authorize_path if config.authorize_path.startswith("/") \
        else "/" + config.authorize_path
    return f"{base}{path}?{urllib.parse.urlencode(query)}"


def _pkce_pair() -> tuple[str, str]:
    """PKCE。桌面客户端没有 secret，用它把授权码钉死在这一次请求上。"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return verifier, challenge


def find_free_port() -> int:
    """探一个空闲端口。给不想依赖系统的调用方用。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class BrowserLoginFlow:
    """一次完整的浏览器跳转登录：开端口 → 开浏览器 → 等回调 → 换令牌。

    ``exchange`` 由调用方提供，因为它要知道该往哪个 token 端点发请求 ——
    那是网关/JYI 的事，不该写死在这个模块里。
    """

    config: OAuthConfig
    exchange: Callable[..., Session]
    open_browser: Callable[[str], bool] = webbrowser.open
    on_progress: Callable[[Any], None] | None = None
    timeout: float = DEFAULT_TIMEOUT
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)

    def cancel(self) -> None:
        self._stop.set()

    def _emit(self, detail: str, **kwargs: Any) -> None:
        if self.on_progress is not None:
            payload = {"detail": detail}
            payload.update(kwargs)
            self.on_progress(payload)

    def run(self) -> Session:
        # 校验 state 是必须的：不校验的话，别人能把自己的授权码塞进回调，
        # 把商户的客户端绑到**他的**账号上。
        state = secrets.token_urlsafe(24)
        verifier, challenge = _pkce_pair() if self.config.use_pkce else ("", "")

        redirect_uri = ""
        with LoopbackReceiver(self.config.redirect_port) as receiver:
            # redirect_uri 要在**服务还活着的时候**记下来。出了 with，
            # 端口已经关了，再去问就只剩一句"回环服务还没启动"。
            redirect_uri = receiver.redirect_uri
            url = build_authorize_url(self.config, redirect_uri, state, challenge)
            self._emit("正在打开浏览器，请在网页上完成登录", authorize_url=url)
            if not self.open_browser(url):
                # 打不开浏览器不是致命错误：把地址给商户，让他自己开。
                self._emit(f"没能自动打开浏览器，请手动打开这个地址登录：{url}")
            result = receiver.wait(self.timeout)

        if result.error:
            raise OAuthError(_friendly_error(result))
        if not result.code:
            raise OAuthError("登录没有成功，请重新点一次登录")
        if not _state_matches(result.state, state):
            # 不该走到这里。走到了就是有人在捣鬼。
            LOGGER.error("oauth state mismatch")
            raise OAuthError("登录校验没通过，请重新点一次登录")

        self._emit("登录成功，正在获取账户信息")
        return self.exchange(
            code=result.code,
            redirect_uri=redirect_uri,
            code_verifier=verifier,
        )
