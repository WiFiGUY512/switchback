import pytest

from conftest import make_factory
from switchback import image


def test_unpatched_image_verifies(factory):
    # Proves the checksum and hash maths match the file before anything is patched.
    assert image.verify_image(factory)


def test_image_without_appended_hash_verifies():
    assert image.verify_image(make_factory(hash_appended=False))


def test_patch_keeps_size_and_verifies(factory):
    patched = image.patch_wifi(factory, "MyNetwork", "correct horse battery")
    assert len(patched) == len(factory)
    assert image.verify_image(patched)
    assert image.SSID_PREFIX not in patched
    assert image.PASS_PREFIX not in patched
    assert b"MyNetwork\x00" in patched
    assert b"correct horse battery\x00" in patched


def test_patch_without_hash_verifies():
    patched = image.patch_wifi(make_factory(hash_appended=False), "Net", "pw")
    assert image.verify_image(patched)


def test_editing_without_fixing_fails_verification(factory):
    raw = bytearray(factory)
    i = raw.find(image.SSID_PREFIX)
    raw[i : i + 3] = b"Net"
    assert not image.verify_image(bytes(raw))


def test_full_length_values_fit(factory):
    assert image.verify_image(image.patch_wifi(factory, "s" * 32, "p" * 63))


def test_too_long_values_are_refused(factory):
    with pytest.raises(image.ImageError):
        image.patch_wifi(factory, "s" * 33, "pw")
    with pytest.raises(image.ImageError):
        image.patch_wifi(factory, "ok", "p" * 64)


def test_image_without_placeholders_is_refused():
    with pytest.raises(image.ImageError):
        image.patch_wifi(make_factory(placeholders=False), "a", "b")


def test_corrupt_image_does_not_verify(factory):
    raw = bytearray(factory)
    raw[0x10000 + 40] ^= 0xFF
    assert not image.verify_image(bytes(raw))
    assert not image.verify_image(b"\x00" * 100)


def test_payload_checks(factory, flasher):
    image.check_payload(factory)
    image.check_flasher(flasher)
    with pytest.raises(image.ImageError):
        image.check_flasher(factory)  # never asks for payload.md5
    with pytest.raises(image.ImageError):
        image.check_payload(factory + b"\x00" * image.WRITE_SIZE)
    no_table = bytearray(factory)
    no_table[0x8000:0x8002] = b"\xff\xff"
    with pytest.raises(image.ImageError):
        image.check_payload(bytes(no_table))
    qio = bytearray(factory)
    qio[2] = 0
    with pytest.raises(image.ImageError):
        image.check_payload(bytes(qio))
    with pytest.raises(image.ImageError):
        image.check_esp32c3(b"\xe9" + b"\x00" * 30, "x")
