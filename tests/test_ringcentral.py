from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ringping.models import ProjectConfig
from ringping.ringcentral import RingCentralClient


def build_client(db_path: Path | None = None) -> RingCentralClient:
    return RingCentralClient(
        SimpleNamespace(
            db_path=db_path or Path("C:/workspace/data/ringping.db"),
            ringcentral_client_id="",
            ringcentral_client_secret="",
            ringcentral_jwt="",
            ringcentral_server_url="https://platform.ringcentral.com",
            ringcentral_verification_token="",
        )
    )


class RingCentralAskParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = build_client()
        self.project = ProjectConfig(
            slug="invoice-extractor",
            name="InvoiceExtractor",
            repo_path="C:/repo",
            ringcentral_chat_ids=["chat-123"],
        )

    def test_extract_incoming_request_marks_ask_messages(self) -> None:
        payload = {
            "body": {
                "id": "msg-1",
                "chatId": "chat-123",
                "type": "TextMessage",
                "text": "ask: can you read these files?",
                "attachments": [{"id": "file-1", "name": "invoice.pdf", "contentUri": "https://example.com/file"}],
            }
        }

        incoming = self.client.extract_incoming_request(
            payload,
            [self.project],
            command_prefix="fix:",
            ask_prefix="ask:",
        )

        self.assertIsNotNone(incoming)
        self.assertTrue(incoming.is_ask)
        self.assertEqual(incoming.prompt, "can you read these files?")
        self.assertEqual(incoming.source_thread_id, "chat-123")

    def test_extract_incoming_request_keeps_attachment_only_ask_alive(self) -> None:
        payload = {
            "body": {
                "id": "msg-2",
                "groupId": "chat-123",
                "type": "TextMessage",
                "text": "ask:",
                "attachments": [{"id": "file-1", "name": "invoice.pdf", "contentUri": "https://example.com/file"}],
            }
        }

        incoming = self.client.extract_incoming_request(
            payload,
            [self.project],
            command_prefix="fix:",
            ask_prefix="ask:",
        )

        self.assertIsNotNone(incoming)
        self.assertTrue(incoming.is_ask)
        self.assertEqual(
            incoming.prompt,
            "Please download the attachments and tell me what you find in them.",
        )

    def test_extract_incoming_request_accepts_capitalized_fix_prefix(self) -> None:
        payload = {
            "body": {
                "id": "msg-3",
                "chatId": "chat-123",
                "type": "TextMessage",
                "text": "Fix: update the vendor mapping",
            }
        }

        incoming = self.client.extract_incoming_request(
            payload,
            [self.project],
            command_prefix="fix:",
            ask_prefix="ask:",
        )

        self.assertIsNotNone(incoming)
        self.assertFalse(incoming.is_ask)
        self.assertEqual(incoming.prompt, "update the vendor mapping")

    def test_extract_incoming_request_accepts_capitalized_ask_prefix(self) -> None:
        payload = {
            "body": {
                "id": "msg-4",
                "chatId": "chat-123",
                "type": "TextMessage",
                "text": "Ask: can you read these files?",
            }
        }

        incoming = self.client.extract_incoming_request(
            payload,
            [self.project],
            command_prefix="fix:",
            ask_prefix="ask:",
        )

        self.assertIsNotNone(incoming)
        self.assertTrue(incoming.is_ask)
        self.assertEqual(incoming.prompt, "can you read these files?")

    def test_get_access_token_reuses_valid_local_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "data" / "ringping.db"
            cache_path = db_path.parent / "ringcentral-token-cache.json"
            cache_path.parent.mkdir(parents=True)
            cache_path.write_text(
                json.dumps({"access_token": "cached-token", "expires_at": time.time() + 600}),
                encoding="utf-8",
            )
            client = build_client(db_path)

            with patch("ringping.ringcentral.urllib.request.urlopen") as urlopen:
                token = client._get_access_token()

            self.assertEqual(token, "cached-token")
            urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
