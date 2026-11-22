"""Synthetic ESP32-C3 images that match the real factory layout closely enough to test with."""

from __future__ import annotations

import hashlib
import struct

import pytest

SSID_SLOT = b"SB_SSID_" + b"A" * 24
PASS_SLOT = b"SB_PASS_" + b"B" * 55
NAME_SLOT = b"SB_NAME_" + b"C" * 23
FRIENDLY_SLOT = b"SB_FRND_" + b"D" * 55


def _header(segments: int, hash_appended: int) -> bytearray:
    head = bytearray(24)
    head[0] = 0xE9
    head[1] = segments
    head[2] = 2  # dio
    struct.pack_into("<H", head, 12, 0x0005)  # ESP32-C3
    head[23] = hash_appended
    return head


def make_app(segments: list[bytes], hash_appended: bool = True) -> bytes:
    body = _header(len(segments), int(hash_appended))
    load = 0x3C000000
    for data in segments:
        body += struct.pack("<II", load, len(data)) + data
        load += 0x1000
    checksum = 0xEF
    for data in segments:
        for b in data:
            checksum ^= b
    while (len(body) + 1) % 16:
        body += b"\x00"
    body += bytes([checksum])
    if hash_appended:
        body += hashlib.sha256(bytes(body)).digest()
    return bytes(body)


def make_factory(placeholders: bool = True, hash_appended: bool = True, name_slots: bool = True) -> bytes:
    img = bytearray(b"\xff" * 0x10000)
    boot = _header(0, 0)
    img[: len(boot)] = boot
    struct.pack_into("<H", img, 0x8000, 0x50AA)
    data1 = b"\x01\x02\x03" * 50
    if placeholders:
        data2 = b"wifi:" + SSID_SLOT + b"\x00" + PASS_SLOT + b"\x00"
    else:
        data2 = b"no wifi here\x00"
    data3 = bytes(range(256)) * 3
    if name_slots:
        data3 += NAME_SLOT + b"\x00" + FRIENDLY_SLOT + b"\x00" + b"\x00" * 3
    img += make_app([data1, data2 + b"\x00" * 3, data3], hash_appended)
    return bytes(img)


def make_flasher() -> bytes:
    return make_app([b"GET /payload.md5 HTTP/1.1\x00" + b"\x00" * 6])


@pytest.fixture
def factory() -> bytes:
    return make_factory()


@pytest.fixture
def flasher() -> bytes:
    return make_flasher()
