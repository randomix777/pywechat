"""商户确认价格之后，必须真的能走出去。

## 这个 bug 的后果

队列里存的是**入队那一刻**的请求快照，里面带着**空的** ``pricing_version``。
商户点完「确认这次费用」之后，``self.pricing_version`` 才拿到新版本号 ——
但那份快照还是空的。

于是：

    提交 → 被拒（缺版本）→ 商户确认 → 延迟重试 → 又被拒（还是缺版本）→ …

**商户确认了价格却永远无法完成分析**，而界面上只会一直显示
「提交前需要先确认这次分析的费用」。他会以为自己没点到，或者软件坏了。

免费档位焊死之后，这条路径成了**每一次分析**都会走的必经之路 ——
所以它从一个边角问题变成了**主链路完全不通**。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pyweixin.local_parse import (  # noqa: E402
    LocalParseService, PRICING_CONFIRM_ERRORS,
)
from pyweixin.client_login import GatewayError  # noqa: E402


class FakeClient:
    """记下每次发出去的 pricing_version。"""

    def __init__(self):
        self.sent: list[str] = []

    def submit_parse(self, request, idempotency_key):
        self.sent.append(str(request.get("pricing_version") or ""))
        if not request.get("pricing_version"):
            raise GatewayError(
                "pricing_version_required",
                "提交前需要先确认这次分析的费用",
                409,
                billing={"pricing_version": "v2", "cost_units_per_job": 1},
            )
        self.last_job_id = "job_1"
        return {"job": {"job_id": "job_1", "state": "queued"}}

    def job(self, job_id):
        return {"job": {"job_id": job_id, "state": "queued"}}

    def cancel(self, job_id):
        return {"job": {"job_id": job_id, "state": "cancelled"}}


def _service():
    import tempfile

    from pyweixin.local_parse import PendingQueue
    from pyweixin.orders import OrderStore

    tmp = Path(tempfile.mkdtemp(prefix="pyw-pricing-"))
    orders = OrderStore(str(tmp / "o.db"))
    client = FakeClient()
    queue = PendingQueue(tmp / "q.db")
    service = LocalParseService(orders=orders, client=client, queue=queue,
                                tenant="t", group="g")
    return service, client


class RetryAfterConfirmationTests(unittest.TestCase):
    def test_a_confirmed_version_is_actually_sent_on_retry(self):
        service, client = _service()
        service.pricing_version = "v2"
        # 队列里那份快照是旧的 —— 版本号是空的。
        service.queue.enqueue({"pricing_version": "", "message": {"id": "m1"}},
                              idempotency_key="msg-m1")
        service.pump()
        self.assertTrue(client.sent, "没有发出任何请求")
        self.assertEqual(client.sent[-1], "v2",
                         "重试时发出去的还是旧快照，商户会永远卡在要确认")

    def test_without_confirmation_the_old_snapshot_is_still_refused(self):
        """反过来也要成立：没确认就还是得拒。

        看的是**发出去的版本号**，不是 ``pump`` 的返回值 ——
        ``pump`` 的返回是最后一个 ``settle`` 的状态，不是本次发送的结果。
        """
        service, client = _service()
        service.queue.enqueue({"pricing_version": "", "message": {"id": "m1"}},
                              idempotency_key="msg-m1")
        service.pump()
        self.assertEqual(client.sent, [""], "没确认就能发出去，那就是替商户同意了")

    def test_the_retry_is_delayed_so_the_merchant_has_time(self):
        """确认之后要等一下才重发 —— 否则系统等于替商户同意了涨价。"""
        service, _client = _service()
        service.queue.enqueue({"pricing_version": "", "message": {"id": "m1"}},
                              idempotency_key="msg-m1")
        service.pump()
        # 被拒后应该被标记为延迟重试，而不是立刻可以再发。
        self.assertEqual(service.queue.ready(limit=5), [],
                         "确认前必须有延迟 —— 否则等于系统替商户同意了涨价")

    def test_only_pricing_version_is_refreshed(self):
        """消息原文不能在排队期间被悄悄改掉。"""
        service, client = _service()
        service.pricing_version = "v9"
        original = {"pricing_version": "", "message": {"id": "m1", "content": "原文"},
                    "products": [{"sku": "S1"}]}
        service.queue.enqueue(dict(original), idempotency_key="msg-m1")
        captured: list[dict] = []

        def spy(request, key):
            captured.append(request)
            return FakeClient.submit_parse(service.client, request, key)

        service.client.submit_parse = spy
        service.pump()
        self.assertTrue(captured)
        self.assertEqual(captured[0]["pricing_version"], "v9")
        self.assertEqual(captured[0]["message"]["content"], "原文")
        self.assertEqual(captured[0]["products"], [{"sku": "S1"}])
        # 入队的那份不能被就地改掉 —— 否则会污染重试历史。
        self.assertEqual(original["pricing_version"], "")


class ConfirmPricingTests(unittest.TestCase):
    def test_it_adopts_the_version_from_the_gateway_facts(self):
        service, client = _service()
        service.queue.enqueue({"pricing_version": "", "message": {"id": "m1"}},
                              idempotency_key="msg-m1")
        service.pump()
        self.assertTrue(service._pending_pricing, "被拒后应当记住网关给的新事实")
        facts = service.confirm_pricing()
        self.assertEqual(service.pricing_version, facts["pricing_version"])
        self.assertEqual(service.pricing_version, "v2")

    def test_it_cannot_be_called_without_a_human(self):
        """confirm_pricing 只应由商户点击触发；没有事实时它什么都不做。"""
        service, _client = _service()
        self.assertEqual(service.confirm_pricing(), {})
        self.assertEqual(service.pricing_version, "")

    def test_the_error_codes_are_the_ones_the_gateway_raises(self):
        self.assertIn("pricing_version_required", PRICING_CONFIRM_ERRORS)
        self.assertIn("pricing_changed", PRICING_CONFIRM_ERRORS)


if __name__ == "__main__":
    unittest.main()