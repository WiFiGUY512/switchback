"""The whole conversion of one plug: Wi-Fi over Bluetooth, the OTA trigger, then follow the server."""

from __future__ import annotations

import asyncio
import csv
import datetime
import json
import socket
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from switchback import ble, label
from switchback.ble import Failed, Plug
from switchback.server import FirmwareServer
from switchback.ui import PROGRESS, log

ESPHOME_SERVICE = "_esphomelib._tcp.local."
ESPHOME_API_PORT = 6053
# A flasher that failed its verify comes back for payload.md5 well inside this.
SETTLE_SECONDS = 60


class Feed(Protocol):
    def poll(self) -> list[dict[str, Any]]: ...
    def describe(self) -> str: ...


class LocalFeed:
    """Events from the server running in this process."""

    def __init__(self, server: FirmwareServer):
        self.server = server
        self.seq = server.events.seq

    def poll(self) -> list[dict[str, Any]]:
        self.seq, found = self.server.events.since(self.seq)
        return found

    def describe(self) -> str:
        pl = self.server.payloads
        return f"{pl.mode} payload, {len(pl.payload)} bytes, md5 {pl.md5}"


class HttpFeed:
    """Events from a server on another box (switchback serve), read from its /events."""

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.seq = 0

    def _get(self, path: str) -> str:
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return str(response.read().decode())

    def poll(self) -> list[dict[str, Any]]:
        feed = json.loads(self._get(f"/events?since={self.seq}"))
        self.seq = feed["seq"]
        return list(feed["events"])

    def describe(self) -> str:
        md5, size = self._get("/payload.md5").split()
        self.poll()  # skip our own request and anything older
        return f"server at {self.base}: payload {size} bytes, md5 {md5}"


class PlugEvents:
    """Picks one plug's events out of the feed and logs them as they arrive."""

    def __init__(self, feed: Feed, plug: Plug, portal: bool):
        self.feed = feed
        self.portal = portal
        self.mac = plug.wifi_mac
        self.ips: set[str] = set()
        self.seen: list[str] = []
        self.done_at: float | None = None

    async def update(self) -> None:
        try:
            events = await asyncio.to_thread(self.feed.poll)
        except (OSError, ValueError) as error:
            log(f"server event feed unavailable: {error}")
            return
        for event in events:
            # The stock firmware sends no MAC, and ARP only knows plugs on this subnet.
            unattributed = event["mac"] is None and (event["ip"] in self.ips or event["event"] == "stage1")
            if event["mac"] != self.mac and not unattributed:
                continue
            if event["event"] == "other":
                continue
            self.ips.add(event["ip"])
            self.seen.append(event["event"])
            detail = f" after {event['seconds']}s" if "seconds" in event else ""
            log(f"server: {event['event']} from {event['ip']}{detail}")
            name = event["event"]
            if name == "payload_done":
                self.done_at = time.monotonic()
                if self.portal:
                    PROGRESS.phase("3/3", "checking it stays quiet", SETTLE_SECONDS)
                else:
                    PROGRESS.phase("3/3", "verifying, booting ESPHome", 40)
            elif name in ("payload_md5", "payload_start"):
                if name == "payload_md5" and "payload_start" in self.seen:
                    log("the flasher is retrying. Leave the plug powered.")
                self.done_at = None
                PROGRESS.phase("2/3", "flasher writing ESPHome", 20)
            elif name == "flip":
                PROGRESS.phase("2/3", "flasher moving itself", 30)

    def saw(self, *names: str) -> bool:
        return any(name in self.seen for name in names)


@dataclass
class Options:
    mode: str  # "portal" or "direct"
    wifi_ssid: str | None
    wifi_pass: str
    fw_byte: int
    stage1_timeout: float = 900.0
    timeout: float = 600.0
    domain: str | None = None
    log_file: Path | None = None
    printer: str | None = None


def api_reachable(host: str) -> bool:
    try:
        with socket.create_connection((host, ESPHOME_API_PORT), timeout=2):
            return True
    except OSError:
        return False


async def wait_for_esphome(plug: Plug, tracker: PlugEvents, opts: Options) -> tuple[str, str]:
    """Follow stage 2 until the plug is up as ESPHome. Returns (address, reported MAC)."""
    from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

    zc = AsyncZeroconf()
    try:
        deadline = time.monotonic() + opts.timeout
        while time.monotonic() < deadline:
            await tracker.update()
            info = AsyncServiceInfo(ESPHOME_SERVICE, f"{plug.name}.{ESPHOME_SERVICE}")
            if await info.async_request(zc.zeroconf, 2000):
                return ", ".join(info.parsed_addresses()), (info.properties.get(b"mac") or b"").decode()
            # mDNS does not cross VLANs. Where DHCP registers names in DNS, this does.
            if opts.domain:
                host = plug.name + opts.domain
                if await asyncio.to_thread(api_reachable, host):
                    return host, ""
            # Once the flasher is done, try the address it used; the plug usually gets it back.
            if tracker.done_at:
                for ip in sorted(tracker.ips):
                    if await asyncio.to_thread(api_reachable, ip):
                        return ip, ""
    finally:
        await zc.async_close()
    if tracker.done_at:
        raise Failed(
            f"{plug.name} wrote ESPHome but did not join {opts.wifi_ssid!r}. The Wi-Fi name or password is most "
            f"likely wrong, so the plug opened its own hotspot instead. Join the '{plug.name}' Wi-Fi and use "
            "its setup page (http://192.168.4.1) to enter the right network. The plug itself is fine."
        )
    raise Failed(
        f"{plug.name} never showed up on the network. Leave the plug powered and check the server log. "
        "If the plug is on another VLAN, mDNS cannot see it: use --domain."
    )


async def wait_for_flash(plug: Plug, tracker: PlugEvents, opts: Options) -> None:
    """A portal payload never joins Wi-Fi, so success is the flasher finishing and not coming back."""
    deadline = time.monotonic() + opts.timeout
    while time.monotonic() < deadline:
        await tracker.update()
        if tracker.done_at and time.monotonic() - tracker.done_at > SETTLE_SECONDS:
            return
        await asyncio.sleep(2)
    raise Failed(f"the flasher never finished writing {plug.name}. Leave the plug powered and check the server log.")


def write_log(path: Path, plug: Plug, address: str) -> None:
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if new:
            writer.writerow(["time", "name", "wifi_mac", "ble_mac", "address"])
        writer.writerow(
            [datetime.datetime.now().isoformat(timespec="seconds"), plug.name, plug.wifi_mac, plug.ble_mac, address]
        )


async def convert(plug: Plug, feed: Feed, opts: Options) -> str:
    """Convert one plug. Returns where it ended up."""
    await asyncio.to_thread(feed.poll)  # skip anything already in the feed
    portal = opts.mode == "portal"
    tracker = PlugEvents(feed, plug, portal)
    log(f"converting {plug}, it will be {plug.name}")
    PROGRESS.start()
    try:
        if opts.wifi_ssid:
            log(f"putting the plug on Wi-Fi {opts.wifi_ssid!r} over Bluetooth")
            await ble.provision(plug.device, opts.wifi_ssid, opts.wifi_pass)
        await ble.run_stage1(plug, opts.fw_byte, tracker.update, tracker.saw, opts.stage1_timeout)
        log("stage 1 done. The flasher is writing ESPHome. Do not unplug the plug.")
        if portal:
            await wait_for_flash(plug, tracker, opts)
            address = "hotspot"
            log(f"{plug.name} is flashed. Join its hotspot, also named {plug.name}, to put it on Wi-Fi.")
        else:
            address, reported = await wait_for_esphome(plug, tracker, opts)
            log(f"{plug.name} is up at {address}")
            if reported and reported.lower() != plug.wifi_mac.replace(":", ""):
                log(f"note: it reports MAC {reported}, expected {plug.wifi_mac}")
    finally:
        PROGRESS.stop()
    log("Leave it powered for at least another minute. A power cut right after an update can roll it back.")

    if opts.log_file:
        write_log(opts.log_file, plug, address)
        log(f"logged to {opts.log_file}")
    if opts.printer:
        try:
            label.send(opts.printer, label.render(plug.ble_mac, plug.wifi_mac))
            log(f"label sent to {opts.printer}")
        except OSError as error:
            log(f"label not printed: {error}")
    return address
