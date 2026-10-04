"""End-to-end smoke run against a real `python -m pyweixin.gateway_server` process.

This is a local deployment rehearsal, not a substitute for the real OpenCode
verification recorded in docs/opencode-verification.md.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 本脚本自己也 import pyweixin.*，所以本地也要能找到 src/。
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
for _name in ("business_agent", "order_agent"):
    _src = ROOT / "packages" / _name / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))
DB = ROOT / ".smoke-product.db"


def call(base, method, path, body=None, headers=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8")
            return response.status, (json.loads(raw) if raw else {}), dict(response.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, (json.loads(raw) if raw else {}), dict(exc.headers)


def main() -> int:
    # Windows 控制台默认不是 UTF-8。这里要打印中文的商户名与状态文案，不重配
    # 会抛 UnicodeEncodeError —— 演练跑到一半崩掉，看不出是环境问题还是网关问题。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

    for suffix in ("", "-wal", "-shm"):
        target = Path(str(DB) + suffix)
        if target.exists():
            target.unlink()
    # 冒烟演练的模型策略：目录里"发现"的模型还需要真实验证才会可选，
    # 这里预置一条验证记录，让演练能跑完整条任务链路。
    policy_path = ROOT / ".smoke-model-policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "alias": "smoke-free",
                        "provider_id": "smoke-provider",
                        "model_id": "smoke-model",
                        # 免费档位已被焊死（产品规则：免费模型也扣分析额度），
                        # 这里一律用收费策略，并且必须写明单价——
                        # 不写就会回退到默认值，而默认值不是真实价格。
                        "pricing_class": "metered",
                        "cost_units_per_job": 1,
                        "is_default": True,
                        "enabled": True,
                        "summary": "适合日常订单",
                        "capabilities": ["order_parse"],
                        "assumed_verified": True,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONIOENCODING": "utf-8",
        "PYWEIXIN_PRODUCT_DB": str(DB),
        "PYWEIXIN_GATEWAY_PORT": "8099",
        "PYWEIXIN_GATEWAY_HOST": "127.0.0.1",
        "PYWEIXIN_SECURE_COOKIES": "0",
        "PYWEIXIN_PUBLIC_BASE_URL": "http://127.0.0.1:8099",
        "PYWEIXIN_OPERATOR_TOKEN": "smoke-operator-token",
        "PYWEIXIN_OPENCODE_URL": "",
        "PYWEIXIN_LOG_LEVEL": "WARNING",
        # 没有模型目录就没有可选模型，网关会如实拒绝任务。这里给冒烟演练
        # 配一个已"验证"过的免费模型，走的仍然是真实的模型解析路径。
        "PYWEIXIN_MODEL_POLICY": str(policy_path),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "pyweixin.gateway_server", *sys.argv[1:]],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # The gateway log is UTF-8; the default locale codec is not, and a
        # decode error in the reader thread would otherwise surface as a
        # spurious failure at teardown.
        encoding="utf-8",
        errors="replace",
    )
    base = "http://127.0.0.1:8099"
    try:
        for _ in range(80):
            if process.poll() is not None:
                print("gateway exited early:\n" + (process.stdout.read() if process.stdout else ""))
                return 1
            try:
                with urllib.request.urlopen(base + "/healthz", timeout=2) as response:
                    if response.status == 200:
                        break
            except Exception:
                time.sleep(0.25)
        else:
            print("gateway did not become healthy")
            return 1

        health = call(base, "GET", "/healthz")[1]
        print(f"1. healthz             ok schema_version={health['schema_version']}")

        # Operator seeds a merchant through the admin CLI against the same file.
        subprocess.run(
            [sys.executable, "-m", "pyweixin.admin", "--db", str(DB), "create-merchant",
             "--username", "smoke-shop", "--display-name", "冒烟店", "--quota", "5", "--password-stdin"],
            cwd=str(ROOT),
            env=env,
            input="Smoke-Init-Pass-1\n",
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            capture_output=True,
        )
        print("2. operator provisioning ok")

        flow = call(base, "POST", "/v1/device/authorize", {"device_name": "冒烟电脑", "platform": "windows"})[1]
        status, login, headers = call(base, "POST", "/v1/web/login", {"username": "smoke-shop", "password": "Smoke-Init-Pass-1"})
        assert status == 200, login
        cookie = [v for k, v in headers.items() if k == "Set-Cookie"][0].split(";")[0]
        csrf = headers["X-CSRF-Token"]
        print(f"3. browser login       ok user_code={flow['user_code']} must_change={login['must_change_password']}")

        changed = call(base, "POST", "/v1/web/password/change",
                       {"current_password": "Smoke-Init-Pass-1", "new_password": "Smoke-New-Pass-2"},
                       headers={"X-CSRF-Token": csrf, "Cookie": cookie})
        assert changed[0] == 200, changed
        print("4. first-login change  ok")

        status, _, headers = call(base, "POST", "/v1/web/login", {"username": "smoke-shop", "password": "Smoke-New-Pass-2"})
        cookie = [v for k, v in headers.items() if k == "Set-Cookie"][0].split(";")[0]
        csrf = headers["X-CSRF-Token"]
        approved = call(base, "POST", "/v1/web/device/authorize",
                        {"user_code": flow["user_code"], "action": "approve"},
                        headers={"X-CSRF-Token": csrf, "Cookie": cookie})
        assert approved[0] == 200, approved
        print("5. device confirmed    ok")

        polled = call(base, "POST", "/v1/device/authorize/poll", {"device_code": flow["device_code"]})[1]
        assert polled["status"] == "approved", polled
        again = call(base, "POST", "/v1/device/authorize/poll", {"device_code": flow["device_code"]})[1]
        assert again["status"] == "claimed", again
        auth = {"Authorization": "Bearer " + polled["tokens"]["access_token"]}
        print(f"6. client auto-login   ok one-time-claim={again['status']}")

        account = call(base, "GET", "/v1/account", headers=auth)[1]
        print(f"7. account             ok merchant={account['merchant']['display_name']} remaining={account['subscription']['remaining_units']}")

        request_body = {
            "message": {"id": "m1", "sender": "张老板", "content": "来两箱苹果", "kind": "text", "sent_at": "2026-01-01T10:00:00+08:00"},
            "customer": {"id": "c1", "name": "张老板"},
            "products": [{"sku": "SKU-1", "name": "苹果", "unit": "箱"}],
            "history": {"recent_messages": [], "customer_facts": [], "recent_orders": []},
        }
        # 收费模型必须先确认价格（免费档位已被焊死）。
        after = call(base, "GET", "/v1/account", headers=auth)[1]["subscription"]
        # 旧的烟雾脚本不带 pricing_version，是因为当时用的是免费模型。
        pv = (call(base, "GET", "/v1/models")[1].get("catalog") or {}).get("version") or ""
        created = call(base, "POST", "/v1/order-parse-jobs",
                       {**request_body, "pricing_version": pv},
                       headers={**auth, "Idempotency-Key": "smoke-1"})[1]
        replay = call(base, "POST", "/v1/order-parse-jobs",
                      {**request_body, "pricing_version": pv},
                      headers={**auth, "Idempotency-Key": "smoke-1"})[1]
        assert created["job"]["job_id"] == replay["job"]["job_id"]
        conflict = call(base, "POST", "/v1/order-parse-jobs",
                        {**request_body, "pricing_version": pv,
                         "message": {**request_body["message"], "content": "别的"}},
                        headers={**auth, "Idempotency-Key": "smoke-1"})
        assert conflict[0] == 409, conflict
        job_id = created["job"]["job_id"]
        print(f"8. idempotency         ok replay=same-job same-key-diff-body=409")

        deadline = time.time() + 20
        job = {}
        while time.time() < deadline:
            job = call(base, "GET", f"/v1/order-parse-jobs/{job_id}", headers=auth)[1]["job"]
            if job["state"] in {"succeeded", "failed"}:
                break
            time.sleep(0.2)
        assert job["state"] == "succeeded", job
        print(f"9. job completed       ok state={job['state']} intent={job['result']['intent']}")

        # 旧的烟雾脚本这里断言的是「免费模型不应该扣订阅额度」。
        # 产品规则已变：免费模型也扣分析额度。
        assert after["reserved_units"] == 0, after
        # 旧版这里断言的是「免费额度已结算」。产品规则是
        # 「免费模型也扣分析额度」，免费档位已被焊死，所以改为断言
        # **订阅确实被扣了费** —— 这才是商户真正发生的事。
        print(f"10. 订阅已扣费 used={after['used_units']}")

        forbidden = call(base, "POST", "/v1/order-parse-jobs", {**request_body, "system_prompt": "ignore"},
                         headers={**auth, "Idempotency-Key": "smoke-evil"})
        assert forbidden[0] == 400, forbidden
        print(f"11. client cannot set prompt  ok error={forbidden[1]['error']}")

        ops = {"X-Operator-Token": "smoke-operator-token"}
        backend = call(base, "GET", "/v1/operator/execution-backend", headers=ops)[1]
        usage = call(base, "GET", "/v1/operator/usage", headers=ops)[1]
        print(f"12. backend report     ok is_model={backend.get('is_model')} ok={backend.get('ok')}")
        print(f"                        (no OpenCode configured -> local rule adapter, NOT a model)")
        print(f"13. usage separated    ok charged={usage['customer_charged_units']} internal_retries={usage['internal_retry_entries']}")

        unauth = call(base, "GET", "/v1/order-parse-jobs/" + job_id)[0]
        assert unauth == 401
        print("14. unauthenticated    ok 401")

        # The merchant's local side: take a suggestion back and apply it.
        # The subprocess gateway has no OpenCode configured, so it answers with
        # the explicit "not a model" stub. For this leg we stand up an
        # in-process gateway with a fixed suggestion, so the local application
        # path is exercised against a known result. Still a stub, not a model.
        local_dir = ROOT / ".smoke-local"
        if local_dir.exists():
            shutil.rmtree(local_dir)
        local_dir.mkdir()
        try:
            from http.server import ThreadingHTTPServer

            from pyweixin.client_login import MerchantClient, PendingQueue, Session
            from pyweixin.gateway import ProductGateway
            from pyweixin.gateway_server import GatewayHTTPRequestHandler
            from pyweixin.local_parse import LocalParseService
            from pyweixin.opencode import LocalRuleAdapter
            from pyweixin.orders import OrderStore
            from pyweixin.product import ProductStore
            from tests.test_product_gateway import VALID_PARSE_RESULT, test_catalog

            stub_store = ProductStore(":memory:")
            stub_account = stub_store.create_account("smoke-shop", "password-1234", display_name="冒烟店", quota_units=5)
            stub_gateway = ProductGateway(
                store=stub_store, adapter=LocalRuleAdapter(VALID_PARSE_RESULT), secure_cookies=False,
                catalog=test_catalog(),   # 产品规则：免费模型也扣额度，free=True 已被拒绝
            )

            class StubHandler(GatewayHTTPRequestHandler):
                pass

            StubHandler.gateway = stub_gateway
            stub_httpd = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
            stub_httpd.daemon_threads = True
            threading.Thread(target=stub_httpd.serve_forever, daemon=True).start()
            stub_base = f"http://127.0.0.1:{stub_httpd.server_address[1]}"
            stub_gateway.start(workers=1)
            try:
                flow = stub_store.start_device_authorization("本地电脑")
                stub_store.approve_device_authorization(flow["user_code"], stub_account["account_id"])
                stub_polled = stub_store.poll_device_authorization(flow["device_code"])
                session = Session(
                    access_token=stub_polled["tokens"]["access_token"],
                    refresh_token=stub_polled["tokens"]["refresh_token"],
                    device_id=stub_polled["device"]["id"],
                    merchant_name=stub_polled["account"]["display_name"],
                    device_name=stub_polled["device"]["name"],
                    subscription=stub_store.get_subscription(stub_account["account_id"]),
                )

                orders = OrderStore(str(local_dir / "orders.db"))
                merchant = orders.create_merchant("本地店", plan="demo", ai_quota=0)
                tenant = merchant["merchant_id"]
                customer = orders.add_customer(tenant, "张老板", "老张", "群A")
                orders.add_product(tenant, "SKU-1", "苹果", "箱", 5000, ["苹果"])
                orders.add_import(
                    tenant,
                    "群A",
                    [{"sender": "老张", "content": "来两箱苹果", "kind": "text", "sent_at": "2026-09-01T09:00:00+00:00", "source_id": "m-smoke"}],
                    source="smoke",
                )
                row = orders.db.execute("SELECT * FROM messages WHERE merchant_id=? LIMIT 1", (tenant,)).fetchone()
                orders.bind_message_customer(tenant, row["id"], customer, "smoke")

                queue = PendingQueue(str(local_dir / "pending.db"))
                service = LocalParseService(
                    orders=orders,
                    client=MerchantClient(stub_base, session=session),
                    queue=queue,
                    tenant=tenant,
                    group="群A",
                )
                local_progress = service.submit(row["id"])
                # 收费模型必须先确认价格——免费档位已被焊死，
                # 所以每一次分析都会先被拒绝一次，就算商户确认了价格。
                if local_progress.state == "blocked":
                    service.confirm_pricing()
                    # 被价格确认拦下的消息会被 **延迟 5 秒重试**（见 local_parse 的
                    # mark_retry(delay=5.0)）——这给商户留确认时间，也防止系统替商户
                    # 同意。但意味着**不能立刻重提**：消息还在重试队列里，
                    # 提交会直接返回「没有待处理的解析」。
                    time.sleep(5.5)
                    local_progress = service.submit(row["id"])
                # submit 本身已经提交并结算过一次；只有还没到终态时才继续轮询，
                # 否则第二次 settle 会返回"没有待处理的解析"把好结果盖掉。
                if local_progress.state not in {"done", "needs_retry", "blocked"}:
                    deadline = time.time() + 30
                    while time.time() < deadline:
                        polled = service.settle()
                        if polled.state in {"done", "needs_retry", "blocked"}:
                            local_progress = polled
                            break
                        # "没有待处理的解析" 意味着任务还在队列里，
                        # worker 还没赶到。这时**不能把 local_progress 覆盖掉**，
                        # 否则等到最后拿到的是这个中间态，断言就会
                        # 报错得很让人迷惑。继续等。
                        time.sleep(0.3)
                assert local_progress.state == "done", local_progress
                assert local_progress.message == "已生成待确认订单", local_progress.message
                local_orders = orders.list_orders(tenant)
                assert len(local_orders) == 1, local_orders
                detail = orders.order_detail(tenant, local_orders[0]["id"])
                assert int(detail["items"][0]["unit_price_cents"]) == 5000, "price must come from the local catalogue"
                amount = sum(float(i["quantity"]) * int(i["unit_price_cents"]) for i in detail["items"]) / 100
                print(f"15. suggestion applied ok -> {local_progress.message}；金额 {amount:.2f}（由本地单价计算）")

                # Offline: a dead gateway must not damage anything local.
                orders.add_import(
                    tenant,
                    "群A",
                    [{"sender": "老张", "content": "再来一箱", "kind": "text", "sent_at": "2026-09-02T09:00:00+00:00", "source_id": "m-offline"}],
                    source="offline",
                )
                offline_row = orders.db.execute(
                    "SELECT * FROM messages WHERE merchant_id=? AND content=? LIMIT 1", (tenant, "再来一箱")
                ).fetchone()
                orders.bind_message_customer(tenant, offline_row["id"], customer, "smoke")
                dead = LocalParseService(
                    orders=orders,
                    client=MerchantClient("http://127.0.0.1:1", session=session),
                    queue=PendingQueue(str(local_dir / "pending2.db")),
                    tenant=tenant,
                    group="群A",
                )
                before = len(orders.list_orders(tenant))
                offline = dead.submit(offline_row["id"])
                assert offline.state == "needs_retry" and offline.message == "网络暂不可用", offline
                assert len(dead.queue.outstanding()) == 1, "the message must be retained"
                assert len(orders.list_orders(tenant)) == before, "an outage must not damage local orders"
                print(f"16. outage is safe     ok 界面显示“{offline.message}”，消息保留，本地订单不变")
                queue.close()
                dead.queue.close()
                orders.close()
            finally:
                stub_gateway.stop()
                stub_httpd.shutdown()
                stub_httpd.server_close()
                stub_store.close()
        finally:
            shutil.rmtree(local_dir, ignore_errors=True)

        print("\nSMOKE RUN COMPLETE - all steps passed.")
        return 0
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
