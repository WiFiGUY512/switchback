"""The SwitchBot Plug Mini Bluetooth protocol, as far as Switchback needs it.

Everything here is pure: it builds bytes and reads replies. The Bluetooth I/O is in ble.py.

All commands go to one GATT characteristic and start with 57 0f. Replies come back as
notifications on a second characteristic. Everything is plaintext.

Wi-Fi provisioning (decoded from the stock app's own traffic):

    57 0f 95                     init                -> 05
    57 0f 0c 00                  begin provisioning  -> 01
    57 0f 01 <len> <page> <data> SSID, paged         -> 01 per page
    57 0f 02 <len> <page> <data> password, paged     -> 01 per page
    57 0f 03                     save and connect    -> 01
    57 0f 04                     status poll         -> 0c <2 ascii digits> ... or 01 35 31 <mac>

The plug keeps only 11 value bytes per SSID or password write. A longer value has to be
sent as several pages, each carrying the TOTAL length and its page index. Sent as one page,
the value is cut at byte 11 and the rest of the plug's buffer fills in the tail (a 14 byte
SSID went out over the air with the last 3 bytes of the factory default SSID).

OTA trigger: 57 0f 0a 01 <version byte> -> 01 accepted, 0c refused. Progress: 57 0f 0b.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

WOAN_COMPANY_ID = 0x0969
SWITCHBOT_SERVICE_DATA = "0000fd3d-0000-1000-8000-00805f9b34fb"
# 'g' and 'j' in normal mode. A plug in programming mode (fast blink) advertises 'G'
# instead, so a plug picked with --only is accepted whatever its model byte says.
PLUG_MINI_MODELS = ("g", "j")
PLUG_MINI_MFR_LEN = 12  # fallback when the scan response with the model byte is missed

WRITE_CHAR = "cba20002-224d-11e6-9fb8-0002a5d5c51b"
NOTIFY_CHAR = "cba20003-224d-11e6-9fb8-0002a5d5c51b"

CMD_PROV_INIT = bytes.fromhex("570f95")
CMD_PROV_BEGIN = bytes.fromhex("570f0c00")
CMD_PROV_SSID = bytes.fromhex("570f01")
CMD_PROV_PASS = bytes.fromhex("570f02")
CMD_PROV_CONNECT = bytes.fromhex("570f03")
CMD_PROV_STATUS = bytes.fromhex("570f04")
CMD_OTA_START = bytes.fromhex("570f0a01")  # followed by the firmware version byte
CMD_OTA_PROGRESS = bytes.fromhex("570f0b")

PROV_PAGE = 11
MAX_SSID = 32
MAX_PASSWORD = 63

ACK = 0x01
BUSY = 0x0C

# Version bytes for the OTA trigger. 0C is v1.2, 0D v1.3, 0E v1.4. The byte has to differ
# from the version the plug runs now. A plug that refuses one byte sometimes takes another.
DEFAULT_FW_BYTE = 0x0C
FALLBACK_FW_BYTES = (0x0D, 0x0E)


def page_value(prefix: bytes, value: bytes) -> list[bytes]:
    """Split one SSID or password write into the pages the plug expects.

    Each page is prefix + <total length> + <page index> + <up to 11 value bytes>.
    An empty value is still one page, so an open network sends an empty password.
    """
    if len(value) > 255:
        raise ValueError("value too long to page")
    pages = []
    for index, start in enumerate(range(0, max(len(value), 1), PROV_PAGE)):
        pages.append(prefix + bytes([len(value), index]) + value[start : start + PROV_PAGE])
    return pages


def ssid_pages(ssid: str) -> list[bytes]:
    raw = ssid.encode("utf-8")
    if not raw:
        raise ValueError("the Wi-Fi name is empty")
    if len(raw) > MAX_SSID:
        raise ValueError(f"the Wi-Fi name is {len(raw)} bytes, the limit is {MAX_SSID}")
    return page_value(CMD_PROV_SSID, raw)


def password_pages(password: str) -> list[bytes]:
    raw = password.encode("utf-8")
    if len(raw) > MAX_PASSWORD:
        raise ValueError(f"the Wi-Fi password is {len(raw)} bytes, the limit is {MAX_PASSWORD}")
    return page_value(CMD_PROV_PASS, raw)


def ota_start(fw_byte: int) -> bytes:
    if not 0 <= fw_byte <= 0xFF:
        raise ValueError("version byte must be 00 to FF")
    return CMD_OTA_START + bytes([fw_byte])


def is_ack(reply: bytes | None) -> bool:
    return bool(reply) and reply[0] == ACK  # type: ignore[index]


class WifiState(enum.Enum):
    CONNECTED = "connected"
    CONNECTING = "connecting"
    REPROVISIONING = "re-provisioning"
    SSID_NOT_FOUND = "network not found"
    ASSOCIATED = "associated, waiting for an address"
    NO_REPLY = "no reply"
    UNKNOWN = "unknown"


_CODES = {
    "10": WifiState.CONNECTING,
    "11": WifiState.REPROVISIONING,
    "30": WifiState.SSID_NOT_FOUND,
    "40": WifiState.SSID_NOT_FOUND,
    "50": WifiState.ASSOCIATED,
    "51": WifiState.CONNECTED,
}


@dataclass(frozen=True)
class WifiStatus:
    state: WifiState
    code: str
    mac: str | None
    raw: bytes

    @property
    def done(self) -> bool:
        return self.state is WifiState.CONNECTED


def parse_status(reply: bytes | None) -> WifiStatus:
    """Read a 57 0f 04 status reply.

    0c <2 ascii digits> <...> while it works on it, 01 35 31 <6 byte MAC> once connected.
    30 and 40 alternate in a loop when the plug cannot find the network it was given;
    that is a retry loop, not progress.
    """
    if not reply:
        return WifiStatus(WifiState.NO_REPLY, "", None, b"")
    code = reply[1:3].decode("ascii", "replace") if len(reply) >= 3 else ""
    mac = reply[3:9].hex(":") if len(reply) >= 9 else None
    if reply[0] == ACK:
        # An 01 prefix is the final answer; the digits read "51" on every plug seen so far.
        return WifiStatus(WifiState.CONNECTED, code, mac, bytes(reply))
    state = _CODES.get(code, WifiState.UNKNOWN)
    if state is WifiState.CONNECTED:  # "51" without the 01 prefix is not final yet
        state = WifiState.CONNECTING
    return WifiStatus(state, code, mac, bytes(reply))


def mac_offset(mac: str, delta: int) -> str:
    value = (int(mac.replace(":", "").replace("-", ""), 16) + delta) % (1 << 48)
    text = f"{value:012x}"
    return ":".join(text[i : i + 2] for i in range(0, 12, 2))


def normalize_mac(text: str | None) -> str | None:
    """aa:bb:cc:dd:ee:ff in lower case, or None if text is not a MAC address."""
    if not text:
        return None
    parts = text.strip().lower().replace("-", ":").split(":")
    if len(parts) != 6 or not all(1 <= len(p) <= 2 and all(c in "0123456789abcdef" for c in p) for p in parts):
        return None
    return ":".join(p.zfill(2) for p in parts)


def esphome_name(wifi_mac: str, prefix: str = "sbplug") -> str:
    return f"{prefix}-{wifi_mac.replace(':', '')[-6:]}"
