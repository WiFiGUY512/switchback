import pytest

from switchback import protocol as p


def test_short_ssid_is_one_page():
    assert p.ssid_pages("Home") == [bytes.fromhex("570f01 04 00") + b"Home"]


def test_exactly_eleven_bytes_is_one_page():
    assert p.ssid_pages("A" * 11) == [bytes.fromhex("570f01 0b 00") + b"A" * 11]


def test_fourteen_byte_ssid_is_two_pages_with_total_length():
    # The bug this guards: sent as one page, the plug kept 11 bytes and filled the
    # rest from its own buffer, so it looked for a network that does not exist.
    pages = p.ssid_pages("Network_IoT_24")
    assert pages == [
        bytes.fromhex("570f01 0e 00") + b"Network_IoT",
        bytes.fromhex("570f01 0e 01") + b"_24",
    ]
    assert all(len(page) <= 5 + p.PROV_PAGE for page in pages)


def test_max_length_ssid_and_password_paging():
    assert len(p.ssid_pages("x" * 32)) == 3
    pages = p.password_pages("y" * 63)
    assert len(pages) == 6
    assert [page[4] for page in pages] == [0, 1, 2, 3, 4, 5]
    assert all(page[3] == 63 for page in pages)
    assert b"".join(page[5:] for page in pages) == b"y" * 63


def test_open_network_sends_one_empty_password_page():
    assert p.password_pages("") == [bytes.fromhex("570f02 00 00")]


def test_utf8_length_is_bytes_not_characters():
    pages = p.ssid_pages("Café")
    assert pages[0][3] == 5


def test_limits():
    with pytest.raises(ValueError):
        p.ssid_pages("")
    with pytest.raises(ValueError):
        p.ssid_pages("x" * 33)
    with pytest.raises(ValueError):
        p.password_pages("x" * 64)


def test_ota_start():
    assert p.ota_start(0x0C) == bytes.fromhex("570f0a010c")
    with pytest.raises(ValueError):
        p.ota_start(256)


@pytest.mark.parametrize(
    ("reply", "state"),
    [
        (bytes.fromhex("0c3130") + bytes(6), p.WifiState.CONNECTING),
        (bytes.fromhex("0c3131") + bytes(6), p.WifiState.REPROVISIONING),
        (bytes.fromhex("0c3330") + bytes(6), p.WifiState.SSID_NOT_FOUND),
        (bytes.fromhex("0c3430") + bytes(6), p.WifiState.SSID_NOT_FOUND),
        (bytes.fromhex("0c3530") + bytes(6), p.WifiState.ASSOCIATED),
        (bytes.fromhex("0c3531") + bytes(6), p.WifiState.CONNECTING),
        (bytes.fromhex("0c3939"), p.WifiState.UNKNOWN),
        (b"", p.WifiState.NO_REPLY),
        (None, p.WifiState.NO_REPLY),
    ],
)
def test_status_states(reply, state):
    status = p.parse_status(reply)
    assert status.state is state
    assert not status.done


def test_connected_status_carries_the_mac():
    status = p.parse_status(bytes.fromhex("013531 020000aabbcc"))
    assert status.done
    assert status.code == "51"
    assert status.mac == "02:00:00:aa:bb:cc"


def test_is_ack():
    assert p.is_ack(b"\x01")
    assert not p.is_ack(b"\x0c")
    assert not p.is_ack(b"")
    assert not p.is_ack(None)


def test_macs():
    assert p.mac_offset("02:00:00:00:01:01", -2) == "02:00:00:00:00:ff"
    assert p.normalize_mac("2-0-0-AA-bb-C") == "02:00:00:aa:bb:0c"
    assert p.normalize_mac("not a mac") is None
    assert p.esphome_name("02:00:00:aa:bb:cc") == "sbplug-aabbcc"
