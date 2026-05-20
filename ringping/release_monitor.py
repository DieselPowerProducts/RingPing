from __future__ import annotations

import json
import subprocess
import threading
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from ringping.config import AppSettings
from ringping.ringcentral import RingCentralClient
from ringping.storage import Storage
from ringping.utils import LOG_PREFIX


class ReleaseMonitor(threading.Thread):
    def __init__(self, settings: AppSettings, storage: Storage, ringcentral_client: RingCentralClient) -> None:
        super().__init__(daemon=True, name="ringping-release-monitor")
        self.settings = settings
        self.storage = storage
        self.ringcentral_client = ringcentral_client
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._check_once()
            except Exception:
                pass
            self._stop_event.wait(self.settings.release_poll_seconds)

    def _check_once(self) -> None:
        cached_manifests: dict[str, dict] = {}
        for request in self.storage.list_pending_release_notifications():
            project = self.storage.get_project(request.project_slug)
            if not request.release_version:
                continue

            manifest_key = project.release_manifest_url or project.repo_path
            manifest = cached_manifests.get(manifest_key)
            if manifest is None:
                manifest = self._fetch_manifest(project)
                cached_manifests[manifest_key] = manifest

            manifest_version = str(manifest.get("version") or "").strip()
            published_at = str(manifest.get("published_at") or "").strip()
            if not manifest_version or not published_at:
                continue
            if self._compare_versions(manifest_version, request.release_version) < 0:
                continue

            self._append_request_log_status(request.id, "Release ready. Update is available.")
            if self.settings.post_status_updates and self.ringcentral_client.is_configured and request.source_thread_id:
                self.ringcentral_client.post_chat_message(
                    request.source_thread_id,
                    "Scuba Steve is done. It is ready for you to update!",
                )
            self.storage.mark_release_ready_notified(request.id)

    def _append_request_log_status(self, request_id: int, text: str) -> None:
        log_path = self.settings.request_logs_dir / f"request-{request_id}.log"
        raw_log_path = self.settings.request_logs_dir / f"request-{request_id}.raw.log"
        for path in (log_path, raw_log_path):
            if not path.exists():
                continue
            with path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(f"{LOG_PREFIX} {text}\n")

    def _fetch_manifest(self, project) -> dict:
        local_manifest = self._fetch_manifest_from_repo(project)
        if local_manifest is not None:
            return local_manifest
        if project.release_manifest_url:
            return self._fetch_manifest_from_url(project.release_manifest_url)
        return {}

    def _fetch_manifest_from_url(self, url: str) -> dict:
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.load(response)

    def _fetch_manifest_from_repo(self, project) -> dict | None:
        repo_path = Path(project.repo_path)
        if not (repo_path.exists() and (repo_path / ".git").exists()):
            return None
        manifest_path = self._manifest_repo_path(project.release_manifest_url)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if __import__("os").name == "nt" else 0
        try:
            subprocess.run(
                ["git", "-C", str(repo_path), "fetch", project.remote_name, project.base_branch],
                capture_output=True,
                text=True,
                check=True,
                creationflags=creationflags,
            )
            result = subprocess.run(
                ["git", "-C", str(repo_path), "show", f"{project.remote_name}/{project.base_branch}:{manifest_path}"],
                capture_output=True,
                text=True,
                check=True,
                creationflags=creationflags,
            )
            return json.loads(result.stdout)
        except Exception:
            return None

    def _manifest_repo_path(self, release_manifest_url: str) -> str:
        if not release_manifest_url:
            return "docs/release.json"
        parsed = urlparse(release_manifest_url)
        parts = [part for part in parsed.path.split("/") if part]
        if "main" in parts:
            main_index = parts.index("main")
            repo_file_parts = parts[main_index + 1 :]
            if repo_file_parts:
                return "/".join(repo_file_parts)
        return "docs/release.json"

    def _compare_versions(self, left: str, right: str) -> int:
        left_parts = [int(part) for part in left.strip().split(".")]
        right_parts = [int(part) for part in right.strip().split(".")]
        max_len = max(len(left_parts), len(right_parts))
        left_parts.extend([0] * (max_len - len(left_parts)))
        right_parts.extend([0] * (max_len - len(right_parts)))
        if left_parts < right_parts:
            return -1
        if left_parts > right_parts:
            return 1
        return 0
