"""The stand-in for SwitchBot's update server. Plain HTTP, standard library only.

What a plug asks for, in order:
  1. Stock firmware, after the OTA trigger: some *.bin under wohand.com. It gets app.bin,
     the flasher. (event "stage1")
  2. The flasher, running from the first OTA slot, asks for /app.bin again to copy itself to
     the second slot, so it can overwrite the start of flash. (event "flip")
  3. The flasher asks /payload.md5 ("<md5> <size>"), then /payload.bin, the ESPHome
     factory image, and writes it from 0x0. (events "payload_md5", "payload_start",
     "payload_done" or "payload_aborted")

GET /events?since=N returns what happened, so a Bluetooth flasher on another computer can
follow along (switchback convert --server http://this-box).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from switchback import image
from switchback.protocol import normalize_mac

EVENT_HISTORY = 1000
Event = dict[str, Any]


def arp_lookup(ip: str) -> str | None:
    """The MAC for ip from this computer's ARP table. Only works on the same subnet."""
    try:
        arp = Path("/proc/net/arp")
        if arp.exists():
            for row in arp.read_text().splitlines()[1:]:
                cols = row.split()
                if len(cols) >= 4 and cols[0] == ip and cols[3] != "00:00:00:00:00:00":
                    return normalize_mac(cols[3])
            return None
        out = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for row in out.splitlines():
        if ip in re.split(r"[\s()]+", row):
            found = re.search(r"([0-9a-fA-F]{1,2}[:-]){5}[0-9a-fA-F]{1,2}", row)
            if found:
                return normalize_mac(found.group(0))
    return None


class EventLog:
    def __init__(self, on_event: Callable[[Event], None] | None = None):
        self._events: list[Event] = []
        self._lock = threading.Lock()
        self.seq = 0
        self.on_event = on_event

    def add(self, event: str, ip: str, mac: str | None, path: str, **extra: Any) -> Event:
        with self._lock:
            self.seq += 1
            entry: Event = {
                "seq": self.seq,
                "time": datetime.datetime.now().isoformat(timespec="seconds"),
                "event": event,
                "ip": ip,
                "mac": mac,
                "path": path,
                **extra,
            }
            self._events.append(entry)
            del self._events[:-EVENT_HISTORY]
        if self.on_event:
            self.on_event(entry)
        return entry

    def since(self, seq: int) -> tuple[int, list[Event]]:
        with self._lock:
            return self.seq, [e for e in self._events if e["seq"] > seq]


class Payloads:
    """What the server hands out. Checked once at load so a bad file never reaches a plug."""

    def __init__(self, app: bytes, portal: bytes, direct: bytes | None = None):
        image.check_flasher(app)
        image.check_payload(portal, "portal payload")
        if direct is not None:
            image.check_payload(direct, "direct payload")
            if not image.has_placeholders(direct) or not image.verify_image(direct):
                raise image.ImageError("direct payload has no Wi-Fi placeholders or fails its own checksum")
        self.app = app
        self.portal = portal
        self.direct = direct
        self.mode = "portal"
        self.payload = portal
        self.md5 = hashlib.md5(portal).hexdigest()

    @classmethod
    def from_files(cls, app: Path, portal: Path, direct: Path | None) -> Payloads:
        return cls(app.read_bytes(), portal.read_bytes(), direct.read_bytes() if direct else None)

    def select(
        self,
        mode: str,
        ssid: str | None = None,
        password: str | None = None,
        name: str | None = None,
        friendly_name: str | None = None,
    ) -> None:
        """Serve the portal build, or the direct build with this Wi-Fi patched in.

        With a name, that name is patched in too, so the plug boots with it.
        """
        if mode == "direct":
            if self.direct is None:
                raise image.ImageError("direct mode needs the direct payload")
            if not ssid:
                raise ValueError("direct mode needs a Wi-Fi name")
            payload = image.patch_wifi(self.direct, ssid, password or "")
        elif mode == "portal":
            payload = self.portal
        else:
            raise ValueError(f"unknown mode {mode!r}")
        if name:
            payload = image.patch_name(payload, name, friendly_name or name)
        if payload is not self.portal and not image.verify_image(payload):
            raise image.ImageError("the patched image failed its own checksum")
        self.payload = payload
        self.mode = mode
        self.md5 = hashlib.md5(self.payload).hexdigest()

    def can_name(self, mode: str) -> bool:
        base = self.direct if mode == "direct" else self.portal
        return base is not None and image.has_name_slots(base)


def _make_handler(payloads: Payloads, events: EventLog, arp: Callable[[str], str | None]) -> type:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "switchback"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _emit(self, event: str, path: str, **extra: Any) -> None:
            ip = self.client_address[0].removeprefix("::ffff:")
            # The flasher sends its MAC. The stock firmware does not, so fall back to ARP.
            mac = normalize_mac(self.headers.get("X-Device-MAC")) or arp(ip)
            events.add(event, ip, mac, path, **extra)

        def _send(self, body: bytes, content_type: str = "application/octet-stream", close: bool = False) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            path = url.path
            if path == "/events":
                try:
                    since = int(parse_qs(url.query).get("since", ["0"])[0])
                except ValueError:
                    since = 0
                seq, found = events.since(since)
                self._send(json.dumps({"seq": seq, "events": found}).encode(), "application/json")
            elif path == "/payload.md5":
                self._emit("payload_md5", path)
                self._send(f"{payloads.md5} {len(payloads.payload)}\n".encode(), "text/plain")
            elif path == "/payload.bin":
                body = payloads.payload
                started = time.monotonic()
                self._emit("payload_start", path, size=len(body))
                ok = False
                try:
                    self._send(body, close=True)
                    self.wfile.flush()
                    # Wait for the plug to hang up, so "done" means the plug has it all and not
                    # just that the kernel buffered it. A plug that failed its check comes back
                    # for payload.md5 within seconds; one that verified reboots.
                    self.connection.shutdown(socket.SHUT_WR)
                    self.connection.settimeout(120)
                    while self.connection.recv(1024):
                        pass
                    ok = True
                except OSError:
                    ok = False
                seconds = round(time.monotonic() - started)
                self._emit("payload_done" if ok else "payload_aborted", path, seconds=seconds)
            else:
                if path == "/app.bin":
                    self._emit("flip", path)
                elif path.endswith(".bin"):
                    self._emit("stage1", path)
                else:
                    self._emit("other", path)
                self._send(payloads.app)

    return Handler


class FirmwareServer:
    def __init__(
        self,
        payloads: Payloads,
        port: int = 80,
        bind: str = "",
        on_event: Callable[[Event], None] | None = None,
        arp: Callable[[str], str | None] = arp_lookup,
    ):
        self.payloads = payloads
        self.events = EventLog(on_event)
        self.httpd = ThreadingHTTPServer((bind, port), _make_handler(payloads, self.events, arp))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="switchback-http", daemon=True)

    def start(self) -> FirmwareServer:
        self._thread.start()
        return self

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
