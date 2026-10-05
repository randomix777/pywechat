"""摘要失败时必须告诉商户**能照着做**的事。

## 出过什么事

点「整理成一段话」，界面上只有：

    整理失败：HTTP 402

商户看到这个学不到任何东西：不知道是自己余额不够、还是软件坏了、
还是该去交钱。而真实原因就写在响应体里：

    You requested up to 2000 tokens, but can only afford 473

## 两个真问题

**一、`max_tokens` 设成了 2000。**
上游按「你最多可能用多少」预扣，**要得比需要的多就白白抬高门槛**。
一份群消息简报几百个字就够，2000 在余额紧的账号上必然失败。
（不设更糟：上游默认 65536。）

**二、那个错误里带着能用的数字，却被丢掉了。**
既然上游说了「你只付得起 473」，就按它重试一次 ——
这决定了「能出一份短一点的简报」还是「什么都出不来」。

## 现在

* 402 → 说清是余额不足、**不是软件问题**，并按可用额度重试一次
* 429 → 说清是临时限流，等一会再点
* 401/403 → 说清是密钥问题
* 其他 → 带上上游原话（截断），不要只给一个码
"""
from __future__ import annotations

import io
import json
import pathlib
import sys
import unittest
import urllib.error
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pyweixin.summary import Summarizer  # noqa: E402


def _http_error(code: int, message: str, previous: list | None = None) -> urllib.error.HTTPError:
    payload: dict = {"error": {"message": message}}
    if previous is not None:
        payload["error"]["metadata"] = {"previous_errors": previous}
    return urllib.error.HTTPError(
        "https://example.invalid", code, "err", {}, io.BytesIO(json.dumps(payload).encode()))


def _ok(content: str = "简报正文") -> mock.MagicMock:
    response = mock.MagicMock()
    response.read.return_value = json.dumps(
        {"choices": [{"message": {"content": content}}], "usage": {"cost": 0.0001}}).encode()
    response.__enter__ = lambda s: s
    response.__exit__ = lambda *a: False
    return response


MESSAGES = [{"sender": "张姐", "content": "要两箱苹果", "sent_at": "2026-10-14T09:00"}]


class MaxTokensTests(unittest.TestCase):
    def test_the_default_is_not_inflated(self):
        """要得比需要的多，就会在余额紧的账号上白撞 402。"""
        self.assertLessEqual(Summarizer("u", "k", "m").max_tokens, 1000,
                             "max_tokens 设太大，上游预扣会直接拒掉")

    def test_it_is_still_set_at_all(self):
        """不设会被上游默认成 65536，请求本身就超了。"""
        self.assertGreater(Summarizer("u", "k", "m").max_tokens, 0)


class BalanceRetryTests(unittest.TestCase):
    """402 里带着可用额度 —— 用它重试一次。"""

    def test_it_retries_with_what_the_account_can_afford(self):
        asked: list[int] = []

        def opener(request, timeout=None):
            asked.append(json.loads(request.data.decode())["max_tokens"])
            if len(asked) == 1:
                raise _http_error(402, "This request requires more credits, or fewer "
                                       "max_tokens. You requested up to 800 tokens, "
                                       "but can only afford 300")
            return _ok("按可用额度出的简报")

        summarizer = Summarizer("https://example.invalid/v1", "k", "m")
        with mock.patch("urllib.request.urlopen", side_effect=opener):
            result = summarizer.summarize(MESSAGES)
        self.assertEqual(asked, [800, 300], "没有按上游说的额度重试")
        self.assertEqual(result["summary"], "按可用额度出的简报")
        self.assertIsNone(result["error"])

    def test_it_reads_the_number_out_of_a_buried_previous_error(self):
        """真正的原因可能被埋在 previous_errors 里（上游重试链）。"""
        asked: list[int] = []

        def opener(request, timeout=None):
            asked.append(json.loads(request.data.decode())["max_tokens"])
            if len(asked) == 1:
                raise _http_error(402, "generic", previous=[
                    {"code": 402, "message": "You requested up to 800 tokens, "
                                             "but can only afford 250"}])
            return _ok()
        summarizer = Summarizer("https://example.invalid/v1", "k", "m")
        with mock.patch("urllib.request.urlopen", side_effect=opener):
            summarizer.summarize(MESSAGES)
        self.assertEqual(asked[-1], 250)

    def test_it_retries_only_once(self):
        """反复重试会把余额耗尽在注定失败的请求上。"""
        calls = []

        def opener(request, timeout=None):
            calls.append(1)
            raise _http_error(402, "can only afford 100")
        summarizer = Summarizer("https://example.invalid/v1", "k", "m")
        with mock.patch("urllib.request.urlopen", side_effect=opener):
            result = summarizer.summarize(MESSAGES)
        self.assertEqual(len(calls), 2, "重试次数应当正好是 2（原始 + 一次）")
        self.assertTrue(result["error"])

    def test_a_tiny_affordable_amount_is_floored(self):
        """上游偶尔报个位数 —— 拿它当 max_tokens 连一句话都写不完。"""
        asked: list[int] = []

        def opener(request, timeout=None):
            asked.append(json.loads(request.data.decode())["max_tokens"])
            if len(asked) == 1:
                raise _http_error(402, "can only afford 3")
            return _ok()
        summarizer = Summarizer("https://example.invalid/v1", "k", "m")
        with mock.patch("urllib.request.urlopen", side_effect=opener):
            summarizer.summarize(MESSAGES)
        self.assertGreaterEqual(asked[-1], 64)


class ErrorMessageTests(unittest.TestCase):
    """错误文案要能照着做。"""

    def _fail_with(self, code, message, previous=None):
        summarizer = Summarizer("https://example.invalid/v1", "k", "m")
        with mock.patch("urllib.request.urlopen",
                        side_effect=_http_error(code, message, previous)):
            return summarizer.summarize(MESSAGES)["error"]

    def test_the_bare_http_code_is_never_the_whole_message(self):
        error = self._fail_with(402, "can only afford 100")
        self.assertNotEqual(error, "HTTP 402")
        self.assertIn("余额", error)

    def test_balance_says_it_is_not_a_software_problem(self):
        """商户第一反应会是「软件坏了」—— 要说清不是。"""
        error = self._fail_with(402, "can only afford 100")
        self.assertIn("不是软件问题", error)

    def test_balance_mentions_the_data_is_not_lost(self):
        error = self._fail_with(402, "")
        self.assertIn("没少", error)

    def test_rate_limit_says_just_wait(self):
        error = self._fail_with(429, "rate limited")
        self.assertIn("再点一次", error)

    def test_a_bad_key_says_so(self):
        for code in (401, 403):
            self.assertIn("密钥", self._fail_with(code, "unauthorized"))

    def test_other_errors_carry_the_upstream_words(self):
        error = self._fail_with(500, "upstream exploded")
        self.assertIn("upstream exploded", error, "不带原话就没法排查")


if __name__ == "__main__":
    unittest.main()