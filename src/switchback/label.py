"""Optional ZPL label for a converted plug: the date, both MACs, and a QR code of both.

Off unless you pass --printer. The layout is for 1.5 x 0.5 inch direct thermal labels at
203 dpi (305 x 112 dots). Any ZPL printer that listens on a raw TCP port (usually 9100)
will take it.
"""

from __future__ import annotations

import datetime
import socket

LABEL_ZPL = (
    "~SD30\n"
    "^XA^CI28^PW305^LL112^LH0,0"
    "^FO6,2^A0N,24,13^FD{date}^FS"
    "^FO6,28^A0N,42,16^FDBLE:  {ble}^FS"
    "^FO6,70^A0N,42,16^FDWiFi: {wifi}^FS"
    "^FO210,8^BQN,2,3^FDMA,{qr}^FS"
    "^XZ"
)


def render(ble_mac: str, wifi_mac: str, date: datetime.date | None = None) -> str:
    ble, wifi = ble_mac.upper(), wifi_mac.upper()
    # Upper case with no colons keeps the QR in compact alphanumeric mode so it fits.
    qr = f"B {ble.replace(':', '')} W {wifi.replace(':', '')}"
    return LABEL_ZPL.format(ble=ble, wifi=wifi, qr=qr, date=(date or datetime.date.today()).isoformat())


def send(printer: str, zpl: str) -> None:
    host, sep, port = printer.rpartition(":")
    if not sep:
        host, port = printer, ""
    with socket.create_connection((host, int(port or 9100)), timeout=10) as connection:
        connection.sendall(zpl.encode("ascii"))
