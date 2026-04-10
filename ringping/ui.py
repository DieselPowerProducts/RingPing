from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from ringping.controller import AppController
from ringping.models import ProjectSnapshot, RequestRecord, RequestStatus


STATUS_COLORS = {
    RequestStatus.PENDING: "#6b7280",
    RequestStatus.RUNNING: "#2563eb",
    RequestStatus.READY: "#d97706",
    RequestStatus.PUSHED: "#15803d",
    RequestStatus.ERROR: "#b91c1c",
    RequestStatus.NO_CHANGES: "#475569",
}


class DashboardApp(tk.Tk):
    def __init__(self, controller: AppController, on_shutdown, startup_notice: str = "") -> None:
        super().__init__()
        self.controller = controller
        self.on_shutdown = on_shutdown
        self.title("RingPing")
        self.geometry("1360x900")
        self.minsize(1100, 720)
        self.configure(bg="#f5f5f4")

        self.status_var = tk.StringVar(value=startup_notice or "RingPing ready.")
        self.webhook_var = tk.StringVar(value=self.controller.webhook_banner())
        self.project_slug_by_label: dict[str, str] = {}
        self.project_release_on_push_by_slug: dict[str, bool] = {}
        self._last_snapshot_signature = None

        self._build_header()
        self._build_projects_canvas()
        self._build_footer()

        self.protocol("WM_DELETE_WINDOW", self._handle_close)
        self.after(250, self._refresh_loop)

    def _build_header(self) -> None:
        header = tk.Frame(self, bg="#111827", padx=18, pady=16)
        header.pack(fill="x")

        title = tk.Label(
            header,
            text="RingPing",
            font=("Segoe UI", 20, "bold"),
            fg="white",
            bg="#111827",
        )
        title.pack(anchor="w")

        subtitle = tk.Label(
            header,
            text="Per-project Codex request routing, review, and push control.",
            font=("Segoe UI", 10),
            fg="#d1d5db",
            bg="#111827",
        )
        subtitle.pack(anchor="w", pady=(4, 0))

        webhook = tk.Label(
            header,
            textvariable=self.webhook_var,
            font=("Consolas", 9),
            fg="#93c5fd",
            bg="#111827",
        )
        webhook.pack(anchor="w", pady=(8, 0))

    def _build_projects_canvas(self) -> None:
        outer = tk.Frame(self, bg="#f5f5f4", padx=18, pady=8)
        outer.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(outer, bg="#f5f5f4", highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=self.canvas.yview)
        self.projects_frame = tk.Frame(self.canvas, bg="#f5f5f4")

        self.projects_frame.bind(
            "<Configure>",
            lambda event: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.projects_window = self.canvas.create_window((0, 0), window=self.projects_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.canvas.bind(
            "<Configure>",
            lambda event: self.canvas.itemconfigure(self.projects_window, width=event.width),
        )

    def _build_footer(self) -> None:
        footer = tk.Frame(self, bg="#e7e5e4", padx=18, pady=10)
        footer.pack(fill="x")
        tk.Label(footer, textvariable=self.status_var, anchor="w", bg="#e7e5e4").pack(fill="x")

    def _refresh_loop(self) -> None:
        try:
            snapshots = self.controller.list_project_snapshots()
            signature = self._build_snapshot_signature(snapshots)
            if signature != self._last_snapshot_signature:
                self._render_projects(snapshots)
                self._last_snapshot_signature = signature
        except Exception as exc:  # noqa: BLE001
            self.status_var.set(f"Refresh error: {exc}")
        finally:
            self.after(2000, self._refresh_loop)

    def _render_projects(self, snapshots: list[ProjectSnapshot]) -> None:
        for snapshot in snapshots:
            self.project_release_on_push_by_slug[snapshot.project.slug] = snapshot.project.release_on_push

        for child in self.projects_frame.winfo_children():
            child.destroy()

        for snapshot in snapshots:
            self._render_project(snapshot)

    def _render_project(self, snapshot: ProjectSnapshot) -> None:
        section = tk.Frame(self.projects_frame, bg="white", bd=1, relief="solid", padx=14, pady=14)
        section.pack(fill="x", pady=(0, 14))

        header = tk.Frame(section, bg="white")
        header.pack(fill="x")

        name = tk.Label(header, text=snapshot.project.name, font=("Segoe UI", 14, "bold"), bg="white")
        name.pack(side="left")

        meta = tk.Label(
            header,
            text=(
                f"{snapshot.project.repo_path} | base {snapshot.project.base_branch} | push {snapshot.project.push_mode}"
                + (
                    f" | release {snapshot.project.release_version_strategy}"
                    if snapshot.project.release_on_push
                    else ""
                )
            ),
            font=("Segoe UI", 9),
            fg="#57534e",
            bg="white",
        )
        meta.pack(side="left", padx=(12, 0))

        auto_var = tk.BooleanVar(value=snapshot.project.auto_push)
        auto_box = ttk.Checkbutton(
            header,
            text="Auto push",
            variable=auto_var,
            command=lambda slug=snapshot.project.slug, var=auto_var: self._set_auto_push(slug, var.get()),
        )
        auto_box.pack(side="right")

        chats = ", ".join(snapshot.project.ringcentral_chat_ids) if snapshot.project.ringcentral_chat_ids else "No chat IDs configured"
        chat_label = tk.Label(section, text=f"RingCentral chats: {chats}", font=("Consolas", 9), fg="#1d4ed8", bg="white")
        chat_label.pack(anchor="w", pady=(6, 10))

        if not snapshot.requests:
            tk.Label(section, text="No requests yet.", fg="#6b7280", bg="white").pack(anchor="w")
            return

        for request in snapshot.requests:
            self._render_request_row(section, request)

    def _render_request_row(self, parent: tk.Widget, request: RequestRecord) -> None:
        row = tk.Frame(parent, bg="#fafaf9", bd=1, relief="solid", padx=10, pady=10)
        row.pack(fill="x", pady=(0, 10))

        top = tk.Frame(row, bg="#fafaf9")
        top.pack(fill="x")

        title = tk.Label(top, text=request.title, font=("Segoe UI", 11, "bold"), bg="#fafaf9")
        title.pack(side="left")

        status = tk.Label(
            top,
            text=request.status.value.upper(),
            fg="white",
            bg=STATUS_COLORS.get(request.status, "#334155"),
            padx=8,
            pady=2,
            font=("Segoe UI", 9, "bold"),
        )
        status.pack(side="right")

        created = request.created_at.replace("T", " ")
        branch = request.branch_name or "branch pending"
        meta = tk.Label(
            row,
            text=f"Created {created} | {branch}",
            font=("Segoe UI", 9),
            fg="#57534e",
            bg="#fafaf9",
        )
        meta.pack(anchor="w", pady=(4, 8))

        snippet = (request.codex_summary or request.error_text or request.prompt).strip().replace("\n", " ")
        if len(snippet) > 180:
            snippet = snippet[:177] + "..."
        tk.Label(row, text=snippet, wraplength=1080, justify="left", bg="#fafaf9").pack(anchor="w")

        buttons = tk.Frame(row, bg="#fafaf9")
        buttons.pack(fill="x", pady=(10, 0))

        ttk.Button(buttons, text="Details", command=lambda rid=request.id: self._show_request_details(rid)).pack(side="left")
        ttk.Button(buttons, text="Diff", command=lambda rid=request.id: self._show_request_diff(rid)).pack(side="left", padx=(8, 0))

        review_state = "normal" if request.worktree_path else "disabled"
        ttk.Button(
            buttons,
            text="Review",
            state=review_state,
            command=lambda rid=request.id: self._run_async(f"Opening review for request {rid}...", self.controller.open_review_target, rid),
        ).pack(side="left", padx=(8, 0))

        push_state = "normal" if request.can_push else "disabled"
        ttk.Button(
            buttons,
            text="Push + Release" if self._project_requests_release(request.project_slug) else "Push",
            state=push_state,
            command=lambda rid=request.id: self._run_async(f"Pushing request {rid}...", self.controller.push_request, rid),
        ).pack(side="left", padx=(8, 0))

        retry_state = "normal" if request.can_retry else "disabled"
        ttk.Button(
            buttons,
            text="Retry",
            state=retry_state,
            command=lambda rid=request.id: self._run_async(f"Retrying request {rid}...", self.controller.retry_request, rid),
        ).pack(side="left", padx=(8, 0))

    def _set_auto_push(self, project_slug: str, enabled: bool) -> None:
        try:
            self.controller.set_project_auto_push(project_slug, enabled)
            self.status_var.set(f"Auto push for {project_slug} set to {enabled}.")
        except Exception as exc:  # noqa: BLE001
            self.status_var.set(f"Auto push update failed: {exc}")

    def _show_request_details(self, request_id: int) -> None:
        try:
            body = self.controller.get_request_detail_text(request_id)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Details unavailable", str(exc))
            return
        self._show_text_dialog(f"Request {request_id} details", body)

    def _show_request_diff(self, request_id: int) -> None:
        try:
            body = self.controller.get_request_diff_text(request_id)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Diff unavailable", str(exc))
            return
        self._show_text_dialog(f"Request {request_id} diff", body)

    def _show_text_dialog(self, title: str, body: str) -> None:
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.geometry("1000x700")
        text = scrolledtext.ScrolledText(dialog, wrap="word", font=("Consolas", 10))
        text.pack(fill="both", expand=True)
        text.insert("1.0", body)
        text.configure(state="disabled")

    def _run_async(self, starting_message: str, func, *args) -> None:
        self.status_var.set(starting_message)

        def target() -> None:
            try:
                result = func(*args)
                message = "Done."
                if isinstance(result, str) and result:
                    message = result
            except Exception as exc:  # noqa: BLE001
                message = f"Action failed: {exc}"
            self.after(0, lambda: self.status_var.set(message))

        threading.Thread(target=target, daemon=True).start()

    def _project_requests_release(self, project_slug: str) -> bool:
        return self.project_release_on_push_by_slug.get(project_slug, False)

    def _build_snapshot_signature(self, snapshots: list[ProjectSnapshot]):
        return tuple(
            (
                snapshot.project.slug,
                snapshot.project.auto_push,
                snapshot.project.release_on_push,
                snapshot.project.push_mode,
                snapshot.project.base_branch,
                tuple(
                    (
                        request.id,
                        request.status.value,
                        request.updated_at,
                        request.commit_sha,
                        request.release_version,
                        request.release_ready_notified_at,
                        request.manual_review_reason,
                        request.error_text,
                    )
                    for request in snapshot.requests
                ),
            )
            for snapshot in snapshots
        )

    def _handle_close(self) -> None:
        self.destroy()
