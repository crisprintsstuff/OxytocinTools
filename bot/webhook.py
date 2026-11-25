"""Optional lightweight HTTP server to trigger immediate polls from GitHub webhooks."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from typing import Callable

from aiohttp import web

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 5 * 1024 * 1024
TRIGGER_EVENTS = {
    "push",
    "release",
    "issues",
    "issue_comment",
    "pull_request",
    "pull_request_review",
    "check_run",
    "check_suite",
    "status",
    "workflow_run",
}


def verify_signature(payload: bytes, signature_header: str | None, secret: str) -> bool:
    """Verify GitHub's ``X-Hub-Signature-256`` header against ``secret``."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


class WebhookServer:
    def __init__(self, *, host: str, port: int, on_trigger: Callable[[], None],
                 secret: str | None = None, path: str = "/webhook",
                 debounce_seconds: float = 2.0):
        self.host = host
        self.port = port
        self.on_trigger = on_trigger
        self.secret = secret
        self.path = path if path.startswith("/") else "/" + path
        self.debounce_seconds = debounce_seconds
        self._runner: web.AppRunner | None = None
        self._debounce_task: asyncio.Task[None] | None = None

    def build_app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_BODY_BYTES)
        app.router.add_get("/health", self._health)
        app.router.add_post(self.path, self._handle_webhook)
        return app

    async def start(self) -> int:
        app = self.build_app()
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        bound_port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        log.info("GitHub webhook listener running on http://%s:%d%s",
                 self.host, bound_port, self.path)
        return bound_port

    async def stop(self) -> None:
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
            try:
                await self._debounce_task
            except asyncio.CancelledError:
                pass
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    def _schedule_trigger(self) -> None:
        if self._debounce_task is not None and not self._debounce_task.done():
            return

        async def _fire() -> None:
            await asyncio.sleep(self.debounce_seconds)
            self.on_trigger()

        self._debounce_task = asyncio.create_task(_fire(), name="webhook-trigger")

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        try:
            body = await request.read()
        except web.HTTPRequestEntityTooLarge:
            return web.json_response({"error": "payload too large"}, status=413)

        if self.secret:
            sig = request.headers.get("X-Hub-Signature-256")
            if not verify_signature(body, sig, self.secret):
                return web.json_response({"error": "invalid signature"}, status=401)

        event = request.headers.get("X-GitHub-Event", "")
        if event == "ping":
            return web.json_response({"ok": True, "event": "ping"})
        if event in TRIGGER_EVENTS:
            self._schedule_trigger()
            return web.json_response({"ok": True, "triggered": True, "event": event}, status=202)
        return web.json_response({"ok": True, "triggered": False, "event": event})
