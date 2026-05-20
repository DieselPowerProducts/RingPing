from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import urlparse


class WebhookServer:
    def __init__(
        self,
        host: str,
        port: int,
        on_payload: Callable[[dict], object | None],
        verification_token: str = "",
        on_training_wake: Callable[[], object | None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.on_payload = on_payload
        self.verification_token = verification_token
        self.on_training_wake = on_training_wake
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        handler = self._build_handler()
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="ringping-webhook")
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def _send_cors_headers(self) -> None:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "content-type")

            def do_OPTIONS(self) -> None:
                if urlparse(self.path).path == "/ringping/training/wake":
                    self.send_response(204)
                    self._send_cors_headers()
                    self.end_headers()
                    return
                self.send_error(404)

            def do_GET(self) -> None:
                if urlparse(self.path).path == "/health":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status":"ok"}')
                    return
                self.send_error(404)

            def do_POST(self) -> None:
                path = urlparse(self.path).path
                if path == "/ringping/training/wake":
                    if parent.on_training_wake is not None:
                        parent.on_training_wake()
                    self.send_response(202)
                    self._send_cors_headers()
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"accepted":true}')
                    return

                if path != "/ringcentral/webhook":
                    self.send_error(404)
                    return

                verification_header = self.headers.get("Verification-Token", "")
                if parent.verification_token and verification_header and verification_header != parent.verification_token:
                    self.send_error(403, "Verification token mismatch")
                    return

                validation_header = self.headers.get("Validation-Token")
                if validation_header:
                    self.send_response(200)
                    self.send_header("Validation-Token", validation_header)
                    self.end_headers()
                    return

                content_length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(content_length) if content_length else b""
                if not raw_body:
                    self.send_response(202)
                    self.end_headers()
                    return

                try:
                    payload = json.loads(raw_body.decode("utf-8"))
                except json.JSONDecodeError:
                    self.send_error(400, "Invalid JSON")
                    return

                parent.on_payload(payload)
                self.send_response(202)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"accepted":true}')

        return Handler
