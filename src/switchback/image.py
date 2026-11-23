"""ESP32-C3 image checks and the runtime Wi-Fi patch for the direct payload.

The direct payload (firmware/esphome/sbplug-direct.yaml) is built with fixed-size
placeholder Wi-Fi credentials. patch_wifi() overwrites them with real ones and fixes the
ESP app image's 1 byte checksum and appended SHA-256 so the plug boots the patched image.

verify_image() recomputes the checksum and hash and compares them with the bytes already in
the file. Run on an unpatched image it proves the algorithm is right before anything is
served to a plug.
"""

from __future__ import annotations

import hashlib
import struct

SSID_PREFIX = b"SB_SSID_"
PASS_PREFIX = b"SB_PASS_"
# Prefix plus the first fill characters, so only the real slot matches.
NAME_PREFIX = b"SB_NAME_CCCC"
FRIENDLY_PREFIX = b"SB_FRND_DDDD"
ESP_IMAGE_MAGIC = 0xE9
ESP_CHECKSUM_MAGIC = 0xEF
ESP32C3_CHIP_ID = 0x0005
APP_OFFSET = 0x10000  # where the app image sits inside the factory image
PARTITION_TABLE_OFFSET = 0x8000
PARTITION_MAGIC = 0x50AA
# The flasher (firmware/flasher/main/include/config.h) writes at most this many bytes from 0x0.
WRITE_SIZE = 0x171000


class ImageError(ValueError):
    pass


def _xor(buf: bytes | bytearray | memoryview, start: int, length: int) -> int:
    x = 0
    for b in bytes(buf[start : start + length]):
        x ^= b
    return x


def _checksum_and_hash(
    img: bytes | bytearray | memoryview, app_offset: int = APP_OFFSET
) -> tuple[int, int, int | None, bytes | None]:
    """Return (checksum, checksum position, hash position or None, computed SHA-256 or None)."""
    if len(img) < app_offset + 24 or img[app_offset] != ESP_IMAGE_MAGIC:
        raise ImageError("no ESP image header at the app offset")
    segments = img[app_offset + 1]
    hash_appended = img[app_offset + 23]
    off = app_offset + 24
    checksum = ESP_CHECKSUM_MAGIC
    for _ in range(segments):
        if off + 8 > len(img):
            raise ImageError("segment header runs past the end of the image")
        _load, length = struct.unpack_from("<II", img, off)
        off += 8
        if off + length > len(img):
            raise ImageError("segment runs past the end of the image")
        checksum ^= _xor(img, off, length)
        off += length
    # Pad to a 16 byte boundary; the last byte of that block is the checksum.
    pos = off
    while (pos - app_offset + 1) % 16 != 0:
        pos += 1
    if pos >= len(img):
        raise ImageError("image is truncated before its checksum")
    image_end = pos + 1
    if hash_appended:
        if image_end + 32 > len(img):
            raise ImageError("image is truncated before its SHA-256")
        tmp = bytearray(img[app_offset:image_end])
        tmp[pos - app_offset] = checksum  # the hash covers the image including the checksum
        return checksum, pos, image_end, hashlib.sha256(bytes(tmp)).digest()
    return checksum, pos, None, None


def verify_image(image: bytes, app_offset: int = APP_OFFSET) -> bool:
    """True if the recomputed checksum and hash match what is in the image."""
    try:
        checksum, pos, hash_pos, sha = _checksum_and_hash(image, app_offset)
    except ImageError:
        return False
    if image[pos] != checksum:
        return False
    return not (hash_pos is not None and image[hash_pos : hash_pos + 32] != sha)


def has_placeholders(image: bytes) -> bool:
    return SSID_PREFIX in image and PASS_PREFIX in image


def has_name_slots(image: bytes) -> bool:
    return NAME_PREFIX in image and FRIENDLY_PREFIX in image


def _fill_slots(image: bytes, slots: tuple[tuple[bytes, str, str], ...], app_offset: int, hint: str) -> bytes:
    """Overwrite each placeholder slot with its value, then fix the checksum and hash."""
    img = bytearray(image)
    for prefix, label, value in slots:
        i = img.find(prefix)
        if i < 0:
            raise ImageError(f"{label} placeholder not found. {hint}")
        end = img.find(b"\x00", i)  # the slot runs to the string's null terminator
        if end < 0 or not (len(prefix) <= end - i <= 64):
            raise ImageError(f"{label} placeholder slot looks wrong")
        slot = end - i
        raw = value.encode("utf-8")
        if len(raw) > slot:
            raise ImageError(f"{label} is too long ({len(raw)} bytes, this build has room for {slot})")
        img[i:end] = raw + b"\x00" * (slot - len(raw))

    checksum, pos, hash_pos, sha = _checksum_and_hash(img, app_offset)
    img[pos] = checksum
    if hash_pos is not None and sha is not None:
        img[hash_pos : hash_pos + 32] = sha
    return bytes(img)


def patch_wifi(image: bytes, ssid: str, password: str, app_offset: int = APP_OFFSET) -> bytes:
    """Return a copy of image with the placeholder Wi-Fi replaced and the checksum and hash fixed."""
    slots = ((SSID_PREFIX, "Wi-Fi name", ssid), (PASS_PREFIX, "Wi-Fi password", password))
    return _fill_slots(image, slots, app_offset, "Is this the direct build?")


def patch_name(image: bytes, name: str, friendly_name: str, app_offset: int = APP_OFFSET) -> bytes:
    """Return a copy of image that boots with this hostname and friendly name."""
    slots = ((NAME_PREFIX, "name", name), (FRIENDLY_PREFIX, "friendly name", friendly_name))
    return _fill_slots(image, slots, app_offset, "This firmware is too old to take a name.")


def hostname_from(text: str) -> str:
    """A valid ESPHome name from what the user typed: 'Garage Freezer' -> 'garage-freezer'."""
    out = []
    for ch in text.strip().lower():
        if ch.isascii() and ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")[:31].strip("-")


def check_esp32c3(image: bytes, name: str) -> None:
    # ESP image header: magic at byte 0, chip id at bytes 12 and 13 (5 is the ESP32-C3)
    if len(image) < 24 or image[0] != ESP_IMAGE_MAGIC or struct.unpack_from("<H", image, 12)[0] != ESP32C3_CHIP_ID:
        raise ImageError(f"{name} is not an ESP32-C3 image")


def check_flasher(image: bytes) -> None:
    """Refuse an app.bin that would leave a plug erased and looping."""
    check_esp32c3(image, "app.bin")
    if b"payload.md5" not in image:
        raise ImageError("app.bin never asks for payload.md5, so it is an upstream build that only takes Tasmota")


def check_payload(image: bytes, name: str = "payload") -> None:
    """Refuse a payload the flasher cannot write safely. These are the checks the old server ran."""
    check_esp32c3(image, name)
    if len(image) > WRITE_SIZE:
        raise ImageError(f"{name} is {len(image)} bytes, the flasher can only write {WRITE_SIZE}")
    if (
        len(image) < PARTITION_TABLE_OFFSET + 2
        or struct.unpack_from("<H", image, PARTITION_TABLE_OFFSET)[0] != PARTITION_MAGIC
    ):
        raise ImageError(f"{name} has no partition table at 0x8000. It must be the factory image, not the OTA one")
    if image[2] not in (2, 3):
        raise ImageError(f"{name} bootloader flash mode is {image[2]}, expected 2 (dio) or 3 (dout)")
