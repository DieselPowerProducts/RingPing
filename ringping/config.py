from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from ringping.models import ProjectConfig
from ringping.utils import truthy


@dataclass(slots=True)
class AppSettings:
    workspace_dir: Path
    db_path: Path
    worktrees_dir: Path
    projects_file: Path
    webhook_host: str
    webhook_port: int
    webhook_public_base_url: str
    poll_interval_seconds: int
    ringcentral_poll_seconds: int
    release_poll_seconds: int
    codex_command: str
    codex_flags: list[str]
    codex_timeout_seconds: int
    ringcentral_server_url: str
    ringcentral_client_id: str
    ringcentral_client_secret: str
    ringcentral_jwt: str
    ringcentral_verification_token: str
    ringcentral_validation_token: str
    ringcentral_command_prefix: str
    post_status_updates: bool
    review_email_enabled: bool
    review_email_mode: str
    review_email_to: str
    review_email_subject: str
    review_email_smtp_host: str
    review_email_smtp_port: int
    review_email_smtp_username: str
    review_email_smtp_password: str
    review_email_smtp_from: str
    review_email_smtp_use_tls: bool

    @property
    def webhook_path(self) -> str:
        return "/ringcentral/webhook"

    @property
    def local_webhook_url(self) -> str:
        return f"http://{self.webhook_host}:{self.webhook_port}{self.webhook_path}"

    @property
    def public_webhook_url(self) -> str:
        if self.webhook_public_base_url:
            return f"{self.webhook_public_base_url.rstrip('/')}{self.webhook_path}"
        return self.local_webhook_url


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_settings(workspace_dir: Path | None = None) -> AppSettings:
    workspace = workspace_dir or Path.cwd()
    load_env_file(workspace / ".env")

    projects_file = Path(os.environ.get("RINGPING_PROJECTS_FILE", "config/projects.json"))
    if not projects_file.is_absolute():
        projects_file = workspace / projects_file

    db_path = Path(os.environ.get("RINGPING_DB_PATH", "data/ringping.db"))
    if not db_path.is_absolute():
        db_path = workspace / db_path

    worktrees_dir = Path(os.environ.get("RINGPING_WORKTREES_DIR", "data/worktrees"))
    if not worktrees_dir.is_absolute():
        worktrees_dir = workspace / worktrees_dir

    codex_flags_raw = os.environ.get("RINGPING_CODEX_FLAGS", "--full-auto").strip()
    codex_flags = shlex.split(codex_flags_raw, posix=False) if codex_flags_raw else []

    settings = AppSettings(
        workspace_dir=workspace,
        db_path=db_path,
        worktrees_dir=worktrees_dir,
        projects_file=projects_file,
        webhook_host=os.environ.get("RINGPING_WEBHOOK_HOST", "127.0.0.1").strip(),
        webhook_port=int(os.environ.get("RINGPING_WEBHOOK_PORT", "8765")),
        webhook_public_base_url=os.environ.get("RINGPING_WEBHOOK_PUBLIC_BASE_URL", "").strip(),
        poll_interval_seconds=int(os.environ.get("RINGPING_POLL_INTERVAL_SECONDS", "2")),
        ringcentral_poll_seconds=int(os.environ.get("RINGPING_RINGCENTRAL_POLL_SECONDS", "20")),
        release_poll_seconds=int(os.environ.get("RINGPING_RELEASE_POLL_SECONDS", "20")),
        codex_command=os.environ.get("RINGPING_CODEX_COMMAND", "codex").strip(),
        codex_flags=codex_flags,
        codex_timeout_seconds=int(os.environ.get("RINGPING_CODEX_TIMEOUT_SECONDS", "3600")),
        ringcentral_server_url=os.environ.get("RINGPING_RINGCENTRAL_SERVER_URL", "https://platform.ringcentral.com").strip(),
        ringcentral_client_id=os.environ.get("RINGPING_RINGCENTRAL_CLIENT_ID", "").strip(),
        ringcentral_client_secret=os.environ.get("RINGPING_RINGCENTRAL_CLIENT_SECRET", "").strip(),
        ringcentral_jwt=os.environ.get("RINGPING_RINGCENTRAL_JWT", "").strip(),
        ringcentral_verification_token=os.environ.get("RINGPING_RINGCENTRAL_VERIFICATION_TOKEN", "").strip(),
        ringcentral_validation_token=os.environ.get("RINGPING_RINGCENTRAL_VALIDATION_TOKEN", "").strip(),
        ringcentral_command_prefix=os.environ.get("RINGPING_RINGCENTRAL_COMMAND_PREFIX", "").strip(),
        post_status_updates=truthy(os.environ.get("RINGPING_POST_STATUS_UPDATES"), False),
        review_email_enabled=truthy(os.environ.get("RINGPING_REVIEW_EMAIL_ENABLED"), False),
        review_email_mode=os.environ.get("RINGPING_REVIEW_EMAIL_MODE", "outlook").strip().lower(),
        review_email_to=os.environ.get("RINGPING_REVIEW_EMAIL_TO", "").strip(),
        review_email_subject=os.environ.get("RINGPING_REVIEW_EMAIL_SUBJECT", "Code Review").strip(),
        review_email_smtp_host=os.environ.get("RINGPING_REVIEW_EMAIL_SMTP_HOST", "").strip(),
        review_email_smtp_port=int(os.environ.get("RINGPING_REVIEW_EMAIL_SMTP_PORT", "587")),
        review_email_smtp_username=os.environ.get("RINGPING_REVIEW_EMAIL_SMTP_USERNAME", "").strip(),
        review_email_smtp_password=os.environ.get("RINGPING_REVIEW_EMAIL_SMTP_PASSWORD", "").strip(),
        review_email_smtp_from=os.environ.get("RINGPING_REVIEW_EMAIL_SMTP_FROM", "").strip(),
        review_email_smtp_use_tls=truthy(os.environ.get("RINGPING_REVIEW_EMAIL_SMTP_USE_TLS"), True),
    )

    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.worktrees_dir.mkdir(parents=True, exist_ok=True)
    settings.projects_file.parent.mkdir(parents=True, exist_ok=True)
    return settings


def load_project_configs(settings: AppSettings) -> list[ProjectConfig]:
    config_path = settings.projects_file
    if not config_path.exists():
        config_path = settings.workspace_dir / "config/projects.example.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    projects = [ProjectConfig.from_dict(item) for item in payload]
    return sorted(projects, key=lambda item: item.name.lower())
