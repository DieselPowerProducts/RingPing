from __future__ import annotations

import subprocess
import tempfile
from email.message import EmailMessage
from pathlib import Path
import smtplib

from ringping.config import AppSettings
from ringping.models import ProjectConfig, RequestRecord


class ReviewEmailError(RuntimeError):
    pass


class ReviewEmailNotifier:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    @property
    def is_configured(self) -> bool:
        return self.settings.review_email_enabled and bool(self.settings.review_email_to)

    def send_manual_review_email(
        self,
        project: ProjectConfig,
        request: RequestRecord,
        review_reason: str,
    ) -> None:
        if not self.is_configured:
            return
        subject = self.settings.review_email_subject.strip() or "Code Review"
        body = self._build_manual_review_body(project, request, review_reason)
        mode = self.settings.review_email_mode or "outlook"
        if mode == "outlook":
            self._send_via_outlook(self.settings.review_email_to, subject, body)
            return
        if mode == "smtp":
            self._send_via_smtp(self.settings.review_email_to, subject, body)
            return
        raise ReviewEmailError(f"Unsupported review email mode: {mode}")

    def _build_manual_review_body(
        self,
        project: ProjectConfig,
        request: RequestRecord,
        review_reason: str,
    ) -> str:
        lines = [
            f"Project: {project.name}",
            f"Request ID: {request.id}",
            f"Title: {request.title}",
            f"Created: {request.created_at}",
            "",
            "Manual review reason:",
            review_reason.strip(),
            "",
            "Prompt:",
            request.prompt.strip(),
        ]
        if request.worktree_path:
            lines.extend(["", f"Worktree: {request.worktree_path}"])
        if request.diff_summary:
            lines.extend(["", "Diff summary:", request.diff_summary.strip()])
        if request.codex_summary:
            lines.extend(["", "Codex summary:", request.codex_summary.strip()])
        return "\n".join(lines).strip() + "\n"

    def _send_via_outlook(self, to_address: str, subject: str, body: str) -> None:
        with tempfile.TemporaryDirectory(prefix="ringping-review-email-") as temp_dir:
            temp_path = Path(temp_dir)
            to_path = temp_path / "to.txt"
            subject_path = temp_path / "subject.txt"
            body_path = temp_path / "body.txt"
            to_path.write_text(to_address, encoding="utf-8")
            subject_path.write_text(subject, encoding="utf-8")
            body_path.write_text(body, encoding="utf-8")
            command = [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                (
                    "$to = Get-Content -Raw -LiteralPath $args[0]; "
                    "$subject = Get-Content -Raw -LiteralPath $args[1]; "
                    "$body = Get-Content -Raw -LiteralPath $args[2]; "
                    "$outlook = New-Object -ComObject Outlook.Application; "
                    "$mail = $outlook.CreateItem(0); "
                    "$mail.To = $to.Trim(); "
                    "$mail.Subject = $subject.Trim(); "
                    "$mail.Body = $body; "
                    "$mail.Send()"
                ),
                str(to_path),
                str(subject_path),
                str(body_path),
            ]
            result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "Unknown Outlook error"
            raise ReviewEmailError(f"Outlook email send failed: {detail}")

    def _send_via_smtp(self, to_address: str, subject: str, body: str) -> None:
        if not self.settings.review_email_smtp_host:
            raise ReviewEmailError("SMTP host is not configured.")
        from_address = self.settings.review_email_smtp_from or self.settings.review_email_smtp_username
        if not from_address:
            raise ReviewEmailError("SMTP from address is not configured.")

        message = EmailMessage()
        message["From"] = from_address
        message["To"] = to_address
        message["Subject"] = subject
        message.set_content(body)

        with smtplib.SMTP(self.settings.review_email_smtp_host, self.settings.review_email_smtp_port, timeout=30) as server:
            if self.settings.review_email_smtp_use_tls:
                server.starttls()
            if self.settings.review_email_smtp_username:
                server.login(
                    self.settings.review_email_smtp_username,
                    self.settings.review_email_smtp_password,
                )
            server.send_message(message)
