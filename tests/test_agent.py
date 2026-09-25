import http.client
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from pyweixin.agent import AgentCommand, AgentConfig, AgentStore, LocalAgent
from pyweixin.Errors import NoSuchFriendError
from pyweixin.cloud import CloudClient, CloudError
from pyweixin.cloud_server import _load_agent_tokens, create_server
from pyweixin.reliability import MessageEvent
from pyweixin.utils import parse_group_chat_history, parse_group_messages


class AgentTests(unittest.TestCase):
    def test_group_history_alignment_does_not_shift_rows(self):
        with_name = [
            ("【pyweixin 端到端测试】这是一条自动化测试消息，请忽略。 2026-9-25 11:47", "mmui::ChatTextItemView", []),
            ("他们回来没有阿？ 2026-9-24 15:32", "mmui::ChatTextItemView", []),
        ]
        without_name = [
            ("他们回来没有阿？ 2026-9-24 15:32", "mmui::ChatTextItemView", []),
            ("Channels菊姐懂婴语 2026-9-24 15:31", "mmui::ChatBubbleItemView", []),
        ]
        contents, senders, timestamps, message_types = parse_group_chat_history(with_name, without_name, [])
        self.assertEqual(contents[0], "【pyweixin 端到端测试】这是一条自动化测试消息，请忽略。")
        self.assertEqual(contents[1], "他们回来没有阿？")
        self.assertIsNone(senders[0])
        self.assertEqual(timestamps[0], "2026-9-25 11:47")
        self.assertEqual(message_types[0], "文本")

    def test_group_messages_extracts_sender_when_pairs_are_aligned(self):
        with_name = [("Alice hello 2026-9-24 15:32", "mmui::ChatTextItemView", [])]
        without_name = [("hello 2026-9-24 15:32", "mmui::ChatTextItemView", [])]
        contents, senders, message_types = parse_group_messages(with_name, without_name, [])
        self.assertEqual(contents, ["hello"])
        self.assertEqual(senders, ["Alice"])
        self.assertEqual(message_types, ["文本"])

    def test_local_agent_serializes_dialog_operations(self):
        active = 0
        maximum = 0
        guard = threading.Lock()

        def opener(**kwargs):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with guard:
                active -= 1
            return object()

        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.db")
            config = AgentConfig(group="chat", db_path=str(Path(directory) / "agent.db"))
            first = LocalAgent(config, store=store, dialog_opener=opener)
            second = LocalAgent(config, store=store, dialog_opener=opener)
            threads = [threading.Thread(target=agent._open_dialog) for agent in (first, second)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
            self.assertEqual(maximum, 1)
            first.close()
            second.close()
            store.close()

    def test_local_agent_retries_dialog_lookup(self):
        calls = []

        class Dialog:
            def close(self):
                pass

        def opener(**kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise NoSuchFriendError
            return Dialog()

        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.db")
            config = AgentConfig(group="chat", db_path=str(Path(directory) / "agent.db"))
            agent = LocalAgent(config, store=store, dialog_opener=opener)
            self.assertIsInstance(agent._open_dialog(), Dialog)
            self.assertEqual(len(calls), 3)
            agent.close()
            store.close()

    def test_local_agent_survives_heartbeat_failure(self):
        class Cloud:
            def send_events(self, events):
                return None

            def fetch_commands(self):
                return []

            def acknowledge(self, command_id, result):
                return None

            def heartbeat(self, health):
                raise OSError("heartbeat offline")

        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.db")
            config = AgentConfig(group="chat", db_path=str(Path(directory) / "agent.db"), history_limit=0, listen_duration="0s")
            agent = LocalAgent(config, store=store, cloud=Cloud())
            summary = agent.run_once()
            self.assertEqual(summary["health"]["state"], "degraded")
            agent.close()
            store.close()

    def test_local_agent_ignores_malformed_command_without_id(self):
        class Cloud:
            def __init__(self):
                self.acks = []

            def fetch_commands(self):
                return [{"action": "noop", "target": "chat"}]

            def acknowledge(self, command_id, result):
                self.acks.append((command_id, result))

            def send_events(self, events):
                return None

            def heartbeat(self, health):
                return None

        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.db")
            cloud = Cloud()
            config = AgentConfig(group="chat", db_path=str(Path(directory) / "agent.db"), history_limit=0, listen_duration="0s")
            agent = LocalAgent(config, store=store, cloud=cloud)
            summary = agent.run_once()
            self.assertEqual(summary["health"]["state"], "idle")
            self.assertEqual(cloud.acks, [])
            agent.close()
            store.close()

    def test_history_bootstrap_keeps_identical_rows_and_runs_once(self):
        calls = []
        item = {"消息发送人": "a", "消息内容": "收到", "消息类型": "文本", "消息发送时间": "12:30"}
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.db")
            config = AgentConfig(group="chat", db_path=str(Path(directory) / "agent.db"), history_limit=2, listen_duration="0s")
            agent = LocalAgent(
                config,
                store=store,
                history_reader=lambda **kwargs: calls.append(kwargs) or [item, item],
            )
            first = agent.run_once()
            second = agent.run_once()
            self.assertEqual(first["history_events"], 2)
            self.assertEqual(second["history_events"], 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(store.event_count(), 2)
            agent.close()
            store.close()

    def test_local_agent_does_not_send_after_stop_requested(self):
        sent = []
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.db")
            store.enqueue(AgentCommand("c-stop", "send_text", "chat", {"text": "no"}))
            config = AgentConfig(group="chat", db_path=str(Path(directory) / "agent.db"), history_limit=0, listen_duration="0s")
            agent = LocalAgent(config, store=store, sender=lambda friend, messages: sent.append((friend, messages)))
            stop_event = threading.Event()
            stop_event.set()
            summary = agent.run_once(stop_event=stop_event)
            self.assertEqual(summary["processed_commands"], 0)
            self.assertEqual(sent, [])
            agent.close()
            store.close()

    def test_store_deduplicates_events_and_persists_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.db")
            event = MessageEvent("chat", "hello", stable_id="m-1")
            self.assertTrue(store.record_event(event))
            self.assertFalse(store.record_event(event))
            command = AgentCommand("c-1", "send_text", "chat", {"text": "ok"})
            self.assertTrue(store.enqueue(command))
            self.assertFalse(store.enqueue(command))
            claimed = store.claim_outbox_leases()
            self.assertEqual([item.command.command_id for item in claimed], ["c-1"])
            store.complete("c-1", claimed[0].token)
            self.assertEqual(store.pending_count(), 0)
            self.assertEqual(store.event_count(), 1)
            store.close()

    def test_store_does_not_complete_with_stale_lease_token(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.db")
            store.enqueue(AgentCommand("c-stale", "noop", "chat"))
            lease = store.claim_outbox_leases()[0]
            self.assertFalse(store.complete("c-stale"))
            self.assertFalse(store.complete("c-stale", "wrong-token"))
            self.assertTrue(store.complete("c-stale", lease.token))
            self.assertEqual(store.get_command("c-stale")["status"], "succeeded")
            store.close()

    def test_store_recovers_null_command_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.db")
            store.enqueue(AgentCommand("c-null", "noop", "chat"))
            store.claim_outbox_leases(lease_seconds=300)
            with store._lock, store._connection:
                store._connection.execute("UPDATE outbox SET lease_until = NULL WHERE command_id = 'c-null'")
            self.assertEqual(store.recover_outbox(), 1)
            self.assertEqual(store.get_command("c-null")["status"], "queued")
            store.close()

    def test_store_recovers_expired_command_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [100.0]
            store = AgentStore(Path(directory) / "agent.db", clock=lambda: now[0])
            store.enqueue(AgentCommand("c-lease", "noop", "chat"))
            self.assertEqual(len(store.claim_outbox(lease_seconds=10)), 1)
            now[0] = 111.0
            self.assertEqual(store.recover_outbox(), 1)
            self.assertEqual(len(store.claim_outbox(lease_seconds=10)), 1)
            store.close()

    def test_local_agent_bootstraps_and_processes_outbox(self):
        sent = []
        dialog_closed = []

        class Dialog:
            def close(self):
                dialog_closed.append(True)

        def history_reader(**kwargs):
            return [{"消息发送人": "alice", "消息内容": "history", "消息类型": "文本"}]

        def listener(**kwargs):
            event = MessageEvent("chat", "live", stable_id="live-1")
            return {"消息事件": [event.to_dict()]}

        def sender(friend, messages):
            sent.append((friend, messages))

        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.db")
            config = AgentConfig(
                group="chat",
                db_path=str(Path(directory) / "agent.db"),
                history_limit=1,
                listen_duration="1s",
            )
            agent = LocalAgent(
                config,
                store=store,
                history_reader=history_reader,
                dialog_opener=lambda **kwargs: Dialog(),
                listener=listener,
                sender=sender,
            )
            agent.enqueue(AgentCommand("c-send", "send_text", "chat", {"text": "reply"}))
            summary = agent.run_once()
            self.assertEqual(summary["history_events"], 1)
            self.assertEqual(summary["live_events"], 1)
            self.assertEqual(summary["processed_commands"], 1)
            self.assertEqual(sent, [("chat", ["reply"])])
            self.assertEqual(dialog_closed, [True])
            self.assertEqual(store.event_count(), 2)
            agent.close()
            store.close()

    def test_local_agent_persists_live_sequence(self):
        class Dialog:
            def close(self):
                pass

        calls = []

        def listener(**kwargs):
            calls.append(kwargs["sequence_start"])
            if len(calls) == 1:
                event = MessageEvent("chat", "live", sequence=kwargs["sequence_start"])
                return {"消息事件": [event.to_dict()]}
            return {"消息事件": []}

        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.db")
            config = AgentConfig(group="chat", db_path=str(Path(directory) / "agent.db"), history_limit=0, listen_duration="1s")
            agent = LocalAgent(config, store=store, dialog_opener=lambda **kwargs: Dialog(), listener=listener)
            agent.run_once()
            agent.run_once()
            self.assertEqual(calls, [0, 1])
            self.assertEqual(store.get_meta("message_sequence"), "1")
            self.assertEqual(store.recent_events(1)[0]["identity_quality"], "sequence")
            agent.close()
            store.close()

    def test_local_agent_deduplicates_live_runtime_snapshots(self):
        class Dialog:
            def close(self):
                pass

        def listener(**kwargs):
            event = MessageEvent("chat", "live", runtime_id=(1, 2))
            return {"消息事件": [event.to_dict()]}

        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.db")
            config = AgentConfig(group="chat", db_path=str(Path(directory) / "agent.db"), history_limit=0, listen_duration="1s")
            agent = LocalAgent(config, store=store, dialog_opener=lambda **kwargs: Dialog(), listener=listener)
            first = agent.run_once()
            second = agent.run_once()
            self.assertEqual(first["live_events"], 1)
            self.assertEqual(second["live_events"], 0)
            self.assertEqual(store.event_count(), 1)
            stored = store.recent_events(1)[0]
            self.assertIsNone(stored["stable_id"])
            self.assertEqual(stored["identity_quality"], "runtime")
            agent.close()
            store.close()

    def test_local_agent_retries_event_upload(self):
        now = [100.0]

        class Cloud:
            def __init__(self):
                self.calls = 0

            def send_events(self, events):
                self.calls += 1
                if self.calls == 1:
                    raise OSError("offline")

            def fetch_commands(self):
                return []

            def acknowledge(self, command_id, result):
                return None

            def heartbeat(self, health):
                return None

        class Dialog:
            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.db", clock=lambda: now[0])
            cloud = Cloud()
            config = AgentConfig(group="chat", db_path=str(Path(directory) / "agent.db"), history_limit=0, listen_duration="1s")
            event = MessageEvent("chat", "live", runtime_id=(1,))
            agent = LocalAgent(
                config,
                store=store,
                dialog_opener=lambda **kwargs: Dialog(),
                listener=lambda **kwargs: {"消息事件": [event.to_dict()]},
                cloud=cloud,
            )
            agent.run_once()
            self.assertEqual(store.pending_upload_count(), 1)
            now[0] = 106.0
            agent.run_once()
            self.assertEqual(store.pending_upload_count(), 0)
            self.assertEqual(cloud.calls, 2)
            agent.close()
            store.close()

    def test_event_upload_claim_is_exclusive(self):
        now = [100.0]
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.db", clock=lambda: now[0])
            store.record_event(MessageEvent("chat", "hello", stable_id="m-upload"))
            first = store.claim_event_uploads(lease_seconds=10)
            self.assertEqual(len(first), 1)
            self.assertEqual(store.claim_event_uploads(lease_seconds=10), [])
            self.assertFalse(store.mark_event_uploaded(first[0][0], "wrong-token"))
            self.assertTrue(store.mark_event_upload_failed(first[0][0], "offline", retry_delay=1, lease_token=first[0][2]))
            now[0] = 102.0
            second = store.claim_event_uploads(lease_seconds=10)
            self.assertEqual(len(second), 1)
            self.assertTrue(store.mark_event_uploaded(second[0][0], lease_token=second[0][2]))
            store.close()

    def test_event_upload_stops_after_max_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.db", clock=lambda: 100.0)
            store.record_event(MessageEvent("chat", "poison", stable_id="m-poison"))
            for _ in range(10):
                claimed = store.claim_event_uploads(lease_seconds=10)
                self.assertEqual(len(claimed), 1)
                self.assertTrue(store.mark_event_upload_failed(claimed[0][0], "permanent", retry_delay=0, lease_token=claimed[0][2]))
            self.assertEqual(store.pending_upload_count(), 0)
            self.assertEqual(store.failed_upload_count(), 1)
            self.assertEqual(store.claim_event_uploads(), [])
            store.close()

    def test_local_agent_syncs_cloud_commands(self):
        sent = []

        class Cloud:
            def __init__(self):
                self.acks = []

            def fetch_commands(self):
                return [{"command_id": "cloud-1", "action": "send_text", "target": "chat", "payload": {"text": "from-cloud"}}]

            def acknowledge(self, command_id, result):
                self.acks.append((command_id, result))

            def send_events(self, events):
                return None

            def heartbeat(self, health):
                return None

        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.db")
            cloud = Cloud()
            config = AgentConfig(group="chat", db_path=str(Path(directory) / "agent.db"), history_limit=0, listen_duration="0s")
            agent = LocalAgent(config, store=store, sender=lambda friend, messages: sent.append((friend, messages)), cloud=cloud)
            summary = agent.run_once()
            self.assertEqual(summary["synced_commands"], 1)
            self.assertEqual(summary["processed_commands"], 1)
            self.assertEqual(sent, [("chat", ["from-cloud"])])
            self.assertEqual(cloud.acks[0][0], "cloud-1")
            self.assertEqual(cloud.acks[-1], ("cloud-1", {"state": "completed"}))
            agent.close()
            store.close()

    def test_local_cloud_reference_server_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            server, store = create_server("127.0.0.1", 0, "secret", str(Path(directory) / "cloud.db"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                client = CloudClient(f"http://127.0.0.1:{port}", "secret", agent_id="agent-1")
                client.heartbeat({"state": "idle"})
                client.send_events([MessageEvent("chat", "hello", stable_id="e-1").to_dict()])
                store.enqueue("agent-1", AgentCommand("c-cloud", "send_text", "chat", {"text": "ok"}))
                commands = client.fetch_commands()
                self.assertEqual(commands[0]["command_id"], "c-cloud")
                client.acknowledge("c-cloud", {"state": "queued"})
                self.assertEqual(store.health()["agents"], 1)
                client.acknowledge("c-cloud", {"state": "completed"})
                with self.assertRaises(CloudError):
                    client.acknowledge("missing", {"state": "completed"})
                self.assertFalse(store.acknowledge("agent-1", "c-cloud", {"state": "queued"}))
                self.assertEqual(store.health()["events"], 1)
            finally:
                server.shutdown()
                server.server_close()
                store.close()
                thread.join(timeout=2)

    def test_local_agent_completes_reference_server_command(self):
        with tempfile.TemporaryDirectory() as directory:
            server, cloud_store = create_server("127.0.0.1", 0, "secret", str(Path(directory) / "cloud.db"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            agent_store = AgentStore(Path(directory) / "agent.db")
            try:
                cloud_store.enqueue("agent-1", AgentCommand("c-e2e", "send_text", "chat", {"text": "ok"}))
                client = CloudClient(f"http://127.0.0.1:{server.server_address[1]}", "secret", agent_id="agent-1")
                sent = []
                config = AgentConfig(group="chat", db_path=str(Path(directory) / "agent.db"), history_limit=0, listen_duration="0s")
                agent = LocalAgent(config, store=agent_store, sender=lambda friend, messages: sent.append((friend, messages)), cloud=client)
                summary = agent.run_once()
                self.assertEqual(summary["processed_commands"], 1)
                self.assertEqual(sent, [("chat", ["ok"])])
                self.assertEqual(cloud_store.list_commands("agent-1"), [])
                agent.close()
            finally:
                agent_store.close()
                cloud_store.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_agent_token_env_rejects_non_string_values(self):
        with patch.dict(os.environ, {"PYWEIXIN_AGENT_TOKENS": '{"agent-1": null}'}):
            with self.assertRaises(ValueError):
                _load_agent_tokens()

    def test_reference_server_binds_agent_token(self):
        with tempfile.TemporaryDirectory() as directory:
            server, store = create_server(
                "127.0.0.1",
                0,
                "",
                str(Path(directory) / "cloud.db"),
                agent_tokens={"agent-1": "secret-1"},
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                CloudClient(base_url, "secret-1", agent_id="agent-1").heartbeat({"state": "idle"})
                with self.assertRaises(CloudError):
                    CloudClient(base_url, "secret-1", agent_id="agent-2").heartbeat({"state": "idle"})
            finally:
                server.shutdown()
                server.server_close()
                store.close()
                thread.join(timeout=2)

    def test_reference_server_requires_per_agent_tokens_off_loopback(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                create_server("0.0.0.0", 0, "shared-token", str(Path(directory) / "cloud.db"))

    def test_cloud_client_requires_https_for_remote_endpoint(self):
        with self.assertRaises(ValueError):
            CloudClient("http://example.com", "token")
        client = CloudClient("http://127.0.0.1:8000", "token")
        self.assertEqual(client.base_url, "http://127.0.0.1:8000")


if __name__ == "__main__":
    unittest.main()
