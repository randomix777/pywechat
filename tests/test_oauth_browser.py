"""浏览器跳转登录：客户端这一侧的行为。

## 界面流程长什么样

    点「登录产品账号」
      → 浏览器自动打开 JYI 登录页
      → 商户在网页上登录
      → JYI 把浏览器重定向回 127.0.0.1
      → 客户端拿授权码换令牌，完成

商户**不用再手动敲验证码**。

## 两件不能省的事

这两条测试就是为它们写的：

* **只监听 127.0.0.1** —— 监听 `0.0.0.0` 等于把换令牌的入口开到局域网，
  同一网络里任何人点一下就能试。
* **校验 state** —— 不校验的话，攻击者能把自己的授权码塞进回调，
  把商户的客户端绑到他自己的账号上，而商户毫无察觉。

## 还有一条容易忽略

超时必须**收摊**：浏览器开错了、或者商户走开了，客户端不能一直挂着等。
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pyweixin.oauth_browser import (  # noqa: E402
    CALLBACK_PATH, BrowserLoginFlow, CallbackResult, LoopbackReceiver,
    OAuthConfig, OAuthError, build_authorize_url, find_free_port,
)


def make_config(**kwargs) -> OAuthConfig:
    base = {
        "authorize_base": "https://jyi.example.com",
        "token_base": "https://api.jyi.example.com",
        "client_id": "pyweixin-desktop",
    }
    base.update(kwargs)
    return OAuthConfig(**base)


class ConfigTests(unittest.TestCase):
    def test_it_refuses_a_plain_http_remote_site(self):
        """授权页会带上 code，裸 http 等于把令牌暴露在路上。"""
        with self.assertRaises(ValueError):
            OAuthConfig(authorize_base="http://jyi.example.com",
                        token_base="https://api.jyi.example.com")

    def test_loopback_is_allowed_over_http(self):
        """本机回环本来就走 http，拦住它自己就把自己挡了。"""
        OAuthConfig(authorize_base="http://127.0.0.1:8080",
                    token_base="http://127.0.0.1:8080")

    def test_an_empty_client_id_is_refused(self):
        with self.assertRaises(ValueError):
            make_config(client_id="")


class AuthorizeUrlTests(unittest.TestCase):
    def test_it_carries_what_the_site_needs(self):
        url = build_authorize_url(make_config(), "http://127.0.0.1:5000/cb", "STATE123")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["client_id"], ["pyweixin-desktop"])
        self.assertEqual(query["redirect_uri"], ["http://127.0.0.1:5000/cb"])
        self.assertEqual(query["state"], ["STATE123"])

    def test_the_paths_are_configurable(self):
        """JYI 的路由跟我们不一样，所以路径不能写死。"""
        url = build_authorize_url(
            make_config(authorize_path="/oauth/authorize"),
            "http://127.0.0.1:5000/cb", "S")
        self.assertIn("/oauth/authorize?", url)

    def test_pkce_is_on_by_default_and_carries_a_challenge(self):
        url = build_authorize_url(make_config(), "http://127.0.0.1:1/cb", "S",
                                  code_challenge="abc123")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["code_challenge"], ["abc123"])


class LoopbackTests(unittest.TestCase):
    def test_it_only_listens_on_loopback(self):
        """最要紧的一条：不能绑 0.0.0.0。"""
        with LoopbackReceiver() as receiver:
            import socket
            host = receiver._server.server_address[0]
            self.assertEqual(host, "127.0.0.1")

    def test_it_serves_one_callback_then_stops(self):
        with LoopbackReceiver() as receiver:
            url = f"http://127.0.0.1:{receiver.port}{CALLBACK_PATH}?code=abc&state=xyz"
            with urllib.request.urlopen(url, timeout=5) as resp:
                body = resp.read().decode("utf-8")
            self.assertEqual(resp.status, 200)
            self.assertIn("登录成功", body)
            self.assertEqual(receiver.received.code, "abc")
            self.assertEqual(receiver.received.state, "xyz")

    def test_the_callback_page_speaks_chinese_not_a_blank_screen(self):
        """商户看的是这个页面。空白页会让人以为坏了。"""
        with LoopbackReceiver() as receiver:
            url = f"http://127.0.0.1:{receiver.port}{CALLBACK_PATH}?code=a&state=b"
            with urllib.request.urlopen(url, timeout=5) as resp:
                body = resp.read().decode("utf-8")
            self.assertIn("回到接单助手", body)

    def test_a_denial_explains_itself(self):
        with LoopbackReceiver() as receiver:
            url = (f"http://127.0.0.1:{receiver.port}{CALLBACK_PATH}"
                   "?error=access_denied")
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    body, status = resp.read().decode("utf-8"), resp.status
            except urllib.error.HTTPError as exc:
                # 拒绝场景给的是 4xx，urlopen 会抛 —— 那正是我们要的行为
                body, status = exc.read().decode("utf-8"), exc.code
            self.assertEqual(status, 400)
            self.assertIn("取消了授权", body)

    def test_an_unknown_error_code_is_not_shown_to_the_merchant(self):
        """内部标识不该出现在商户屏幕上。"""
        with LoopbackReceiver() as receiver:
            url = (f"http://127.0.0.1:{receiver.port}{CALLBACK_PATH}"
                   "?error=internal_db_shard_7_broken")
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    body = resp.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8")
            self.assertNotIn("internal_db_shard_7_broken", body)

    def test_timeout_does_not_hang_forever(self):
        """商户走开了，客户端不能一直等。"""
        receiver = LoopbackReceiver()
        receiver.start()
        try:
            started = time.time()
            with self.assertRaises(OAuthError):
                receiver.wait(0.4)
            self.assertLess(time.time() - started, 3.0)
        finally:
            receiver.stop()

    def test_a_free_port_helper_works(self):
        port = find_free_port()
        self.assertGreater(port, 0)


class EndToEndTests(unittest.TestCase):
    """跑一次完整流程：开端口 → 假装浏览器跳回来 → 换令牌。"""

    def _run(self, browser_result: dict, exchange_result="SESSION",
             echo_state: bool = True):
        captured: dict = {}

        def fake_open(url: str) -> bool:
            captured["url"] = url
            # 模拟浏览器：把授权页地址改成回调地址，再自己请求。
            parsed = urllib.parse.urlparse(url)
            query = dict(urllib.parse.parse_qsl(parsed.query))
            redirect = query["redirect_uri"]
            payload = dict(browser_result)
            # 真实站点会把 state 原样带回去；伪造回调才会被拒绝。
            if echo_state:
                payload.setdefault("state", query.get("state", ""))
            callback = f"{redirect}?{urllib.parse.urlencode(payload)}"
            def _visit() -> None:
                try:
                    urllib.request.urlopen(callback, timeout=5).close()
                except Exception:
                    pass          # 测试里回调返回 4xx 是正常的

            threading.Thread(target=_visit, daemon=True).start()
            return True

        def fake_exchange(**kwargs):
            captured["exchange"] = kwargs
            return exchange_result

        flow = BrowserLoginFlow(config=make_config(), exchange=fake_exchange,
                                open_browser=fake_open, timeout=8.0)
        return flow.run(), captured

    def test_a_happy_path_returns_the_session(self):
        session, captured = self._run({"code": "AUTH_CODE"})
        self.assertEqual(session, "SESSION")
        self.assertEqual(captured["exchange"]["code"], "AUTH_CODE")

    def test_the_exchange_receives_the_redirect_uri_it_advertised(self):
        """JYI 会核对：换令牌时的 redirect_uri 必须和授权时一致。"""
        _, captured = self._run({"code": "AUTH_CODE"})
        self.assertTrue(captured["exchange"]["redirect_uri"].endswith(CALLBACK_PATH))

    def test_a_mismatched_state_is_refused(self):
        """有人在伪造回调。不校验 state 就等于把账号绑到他手上。"""
        with self.assertRaises(OAuthError) as ctx:
            self._run({"code": "STOLEN", "state": "不是我发的"}, echo_state=False)
        self.assertIn("校验", str(ctx.exception))

    def test_a_denial_surfaces_in_chinese(self):
        with self.assertRaises(OAuthError) as ctx:
            self._run({"error": "access_denied"})
        self.assertIn("取消了授权", str(ctx.exception))

    def test_a_callback_without_a_code_is_refused(self):
        with self.assertRaises(OAuthError):
            self._run({"code": ""})

    def test_cancel_stops_the_flow(self):
        flow = BrowserLoginFlow(config=make_config(), exchange=lambda **k: "S")
        flow.cancel()
        self.assertTrue(flow._stop.is_set())


if __name__ == "__main__":
    unittest.main()