from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from ringping.controller import AppController
from ringping.models import IncomingRequest, RequestRecord, RequestStatus


class ControllerStatusMessageTests(unittest.TestCase):
    def test_new_ringcentral_request_posts_scuba_steve_acknowledgement(self) -> None:
        settings = SimpleNamespace(
            ringcentral_command_prefix="fix:",
            ringcentral_ask_prefix="ask:",
            ringcentral_legacy_requests_enabled=True,
            ringcentral_online_training_url="https://invoice-extractor-online.vercel.app/",
            post_status_updates=True,
        )
        incoming = IncomingRequest(
            project_slug="invoice-extractor",
            title="Fix invoice",
            prompt="Fix invoice",
            source="ringcentral",
            source_thread_id="thread-1",
            source_message_id="message-1",
        )
        request = RequestRecord(
            id=1,
            project_slug="invoice-extractor",
            source="ringcentral",
            source_thread_id="thread-1",
            source_message_id="message-1",
            title="Fix invoice",
            prompt="Fix invoice",
            attachments=[],
            status=RequestStatus.PENDING,
            branch_name=None,
            worktree_path=None,
            codex_summary=None,
            diff_summary=None,
            manual_review_reason=None,
            error_text=None,
            commit_sha=None,
            release_version=None,
            created_at="2026-04-24T16:24:42+00:00",
            updated_at="2026-04-24T16:24:42+00:00",
            started_at=None,
            completed_at=None,
            pushed_at=None,
            release_ready_notified_at=None,
            is_ask=False,
        )

        storage = Mock()
        storage.list_projects.return_value = []
        storage.create_request_result.return_value = (request, True)
        ringcentral_client = Mock()
        ringcentral_client.is_configured = True
        ringcentral_client.extract_incoming_request.return_value = incoming
        controller = AppController(settings, storage, None, ringcentral_client)

        controller.ingest_ringcentral_payload({"body": {"id": "message-1"}})

        ringcentral_client.post_chat_message.assert_called_once_with(
            "thread-1",
            "Scuba Steve is looking at the issue right now.",
        )

    def test_scuba_steve_readiness_ask_posts_immediate_reply(self) -> None:
        settings = SimpleNamespace(
            ringcentral_command_prefix="fix:",
            ringcentral_ask_prefix="ask:",
            ringcentral_legacy_requests_enabled=True,
            ringcentral_online_training_url="https://invoice-extractor-online.vercel.app/",
            post_status_updates=True,
        )
        incoming = IncomingRequest(
            project_slug="invoice-extractor",
            title="is scuba steve ready and willing to help the team",
            prompt="is scuba steve ready and willing to help the team",
            source="ringcentral",
            source_thread_id="thread-1",
            source_message_id="message-1",
            is_ask=True,
        )
        request = RequestRecord(
            id=2,
            project_slug="invoice-extractor",
            source="ringcentral",
            source_thread_id="thread-1",
            source_message_id="message-1",
            title="is scuba steve ready and willing to help the team",
            prompt="is scuba steve ready and willing to help the team",
            attachments=[],
            status=RequestStatus.PENDING,
            branch_name=None,
            worktree_path=None,
            codex_summary=None,
            diff_summary=None,
            manual_review_reason=None,
            error_text=None,
            commit_sha=None,
            release_version=None,
            created_at="2026-04-24T16:24:42+00:00",
            updated_at="2026-04-24T16:24:42+00:00",
            started_at=None,
            completed_at=None,
            pushed_at=None,
            release_ready_notified_at=None,
            is_ask=True,
        )

        storage = Mock()
        storage.list_projects.return_value = []
        storage.create_request_result.return_value = (request, True)
        ringcentral_client = Mock()
        ringcentral_client.is_configured = True
        ringcentral_client.extract_incoming_request.return_value = incoming
        controller = AppController(settings, storage, None, ringcentral_client)

        controller.ingest_ringcentral_payload({"body": {"id": "message-1"}})

        storage.mark_request_no_changes.assert_called_once_with(
            2,
            "Handled by Scuba Steve quick response.",
            "",
        )
        ringcentral_client.post_chat_message.assert_called_once_with(
            "thread-1",
            "Scuba Steve is ready and willing to help the team.",
        )

    def test_ringcentral_fix_command_redirects_to_online_training_when_legacy_requests_disabled(self) -> None:
        settings = SimpleNamespace(
            ringcentral_command_prefix="fix:",
            ringcentral_ask_prefix="ask:",
            ringcentral_legacy_requests_enabled=False,
            ringcentral_online_training_url="https://invoice-extractor-online.vercel.app/",
            post_status_updates=True,
        )
        storage = Mock()
        storage.list_projects.return_value = []
        ringcentral_client = Mock()
        ringcentral_client.extract_online_redirect_target.return_value = ("thread-1", "InvoiceExtractor")
        controller = AppController(settings, storage, None, ringcentral_client)

        controller.ingest_ringcentral_payload({"body": {"id": "message-1", "text": "fix: add vendor"}})

        storage.create_request_result.assert_not_called()
        ringcentral_client.extract_incoming_request.assert_not_called()
        ringcentral_client.post_chat_message.assert_called_once_with(
            "thread-1",
            (
                "InvoiceExtractor parser training and fix requests are now made online on the "
                "training tab here: https://invoice-extractor-online.vercel.app/"
            ),
        )


if __name__ == "__main__":
    unittest.main()
