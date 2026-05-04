from __future__ import annotations

import unittest
from types import SimpleNamespace

from ringping.poller import RingCentralPoller


class StorageStub:
    def __init__(self, known_ids: set[str], projects: list[object]) -> None:
        self.known_ids = known_ids
        self.projects = projects
        self.synced = False

    def sync_projects(self, projects) -> None:
        self.synced = True

    def list_projects(self):
        return self.projects

    def list_source_message_ids(self, source: str, source_thread_id: str) -> set[str]:
        return set(self.known_ids)


class ControllerStub:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def ingest_ringcentral_payload(self, payload: dict) -> None:
        self.payloads.append(payload)


class RingCentralClientStub:
    def __init__(self, posts_by_chat: dict[str, list[dict]], failing_chats: set[str] | None = None) -> None:
        self.is_configured = True
        self.posts_by_chat = posts_by_chat
        self.failing_chats = failing_chats or set()

    def list_recent_posts(self, chat_id: str, record_count: int = 20) -> list[dict]:
        if chat_id in self.failing_chats:
            raise RuntimeError("rate limited")
        return list(self.posts_by_chat.get(chat_id, []))


class RingCentralPollerTests(unittest.TestCase):
    def test_first_poll_ingests_recent_posts_not_already_in_db(self) -> None:
        chat_id = "1563175665666"
        posts = [
            {"id": "old-1", "text": "fix: old", "type": "TextMessage"},
            {"id": "new-1", "text": "ask: inspect this", "type": "TextMessage"},
            {"id": "new-2", "text": "ask: and this", "type": "TextMessage"},
        ]
        project = SimpleNamespace(ringcentral_chat_ids=[chat_id])
        storage = StorageStub({"old-1"}, [project])
        controller = ControllerStub()
        client = RingCentralClientStub({chat_id: posts})
        settings = SimpleNamespace(ringcentral_poll_seconds=20)

        poller = RingCentralPoller(settings, storage, controller, client)
        poller._poll_once()

        self.assertEqual(
            [payload["body"]["id"] for payload in controller.payloads],
            ["new-2", "new-1"],
        )
        self.assertEqual(poller._seen_ids_by_chat[chat_id], ["old-1", "new-1", "new-2"])

    def test_first_poll_with_no_history_sets_baseline_only(self) -> None:
        chat_id = "1563175665666"
        posts = [
            {"id": "post-1", "text": "ask: inspect this", "type": "TextMessage"},
            {"id": "post-2", "text": "ask: inspect that", "type": "TextMessage"},
        ]
        project = SimpleNamespace(ringcentral_chat_ids=[chat_id])
        storage = StorageStub(set(), [project])
        controller = ControllerStub()
        client = RingCentralClientStub({chat_id: posts})
        settings = SimpleNamespace(ringcentral_poll_seconds=20)

        poller = RingCentralPoller(settings, storage, controller, client)
        poller._poll_once()

        self.assertEqual(controller.payloads, [])
        self.assertEqual(poller._seen_ids_by_chat[chat_id], ["post-1", "post-2"])

    def test_poll_error_for_one_chat_does_not_abort_other_chats(self) -> None:
        failing_chat_id = "chat-rate-limited"
        working_chat_id = "chat-ok"
        posts = [{"id": "post-1", "text": "fix: inspect this", "type": "TextMessage"}]
        project = SimpleNamespace(ringcentral_chat_ids=[failing_chat_id, working_chat_id])
        storage = StorageStub({"old-1"}, [project])
        controller = ControllerStub()
        client = RingCentralClientStub({working_chat_id: posts}, failing_chats={failing_chat_id})
        settings = SimpleNamespace(ringcentral_poll_seconds=20)

        poller = RingCentralPoller(settings, storage, controller, client)
        poller._poll_once()

        self.assertEqual([payload["body"]["id"] for payload in controller.payloads], ["post-1"])
        self.assertEqual(poller._seen_ids_by_chat[working_chat_id], ["post-1"])
        self.assertNotIn(failing_chat_id, poller._seen_ids_by_chat)


if __name__ == "__main__":
    unittest.main()
