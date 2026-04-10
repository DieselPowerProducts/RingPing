from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ringping.config import AppSettings
from ringping.models import IncomingRequest, ProjectConfig, RequestAttachment


class RingCentralError(RuntimeError):
    pass


class RingCentralClient:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    @property
    def is_configured(self) -> bool:
        return bool(
            self.settings.ringcentral_client_id
            and self.settings.ringcentral_client_secret
            and self.settings.ringcentral_jwt
        )

    def create_post_subscription(self, webhook_url: str) -> dict:
        payload = {
            "eventFilters": ["/team-messaging/v1/posts"],
            "deliveryMode": {
                "transportType": "WebHook",
                "address": webhook_url,
                "verificationToken": self.settings.ringcentral_verification_token,
            },
        }
        return self._api_request("POST", "/restapi/v1.0/subscription", payload)

    def post_chat_message(self, chat_id: str, text: str) -> dict:
        return self._api_request("POST", f"/team-messaging/v1/chats/{chat_id}/posts", {"text": text})

    def list_recent_posts(self, chat_id: str, record_count: int = 20) -> list[dict]:
        payload = self._api_request("GET", f"/team-messaging/v1/chats/{chat_id}/posts?recordCount={record_count}")
        records = payload.get("records", [])
        return records if isinstance(records, list) else []

    def download_attachment(self, attachment: RequestAttachment, destination_dir: Path) -> Path:
        if not attachment.content_uri:
            raise RingCentralError(f"Attachment {attachment.name} does not have a content URI.")
        token = self._get_access_token()
        safe_name = self._sanitize_filename(attachment.name or f"{attachment.id}.bin")
        destination_dir.mkdir(parents=True, exist_ok=True)
        target_path = destination_dir / safe_name
        request = urllib.request.Request(
            attachment.content_uri,
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                target_path.write_bytes(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RingCentralError(f"Attachment download failed {exc.code}: {detail}") from exc
        return target_path

    def extract_incoming_request(
        self,
        payload: dict,
        projects: list[ProjectConfig],
        command_prefix: str = "",
    ) -> IncomingRequest | None:
        body = payload.get("body") if isinstance(payload.get("body"), dict) else payload
        if not isinstance(body, dict):
            return None

        event_type = body.get("eventType") or payload.get("eventType")
        post_type = body.get("type")
        text = str(body.get("text") or "").strip()
        group_id = str(body.get("groupId") or body.get("chatId") or "").strip()
        message_id = str(body.get("id") or "").strip() or None
        attachments = [
            RequestAttachment.from_dict(item)
            for item in body.get("attachments", [])
            if isinstance(item, dict) and str(item.get("type") or "File").strip() == "File"
        ]

        if event_type and event_type not in {"PostAdded", "PostChanged"}:
            return None
        if post_type and post_type != "TextMessage":
            return None
        if not group_id:
            return None

        project_by_chat = {
            chat_id: project
            for project in projects
            for chat_id in project.ringcentral_chat_ids
        }
        project = project_by_chat.get(group_id)
        if project is None:
            return None

        normalized_prompt = text
        if command_prefix:
            if not normalized_prompt.startswith(command_prefix):
                return None
            normalized_prompt = normalized_prompt[len(command_prefix) :].strip()
            if not normalized_prompt and attachments:
                normalized_prompt = "Investigate the attached files and fix the parser issue shown in them."
            if not normalized_prompt:
                return None

        title = normalized_prompt.splitlines()[0][:80]
        return IncomingRequest(
            project_slug=project.slug,
            title=title,
            prompt=normalized_prompt,
            attachments=attachments,
            source="ringcentral",
            source_thread_id=group_id,
            source_message_id=message_id,
        )

    def _api_request(self, method: str, path: str, payload: dict | None = None) -> dict:
        if not self.is_configured:
            raise RingCentralError("RingCentral credentials are not configured.")
        token = self._get_access_token()
        url = f"{self.settings.ringcentral_server_url.rstrip('/')}{path}"
        headers = {"Authorization": f"Bearer {token}"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RingCentralError(f"RingCentral API error {exc.code}: {detail}") from exc

    def _get_access_token(self) -> str:
        if self._access_token and time.time() < self._expires_at:
            return self._access_token

        token_url = f"{self.settings.ringcentral_server_url.rstrip('/')}/restapi/oauth/token"
        credentials = f"{self.settings.ringcentral_client_id}:{self.settings.ringcentral_client_secret}".encode("utf-8")
        authorization = base64.b64encode(credentials).decode("ascii")
        body = urllib.parse.urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": self.settings.ringcentral_jwt,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            token_url,
            data=body,
            headers={
                "Authorization": f"Basic {authorization}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RingCentralError(f"RingCentral auth failed {exc.code}: {detail}") from exc

        self._access_token = payload["access_token"]
        self._expires_at = time.time() + int(payload.get("expires_in", 3600)) - 60
        return self._access_token

    def _sanitize_filename(self, name: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")
        return cleaned or "attachment.bin"
