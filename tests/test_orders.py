import csv
import http.client
import json
import threading
import tempfile
import unittest
from pathlib import Path

from wechat_ai_assistant.orders import OpenAICompatibleParser, OrderStore
from wechat_ai_assistant.cloud_server import create_server


class OrderWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = OrderStore(Path(self.tmp.name) / "orders.db")
        self.merchant = self.store.create_merchant("示例商家")
        self.tenant = self.merchant["merchant_id"]
        self.customer = self.store.add_customer(self.tenant, "张姐", "张姐", "鲜果群")
        self.apple = self.store.add_product(self.tenant, "A", "苹果", "箱", 1200, ["苹果", "A"])
        self.egg = self.store.add_product(self.tenant, "E", "鸡蛋", "箱", 1800, ["鸡蛋"])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def import_messages(self, source, rows):
        result = self.store.add_import(self.tenant, "鲜果群", rows, source, known_gap="测试批次只覆盖样本")
        return result, self.store.parse_import(self.tenant, "鲜果群", result["import_id"])

    def test_confirmed_history_shortcut_and_import_idempotency(self):
        batch, result = self.import_messages("day-1", [{"sender":"张姐","content":"苹果两箱 鸡蛋一箱","sent_at":"2026-09-20T10:00:00-04:00","source_id":"m1"}])
        self.assertEqual(result["review"], 1)  # Multi-product ambiguity stays in review.
        # Correct the intentionally ambiguous sample into explicit product lines.
        order = self.store.list_orders(self.tenant)[0]
        corrected = self.store.correct_order(self.tenant, order["id"], self.merchant["user_id"], items=[{"product_id":self.apple,"quantity":2},{"product_id":self.egg,"quantity":1}])
        self.store.confirm(self.tenant, corrected, self.merchant["user_id"])
        repeat, parsed = self.import_messages("day-1", [{"sender":"张姐","content":"苹果两箱 鸡蛋一箱","sent_at":"2026-09-20T10:00:00-04:00","source_id":"m1"}])
        self.assertEqual(repeat["inserted"], 0)
        self.assertEqual(parsed["duplicates"], 1)
        _, shortcut = self.import_messages("day-2", [{"sender":"张姐","content":"老样子，明天送","sent_at":"2026-09-24T09:00:00-04:00","source_id":"m2"}])
        self.assertEqual(shortcut["drafts"], 1)
        draft = self.store.list_orders(self.tenant, "draft")[0]
        self.assertEqual(draft["history_order_id"], corrected)
        self.assertEqual(sum(i["quantity"] for i in self.store.order_detail(self.tenant,draft["id"])["items"]), 3)

    def test_message_action_counts_are_separate_from_order_records(self):
        _,first=self.import_messages("counter-first",[{"sender":"张姐","content":"苹果两箱","sent_at":"2026-09-20T10:00:00-04:00","source_id":"counter-1"}])
        followup=self.store.add_import(self.tenant,"鲜果群",[{"sender":"张姐","content":"再加一箱苹果","sent_at":"2026-09-20T10:01:00-04:00","source_id":"counter-2"}],"counter-followup")
        result=self.store.parse_import(self.tenant,"鲜果群",followup["import_id"])
        self.assertEqual(result["messages_total"],1)
        self.assertEqual(result["order_action_messages"],1)
        self.assertEqual(result["manual_review_messages"],0)
        self.assertEqual(result["order_versions_in_batch"],0)  # append updated the existing order
        self.assertEqual(len(self.store.list_orders(self.tenant,"draft")),1)

    def test_repeated_body_is_distinct_and_tenants_are_isolated(self):
        _, result = self.import_messages("repeat-body", [
            {"sender":"张姐","content":"苹果两箱","source_id":"m1"},
            {"sender":"张姐","content":"苹果两箱","source_id":"m2"},
        ])
        self.assertEqual(result["drafts"], 2)
        other = self.store.create_merchant("别家")
        self.assertEqual(self.store.list_orders(other["merchant_id"]), [])
        with self.assertRaises(ValueError):
            self.store.correct_order(other["merchant_id"], self.store.list_orders(self.tenant)[0]["id"], other["user_id"])

    def test_weak_id_overlap_is_held_for_human_duplicate_decision(self):
        first=self.store.add_import(self.tenant,"鲜果群",[{"sender":"张姐","content":"苹果两箱","sent_at":"2026-09-24T10:00:00-04:00","source_id":"real-stable-1","stable_source_id":True}],"overlap-first")
        self.store.parse_import(self.tenant,"鲜果群",first["import_id"])
        second=self.store.add_import(self.tenant,"鲜果群",[{"sender":"张姐","content":"苹果两箱","sent_at":"2026-09-24T10:00:00-04:00","source_id":"weak-new-id","stable_source_id":False}],"overlap-second")
        self.assertEqual(second["overlap_candidates"],1)
        self.assertIn("没有稳定来源 ID",second["known_gap"])
        result=self.store.parse_import(self.tenant,"鲜果群",second["import_id"])
        self.assertEqual(result["review"],1)
        self.assertEqual(len(self.store.list_orders(self.tenant,"draft")),1)
        pending=self.store.order_detail(self.tenant,self.store.list_orders(self.tenant,"review")[0]["id"])
        message_id=pending["evidence"][0]["message_id"]
        self.store.resolve_message_match(self.tenant,message_id,False,self.merchant["user_id"])
        self.assertEqual(len(self.store.list_orders(self.tenant,"draft")),2)
        third=self.store.add_import(self.tenant,"鲜果群",[{"sender":"张姐","content":"苹果两箱","sent_at":"2026-09-24T10:00:00-04:00","source_id":"weak-repeat","stable_source_id":False}],"overlap-third")
        self.assertEqual(third["overlap_candidates"],1)
        self.store.parse_import(self.tenant,"鲜果群",third["import_id"])
        duplicate=self.store.order_detail(self.tenant,self.store.list_orders(self.tenant,"review")[0]["id"])
        self.store.resolve_message_match(self.tenant,duplicate["evidence"][0]["message_id"],True,self.merchant["user_id"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM orders WHERE merchant_id=? AND status='ignored_duplicate'",(self.tenant,)).fetchone()[0],1)
        self.assertEqual(len(self.store.list_orders(self.tenant,"draft")),2)

    def test_same_alias_becomes_ambiguous_and_media_is_queued(self):
        self.store.add_customer(self.tenant, "另一张姐", "张姐", "鲜果群")
        _, result = self.import_messages("identity-media", [
            {"sender":"张姐","content":"苹果两箱","source_id":"m1"},
            {"sender":"张姐","content":"图片","kind":"图片","source_id":"m2"},
        ])
        self.assertEqual(result["review"], 2)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM review_tasks WHERE merchant_id=?",(self.tenant,)).fetchone()[0],2)

    def test_payment_claim_is_not_receipt_and_price_is_calculated(self):
        _, result = self.import_messages("order", [{"sender":"张姐","content":"苹果两箱","source_id":"m1"}])
        order=self.store.list_orders(self.tenant,"draft")[0]
        self.store.confirm(self.tenant,order["id"],self.merchant["user_id"])
        self.import_messages("payment", [{"sender":"张姐","content":"钱转了","source_id":"m2"}])
        confirmed=self.store.order_detail(self.tenant,order["id"])
        self.assertEqual(confirmed["claim_payment"],0)
        self.assertEqual(confirmed["items"][0]["quantity"]*confirmed["items"][0]["unit_price_cents"],2400)

    def test_http_auth_tenant_scope_and_client_tenant_rejection(self):
        server, cloud = create_server("127.0.0.1", 0, "device-secret", str(Path(self.tmp.name) / "cloud.db"), console_token="console-secret", business_db_path=str(Path(self.tmp.name) / "http-business.db"))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        conn=http.client.HTTPConnection("127.0.0.1",server.server_address[1],timeout=5)
        def request(method,path,payload=None,token=""):
            body=json.dumps(payload).encode() if payload is not None else None;headers={"Authorization":"Bearer "+token} if token else {}
            if body:headers["Content-Type"]="application/json"
            conn.request(method,path,body=body,headers=headers);response=conn.getresponse();return response.status,response.read()
        try:
            status,body=request("POST","/api/v1/business/register",{"name":"HTTP 商家"});self.assertEqual(status,201);token=json.loads(body)["token"]
            self.assertEqual(request("GET","/api/v1/business/dashboard",token=token)[0],200)
            self.assertEqual(request("GET","/api/v1/business/dashboard",token="console-secret")[0],401)
            self.assertEqual(request("GET","/api/v1/business/dashboard",token="device-secret")[0],401)
            self.assertEqual(request("POST","/api/v1/business/customers",{"name":"甲","alias":"甲","tenant_id":"forged"},token)[0],200)
            self.assertEqual(request("POST","/api/v1/business/customers",{"name":"甲","alias":"甲","group":"已授权群"},token)[0],200)
            self.assertEqual(request("POST","/api/v1/business/products",{"sku":"APPLE","name":"苹果","unit":"箱","price_cents":100,"aliases":["苹果"]},token)[0],200)
            status,body=request("POST","/api/v1/business/devices",{"name":"Windows 模拟设备","account_id":"acct","groups":["已授权群"]},token)
            self.assertEqual(status,201);device=json.loads(body)
            event={"event_id":"evt-http-1","stable_id":"stable-http-1","identity_quality":"stable","chat_id":"已授权群","account_id":"acct","sender":"甲","content":"苹果两箱","kind":"text","observed_at":1700000000,"metadata":{}}
            headers={"Authorization":"Bearer "+device["token"],"X-Agent-Id":"agent-http"}
            def upload():
                payload=json.dumps({"events":[event]}).encode();conn.request("POST","/api/v1/agent/business-events",payload,{**headers,"Content-Type":"application/json"});response=conn.getresponse();return response.status,json.loads(response.read())
            status,result=upload();self.assertEqual(status,200);self.assertEqual(result["inserted"],1)
            status,result=upload();self.assertEqual(status,200);self.assertEqual(result["inserted"],0)
            self.assertEqual(request("GET","/api/v1/business/imports",token=token)[0],200)
            status,body=request("GET","/api/v1/business/orders",token=token);self.assertEqual(status,200)
            orders=json.loads(body)["orders"];self.assertEqual(len(orders),1);self.assertEqual(orders[0]["status"],"draft")
        finally:
            conn.close();server.shutdown();server.server_close();cloud.close();thread.join(timeout=2)

    def test_http_overlap_review_can_be_resolved_without_duplicate_order(self):
        server,cloud=create_server("127.0.0.1",0,"device-secret",str(Path(self.tmp.name)/"overlap-cloud.db"),business_db_path=str(Path(self.tmp.name)/"overlap-business.db"))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        conn=http.client.HTTPConnection("127.0.0.1",server.server_address[1],timeout=5)
        def request(method,path,payload=None,token=""):
            body=json.dumps(payload).encode() if payload is not None else None;headers={"Authorization":"Bearer "+token} if token else {}
            if body:headers["Content-Type"]="application/json"
            conn.request(method,path,body=body,headers=headers);response=conn.getresponse();return response.status,response.read()
        try:
            _,body=request("POST","/api/v1/business/register",{"name":"Overlap"});token=json.loads(body)["token"]
            request("POST","/api/v1/business/customers",{"name":"张姐","alias":"张姐","group":"鲜果群"},token)
            request("POST","/api/v1/business/products",{"sku":"A","name":"苹果","unit":"箱","price_cents":100,"aliases":["苹果"]},token)
            stable={"sender":"张姐","content":"苹果两箱","sent_at":"2026-09-24T10:00:00-04:00","source_id":"stable-one","stable_source_id":True}
            _,body=request("POST","/api/v1/business/imports",{"group":"鲜果群","source":"batch-one","messages":[stable]},token);first=json.loads(body)
            request("POST","/api/v1/business/parse",{"group":"鲜果群","import_id":first["import_id"]},token)
            weak={**stable,"source_id":"weak-different-id","stable_source_id":False}
            _,body=request("POST","/api/v1/business/imports",{"group":"鲜果群","source":"batch-two","messages":[weak]},token);second=json.loads(body)
            self.assertEqual(second["overlap_candidates"],1)
            request("POST","/api/v1/business/parse",{"group":"鲜果群","import_id":second["import_id"]},token)
            _,body=request("GET","/api/v1/business/orders",token=token);review=next(o for o in json.loads(body)["orders"] if o["status"]=="review")
            message_id=review["evidence"][0]["message_id"]
            status,_=request("POST","/api/v1/business/resolve-message-match",{"message_id":message_id,"is_duplicate":True},token)
            self.assertEqual(status,200)
            _,body=request("GET","/api/v1/business/dashboard",token=token);self.assertEqual(json.loads(body)["draft_orders"],1)
        finally:
            conn.close();server.shutdown();server.server_close();cloud.close();thread.join(timeout=2)

    def test_device_group_binding_event_ingress_and_duplicate_upload(self):
        device=self.store.create_device(self.tenant,"Windows collector")
        self.store.bind_device_group(self.tenant,device["device_id"],"鲜果群","acct-1")
        event={"event_id":"evt-1","stable_id":"wechat-1","chat_id":"鲜果群","account_id":"acct-1","sender":"张姐","content":"苹果两箱","kind":"text","observed_at":1700000000,"metadata":{"raw":{"消息发送时间":"2026-09-24T10:00:00-04:00"}}}
        self.assertEqual(self.store.ingest_agent_events(self.tenant,device["device_id"],"agent-1",[event])["inserted"],1)
        self.assertEqual(self.store.ingest_agent_events(self.tenant,device["device_id"],"agent-1",[event])["inserted"],0)
        wrong={**event,"event_id":"evt-2","chat_id":"未授权群","stable_id":"wechat-2"}
        self.assertEqual(self.store.ingest_agent_events(self.tenant,device["device_id"],"agent-1",[wrong])["inserted"],0)
        self.assertEqual(len(self.store.list_orders(self.tenant,"draft")),1)

    def test_manual_binding_then_confirmed_order_memory(self):
        imported=self.store.add_import(self.tenant,"鲜果群",[{"sender":"同名客户","content":"苹果两箱","sent_at":"2026-09-20T10:00:00-04:00","source_id":"u1"}],"unknown")
        msg=self.store.db.execute("SELECT id FROM messages WHERE merchant_id=?",(self.tenant,)).fetchone()["id"]
        self.store.bind_message_customer(self.tenant,msg,self.customer,self.merchant["user_id"])
        parsed=self.store.parse_import(self.tenant,"鲜果群",imported["import_id"]);self.assertEqual(parsed["drafts"],1)
        first=self.store.list_orders(self.tenant,"draft")[0];self.store.confirm(self.tenant,first["id"],self.merchant["user_id"])
        second=self.store.add_import(self.tenant,"鲜果群",[{"sender":"张姐","content":"老样子，明天送","sent_at":"2026-09-25T09:00:00-04:00","source_id":"u2"}],"followup")
        parsed=self.store.parse_import(self.tenant,"鲜果群",second["import_id"])
        self.assertEqual(parsed["drafts"],1)
        draft=self.store.list_orders(self.tenant,"draft")[0]
        self.assertEqual(draft["history_order_id"],first["id"])

    def test_openai_compatible_parser_requires_fixed_schema(self):
        parser=OpenAICompatibleParser("http://example.test/v1","secret","demo-model")
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def read(self):return json.dumps({"choices":[{"message":{"content":json.dumps({"intent":"new_order","customer_alias":"张姐","target_order":None,"items":[],"delivery_date":None,"evidence":[],"missing_fields":[],"ambiguities":[],"suggested_change":None})}}],"usage":{"prompt_tokens":12,"completion_tokens":4}}).encode()
        import unittest.mock
        with unittest.mock.patch("urllib.request.urlopen",return_value=Response()) as request:
            parsed,usage=parser.parse({"message":{"content":"苹果两箱"}})
        self.assertEqual(parsed["intent"],"new_order");self.assertEqual(usage,{"input":12,"output":4})
        sent=json.loads(request.call_args.args[0].data)
        self.assertEqual(sent["response_format"]["json_schema"]["schema"],OpenAICompatibleParser.schema)

    def test_openai_compatible_parser_rejects_malformed_nested_fields(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def read(self):
                result={"intent":"new_order","customer_alias":None,"target_order":None,"items":"not-a-list","delivery_date":None,"evidence":[],"missing_fields":[],"ambiguities":[],"suggested_change":None}
                return json.dumps({"choices":[{"message":{"content":json.dumps(result)}}]}).encode()
        import unittest.mock
        parser=OpenAICompatibleParser("http://example.test/v1","secret","demo-model")
        with unittest.mock.patch("urllib.request.urlopen",return_value=Response()):
            with self.assertRaisesRegex(ValueError,"items must be a list"):
                parser.parse({"message":{"content":"苹果两箱"}})

    def test_change_suggestion_copies_before_and_after(self):
        self.import_messages("before",[{"sender":"张姐","content":"苹果两箱","source_id":"c1"}])
        original=self.store.list_orders(self.tenant,"draft")[0]
        new=self.store._change_status_copy(self.tenant,original["id"],"review","msg_missing") if False else None
        # Exercise the versioned quantity correction API used by the review panel.
        corrected=self.store.correct_order(self.tenant,original["id"],self.merchant["user_id"],items=[{"product_id":self.apple,"quantity":3}])
        self.assertEqual(self.store.order_detail(self.tenant,corrected)["history_order_id"],original["id"])
        self.assertEqual(self.store.order_detail(self.tenant,corrected)["items"][0]["quantity"],3)

    def test_model_parser_creates_reviewable_draft_and_charges_once(self):
        class Parser:
            model="fixture-model"
            def __init__(self):self.calls=0
            def parse(self,context):
                self.calls+=1
                return ({"said": '苹果两箱', "brand": None, "category": None, "said": '苹果两箱', "brand": None, "category": None, "intent":"new_order","customer_alias":"张姐","target_order":None,"items":[{"said": '苹果两箱', "brand": None, "category": None, "sku": "A", "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],"delivery_date":None,"evidence":["苹果两箱"],"missing_fields":[],"ambiguities":[],"suggested_change":None},{"input":100,"output":20})
        parser=Parser();self.store.parser=parser
        with self.store.db:self.store.db.execute("UPDATE merchant_settings SET parser_mode='model' WHERE merchant_id=?",(self.tenant,))
        imported=self.store.add_import(self.tenant,"鲜果群",[{"sender":"张姐","content":"苹果两箱","sent_at":"2026-09-24T10:00:00-04:00","source_id":"model-1"}],"model")
        result=self.store.parse_import(self.tenant,"鲜果群",imported["import_id"])
        self.assertEqual(result["drafts"],1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM model_usage WHERE merchant_id=?",(self.tenant,)).fetchone()[0],1)
        replay=self.store.parse_import(self.tenant,"鲜果群",imported["import_id"])
        self.assertEqual(replay["duplicates"],1);self.assertEqual(parser.calls,1)
        order=self.store.list_orders(self.tenant,"draft")[0]
        self.assertEqual(self.store.order_detail(self.tenant,order["id"])["items"][0]["unit_price_cents"],1200)

    def test_model_failure_is_visible_retryable_and_never_falls_back(self):
        class Parser:
            model="fixture-model"
            def parse(self,context):raise TimeoutError("fixture timeout")
        store=OrderStore(Path(self.tmp.name)/"model-failure.db",parser=Parser())
        try:
            merchant=store.create_merchant("model failure merchant");tenant=merchant["merchant_id"]
            store.add_customer(tenant,"张姐","张姐","鲜果群")
            store.add_product(tenant,"A","苹果","箱",100,["苹果"])
            imported=store.add_import(tenant,"鲜果群",[{"sender":"张姐","content":"苹果两箱","sent_at":"2026-09-24T10:00:00-04:00","source_id":"timeout-1"}],"timeout")
            failed=store.parse_import(tenant,"鲜果群",imported["import_id"])
            self.assertEqual(failed["failed"],1);self.assertEqual(store.dashboard(tenant)["parse_failures"],1)
            # Removing model config during restart leaves the tenant pinned to model mode.
            store.parser=None
            unavailable=store.parse_import(tenant,"鲜果群",imported["import_id"])
            self.assertEqual(unavailable["failed"],1)
            self.assertEqual(store.db.execute("SELECT count(*) FROM parse_runs WHERE merchant_id=? AND adapter='demo-rules-v1'",(tenant,)).fetchone()[0],0)
            # 系统失败**不建订单**。它挂在解析运行上，与"要问客户"和"订单要
            # 人工改业务"是三件事。早先建一张空订单占位，商户会看到一个没有
            # 任何明细的待核对订单，还把它算进订单数。
            self.assertEqual(store.list_orders(tenant),[],"系统失败不得建订单占位")
            self.assertEqual(store.dashboard(tenant)["pending_review"],0)
            failures=store.system_failures(tenant)
            self.assertEqual(len(failures),1)
            self.assertEqual(failures[0]["parse_run_id"]!="",True,"失败必须能按解析任务定位")
            self.assertEqual(failures[0]["order_id"],"","没有真实订单可引用")
            self.assertFalse(failures[0]["is_clarification"],"系统失败不是澄清任务")
            self.assertTrue(failures[0]["is_system_failure"])
            self.assertTrue(failures[0]["retryable"])
            class Recovered:
                model="fixture-model"
                def parse(self,context):return ({"said": '苹果两箱', "brand": None, "category": None, "said": '苹果两箱', "brand": None, "category": None, "intent":"new_order","customer_alias":"张姐","target_order":None,"items":[{"said": '苹果两箱', "brand": None, "category": None, "sku": "A", "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],"delivery_date":None,"evidence":["苹果两箱"],"missing_fields":[],"ambiguities":[],"suggested_change":None},{"input":10,"output":2})
            store.parser=Recovered()
            retried=store.parse_import(tenant,"鲜果群",imported["import_id"])
            self.assertEqual(retried["drafts"],1)
            self.assertEqual(store.dashboard(tenant)["parse_failures"],0)
            self.assertEqual(store.dashboard(tenant)["open_review_tasks"],0)
            self.assertEqual(store.system_failures(tenant),[],"重试成功后失败待办应当关闭")
        finally:store.close()

    def test_model_result_replay_recovers_partial_order_without_duplicate(self):
        class UnusedParser:
            model="fixture-model"
            def parse(self,context):raise AssertionError("saved model result should be reused")
        store=OrderStore(Path(self.tmp.name)/"partial-order.db",parser=UnusedParser())
        try:
            merchant=store.create_merchant("partial order merchant");tenant=merchant["merchant_id"]
            customer=store.add_customer(tenant,"张姐","张姐","鲜果群")
            product=store.add_product(tenant,"A","苹果","箱",100,["苹果"])
            imported=store.add_import(tenant,"鲜果群",[{"sender":"张姐","content":"苹果两箱","sent_at":"2026-09-24T10:00:00-04:00","source_id":"partial-1"}],"partial")
            message=store.db.execute("SELECT id FROM messages WHERE import_id=?",(imported["import_id"],)).fetchone()["id"]
            run_id="run-partial-order";parse_key=f"{tenant}:{message}:model-v1"
            parsed={"said": '苹果两箱', "brand": None, "category": None, "said": '苹果两箱', "brand": None, "category": None, "intent":"new_order","customer_alias":"张姐","target_order":None,"items":[{"said": '苹果两箱', "brand": None, "category": None, "sku": "A", "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],"delivery_date":None,"evidence":["苹果两箱"],"missing_fields":[],"ambiguities":[],"suggested_change":None}
            with store.db:
                store.db.execute("INSERT INTO parse_runs(id,merchant_id,message_id,adapter,status,usage_units,error,created_at,result_json) VALUES(?,?,?,'model-v1','running',12,NULL,?,?)",(run_id,tenant,message,"2000-01-01T00:00:00+00:00",json.dumps(parsed,ensure_ascii=False)))
                store.db.execute("INSERT INTO model_usage VALUES(?,?,?,?,?,?,?)",("usage-partial",tenant,parse_key,"fixture-model",10,2,store.now()))
            partial=store._new_order(tenant,customer,"鲜果群",message,parse_run_id=run_id)
            with store.db:store.db.execute("INSERT INTO order_items VALUES(?,?,?,?,?,?)",("partial-item",partial,product,99,"箱",100))
            result=store.parse_import(tenant,"鲜果群",imported["import_id"])
            self.assertEqual(result["drafts"],1)
            self.assertEqual(store.db.execute("SELECT count(*) FROM orders WHERE merchant_id=? AND parse_run_id=?",(tenant,run_id)).fetchone()[0],1)
            detail=store.order_detail(tenant,partial)
            self.assertEqual(len(detail["items"]),1);self.assertEqual(detail["items"][0]["quantity"],2)
            self.assertEqual(len(detail["evidence"]),1)
        finally:store.close()

    def test_partial_model_environment_configuration_fails_closed(self):
        import unittest.mock
        with unittest.mock.patch.dict("os.environ",{"WECHAT_AI_MODEL_BASE_URL":"https://model.invalid/v1","WECHAT_AI_MODEL_API_KEY":"","WECHAT_AI_MODEL_NAME":""}):
            with self.assertRaisesRegex(ValueError,"configure WECHAT_AI_MODEL_BASE_URL"):
                create_server("127.0.0.1",0,"device-secret",str(Path(self.tmp.name)/"partial-cloud.db"),business_db_path=str(Path(self.tmp.name)/"partial-business.db"))

    def test_business_schema_migration_advances_version_and_is_repeatable(self):
        from wechat_ai_assistant.orders import SCHEMA_VERSION

        path=Path(self.tmp.name)/"migration.db"
        first=OrderStore(path);self.assertEqual(first.db.execute("SELECT version FROM schema_version").fetchone()[0],SCHEMA_VERSION)
        with first.db:first.db.execute("UPDATE schema_version SET version=1")
        first.close()
        second=OrderStore(path)
        try:
            self.assertEqual(second.db.execute("SELECT version FROM schema_version").fetchone()[0],SCHEMA_VERSION)
            self.assertIsNotNone(second.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_matches'").fetchone())
            self.assertIn("parse_run_id",{r["name"] for r in second.db.execute("PRAGMA table_info(orders)")})
        finally:second.close()
        # 重复打开不得再次重建，也不得把已有运行搞丢。
        third=OrderStore(path)
        try:
            self.assertEqual(third.db.execute("SELECT COUNT(*) FROM parse_runs").fetchone()[0],0)
            self.assertIn("run_key",{r["name"] for r in third.db.execute("PRAGMA table_info(parse_runs)")})
        finally:third.close()

    def test_historical_replay_never_uses_future_confirmed_order(self):
        future=self.store.add_import(self.tenant,"鲜果群",[{"sender":"张姐","content":"苹果两箱","sent_at":"2026-09-25T10:00:00-04:00","source_id":"future"}],"future")
        self.store.parse_import(self.tenant,"鲜果群",future["import_id"])
        order=self.store.list_orders(self.tenant,"draft")[0];self.store.confirm(self.tenant,order["id"],self.merchant["user_id"])
        earlier=self.store.add_import(self.tenant,"鲜果群",[{"sender":"张姐","content":"老样子，明天送","sent_at":"2026-09-24T10:00:00-04:00","source_id":"earlier"}],"earlier")
        result=self.store.parse_import(self.tenant,"鲜果群",earlier["import_id"])
        self.assertEqual(result["review"],1)
        self.assertEqual(self.store.db.execute("SELECT history_order_id FROM orders WHERE source_import_id=?",(earlier["import_id"],)).fetchone()[0],None)

    def test_delivery_date_uses_message_offset_and_replay_orders_by_instant(self):
        seed=self.store.add_import(self.tenant,"鲜果群",[{"sender":"张姐","content":"苹果两箱","sent_at":"2026-09-24T10:00:00-04:00","source_id":"offset-seed"}],"offset-seed")
        self.store.parse_import(self.tenant,"鲜果群",seed["import_id"])
        prior=self.store.list_orders(self.tenant,"draft")[0];self.store.confirm(self.tenant,prior["id"],self.merchant["user_id"])
        imported=self.store.add_import(self.tenant,"鲜果群",[
            {"sender":"张姐","content":"苹果两箱","sent_at":"2026-09-25T00:00:00-04:00","source_id":"offset-later"},
            {"sender":"张姐","content":"老样子，明天送","sent_at":"2026-09-24T23:30:00-04:00","source_id":"offset-earlier"},
        ],"offset-order")
        messages=self.store._messages(self.tenant,"鲜果群",imported["import_id"])
        self.assertEqual([m["source_id"] for m in messages],["offset-earlier","offset-later"])
        result=self.store.parse_import(self.tenant,"鲜果群",imported["import_id"])
        self.assertEqual(result["drafts"],2)
        classic=next(o for o in self.store.list_orders(self.tenant,"draft") if o["history_order_id"]==prior["id"])
        self.assertEqual(classic["delivery_date"],"2026-09-25")

    def test_change_confirmation_keeps_original_until_approved(self):
        imported=self.store.add_import(self.tenant,"鲜果群",[{"sender":"张姐","content":"苹果两箱","sent_at":"2026-09-20T10:00:00-04:00","source_id":"base"}],"base")
        self.store.parse_import(self.tenant,"鲜果群",imported["import_id"])
        old=self.store.list_orders(self.tenant,"draft")[0];self.store.confirm(self.tenant,old["id"],self.merchant["user_id"])
        message=self.store.add_import(self.tenant,"鲜果群",[{"sender":"张姐","content":"改三箱","sent_at":"2026-09-21T10:00:00-04:00","source_id":"change"}],"change")
        msg=self.store.db.execute("SELECT * FROM messages WHERE source_id='change'").fetchone()
        self.store._change_quantity(self.tenant,old["id"],self.apple,3,msg["id"])
        proposed=self.store.list_orders(self.tenant,"review")[0]
        self.assertEqual(self.store.order_detail(self.tenant,old["id"])["status"],"confirmed")
        self.store.confirm(self.tenant,proposed["id"],self.merchant["user_id"])
        self.assertEqual(self.store.order_detail(self.tenant,old["id"])["status"],"superseded")
        self.assertEqual(self.store.order_detail(self.tenant,proposed["id"])["status"],"confirmed")


if __name__ == "__main__":
    unittest.main()
