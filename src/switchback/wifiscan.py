"""Wi-Fi networks this computer can see, so the user can pick one instead of typing a name.

It asks the operating system and sends nothing itself. Every failure ends in an empty
list, and the caller falls back to asking for the name.

  Windows  netsh wlan show networks mode=bssid
  Linux    nmcli device wifi list
  macOS    only the network the computer is on (macOS hides the rest from programs
           without location permission)
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass

# The word for "Channel" in the netsh output of common Windows languages.
_CHANNEL = r"(?:channel|kanal|canal|canale|kanaal|kanał)"


@dataclass
class Network:
    ssid: str
    signal: int = 0  # percent, 0 if unknown
    band24: bool | None = None  # True if any access point is on 2.4 GHz, None if unknown
    current: bool = False


def _run(cmd: list[str]) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8", errors="replace")


def _merge(found: dict[str, Network], ssid: str, signal: int, band24: bool | None) -> None:
    if not ssid:
        return  # hidden network
    net = found.setdefault(ssid, Network(ssid))
    net.signal = max(net.signal, signal)
    if band24:
        net.band24 = True
    elif band24 is False and net.band24 is None:
        net.band24 = False


def parse_netsh_networks(text: str) -> list[Network]:
    """Parse `netsh wlan show networks mode=bssid`. One SSID block holds one or more BSSIDs."""
    found: dict[str, Network] = {}
    ssid = ""
    signal = 0
    in_bssid = False

    def flush(channel: int | None) -> None:
        if ssid and in_bssid:
            _merge(found, ssid, signal, None if channel is None else channel <= 14)

    channel: int | None = None
    for line in text.splitlines():
        if m := re.match(r"^\s*SSID\s+\d+\s*:\s?(.*)$", line):
            flush(channel)
            ssid, signal, channel, in_bssid = m.group(1).strip(), 0, None, False
        elif re.match(r"^\s*BSSID\s+\d+\s*:", line):
            flush(channel)
            signal, channel, in_bssid = 0, None, True
        elif in_bssid and (m := re.match(r"^\s*[^:]+:\s*(\d+)\s*%\s*$", line)):
            signal = int(m.group(1))
        elif in_bssid and (m := re.match(rf"^\s*{_CHANNEL}\s*:\s*(\d+)\s*$", line, re.IGNORECASE)):
            channel = int(m.group(1))
    flush(channel)
    # A network listed with no BSSID lines at all still counts, band unknown.
    for line in text.splitlines():
        if m := re.match(r"^\s*SSID\s+\d+\s*:\s?(.*)$", line):
            _merge(found, m.group(1).strip(), 0, None)
    return list(found.values())


def parse_netsh_current(text: str) -> str | None:
    """The connected SSID from `netsh wlan show interfaces` (the BSSID line is skipped)."""
    for line in text.splitlines():
        if m := re.match(r"^\s*SSID\s*:\s?(.*)$", line):
            return m.group(1).strip() or None
    return None


def parse_nmcli(text: str) -> list[Network]:
    """Parse `nmcli -t -f ACTIVE,SIGNAL,FREQ,SSID device wifi list`. SSID is last, colons escaped."""
    found: dict[str, Network] = {}
    for line in text.splitlines():
        parts = re.split(r"(?<!\\):", line, maxsplit=3)
        if len(parts) != 4:
            continue
        active, signal, freq, ssid = parts
        ssid = ssid.replace("\\:", ":").replace("\\\\", "\\")
        mhz = re.match(r"\d+", freq)
        band24 = int(mhz.group()) < 3000 if mhz else None
        _merge(found, ssid, int(signal) if signal.isdigit() else 0, band24)
        if ssid and active.lower() in ("yes", "*"):
            found[ssid].current = True
    return list(found.values())


def parse_networksetup(text: str) -> str | None:
    """The SSID from `networksetup -getairportnetwork <device>`."""
    m = re.match(r"^Current Wi-Fi Network:\s?(.*)$", text.strip())
    return (m.group(1).strip() or None) if m else None


def _order(networks: list[Network]) -> list[Network]:
    return sorted(networks, key=lambda n: (not n.current, -n.signal, n.ssid.lower()))


def scan() -> list[Network]:
    """Networks in range, the computer's own first, then strongest first. Empty if we cannot tell."""
    if sys.platform.startswith("win"):
        networks = parse_netsh_networks(_run(["netsh", "wlan", "show", "networks", "mode=bssid"]))
        current = parse_netsh_current(_run(["netsh", "wlan", "show", "interfaces"]))
        for net in networks:
            net.current = net.ssid == current
    elif sys.platform == "darwin":
        networks = []
        for device in ("en0", "en1"):
            ssid = parse_networksetup(_run(["networksetup", "-getairportnetwork", device]))
            if ssid:
                networks = [Network(ssid, current=True)]
                break
    else:
        networks = parse_nmcli(_run(["nmcli", "-t", "-f", "ACTIVE,SIGNAL,FREQ,SSID", "device", "wifi", "list"]))
    return _order(networks)


def usable(networks: list[Network]) -> tuple[list[Network], int]:
    """The networks a plug can join (2.4 GHz or unknown band), and how many 5 GHz-only ones were left out."""
    keep = [n for n in networks if n.band24 is not False]
    return keep, len(networks) - len(keep)
