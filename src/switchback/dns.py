"""A tiny DNS responder that sends wohand.com to this computer and forwards everything else.

The stock plug firmware asks wohand.com for its update. Switchback answers that one name
with this computer's address, so the plug downloads the flasher from here. Every other
name is passed to an upstream resolver unchanged.
"""

from __future__ import annotations

import contextlib
import socket
import struct
import subprocess
import sys
import threading

OTA_DOMAIN = "wohand.com"
OTA_HOSTS = ("wohand.com", "www.wohand.com")
TYPE_A = 1
CLASS_IN = 1


def is_ota_name(name: str) -> bool:
    name = name.lower().rstrip(".")
    return name == OTA_DOMAIN or name.endswith("." + OTA_DOMAIN)


def parse_question(packet: bytes) -> tuple[str, int, int]:
    """Return (name, qtype, offset just past the question) for the first question."""
    if len(packet) < 12:
        raise ValueError("short DNS packet")
    labels = []
    i = 12
    while True:
        if i >= len(packet):
            raise ValueError("name runs past the packet")
        n = packet[i]
        if n == 0:
            i += 1
            break
        if n & 0xC0:
            raise ValueError("compressed name in a question")
        labels.append(packet[i + 1 : i + 1 + n].decode("ascii", "replace"))
        i += n + 1
    if i + 4 > len(packet):
        raise ValueError("question runs past the packet")
    qtype, _qclass = struct.unpack_from(">HH", packet, i)
    return ".".join(labels), qtype, i + 4


def build_answer(query: bytes, address: str, ttl: int = 60) -> bytes | None:
    """Answer a query for wohand.com with address. None if the query is not ours to answer.

    A queries get one A record. Other types for wohand.com (AAAA and so on) get an empty
    NOERROR answer, so nothing falls through to the real servers.
    """
    name, qtype, end = parse_question(query)
    if not is_ota_name(name):
        return None
    flags = 0x8180  # response, recursion desired and available, no error
    answers = 1 if qtype == TYPE_A else 0
    header = query[:2] + struct.pack(">HHHHH", flags, 1, answers, 0, 0)
    question = query[12:end]
    if not answers:
        return header + question
    record = b"\xc0\x0c" + struct.pack(">HHIH", TYPE_A, CLASS_IN, ttl, 4) + socket.inet_aton(address)
    return header + question + record


def build_query(name: str, txid: int = 0x4242) -> bytes:
    labels = b"".join(bytes([len(p)]) + p.encode("ascii") for p in name.split("."))
    return struct.pack(">HHHHHH", txid, 0x0100, 1, 0, 0, 0) + labels + b"\x00" + struct.pack(">HH", TYPE_A, CLASS_IN)


def first_a_record(response: bytes) -> str | None:
    """The first A record in a response, or None."""
    try:
        _name, _qtype, i = parse_question(response)
        answers = struct.unpack_from(">H", response, 6)[0]
        for _ in range(answers):
            # Skip the name: a pointer (2 bytes) or a label sequence.
            if response[i] & 0xC0 == 0xC0:
                i += 2
            else:
                while response[i] != 0:
                    i += response[i] + 1
                i += 1
            rtype, _rclass, _ttl, rdlength = struct.unpack_from(">HHIH", response, i)
            i += 10
            if rtype == TYPE_A and rdlength == 4:
                return socket.inet_ntoa(response[i : i + 4])
            i += rdlength
    except (ValueError, IndexError, struct.error):
        return None
    return None


def query_a(server: str, name: str, port: int = 53, timeout: float = 3.0) -> str | None:
    """Ask one DNS server for an A record."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(build_query(name), (server, port))
        response, _ = sock.recvfrom(1500)
        return first_a_record(response)
    except OSError:
        return None
    finally:
        sock.close()


def system_lookup(name: str) -> str | None:
    try:
        return socket.gethostbyname(name)
    except OSError:
        return None


def flush_cache() -> bool:
    """Clear the OS DNS cache so a stale wohand.com answer cannot fool the check."""
    if sys.platform.startswith("win"):
        cmd = ["ipconfig", "/flushdns"]
    elif sys.platform == "darwin":
        cmd = ["dscacheutil", "-flushcache"]
    else:
        cmd = ["resolvectl", "flush-caches"]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=10, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class DnsServer:
    """Answers wohand.com with address, forwards the rest to upstream. Runs in a thread."""

    def __init__(self, address: str, port: int = 53, upstream: str = "1.1.1.1", bind: str = ""):
        self.address = address
        self.upstream = upstream
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if not sys.platform.startswith("win"):
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((bind, port))
        self.port = self.sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, name="switchback-dns", daemon=True)

    def start(self) -> DnsServer:
        self._thread.start()
        return self

    def close(self) -> None:
        self.sock.close()

    def _forward(self, packet: bytes) -> bytes | None:
        fwd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        fwd.settimeout(3)
        try:
            fwd.sendto(packet, (self.upstream, 53))
            reply, _ = fwd.recvfrom(4096)
            return reply
        except OSError:
            return None
        finally:
            fwd.close()

    def _handle(self, packet: bytes, client: tuple[str, int]) -> None:
        try:
            reply = build_answer(packet, self.address)
        except ValueError:
            return
        if reply is None:
            reply = self._forward(packet)
        if reply is not None:
            with contextlib.suppress(OSError):
                self.sock.sendto(reply, client)

    def _serve(self) -> None:
        while True:
            try:
                packet, client = self.sock.recvfrom(4096)
            except ConnectionResetError:
                continue  # Windows reports an earlier ICMP "port unreachable" here
            except OSError:
                return  # closed
            threading.Thread(target=self._handle, args=(packet, client), daemon=True).start()
