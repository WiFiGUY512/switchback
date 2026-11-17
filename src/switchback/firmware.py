"""Get the firmware files from a GitHub Release and verify them by SHA-256.

Nothing binary ships in the Python package or in git. The release workflow builds the
files in GitHub Actions and publishes them with a SHA256SUMS file and a build provenance
attestation. This module downloads SHA256SUMS first, then each file, and refuses any
file whose hash does not match. Verified files are cached per release tag.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from switchback import __version__

REPO = os.environ.get("SWITCHBACK_REPO", "WiFiGUY512/switchback")
SUMS = "SHA256SUMS"
APP = "app.bin"
PORTAL = "sbplug-portal.bin"
DIRECT = "sbplug-direct.bin"
REQUIRED = (APP, PORTAL)
OPTIONAL = (DIRECT,)

Opener = Callable[[str], IO[bytes]]


class FirmwareError(RuntimeError):
    pass


class ChecksumError(FirmwareError):
    pass


@dataclass
class FirmwareSet:
    source: str  # a release tag, or a folder
    folder: Path
    app: Path
    portal: Path
    direct: Path | None
    verified: bool


def parse_sums(text: str) -> dict[str, str]:
    """Read sha256sum output: '<64 hex>  <name>' (or '<hex> *<name>' for binary mode)."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            raise FirmwareError(f"cannot read this {SUMS} line: {line!r}")
        digest, name = parts[0].lower(), parts[1].strip().lstrip("*")
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise FirmwareError(f"bad hash in {SUMS} for {name}")
        if "/" in name or "\\" in name:
            name = name.replace("\\", "/").rsplit("/", 1)[-1]
        sums[name] = digest
    return sums


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def check_file(path: Path, expected: str) -> None:
    actual = sha256_file(path)
    if actual != expected.lower():
        raise ChecksumError(f"{path.name} does not match {SUMS} (got {actual[:12]}..., expected {expected[:12]}...)")


def cache_root() -> Path:
    override = os.environ.get("SWITCHBACK_CACHE")
    if override:
        return Path(override)
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "switchback" / "cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "switchback"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "switchback"


def _open(url: str) -> IO[bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": f"switchback/{__version__}"})
    response: IO[bytes] = urllib.request.urlopen(request, timeout=60)
    return response


def release_url(repo: str, tag: str, asset: str) -> str:
    return f"https://github.com/{repo}/releases/download/{tag}/{asset}"


def latest_tag(repo: str = REPO, opener: Opener = _open) -> str:
    """The newest published release, pre-releases included (GitHub's 'latest' skips them)."""
    with opener(f"https://api.github.com/repos/{repo}/releases?per_page=10") as response:
        releases = json.load(response)
    for release in releases:
        if not release.get("draft"):
            return str(release["tag_name"])
    raise FirmwareError(f"{repo} has no published releases yet")


def _download(url: str, target: Path, opener: Opener) -> None:
    part = target.with_name(target.name + ".part")
    try:
        with opener(url) as response, part.open("wb") as out:
            shutil.copyfileobj(response, out)
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)


def fetch(
    tag: str | None = None,
    repo: str = REPO,
    cache: Path | None = None,
    base_url: str | None = None,
    opener: Opener = _open,
    log: Callable[[str], None] = print,
) -> FirmwareSet:
    """Download (or reuse from the cache) and verify the firmware for one release.

    tag None means the release that matches this version of Switchback, falling back to
    the newest release if that tag does not exist. "latest" means the newest release.
    base_url replaces the GitHub download URL (tests, mirrors).
    """
    explicit = tag is not None
    tag = tag or f"v{__version__}"
    if tag == "latest" and base_url is None:
        tag = latest_tag(repo, opener)

    def url(asset: str) -> str:
        return f"{base_url.rstrip('/')}/{asset}" if base_url else release_url(repo, tag, asset)

    folder = (cache or cache_root()) / tag
    folder.mkdir(parents=True, exist_ok=True)
    sums_path = folder / SUMS
    try:
        _download(url(SUMS), sums_path, opener)
    except urllib.error.HTTPError as error:
        if error.code == 404 and not explicit and base_url is None:
            newest = latest_tag(repo, opener)
            log(f"No release {tag} yet, using the newest release, {newest}.")
            return fetch(newest, repo, cache, None, opener, log)
        raise FirmwareError(f"could not get {SUMS} for {tag}: HTTP {error.code}") from error
    except OSError as error:
        if sums_path.exists():
            log(f"Offline? Using the cached {SUMS} for {tag}.")
        else:
            raise FirmwareError(f"could not get {SUMS} for {tag}: {error}") from error
    sums = parse_sums(sums_path.read_text(encoding="utf-8"))

    for asset in REQUIRED:
        if asset not in sums:
            raise FirmwareError(f"{SUMS} for {tag} does not list {asset}")
    paths: dict[str, Path] = {}
    for asset in REQUIRED + OPTIONAL:
        if asset not in sums:
            continue
        target = folder / asset
        if target.exists() and sha256_file(target) == sums[asset]:
            paths[asset] = target
            continue
        log(f"Downloading {asset} ({tag}) ...")
        try:
            _download(url(asset), target, opener)
        except OSError as error:
            raise FirmwareError(f"could not download {asset}: {error}") from error
        try:
            check_file(target, sums[asset])
        except ChecksumError:
            target.unlink(missing_ok=True)
            raise
        paths[asset] = target
    return FirmwareSet(tag, folder, paths[APP], paths[PORTAL], paths.get(DIRECT), True)


def from_folder(folder: Path, log: Callable[[str], None] = print) -> FirmwareSet:
    """Use firmware you built yourself. Verified against SHA256SUMS if the folder has one."""
    folder = Path(folder)
    for asset in REQUIRED:
        if not (folder / asset).is_file():
            raise FirmwareError(f"{folder} has no {asset}")
    direct = folder / DIRECT if (folder / DIRECT).is_file() else None
    verified = False
    if (folder / SUMS).is_file():
        sums = parse_sums((folder / SUMS).read_text(encoding="utf-8"))
        for asset in REQUIRED + OPTIONAL:
            if (folder / asset).is_file():
                if asset not in sums:
                    raise FirmwareError(f"{SUMS} in {folder} does not list {asset}")
                check_file(folder / asset, sums[asset])
        verified = True
    else:
        log(f"Note: {folder} has no {SUMS}, so these files are not checksum verified.")
    return FirmwareSet(str(folder), folder, folder / APP, folder / PORTAL, direct, verified)


def verify_attestation(path: Path, repo: str = REPO) -> bool | None:
    """Check GitHub's build provenance for one file with the gh CLI. None if gh is not installed."""
    gh = shutil.which("gh")
    if not gh:
        return None
    result = subprocess.run(
        [gh, "attestation", "verify", str(path), "-R", repo], capture_output=True, text=True, check=False
    )
    return result.returncode == 0
