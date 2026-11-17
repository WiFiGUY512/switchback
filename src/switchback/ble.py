"""Bluetooth: find stock Plug Minis, set their Wi-Fi, and start the stock OTA."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from switchback import protocol as p
from switchback.ui import PROGRESS, log

BLE_ERRORS = (BleakError, OSError, asyncio.TimeoutError, EOFError)

# How long a status may sit in a state that means "this will not work" before we stop.
SSID_NOT_FOUND_LIMIT = 45
NO_ADDRESS_LIMIT = 75


class Failed(Exception):
    pass


class DeadTrigger(Failed):
    """The plug refuses the Bluetooth OTA trigger. Newer stock firmware does this."""


class Plug:
    def __init__(self, device: Any, adv: Any):
        self.device = device
        mfr = adv.manufacturer_data[p.WOAN_COMPANY_ID]
        service = adv.service_data.get(p.SWITCHBOT_SERVICE_DATA)
        self.model = chr(service[0] & 0x7F) if service else None
        self.rssi = adv.rssi
        self.is_plug_mini = self.model in p.PLUG_MINI_MODELS or (self.model is None and len(mfr) == p.PLUG_MINI_MFR_LEN)
        # macOS hides the real address behind a UUID, but the advertisement carries it.
        address = device.address.lower()
        if address.count(":") != 5 and len(mfr) >= 6:
            address = mfr[:6].hex(":")
        self.ble_mac = address
        # ESP32 convention: the Bluetooth MAC is the Wi-Fi station MAC plus 2.
        self.wifi_mac = p.mac_offset(address, -2)
        self.name = p.esphome_name(self.wifi_mac)

    def __str__(self) -> str:
        return f"{self.ble_mac} (Wi-Fi {self.wifi_mac}, model {self.model or '?'}, {self.rssi} dBm)"


async def scan(scan_time: float = 10.0, only: str | None = None, quiet: bool = False) -> list[Plug]:
    """The Plug Minis in range, strongest signal first."""
    only = p.normalize_mac(only) if only else None
    found = await BleakScanner.discover(timeout=scan_time, return_adv=True)
    plugs = []
    for device, adv in found.values():
        if p.WOAN_COMPANY_ID not in adv.manufacturer_data:
            continue
        plug = Plug(device, adv)
        targeted = only is not None and plug.ble_mac == only
        if only and not targeted:
            continue
        if not plug.is_plug_mini and not targeted:
            if not quiet:
                log(f"skipping a SwitchBot device that is not a Plug Mini: {plug}")
            continue
        plugs.append(plug)
    return sorted(plugs, key=lambda plug: plug.rssi, reverse=True)


class Link:
    """One BLE connection with the notify characteristic wired to a queue."""

    def __init__(self, client: BleakClient):
        self.client = client
        self.replies: asyncio.Queue[bytes] = asyncio.Queue()

    async def open(self) -> Link:
        await self.client.start_notify(p.NOTIFY_CHAR, lambda _c, data: self.replies.put_nowait(bytes(data)))
        return self

    async def send(self, data: bytes, timeout: float = 5.0, response: bool = True) -> bytes:
        while not self.replies.empty():
            self.replies.get_nowait()
        await self.client.write_gatt_char(p.WRITE_CHAR, data, response=response)
        try:
            return await asyncio.wait_for(self.replies.get(), timeout)
        except asyncio.TimeoutError:
            return b""


async def _send_pages(link: Link, pages: list[bytes]) -> bytes:
    reply = b""
    for page in pages:
        reply = await link.send(page)
        if not p.is_ack(reply):
            break
    return reply


async def provision_once(device: Any, ssid: str, password: str, connect_timeout: float = 120.0) -> p.WifiStatus:
    """Set the plug's Wi-Fi over Bluetooth and wait until it reports connected."""
    ssid_pages = p.ssid_pages(ssid)
    pass_pages = p.password_pages(password)
    async with BleakClient(device, timeout=30) as client:
        link = await Link(client).open()
        await link.send(p.CMD_PROV_INIT)
        await link.send(p.CMD_PROV_BEGIN)
        r_ssid = await _send_pages(link, ssid_pages)
        r_pass = await _send_pages(link, pass_pages)
        r_conn = await link.send(p.CMD_PROV_CONNECT)
        if not (p.is_ack(r_ssid) and p.is_ack(r_pass) and p.is_ack(r_conn)):
            raise Failed(
                "the plug did not accept the Wi-Fi details "
                f"(name {_hex(r_ssid)}, password {_hex(r_pass)}, connect {_hex(r_conn)})"
            )
        log(f"Wi-Fi details sent ({len(ssid_pages)} + {len(pass_pages)} pages). Waiting for the plug to join.")
        started = time.monotonic()
        state_since = started
        last: p.WifiStatus | None = None
        while time.monotonic() - started < connect_timeout:
            status = p.parse_status(await link.send(p.CMD_PROV_STATUS))
            if status.done:
                log(f"the plug is on Wi-Fi [{_hex(status.raw)}]")
                return status
            if last is None or status.state is not last.state:
                state_since = time.monotonic()
                if status.state is not p.WifiState.NO_REPLY:
                    log(f"joining Wi-Fi: {status.state.value} (status {status.code or '?'})")
            last = status
            stuck = time.monotonic() - state_since
            if status.state is p.WifiState.SSID_NOT_FOUND and stuck > SSID_NOT_FOUND_LIMIT:
                raise Failed(
                    f"the plug cannot find the network {ssid!r}. It only sees 2.4 GHz. "
                    "Check the exact name (it is case sensitive) and that the access point is in range."
                )
            if status.state is p.WifiState.ASSOCIATED and stuck > NO_ADDRESS_LIMIT:
                raise Failed(
                    f"the plug joined {ssid!r} but never got an IP address. Stock firmware has been seen "
                    "failing DHCP on some VLANs. Flash over a plain, flat 2.4 GHz network instead."
                )
            await asyncio.sleep(2)
    raise Failed(f"the plug did not join {ssid!r} in time. Check the Wi-Fi name and password.")


async def provision(device: Any, ssid: str, password: str, attempts: int = 3) -> p.WifiStatus:
    """provision_once with retries. Windows Bluetooth drops the odd connection; this is safe to repeat."""
    for attempt in range(1, attempts + 1):
        try:
            return await provision_once(device, ssid, password)
        except BLE_ERRORS as error:
            if attempt == attempts:
                raise Failed(f"Bluetooth to the plug kept dropping ({type(error).__name__}: {error})") from error
            log(f"Bluetooth dropped ({type(error).__name__}), trying again")
            await asyncio.sleep(3)
    raise AssertionError("unreachable")


def _hex(data: bytes | None) -> str:
    return data.hex(" ") if data else "no reply"


async def trigger_ota(link: Link, fw_byte: int) -> int:
    """Send the OTA trigger, trying other version bytes if the plug says 0c. Returns the byte used.

    A refusal on one byte is not always final: one plug refused 0C after being provisioned
    twice in one power cycle and took 0D at once. If every byte is refused, the plug runs the
    newer stock firmware that ignores this trigger (seen from about v2.7).
    """
    tried = []
    for byte in (fw_byte, *[b for b in p.FALLBACK_FW_BYTES if b != fw_byte]):
        reply = await link.send(p.ota_start(byte), response=False)
        tried.append(f"{byte:02X}")
        log(f"OTA start with version byte {byte:02X}: {_hex(reply)}")
        if not reply or reply[0] != p.BUSY:
            return byte
        await asyncio.sleep(1)
    raise DeadTrigger(
        f"the plug refused the Bluetooth update trigger with every version byte tried ({', '.join(tried)}). "
        "It most likely runs newer stock firmware that ignores this trigger. It is unharmed. "
        "Use the app re-onboard path instead: keep 'switchback serve' running with wohand.com pointed here, "
        "reset the plug, and add it again in the SwitchBot app. See the README, 'Newer firmware'."
    )


async def run_stage1(
    plug: Plug,
    fw_byte: int,
    update: Callable[[], Awaitable[None]],
    saw: Callable[..., bool],
    timeout: float = 900.0,
) -> None:
    """Start the stock OTA and keep asking for progress until the flasher reaches the server.

    The stock firmware drops the Bluetooth link while it downloads and again when it reboots,
    so a lost link means nothing by itself. The only proof that stage 1 is over is the
    flasher's own request reaching the server. A plug that is never asked for progress after
    the download still reboots into the flasher, about ten minutes later.
    """
    started = time.monotonic()
    triggered = False
    link_lost = False
    last_reply = b""
    PROGRESS.phase("1/3", "stock OTA, waiting for reboot", 630)

    def over() -> bool:
        if saw("flip", "payload_md5", "payload_start", "payload_done"):
            return True
        if triggered and not saw("stage1") and time.monotonic() - started > 75:
            raise Failed(
                "the plug never fetched anything from the server. Check that it is on Wi-Fi, that its DNS "
                "sends wohand.com to the server, and that nothing blocks port 80 to the server."
            )
        return False

    while time.monotonic() - started < timeout:
        try:
            async with BleakClient(plug.device, timeout=10) as client:
                link = await Link(client).open()
                if not triggered:
                    await trigger_ota(link, fw_byte)
                    triggered = True
                while client.is_connected and time.monotonic() - started < timeout:
                    await asyncio.sleep(2)
                    await update()
                    if over():
                        return
                    reply = await link.send(p.CMD_OTA_PROGRESS, response=False)
                    if reply and reply != last_reply:
                        last_reply = reply
                        percent = f"{reply[1]}%" if len(reply) >= 2 else "no percentage"
                        PROGRESS.note = f"dl {percent}"
                        log(f"stage 1 progress: {percent} [{_hex(reply)}]")
        except DeadTrigger:
            raise
        except BLE_ERRORS:
            if triggered and not link_lost:
                log("Bluetooth link lost. That is normal while the plug downloads. Still watching.")
            link_lost = True
        else:
            link_lost = False
        await update()
        if over():
            return
        await asyncio.sleep(3)
    raise Failed("stage 1 did not finish in time. The plug is still on stock firmware if 'switchback scan' finds it.")
