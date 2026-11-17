import hashlib
import json
import urllib.request

import pytest

from conftest import make_factory
from switchback import image
from switchback.server import FirmwareServer, Payloads


@pytest.fixture
def server(flasher, factory):
    payloads = Payloads(flasher, make_factory(placeholders=False), factory)
    srv = FirmwareServer(payloads, port=0, bind="127.0.0.1", arp=lambda _ip: None).start()
    yield srv
    srv.close()


def get(srv, path, headers=None):
    request = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", headers=headers or {})
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.read()


def events(srv):
    return [e["event"] for e in json.loads(get(srv, "/events?since=0"))["events"]]


def test_flow_is_served_and_recorded(server, flasher):
    mac = {"X-Device-MAC": "02:00:00:AA:BB:CC"}
    assert get(server, "/firmware/WoPlug_V12.bin") == flasher  # stock firmware's request
    assert get(server, "/app.bin", mac) == flasher  # the flasher moving itself
    md5, size = get(server, "/payload.md5", mac).decode().split()
    body = get(server, "/payload.bin", mac)
    assert hashlib.md5(body).hexdigest() == md5
    assert len(body) == int(size)
    seen = events(server)
    assert seen[:4] == ["stage1", "flip", "payload_md5", "payload_start"]
    found = json.loads(get(server, "/events?since=1"))["events"]
    assert all(e["mac"] == "02:00:00:aa:bb:cc" for e in found)


def test_events_since(server):
    get(server, "/x.bin")
    feed = json.loads(get(server, "/events?since=0"))
    assert feed["seq"] == 1
    assert json.loads(get(server, "/events?since=1"))["events"] == []


def test_direct_mode_serves_a_patched_image(server):
    server.payloads.select("direct", "MyNet", "secret-pass")
    body = get(server, "/payload.bin")
    assert b"MyNet\x00" in body
    assert image.verify_image(body)
    md5, _size = get(server, "/payload.md5").decode().split()
    assert md5 == hashlib.md5(body).hexdigest()


def test_bad_files_are_refused(flasher, factory):
    with pytest.raises(image.ImageError):
        Payloads(factory, factory)  # an app.bin that never asks for payload.md5
    with pytest.raises(image.ImageError):
        Payloads(flasher, b"\x00" * 64)
    with pytest.raises(image.ImageError):
        Payloads(flasher, factory, make_factory(placeholders=False))  # direct without placeholders


def test_unknown_mode(server):
    with pytest.raises(ValueError):
        server.payloads.select("other")
