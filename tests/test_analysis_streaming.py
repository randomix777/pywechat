"""Regression coverage for reasoning exhausting JYI's budget before final text."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wechat_ai_assistant.client_login import GatewayError  # noqa: E402
from wechat_ai_assistant.jyi_client import JYIClient  # noqa: E402
from wechat_ai_assistant.summary_task import (  # noqa: E402
    _complete_jyi_chunk, _complete_summary_prompt, _jyi_model_limits,
    _run_summary_jyi,
)


def completion(text: str, *, finish: str = "stop", reasoning: str = "") -> bytes:
    events = []
    if reasoning:
        events.append({"choices": [{"delta": {"reasoning_content": reasoning}}]})
    events.extend([
        {"model": "deepseek-v4.1-flash", "choices": [{"delta": {"content": text}}]},
        {"choices": [{"delta": {}, "finish_reason": finish}],
         "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                   "completion_tokens_details": {"reasoning_tokens": 0}}},
    ])
    return ("".join("data: " + json.dumps(event) + "\n\n" for event in events)
            + "data: [DONE]\n\n").encode()


class Response(io.BytesIO):
    status = 200
    headers = {"x-jyi-request-id": "gwr_test_diagnostics"}


class Opener:
    def __init__(self, *responses: bytes):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("Unexpected extra paid request")
        return Response(self.responses.pop(0))

    def bodies(self):
        return [json.loads(request.data) for request in self.requests]


class AnalysisStreamingTests(unittest.TestCase):
    def test_final_content_is_streamed_without_exposing_reasoning(self):
        opener = Opener(completion("订单分析正文", reasoning="内部推理"))
        chunks = []
        with patch("wechat_ai_assistant.client_login.build_opener", return_value=opener):
            report = _complete_summary_prompt(
                JYIClient(), "jyi-default", [{"role": "user", "content": "苹果两箱"}],
                on_stream=chunks.append)
        self.assertEqual(report, "订单分析正文")
        self.assertEqual("".join(chunks), report)
        body = opener.bodies()[0]
        self.assertEqual(body["thinking"], {"type": "enabled"})
        self.assertEqual(body["reasoning_effort"], "max")
        self.assertEqual(body["temperature"], 1.0)
        self.assertEqual(body["max_tokens"], 131072)

    def test_images_use_the_same_endpoint_model_and_maximum_reasoning(self):
        from PIL import Image

        opener = Opener(completion("文字分析"), completion("图片分析"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "order.png"
            Image.new("RGB", (100, 100), "white").save(path)
            with patch("wechat_ai_assistant.client_login.build_opener", return_value=opener):
                client = JYIClient()
                for message, images in [
                    ({"kind": "text", "content": "苹果两箱"}, False),
                    ({"kind": "image", "content": "图片", "attachments": [str(path)]}, True),
                ]:
                    _complete_jyi_chunk(client, "jyi-default", "order", [message],
                                        include_images=images)
        self.assertEqual({r.full_url for r in opener.requests},
                         {"https://gateway.jyisolutions.com/v1/chat/completions"})
        for body in opener.bodies():
            self.assertEqual(body["model"], "jyi-default")
            self.assertEqual(body["thinking"], {"type": "enabled"})
            self.assertEqual(body["reasoning_effort"], "max")
        parts = opener.bodies()[1]["messages"][-1]["content"]
        image = next(part for part in parts if part["type"] == "image_url")
        self.assertTrue(image["image_url"]["url"].startswith("data:image/webp;base64,"))

    def test_continuations_keep_the_same_reasoning_settings(self):
        opener = Opener(completion("第一部分", finish="length"), completion("第二部分"))
        with patch("wechat_ai_assistant.client_login.build_opener", return_value=opener):
            report = _complete_summary_prompt(
                JYIClient(), "jyi-default", [{"role": "user", "content": "待分析消息"}])
        self.assertIn("第一部分", report)
        self.assertIn("第二部分", report)
        self.assertEqual(len(opener.requests), 2)
        for body in opener.bodies():
            self.assertEqual(body["reasoning_effort"], "max")
            self.assertEqual(body["thinking"]["type"], "enabled")

    def test_non_streaming_fallback_keeps_the_same_reasoning_settings(self):
        body = {"choices": [{"message": {"content": "完整正文"}, "finish_reason": "stop"}]}
        opener = Opener(completion(""), json.dumps(body).encode())
        with patch("wechat_ai_assistant.client_login.build_opener", return_value=opener):
            report = _complete_summary_prompt(
                JYIClient(), "jyi-default", [{"role": "user", "content": "待分析消息"}])
        self.assertEqual(report, "完整正文")
        self.assertEqual(len(opener.requests), 2)
        self.assertTrue(opener.bodies()[0]["stream"])
        self.assertNotIn("stream", opener.bodies()[1])
        self.assertEqual(opener.bodies()[1]["thinking"], {"type": "enabled"})

    def test_reasoning_only_length_failure_does_not_retry_or_split(self):
        opener = Opener(completion("", finish="length", reasoning="持续推理"))
        with patch("wechat_ai_assistant.client_login.build_opener", return_value=opener):
            with self.assertRaisesRegex(ValueError, "推理过程"):
                _complete_jyi_chunk(JYIClient(), "jyi-default", "order", [
                    {"kind": "text", "content": "苹果两箱"},
                    {"kind": "text", "content": "梨一箱"},
                ])
        self.assertEqual(len(opener.requests), 1)

    def test_other_models_do_not_receive_deepseek_specific_settings(self):
        opener = Opener(completion("分析正文"))
        with patch("wechat_ai_assistant.client_login.build_opener", return_value=opener):
            _complete_summary_prompt(JYIClient(), "another-model", [
                {"role": "user", "content": "待分析消息"}])
        self.assertNotIn("thinking", opener.bodies()[0])
        self.assertNotIn("reasoning_effort", opener.bodies()[0])

    def test_sse_error_is_reported_without_a_second_request(self):
        error = {"error": {"code": "upstream_error", "message": "Provider unavailable"}}
        opener = Opener(("data: " + json.dumps(error) + "\n\ndata: [DONE]\n\n").encode())
        with patch("wechat_ai_assistant.client_login.build_opener", return_value=opener):
            with self.assertRaises(GatewayError) as raised:
                _complete_summary_prompt(JYIClient(), "jyi-default", [
                    {"role": "user", "content": "待分析消息"}])
        self.assertEqual(raised.exception.code, "upstream_error")
        self.assertEqual(len(opener.requests), 1)

    def test_diagnostics_record_actual_request_budget_and_reasoning_usage(self):
        opener = Opener(completion("正文"))
        stats = []
        with patch("wechat_ai_assistant.client_login.build_opener", return_value=opener):
            JYIClient().chat_completions_stream(
                "jyi-default", [{"role": "user", "content": "待分析消息"}],
                max_tokens=384000, thinking={"type": "enabled"}, reasoning_effort="max",
                on_diagnostics=stats.append)
        self.assertEqual(stats[0]["http_status"], 200)
        self.assertEqual(stats[0]["request_max_tokens"], 384000)
        self.assertEqual(stats[0]["request_thinking"], "enabled")
        self.assertEqual(stats[0]["usage_reasoning_tokens"], 0)
        self.assertEqual(stats[0]["response_model"], "deepseek-v4.1-flash")
        self.assertEqual(stats[0]["gateway_request_id"], "gwr_test_diagnostics")

    def test_model_capacity_reserves_room_for_maximum_reasoning(self):
        app = types.SimpleNamespace(model_catalog={"models": [{
            "alias": "jyi-default", "context_window": 1_000_000,
            "max_output_tokens": 384000,
        }]})
        output, input_budget = _jyi_model_limits(app, "jyi-default")
        self.assertEqual(output, 131072)
        self.assertEqual(input_budget, 20000)

    def test_gateway_image_rejection_is_distinct_from_decryption_failure(self):
        from PIL import Image

        client = JYIClient()

        def reply(model, messages, **kwargs):
            if isinstance(messages[-1]["content"], list):
                raise GatewayError("unsupported_content", "Image inputs are not supported", 422)
            return {"choices": [{"message": {"content": "文字订单分析"}}]}

        client.chat_completions = reply
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "order.png"
            Image.new("RGB", (100, 100), "white").save(path)
            image = {"kind": "image", "content": "图片", "attachments": [str(path)]}
            with self.assertLogs("wechat_ai_assistant.summary", level="ERROR"):
                outcome = _run_summary_jyi(client, [
                    {"kind": "text", "content": "苹果两箱"}, image,
                ], role_id="order")
        self.assertIn("文字订单分析", outcome["summary"])
        self.assertEqual(outcome["message_count"], 1)
        self.assertIn("已解密", outcome["partial_error"])
        self.assertIn("网关", outcome["partial_error"])
        self.assertIn("已解密", image["media_status"])


if __name__ == "__main__":
    unittest.main()
