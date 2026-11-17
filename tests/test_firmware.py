import hashlib
import io
import json
import urllib.error
from pathlib import Path

import pytest

from switchback import firmware

APP = b"app bytes"
PORTAL = b"portal bytes"
DIRECT = b"direct bytes"


def sums_for(files: dict[str, bytes]) -> str:
    return "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in files.items())


class FakeRelease:
    """Stands in for GitHub: maps URLs to bytes and records what was fetched."""

    def __init__(self, files: dict[str, bytes], base: str = "https://mirror.example/r"):
        self.base = base
        self.files = files
        self.fetched: list[str] = []

    def __call__(self, url: str):
        self.fetched.append(url)
        name = url.rsplit("/", 1)[-1]
        if not url.startswith(self.base) or name not in self.files:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]
        return io.BytesIO(self.files[name])


def release(**overrides: bytes) -> dict[str, bytes]:
    files = {firmware.APP: APP, firmware.PORTAL: PORTAL, firmware.DIRECT: DIRECT}
    files[firmware.SUMS] = sums_for(files).encode()
    files.update(overrides)
    return files


def test_parse_sums():
    text = "# comment\n" + "a" * 64 + "  app.bin\n" + "B" * 64 + " *dir/sbplug-portal.bin\n\n"
    assert firmware.parse_sums(text) == {"app.bin": "a" * 64, "sbplug-portal.bin": "b" * 64}
    with pytest.raises(firmware.FirmwareError):
        firmware.parse_sums("xyz  app.bin\n")
    with pytest.raises(firmware.FirmwareError):
        firmware.parse_sums("justonefield\n")


def test_fetch_verifies_and_caches(tmp_path: Path):
    fake = FakeRelease(release())
    fw = firmware.fetch("v9.9.9", cache=tmp_path, base_url=fake.base, opener=fake, log=lambda _m: None)
    assert fw.verified
    assert fw.app.read_bytes() == APP
    assert fw.portal.read_bytes() == PORTAL
    assert fw.direct is not None and fw.direct.read_bytes() == DIRECT
    assert fw.folder == tmp_path / "v9.9.9"

    # Second run: only SHA256SUMS is fetched again, the verified files come from the cache.
    fake.fetched.clear()
    firmware.fetch("v9.9.9", cache=tmp_path, base_url=fake.base, opener=fake, log=lambda _m: None)
    assert [u.rsplit("/", 1)[-1] for u in fake.fetched] == [firmware.SUMS]


def test_tampered_download_is_refused_and_removed(tmp_path: Path):
    files = release()
    files[firmware.PORTAL] = b"something else"
    fake = FakeRelease(files)
    with pytest.raises(firmware.ChecksumError):
        firmware.fetch("v1", cache=tmp_path, base_url=fake.base, opener=fake, log=lambda _m: None)
    assert not (tmp_path / "v1" / firmware.PORTAL).exists()


def test_tampered_cache_is_downloaded_again(tmp_path: Path):
    fake = FakeRelease(release())
    firmware.fetch("v1", cache=tmp_path, base_url=fake.base, opener=fake, log=lambda _m: None)
    (tmp_path / "v1" / firmware.APP).write_bytes(b"changed on disk")
    fw = firmware.fetch("v1", cache=tmp_path, base_url=fake.base, opener=fake, log=lambda _m: None)
    assert fw.app.read_bytes() == APP


def test_sums_must_list_required_files(tmp_path: Path):
    files = release()
    files[firmware.SUMS] = sums_for({firmware.APP: APP}).encode()
    fake = FakeRelease(files)
    with pytest.raises(firmware.FirmwareError, match=r"sbplug-portal.bin"):
        firmware.fetch("v1", cache=tmp_path, base_url=fake.base, opener=fake, log=lambda _m: None)


def test_direct_is_optional(tmp_path: Path):
    files = {firmware.APP: APP, firmware.PORTAL: PORTAL}
    files[firmware.SUMS] = sums_for(files).encode()
    fake = FakeRelease(files)
    fw = firmware.fetch("v1", cache=tmp_path, base_url=fake.base, opener=fake, log=lambda _m: None)
    assert fw.direct is None


def test_missing_release_falls_back_to_newest(tmp_path: Path):
    repo = "owner/repo"
    gh = "https://github.com/owner/repo/releases/download/v0.9.0"
    files = release()

    def opener(url: str):
        if url.startswith("https://api.github.com/repos/owner/repo/releases"):
            body = [{"tag_name": "v0.9.1", "draft": True}, {"tag_name": "v0.9.0", "draft": False}]
            return io.BytesIO(json.dumps(body).encode())
        if url.startswith(gh):
            return io.BytesIO(files[url.rsplit("/", 1)[-1]])
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    messages: list[str] = []
    fw = firmware.fetch(None, repo=repo, cache=tmp_path, opener=opener, log=messages.append)
    assert fw.source == "v0.9.0"
    assert any("v0.9.0" in m for m in messages)


def test_explicit_missing_tag_is_an_error(tmp_path: Path):
    fake = FakeRelease({})
    with pytest.raises(firmware.FirmwareError):
        firmware.fetch("v404", repo="owner/repo", cache=tmp_path, opener=fake, log=lambda _m: None)


def test_from_folder(tmp_path: Path):
    for name, data in release().items():
        (tmp_path / name).write_bytes(data)
    assert firmware.from_folder(tmp_path, log=lambda _m: None).verified
    (tmp_path / firmware.APP).write_bytes(b"tampered")
    with pytest.raises(firmware.ChecksumError):
        firmware.from_folder(tmp_path, log=lambda _m: None)


def test_from_folder_without_sums_is_unverified(tmp_path: Path):
    (tmp_path / firmware.APP).write_bytes(APP)
    (tmp_path / firmware.PORTAL).write_bytes(PORTAL)
    notes: list[str] = []
    fw = firmware.from_folder(tmp_path, log=notes.append)
    assert not fw.verified
    assert notes
