from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .types import SiteUnavailable


_SLUG = re.compile(r"^[a-z0-9-]+$")
PROTOCOL_VERSION = 3
REQUIRED_CAPABILITIES = frozenset({
    "command_phase", "execution_heartbeat", "submission_recovery", "write_authorization",
})
READ_ONLY_COMMANDS = frozenset({"check_submission", "submission_baseline", "recover_submission"})


class _BridgeHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class BrowserBridge:
    def __init__(self, token_path: Path, poll_interval_seconds: float = 0.5):
        token_path.parent.mkdir(parents=True, exist_ok=True)
        if token_path.exists():
            self.token = token_path.read_text(encoding="ascii").strip()
        else:
            self.token = secrets.token_urlsafe(32)
            token_path.write_text(self.token + "\n", encoding="ascii")
        self.pair_code = f"{secrets.randbelow(1_000_000):06d}"
        self.pair_expires = time.monotonic() + 600
        self.pair_attempts = 0
        self.cv = threading.Condition()
        self.bound_tab: int | None = None
        self.current: dict[str, Any] | None = None
        self.pending: dict[str, dict[str, Any]] = {}
        self.stopped = False
        self.on_phase = None
        self.poll_interval_ms = int(poll_interval_seconds * 1000)

    @staticmethod
    def require_protocol(page: dict[str, Any]) -> None:
        capabilities = page.get("capabilities")
        if (page.get("protocol_version") != PROTOCOL_VERSION
                or not isinstance(capabilities, list)
                or not all(isinstance(value, str) for value in capabilities)
                or not REQUIRED_CAPABILITIES.issubset(capabilities)
                or not isinstance(page.get("document_id"), str)
                or not page["document_id"]):
            raise SiteUnavailable(
                "Edge extension is outdated: reload Agent4PS in edge://extensions and refresh the "
                f"LeetCode problem tab (page protocol {page.get('protocol_version')!r}; required {PROTOCOL_VERSION})"
            )

    def pair(self, code: str) -> str | None:
        with self.cv:
            self.pair_attempts += 1
            if not self.pair_code or self.pair_attempts > 5 or time.monotonic() > self.pair_expires:
                return None
            if not hmac.compare_digest(code, self.pair_code):
                return None
            self.pair_code = ""
            return self.token

    def authorized(self, header: str | None) -> bool:
        return hmac.compare_digest(header or "", f"Bearer {self.token}")

    def hello(self, data: dict[str, Any]) -> dict[str, Any]:
        tab_id = data.get("tab_id")
        slug = data.get("slug")
        active = data.get("active") is True
        url = data.get("url") or ""
        if not isinstance(tab_id, int) or tab_id < 0:
            raise ValueError("invalid tab_id")
        if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
            raise ValueError("invalid problem slug")
        if not isinstance(url, str) or not url.startswith(f"https://leetcode.cn/problems/{slug}/"):
            raise ValueError("invalid problem URL")
        with self.cv:
            if self.bound_tab is None and active and isinstance(data.get("problem"), dict):
                self.bound_tab = tab_id
            if tab_id != self.bound_tab:
                return {"bound": False, "command": None, "poll_interval_ms": self.poll_interval_ms}
            self.current = {**data, "seen_at": time.monotonic()}
            self.cv.notify_all()
            command = None
            for entry in self.pending.values():
                queued = entry["command"]
                can_deliver = (active and data.get("ready") is not False
                               or queued["kind"] in READ_ONLY_COMMANDS)
                if can_deliver and not entry["delivered"] and queued.get("target_slug") == slug:
                    entry["delivered"] = True
                    entry["document_id"] = data.get("document_id")
                    entry["phase"] = "delivered"
                    entry["phase_at"] = time.monotonic()
                    command = {**queued, "document_id": data.get("document_id")}
                    break
            return {"bound": True, "command": command, "poll_interval_ms": self.poll_interval_ms}

    def phase(self, data: dict[str, Any]) -> None:
        command_id = data.get("command_id")
        phase = data.get("phase")
        with self.cv:
            entry = self.pending.get(command_id)
            if (data.get("tab_id") != self.bound_tab or not entry or not entry["delivered"]
                    or data.get("kind") != entry["command"]["kind"]
                    or data.get("slug") != entry["command"]["target_slug"]
                    or data.get("document_id") != entry["document_id"]
                    or phase not in {"started", "editor_ready", "triggering", "request_seen", "id_seen", "terminal"}):
                raise ValueError("invalid command phase")
            entry["phase"] = phase
            entry["phase_at"] = time.monotonic()
            self.cv.notify_all()
            callback = self.on_phase
        if callback:
            try:
                callback(entry["command"]["kind"], phase, command_id)
            except Exception:
                pass

    def command_active(self, data: dict[str, Any]) -> bool:
        with self.cv:
            entry = self.pending.get(data.get("command_id"))
            page = self.current or {}
            return bool(
                entry and entry["delivered"] and entry["command"]["kind"] == "run"
                and data.get("tab_id") == self.bound_tab
                and data.get("slug") == entry["command"]["target_slug"] == page.get("slug")
                and data.get("document_id") == entry.get("document_id") == page.get("document_id")
            )

    def result(self, data: dict[str, Any]) -> None:
        command_id = data.get("command_id")
        tab_id = data.get("tab_id")
        with self.cv:
            entry = self.pending.get(command_id)
            if tab_id != self.bound_tab or not entry or not entry["delivered"]:
                raise ValueError("unknown command result")
            reported_kind = data.get("command_kind", data.get("kind"))
            expected_kind = entry["command"]["kind"]
            if (reported_kind is not None and reported_kind != expected_kind
                    and not (expected_kind == "check_submission" and reported_kind == "submit")
                    or data.get("slug") is not None and data["slug"] != entry["command"]["target_slug"]
                    or data.get("document_id") is not None and data["document_id"] != entry["document_id"]):
                raise ValueError("command result does not match the delivered command")
            if entry["result"] is None:
                entry["result"] = data
                self.cv.notify_all()

    def wait_for_page(self, slug: str | None = None, timeout: float = 180,
                      since: float = 0, require_active: bool = True) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        with self.cv:
            while not self.stopped:
                page = self.current
                if (page and page.get("seen_at", 0) > since
                        and time.monotonic() - page.get("seen_at", 0) < 10
                        and (not require_active or page.get("active") is True)
                        and isinstance(page.get("problem"), dict)):
                    if slug is None or page.get("slug") == slug:
                        return dict(page)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SiteUnavailable("waiting for an active LeetCode problem tab timed out")
                self.cv.wait(min(remaining, 2))
        raise SiteUnavailable("browser bridge stopped")

    def call(self, kind: str, target_slug: str, payload: dict[str, Any] | None = None, timeout: float = 120) -> dict[str, Any]:
        command_id = secrets.token_hex(12)
        command = {"id": command_id, "kind": kind, "target_slug": target_slug, "payload": payload or {}}
        deadline = time.monotonic() + timeout
        with self.cv:
            self.pending[command_id] = {
                "command": command, "delivered": False, "result": None,
                "phase": "queued", "phase_at": time.monotonic(),
            }
            self.cv.notify_all()
            try:
                while not self.stopped:
                    entry = self.pending[command_id]
                    if entry["result"] is not None:
                        result = entry["result"]
                        if result.get("ok") is not True:
                            raise SiteUnavailable(str(result.get("error") or f"{kind} failed")[:500])
                        return result
                    page = self.current
                    if (kind not in READ_ONLY_COMMANDS | {"run"} and entry["delivered"] and page
                            and time.monotonic() - page.get("seen_at", 0) < 10
                            and page.get("active") is False):
                        raise SiteUnavailable(f"bound Edge tab became inactive during {kind}; current problem preserved")
                    if (entry["delivered"] and page and isinstance(page.get("problem"), dict)
                            and page["problem"].get("challenge") is True):
                        raise SiteUnavailable("LeetCode requested a verification challenge")
                    if (kind == "submit" and entry["delivered"] and page
                            and entry.get("document_id") and page.get("document_id")
                            and entry["document_id"] != page["document_id"]):
                        raise SiteUnavailable("submit page reloaded before the receipt arrived")
                    if (kind != "navigate" and page and page.get("slug") != target_slug
                            and time.monotonic() - page.get("seen_at", 0) < 10):
                        raise SiteUnavailable(f"bound Edge tab left {target_slug} during {kind}; current problem preserved")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        phase = entry["phase"]
                        raise SiteUnavailable(f"{kind} result could not be confirmed (last phase: {phase})")
                    self.cv.wait(min(remaining, 2))
            finally:
                self.pending.pop(command_id, None)
        raise SiteUnavailable("browser bridge stopped")

    def stop(self) -> None:
        with self.cv:
            self.stopped = True
            self.cv.notify_all()

    def status(self) -> dict[str, Any]:
        with self.cv:
            page = self.current or {}
            return {
                "connected": bool(page) and time.monotonic() - page.get("seen_at", 0) < 10,
                "bound_tab": self.bound_tab,
                "slug": page.get("slug"),
                "active": page.get("active") is True,
                "pending_action": [
                    {"kind": entry["command"]["kind"], "phase": entry["phase"],
                     "elapsed_seconds": round(time.monotonic() - entry["phase_at"], 1)}
                    for entry in self.pending.values()
                ],
            }


def make_server(host: str, port: int, bridge: BrowserBridge) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            return

        def _send(self, status: int, data: dict[str, Any]) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            origin = self.headers.get("Origin", "")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if re.fullmatch(r"chrome-extension://[a-p]{32}", origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:
            self._send(204, {})

        def _read(self) -> dict[str, Any]:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > 200_000:
                raise ValueError("invalid request size")
            value = json.loads(self.rfile.read(size))
            if not isinstance(value, dict):
                raise ValueError("JSON object required")
            return value

        def _auth(self) -> bool:
            if bridge.authorized(self.headers.get("Authorization")):
                return True
            self._send(401, {"error": "not paired"})
            return False

        def do_POST(self) -> None:
            try:
                if self.path == "/v1/pair":
                    token = bridge.pair(str(self._read().get("code", "")))
                    self._send(200 if token else 403, {"token": token} if token else {"error": "invalid pairing code"})
                    return
                if not self._auth():
                    return
                if self.path == "/v1/hello":
                    self._send(200, bridge.hello(self._read()))
                elif self.path == "/v1/command-active":
                    self._send(200, {"active": bridge.command_active(self._read())})
                elif self.path == "/v1/result":
                    bridge.result(self._read())
                    self._send(200, {"ok": True})
                elif self.path == "/v1/phase":
                    bridge.phase(self._read())
                    self._send(200, {"ok": True})
                else:
                    self._send(404, {"error": "not found"})
            except (ValueError, json.JSONDecodeError) as exc:
                self._send(400, {"error": str(exc)[:200]})

        def do_GET(self) -> None:
            if not self._auth():
                return
            if self.path == "/v1/status":
                self._send(200, bridge.status())
            else:
                self._send(404, {"error": "not found"})

    return _BridgeHTTPServer((host, port), Handler)
