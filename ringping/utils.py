from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path


TIME_ONLY_PATTERN = re.compile(r"\b([0-9]{1,2}:[0-9]{2}\s?(?:[APap][Mm])?)\b")
ISO_PATTERN = re.compile(r"\b(20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))\b")
UNIX_TS_PATTERN = re.compile(r'"resets_at"\s*:\s*(\d{10,})')


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def tail_text(value: str, limit: int = 4000) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[-limit:]


def detect_codex_reset_time(text: str, *, fallback_home: Path | None = None) -> datetime | None:
    reset_time = _detect_reset_time_from_text(text)
    if reset_time is not None:
        return reset_time
    return _latest_codex_session_reset_time(fallback_home or Path.home())


def format_local_time(value: datetime) -> str:
    local_value = value.astimezone()
    return local_value.strftime("%I:%M %p").lstrip("0")


def _detect_reset_time_from_text(text: str) -> datetime | None:
    if not text:
        return None

    unix_match = UNIX_TS_PATTERN.search(text)
    if unix_match:
        try:
            return datetime.fromtimestamp(int(unix_match.group(1)), tz=timezone.utc)
        except ValueError:
            pass

    iso_match = ISO_PATTERN.search(text)
    if iso_match:
        try:
            return datetime.fromisoformat(iso_match.group(1).replace("Z", "+00:00"))
        except ValueError:
            pass

    lowered = text.lower()
    if "limit" not in lowered and "credit" not in lowered and "quota" not in lowered and "rate" not in lowered:
        return None

    time_match = TIME_ONLY_PATTERN.search(text)
    if not time_match:
        return None
    time_text = time_match.group(1).upper().replace(" ", "")
    formats = ("%I:%M%p", "%H:%M")
    today = datetime.now().astimezone()
    for time_format in formats:
        try:
            parsed_time = datetime.strptime(time_text, time_format).time()
            candidate = today.replace(
                hour=parsed_time.hour,
                minute=parsed_time.minute,
                second=0,
                microsecond=0,
            )
            if candidate < today:
                candidate = candidate + timedelta(days=1)
            return candidate
        except ValueError:
            continue
    return None


def _latest_codex_session_reset_time(home_dir: Path) -> datetime | None:
    sessions_root = home_dir / ".codex" / "sessions"
    if not sessions_root.exists():
        return None

    candidates = sorted(
        (path for path in sessions_root.rglob("*.jsonl") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates[:5]:
        reset_time = _read_reset_time_from_session(path)
        if reset_time is not None:
            return reset_time
    return None


def _read_reset_time_from_session(path: Path) -> datetime | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None

    for line in reversed(raw.splitlines()[-500:]):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_payload = payload.get("payload") if isinstance(payload, dict) else None
        if not isinstance(event_payload, dict):
            continue
        if event_payload.get("type") != "token_count":
            continue
        rate_limits = event_payload.get("rate_limits")
        if not isinstance(rate_limits, dict):
            continue
        if rate_limits.get("limit_id") != "codex":
            continue
        primary = rate_limits.get("primary")
        if not isinstance(primary, dict):
            continue
        resets_at = primary.get("resets_at")
        if not resets_at:
            continue
        try:
            return datetime.fromtimestamp(int(resets_at), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            continue
    return None
