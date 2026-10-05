"""开发调试用的 Web 界面 —— 跑的是**同一个应用对象**。

## 为什么这一层几乎不用改应用代码

``application.py`` 声明过「界面无关」：它通过 ``AppEvents`` 回调往外推送状态，
而不是让界面去轮询它的私有字段。所以 Web 界面**订阅同一套回调**即可 ——
``application.py`` 一个字节都不用动。

对照 Tk 界面：

* Tk：``MerchantWindow`` 订阅 ``AppEvents``，回调里往队列塞，主线程 drain
* Web：本模块订阅 ``AppEvents``，回调里加锁写状态，浏览器轮询 ``/api/state``

## 长任务怎么处理

两类，不能混：

* **异步的**（``read_history``）：内部起线程，通过
  ``on_history_progress`` / ``on_history_done`` 回调。Web 端只是**轮询状态**。
* **阻塞的**（``analyze``、``run_summary``）：内部 ``thread.join()``。
  **放在 HTTP 处理线程里会把整个服务卡住** —— 浏览器一直转圈，
  连 ``/api/state`` 都取不到，页面看起来像死了。所以另起线程跑。

## 安全边界

**只绑本机。** 这个界面能读商户的全部消息、订单和密钥配置，
没有任何鉴权 —— 它只该在本机开发时开。绑定地址默认 ``127.0.0.1``，
且**拒绝非回环地址的请求**，避免手滑写成 ``0.0.0.0`` 之后被局域网摸到。

## 跑法

    .\\.venv\\Scripts\\python.exe -X utf8 -m pyweixin.web_dev
    # 然后浏览器打开 http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping

from . import summary_task
from .application import AppEvents, MerchantApplication

LOGGER = logging.getLogger(__name__)

#: 默认端口。和云端控制台刻意错开，免得两个服务抢端口。
DEFAULT_PORT = 8765

#: 日志面板留多少行。多了浏览器会卡，而调试看最近的���百行足够。
LOG_LINES = 300


class WebState:
    """界面要看的全部状态。**加锁**。

    两类线程都会碰它：HTTP 处理线程（读）和应用线程（回调写入）。
    不是为了严谨，是因为不加锁时 ``/api/state`` 偶尔会读到**写了一半**的
    字典，浏览器里就报 ``undefined`` —— 这种偶发问题最难查。
    """

    def __init__(self, app: Any = None) -> None:
        self._lock = threading.RLock()
        #: 放行判据要拿真实的 orders/tenant。**不能拿占位对象**——
        #: 那样 ``has_messages_for`` 永远返回 False，「能不能摘要」就成了
        #: 一个恒为假的开关，页面上表现为「明明读到消息却不让整理」。
        self.app = app
        self.logs: deque[str] = deque(maxlen=LOG_LINES)
        self.error: str = ""
        self.step: str = ""
        self.busy: str = ""
        #: 读取进度（HistoryProgress 的可见部分）
        self.progress: dict[str, Any] | None = None
        #: 上一次读取的结果。有它才能摘要、才能判断「能不能下一步」。
        self.history: Any = None
        self.summary: dict[str, Any] | None = None
        self.analysis: dict[str, Any] | None = None
        self.conversations: list[dict[str, Any]] = []
        self.orders: list[dict[str, Any]] = []
        self.notice: str = ""
        self._objects: dict[str, Any] = {}
        self.account: dict[str, Any] = {}

    # ---- 写入（应用线程回调里） ----

    def log(self, line: str) -> None:
        with self._lock:
            self.logs.append(line)

    def fail(self, message: str) -> None:
        with self._lock:
            self.error = message
            self.busy = ""

    def clear_error(self) -> None:
        with self._lock:
            self.error = ""

    def start(self, what: str) -> None:
        with self._lock:
            self.busy = what
            self.error = ""
            self.notice = ""

    def finish(self, notice: str = "") -> None:
        with self._lock:
            self.busy = ""
            if notice:
                self.notice = notice

    def set(self, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, value)

    # ---- 读取（HTTP 处理线程） ----

    def set_conversation_objects(self, items: Any) -> None:
        """保留 Conversation 对象本身。页面传回来的只是名字。"""
        with self._lock:
            self._objects = {str(getattr(c, "name", "")): c for c in (items or [])}

    def conversation_objects(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._objects)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            history = self.history
            return {
                "account": dict(self.account),
                "step": self.step,
                "busy": self.busy,
                "error": self.error,
                "notice": self.notice,
                "progress": dict(self.progress) if self.progress else None,
                "logs": list(self.logs),
                "conversations": list(self.conversations),
                "orders": list(self.orders),
                "summary": dict(self.summary) if self.summary else None,
                "analysis": dict(self.analysis) if self.analysis else None,
                "history": _history_view(history),
                # 判据直接用共用逻辑，和 Tk 界面是同一份。
                "can_summarize": summary_task.has_messages_for(self.app, history),
            }


def _history_view(result: Any) -> dict[str, Any] | None:
    """读取结果里**界面真正要用**的那几项。

    不整个序列化：``coverage`` 里有 datetime，``import_ids`` 可能有几百个，
    整个丢给浏览器只会让页面变慢，而且商户看不懂。
    """
    if result is None:
        return None
    rows = []
    for item in getattr(result, "coverage", None) or []:
        conversation = getattr(item, "conversation", None)
        rows.append({
            "name": getattr(conversation, "name", "") if conversation is not None else "",
            "kind": getattr(conversation, "kind", "") if conversation is not None else "",
            "count": int(getattr(item, "message_count", 0) or 0),
            "truncated": bool(getattr(item, "truncated", False)),
            "unresolved": int(getattr(item, "unresolved_senders", 0) or 0),
            "skipped": bool(getattr(item, "skipped", False)),
            "error": str(getattr(item, "error", "") or ""),
        })
    return {
        "total": int(getattr(result, "total_messages", 0) or 0),
        "mode": str(getattr(result, "mode", "") or ""),
        "coverage": rows,
        "imported": len(list(getattr(result, "import_ids", None) or [])),
    }


def _conversation_view(conversation: Any) -> dict[str, Any]:
    return {
        "name": str(getattr(conversation, "name", "")),
        "kind": str(getattr(conversation, "kind", "")),
        "members": int(getattr(conversation, "member_count", 0) or 0),
    }


def _order_view(row: Any) -> dict[str, Any]:
    """OrderRow 的字段按名字取，取不到就空着 —— **不猜字段**。

    猜字段的后果是页面上出现一个恒为空的列，而没人会去怀疑代码。
    """
    def value(name: str) -> Any:
        if isinstance(row, Mapping):
            return row.get(name)
        return getattr(row, name, None)
    return {
        "id": str(value("id") or ""),
        "customer": str(value("customer_name") or value("customer") or ""),
        "status": str(value("status") or ""),
        "total": value("total_cents", value("total")),
        "summary": str(value("summary") or ""),
        "created_at": str(value("created_at") or ""),
    }


class DevHandler(BaseHTTPRequestHandler):
    app: MerchantApplication
    state: WebState
    #: 这个服务读的是哪个数据目录。**必须告诉使用者**。
    #: 目录不对时商户只会看到「没数据 / 未登录」，看不出原因。
    data_dir: str = ""
    server_version = "pyweixin-dev"

    # ---- 基础设施 ----

    def log_message(self, fmt: str, *args: Any) -> None:
        del fmt, args  # 每次请求都打会把控制台刷满，反而看不见内容

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(dict(payload), ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        # 上限 8 MB：名单 CSV 可能不小，但不该让一个请求吃光内存。
        raw = self.rfile.read(min(length, 8 * 1024 * 1024))
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _reject_remote(self) -> bool:
        """只放回环。详见模块文档：没有鉴权，绝不能被局域网摸到。"""
        host = self.client_address[0] if self.client_address else ""
        if host in ("127.0.0.1", "::1", "::ffff:127.0.0.1") or host.startswith("127."):
            return False
        self._send_json(403, {"error": "这个界面只允许本机访问。"})
        return True

    # ---- 路由 ----

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        if self._reject_remote():
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            from .web_page import PAGE

            # 数据目录由服务端注入。它不能从 /api/state 拿——
            # 那是「服务运行中」的事。而目录错了正好表现为
            # 「没数据 / 未登录」，不显示出来没人会往哪找。
            self._send_html(200, PAGE.replace("__DATADIR__", self.data_dir or "（未知）"))
        elif path == "/api/state":
            self._send_json(200, self.state.snapshot())
        elif path == "/api/conversations":
            self._refresh_conversations()
        elif path == "/api/orders":
            rows = [ _order_view(r) for r in self.app.orders_for_display() ]
            self.state.set(orders=rows)
            self._send_json(200, {"orders": rows})
        else:
            self._send_json(404, {"error": f"没有这个地址：{path}"})

    def do_POST(self) -> None:  # noqa: N802
        if self._reject_remote():
            return
        path = self.path.split("?", 1)[0]
        body = self._body()
        try:
            if path == "/api/wechat/check":
                status = self.app.check_wechat()
                self.state.log(f"检查微信：{getattr(status, 'state', '')} {getattr(status, 'detail', '')}")
                self._send_json(200, {"ok": True, "state": str(getattr(status, "state", ""))})
            elif path == "/api/wechat/start":
                self.app.start_wechat()
                self._send_json(200, {"ok": True})
            elif path == "/api/conversations/choose":
                names = [str(x) for x in (body.get("names") or [])]
                # 要的是 **Conversation 对象**，不是字符串。给字符串会在
                # wizard 里挂在 ``item.identity`` 上报 AttributeError。
                picked = self._resolve(names)
                self.app.choose_history_conversations(picked)
                self.state.log(f"\u5df2\u9009\u62e9\u4f1a\u8bdd\uff1a{'\u3001'.join(names) or '\uff08\u6e05\u7a7a\uff09'}")
                self._send_json(200, {"ok": True, "count": len(picked)})
            elif path == "/api/history/read":
                self.app.read_history(max_messages=int(body.get("max_messages") or 0),
                                      mode=str(body.get("mode") or ""))
                self.state.log("\u5df2\u5f00\u59cb\u8bfb\u53d6\u5386\u53f2\uff08\u53ef\u4ee5\u5173\u95ed\u6b64\u9875\uff0c\u4f1a\u5728\u540e\u53f0\u8dd1\uff09")
                self._send_json(200, {"ok": True})
            elif path == "/api/history/cancel":
                self.app.cancel_history()
                self._send_json(200, {"ok": True})
            elif path == "/api/analyze":
                self._send_json(200, {"ok": True, "started": self._run_blocking(
                    "\u667a\u80fd\u6574\u7406", self._do_analyze)})
            elif path == "/api/summary":
                self._send_json(200, {"ok": True, "started": self._run_blocking(
                    "\u6574\u7406\u6210\u4e00\u6bb5\u8bdd", self._do_summary)})
            elif path == "/api/login":
                # 登录是**前置条件**，不是第一步。没登录时智能整理必然失败
                # （排序依赖云端账号），所以页面开头就要能登录。
                self.app.login_requested()
                self._send_json(200, {"ok": True})
            elif path == "/api/logout":
                # 退出不删本地数据。页面上也说清楚这一点。
                self.app.detach()
                self.state.set(account={})
                self._send_json(200, {"ok": True})
            elif path == "/api/reference":
                kind = str(body.get("kind") or "")
                outcome = summary_task.import_reference(self.app, kind, str(body.get("text") or ""))
                self.state.log(f"\u5bfc\u5165\u540d\u5355\uff08{kind}\uff09\uff1a{outcome}")
                self._send_json(200, outcome)
            else:
                self._send_json(404, {"error": f"\u6ca1\u6709\u8fd9\u4e2a\u5730\u5740\uff1a{path}"})
        except Exception as exc:
            # \u5c3d\u91cf\u8ba9\u63a5\u53e3\u56de\u4e00\u4e2a\u53ef\u8bfb\u7684\u9519\u8bef\uff0c\u800c\u4e0d\u662f 500 \u548c\u4e00\u9762\u6807\u8bb0\u3002
            LOGGER.exception("request failed: %s", path)
            self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"[:300]})

    # ---- 动作 ----

    def _sync_account(self) -> None:
        """把当前登录状态写进状态。

        **字段名照 Session 的真实定义做，不猜。** 页面上出现一个恒为空的
        「账号」栏，而日志里出现「Logged in as」，两者对不上。
        """
        session = getattr(self.app, "session", None)
        self.state.set(account={
            "logged_in": session is not None,
            "name": str(getattr(session, "merchant_name", "") or "") if session else "",
            "merchant_id": str(getattr(session, "merchant_id", "") or "") if session else "",
        })

    def _resolve(self, names: list[str]) -> list[Any]:
        """按名字取回真的 Conversation 对象。

        页面只能拿到**名字**（JSON 里就这些），而 wizard 要的是对象。
        拿不到的名字不能“自己点一下等于那个”——
        选错了读取的就是别的会话，而商户看不出差异。
        """
        known = self.state.conversation_objects()
        return [known[n] for n in names if n in known]

    def _refresh_conversations(self) -> None:
        found = list(self.app.refresh_conversations())
        rows = [_conversation_view(c) for c in found]
        self.state.set(conversations=rows)
        self.state.set_conversation_objects(found)
        self._sync_account()
        self._send_json(200, {"conversations": rows})

    def _run_blocking(self, what: str, work: Callable[[], Any]) -> bool:
        """另起线程跑阻塞动作。

        **必须另起。** ``analyze`` 和 ``run_summary`` 内部都是 ``join()``\uff0c
        放在处理线程里会把服务卡死 —— 连 ``/api/state`` 都取不到，
        页面看起来像整体挂掉了，而且看不出是哪个接口的问题。
        """
        if self.state.busy:
            self._send_json(409, {"error": f"\u6b63\u5728\u505a\u5176\u4ed6\u4e8b\uff1a{self.state.busy}"})
            return False
        self.state.start(what)

        def runner() -> None:
            try:
                work()
            except Exception as exc:
                LOGGER.exception("%s failed", what)
                self.state.fail(f"{what}\u5931\u8d25\uff1a{type(exc).__name__}: {exc}"[:300])
            else:
                self.state.finish(f"{what}\u5b8c\u6210")
        threading.Thread(target=runner, name=f"dev-{what}", daemon=True).start()
        return True

    def _do_analyze(self) -> None:
        summary = self.app.analyze()
        if summary is None:
            # analyze \u5931\u8d25\u65f6\u81ea\u5df1\u5199\u8fc7 error\uff0c\u8fd9\u91cc\u4e0d\u8986\u76d6\u3002
            return
        self.state.set(analysis={
            "processed": summary.get("processed", 0),
            "with_result": summary.get("with_result", 0),
            "needs_review": summary.get("needs_review", 0),
            "already_analyzed": summary.get("already_analyzed", 0),
            "submitted_jobs": summary.get("submitted_jobs", 0),
            "charged_micro_usd": summary.get("charged_micro_usd", 0),
        })
        self.state.log(f"\u6574\u7406\u5b8c\u6210\uff1a\u5904\u7406 {summary.get('processed', 0)} \u6761\u6d88\u606f")

    def _do_summary(self) -> None:
        result = summary_task.run_summary(self.app, self.state.history)
        self.state.set(summary=result)
        if result.get("error"):
            self.state.log(f"\u6458\u8981\u5931\u8d25\uff1a{result['error']}")
        else:
            self.state.log(f"\u6458\u8981\u5b8c\u6210\uff0c{result.get('message_count', 0)} \u6761\u6d88\u606f")


def attach_events(app: MerchantApplication, state: WebState) -> None:
    """订阅 ``AppEvents`` —— 和 Tk 界面订阅的是**同一套回调**。"""
    events = app.events

    def on_step(step: str) -> None:
        state.set(step=str(step))

    def on_log(line: str) -> None:
        state.log(str(line))

    def on_error(message: str) -> None:
        state.fail(str(message))

    def on_progress(progress: Any) -> None:
        # 字段名照 HistoryProgress 的真实定义抄的（done/total/current/
        # cancelled/finished/message/messages_read/message_target）——
        # 这里没有 label，取它只会永远拿到空字符串。
        state.set(progress={
            "done": int(getattr(progress, "done", 0) or 0),
            "total": int(getattr(progress, "total", 0) or 0),
            "current": str(getattr(progress, "current", "") or ""),
            "cancelled": bool(getattr(progress, "cancelled", False)),
            "finished": bool(getattr(progress, "finished", False)),
            "message": str(getattr(progress, "message", "") or ""),
            "messages_read": int(getattr(progress, "messages_read", 0) or 0),
        })

    def on_history_done(result: Any) -> None:
        state.set(history=result, progress=None)
        state.finish("\u8bfb\u53d6\u5b8c\u6210")
        total = int(getattr(result, "total_messages", 0) or 0)
        state.log(f"\u8bfb\u53d6\u5b8c\u6210\uff0c\u672c\u6b21 {total} \u6761")

    def on_conversations(items: Any) -> None:
        state.set(conversations=[_conversation_view(c) for c in (items or [])])
        state.set_conversation_objects(items)

    events.on_step = on_step
    events.on_log = on_log
    events.on_error = on_error
    events.on_history_progress = on_progress
    events.on_history_done = on_history_done
    events.on_conversations = on_conversations


def create_server(app: MerchantApplication, *, host: str = "127.0.0.1",
                  port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """建服务器。**不启动。** 启动交给调用方，免得测试里意外占了端口。"""
    state = WebState(app)
    attach_events(app, state)
    found = list(app.refresh_conversations())
    state.set(conversations=[_conversation_view(c) for c in found])
    state.set_conversation_objects(found)

    state.set(account=({"logged_in": getattr(app, "session", None) is not None,
                        "name": str(getattr(getattr(app, "session", None), "merchant_name", "") or ""),
                        "merchant_id": str(getattr(getattr(app, "session", None), "merchant_id", "") or "")}))
    handler = type("BoundDevHandler", (DevHandler,),
                   {"app": app, "state": state, "data_dir": str(getattr(app, "data_dir", "") or "")})
    server = ThreadingHTTPServer((host, port), handler)
    server.dev_state = state  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> int:
    from . import diagnostics
    from .client_login import CredentialStore, PendingQueue
    from .merchant_app import (DEFAULT_GATEWAY, build_application, default_data_dir,
                               default_device_name, ensure_local_store)

    parser = argparse.ArgumentParser(prog="pyweixin-dev-web",
                                     description="\u5f00\u53d1\u8c03\u8bd5\u7528\u7684 Web \u754c\u9762")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--gateway", default=os.environ.get("PYWEIXIN_GATEWAY_URL", DEFAULT_GATEWAY))
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    diagnostics.install(data_dir)
    diagnostics.quiet_noisy_modules()

    orders, tenant = ensure_local_store(data_dir / "orders.db")
    credentials = CredentialStore(data_dir / "credentials.bin")
    queue = PendingQueue(data_dir / "pending.db")
    app = build_application(data_dir=data_dir, orders=orders, tenant=tenant,
                            gateway_url=args.gateway, queue=queue,
                            credentials=credentials,
                            device_name=default_device_name())
    server = create_server(app, port=args.port)
    host, port = server.server_address[:2]
    print(f"\u5f00\u53d1\u754c\u9762\u5df2\u542f\u52a8\uff1ahttp://{host}:{port}")
    print("\u53ea\u5141\u8bb8\u672c\u673a\u8bbf\u95ee\u3002\u6309 Ctrl+C \u505c\u6b62\u3002")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\u5df2\u505c\u6b62\u3002")
    finally:
        server.shutdown()
        server.server_close()
        app.close()
        orders.close()
        queue.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())