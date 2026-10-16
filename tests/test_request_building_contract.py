"""请求构造的契约：参数名必须和 ``business_agent`` 的真实定义一致。

## 这一条为什么存在

``_build_agent_request`` 曾给 ``Budget`` 传 ``deadline_seconds``，而真实的
字段叫 ``timeout_seconds``。

结果是 **每一次调用都在构造请求时抛 TypeError** —— 适配器从来没跑通过。
它之所以长期没被发现，是因为没有任何测试真正走到这一行：单测都注入了假后端，
而真实调用需要密钥，没有密钥就没人试。

**TypeError 不会出现在"没接上游"的路径上**，所以它一直静静躺着。

这里用真实契约（不是 mock）构造一次请求，确保参数名对得上。
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for extra in ("src", "packages/business_agent/src", "packages/order_agent/src"):
    sys.path.insert(0, str(ROOT / extra))

from wechat_ai_assistant.deepseek_adapter import _build_agent_request  # noqa: E402


PAYLOAD = {
    "messages": [{"role": "user", "content": "张姐：明天下午要 3 箱花工，约 300 元"}],
    "merchant_id": "m1",
}


class RequestBuildingTests(unittest.TestCase):
    def test_it_builds_a_real_request_with_the_real_contract(self):
        """用真实 dataclass 构造 —— 参数名错了这里就会炸。"""
        request = _build_agent_request(PAYLOAD, "job-1", time.time() + 60)
        self.assertEqual(request.request_id, "job-1")
        self.assertEqual(request.task_type, "order_parse")

    def test_the_budget_gets_a_usable_timeout(self):
        """剩余时间必须真的落到 ``timeout_seconds`` 上。

        ``Budget`` 会校验它大于零，所以这里连"传成了 0"都过不了 ——
        也就是说，写错字段名连构造都构造不出来。
        """
        request = _build_agent_request(PAYLOAD, "job-2", time.time() + 60)
        self.assertGreater(request.budget.timeout_seconds, 0)

    def test_a_deadline_in_the_past_still_yields_a_positive_timeout(self):
        """超时了也要给一个下限，不能是 0 或负数 —— 否则 Budget 直接拒绝。"""
        request = _build_agent_request(PAYLOAD, "job-3", time.time() - 3600)
        self.assertGreater(request.budget.timeout_seconds, 0)

    def test_no_deadline_falls_back_to_a_sane_default(self):
        request = _build_agent_request(PAYLOAD, "job-4", 0.0)
        self.assertGreater(request.budget.timeout_seconds, 0)

    def test_it_can_be_serialised(self):
        """能 to_dict 才能发出去；构造出来但发不出去等于没跑通。"""
        payload = _build_agent_request(PAYLOAD, "job-5", time.time() + 60).to_dict()
        self.assertIn("budget", payload)
        self.assertIn("timeout_seconds", payload["budget"])


if __name__ == "__main__":
    unittest.main()