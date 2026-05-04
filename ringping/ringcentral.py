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
    MAX_HTTP_ATTEMPTS = 2

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

    def find_chat_id(self, name_query: str) -> list[dict]:
        """Return chats whose name contains name_query (case-insensitive)."""
        results = []
        page_token = None
        while True:
            path = "/team-messaging/v1/chats?recordCount=250"
            if page_token:
                path += f"&pageToken={page_token}"
            payload = self._api_request("GET", path)
            records = payload.get("records") or []
            for chat in records:
                chat_name = str(chat.get("name") or "")
                members = chat.get("members") or []
                searchable = chat_name + " " + " ".join(
                    str(m.get("name") or m.get("firstName") or m.get("id") or "") for m in members
                )
                if not name_query or name_query.lower() in searchable.lower():
                    results.append({"id": chat.get("id"), "name": chat_name or "(direct/personal)", "type": chat.get("type")})
            nav = payload.get("navigation") or {}
            page_token = nav.get("nextPageToken")
            if not page_token or not records:
                break
        return results

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
            with self._urlopen_with_retries(request, timeout=60, operation="Attachment download") as response:
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
        ask_prefix: str = "ask:",
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

        # Check for ask: prefix first (conversational — no code changes)
        is_ask = False
        normalized_prompt = text
        if ask_prefix and normalized_prompt.lower().startswith(ask_prefix.lower()):
            normalized_prompt = normalized_prompt[len(ask_prefix):].strip()
            is_ask = True
            if not normalized_prompt and attachments:
                normalized_prompt = "Please download the attachments and tell me what you find in them."
            if not normalized_prompt:
                return None
        elif command_prefix:
            if not normalized_prompt.lower().startswith(command_prefix.lower()):
                return None
            normalized_prompt = normalized_prompt[len(command_prefix):].strip()
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
            is_ask=is_ask,
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
            with self._urlopen_with_retries(request, timeout=30, operation="RingCentral API request") as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RingCentralError(f"RingCentral API error {exc.code}: {detail}") from exc

    def _get_access_token(self) -> str:
        if self._access_token and time.time() < self._expires_at:
            return self._access_token
        cached_token = self._load_cached_access_token()
        if cached_token:
            return cached_token

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
            with self._urlopen_with_retries(request, timeout=30, operation="RingCentral auth") as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RingCentralError(f"RingCentral auth failed {exc.code}: {detail}") from exc

        self._access_token = payload["access_token"]
        self._expires_at = time.time() + int(payload.get("expires_in", 3600)) - 60
        self._write_cached_access_token()
        return self._access_token

    def _load_cached_access_token(self) -> str | None:
        cache_path = self._token_cache_path()
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            access_token = str(payload.get("access_token") or "").strip()
            expires_at = float(payload.get("expires_at") or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None
        if not access_token or time.time() >= expires_at:
            return None
        self._access_token = access_token
        self._expires_at = expires_at
        return access_token

    def _write_cached_access_token(self) -> None:
        if not self._access_token:
            return
        cache_path = self._token_cache_path()
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = cache_path.with_suffix(".tmp")
            temp_path.write_text(
                json.dumps({"access_token": self._access_token, "expires_at": self._expires_at}),
                encoding="utf-8",
            )
            temp_path.replace(cache_path)
        except OSError:
            pass

    def _token_cache_path(self) -> Path:
        return self.settings.db_path.parent / "ringcentral-token-cache.json"

    def _urlopen_with_retries(self, request: urllib.request.Request, *, timeout: int, operation: str):
        last_error: urllib.error.HTTPError | None = None
        for attempt in range(self.MAX_HTTP_ATTEMPTS):
            try:
                return urllib.request.urlopen(request, timeout=timeout)
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code != 429 or attempt >= self.MAX_HTTP_ATTEMPTS - 1:
                    raise
                time.sleep(self._retry_after_seconds(exc, attempt))
        if last_error is not None:
            raise last_error
        raise RingCentralError(f"{operation} failed before a request was sent.")

    def _retry_after_seconds(self, exc: urllib.error.HTTPError, attempt: int) -> float:
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        if retry_after:
            try:
                return min(max(float(retry_after), 1.0), 10.0)
            except ValueError:
                pass
        return min(3.0 * (attempt + 1), 10.0)

    def _sanitize_filename(self, name: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")
        return cleaned or "attachment.bin"
