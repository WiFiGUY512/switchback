import socket

import pytest

from switchback import dns


@pytest.mark.parametrize("name", ["wohand.com", "www.wohand.com", "WWW.WoHand.com.", "a.b.wohand.com"])
def test_ota_names(name):
    assert dns.is_ota_name(name)


@pytest.mark.parametrize("name", ["notwohand.com", "wohand.com.evil.example", "example.com"])
def test_other_names(name):
    assert not dns.is_ota_name(name)


def test_a_answer_round_trip():
    query = dns.build_query("www.wohand.com", txid=0x1234)
    answer = dns.build_answer(query, "192.0.2.7")
    assert answer is not None
    assert answer[:2] == b"\x12\x34"
    assert dns.first_a_record(answer) == "192.0.2.7"


def test_other_names_are_not_answered():
    assert dns.build_answer(dns.build_query("example.com"), "192.0.2.7") is None


def test_aaaa_gets_an_empty_answer():
    query = bytearray(dns.build_query("wohand.com"))
    query[-4:-2] = (28).to_bytes(2, "big")  # AAAA
    answer = dns.build_answer(bytes(query), "192.0.2.7")
    assert answer is not None
    assert int.from_bytes(answer[6:8], "big") == 0


def test_bad_packets():
    with pytest.raises(ValueError):
        dns.parse_question(b"\x00" * 5)
    with pytest.raises(ValueError):
        dns.parse_question(b"\x00" * 12 + b"\x05ab")


def test_server_answers_over_udp():
    server = dns.DnsServer("192.0.2.9", port=0, bind="127.0.0.1", upstream="127.0.0.1").start()
    try:
        assert dns.query_a("127.0.0.1", "www.wohand.com", port=server.port, timeout=3) == "192.0.2.9"
    finally:
        server.close()


def test_server_survives_garbage():
    server = dns.DnsServer("192.0.2.9", port=0, bind="127.0.0.1", upstream="127.0.0.1").start()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(b"junk", ("127.0.0.1", server.port))
        s.close()
        assert dns.query_a("127.0.0.1", "wohand.com", port=server.port, timeout=3) == "192.0.2.9"
    finally:
        server.close()
