from __future__ import annotations

import threading

from ringping.config import AppSettings, load_project_configs
from ringping.controller import AppController
from ringping.ringcentral import RingCentralClient
from ringping.storage import Storage


class RingCentralPoller(threading.Thread):
    def __init__(
        self,
        settings: AppSettings,
        storage: Storage,
        controller: AppController,
        ringcentral_client: RingCentralClient,
    ) -> None:
        super().__init__(daemon=True, name="ringping-ringcentral-poller")
        self.settings = settings
        self.storage = storage
        self.controller = controller
        self.ringcentral_client = ringcentral_client
        self._stop_event = threading.Event()
        self._seen_ids_by_chat: dict[str, list[str]] = {}

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        if not self.ringcentral_client.is_configured:
            return

        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception:
                pass
            self._stop_event.wait(self.settings.ringcentral_poll_seconds)

    def _poll_once(self) -> None:
        try:
            self.storage.sync_projects(load_project_configs(self.settings))
        except Exception:
            pass
        projects = self.storage.list_projects()
        chat_ids = sorted({chat_id for project in projects for chat_id in project.ringcentral_chat_ids})

        for chat_id in chat_ids:
            try:
                posts = self.ringcentral_client.list_recent_posts(chat_id, record_count=20)
            except Exception:
                continue
            current_ids = [str(post.get("id")) for post in posts if post.get("id")]
            seen_ids = self._seen_ids_by_chat.get(chat_id)
            prior_ids = seen_ids or []

            if seen_ids is None:
                known_ids = self.storage.list_source_message_ids("ringcentral", chat_id)
                if known_ids:
                    new_posts = [post for post in reversed(posts) if str(post.get("id")) not in known_ids]
                else:
                    new_posts = []
            else:
                new_posts = [post for post in reversed(posts) if str(post.get("id")) not in seen_ids]
            for post in new_posts:
                payload = {
                    "body": {
                        **post,
                        "chatId": chat_id,
                        "eventType": "PostAdded",
                    }
                }
                self.controller.ingest_ringcentral_payload(payload)

            merged = current_ids + [post_id for post_id in prior_ids if post_id not in current_ids]
            self._seen_ids_by_chat[chat_id] = merged[:200]
