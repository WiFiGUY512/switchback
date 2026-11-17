#!/usr/bin/env python3
"""Build one ESPHome payload for the flasher and copy the factory image out.

    python scripts/build_payload.py portal  -o out/sbplug-portal.bin
    python scripts/build_payload.py direct  -o out/sbplug-direct.bin
    python scripts/build_payload.py implant -o sbplug-implant.bin   (needs secrets.yaml)

Run it with the Python that has ESPHome installed (the release workflow pins the version).
It refuses to build the implant variant until every secret it needs is filled in, and it
refuses to copy an image the flasher cannot write.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ESPHOME_DIR = ROOT / "firmware" / "esphome"
BUILD_ROOT = ESPHOME_DIR / ".esphome" / "build"
WRITE_SIZE = 0x171000  # keep in step with firmware/flasher/main/include/config.h
VARIANTS = {
    "portal": (),
    "direct": (),
    "implant": ("api_encryption_key", "ota_password", "wifi_ssid", "wifi_password"),
}


def check_secrets(variant: str) -> None:
    import yaml  # comes with ESPHome

    path = ESPHOME_DIR / "secrets.yaml"
    if not path.exists():
        sys.exit(f"{path} is missing. Copy secrets.yaml.example to secrets.yaml and fill it in.")
    secrets = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    problems = [f"{name} is blank" for name in VARIANTS[variant] if not str(secrets.get(name) or "").strip()]
    key = str(secrets.get("api_encryption_key") or "").strip()
    if key:
        try:
            valid = len(base64.b64decode(key, validate=True)) == 32
        except binascii.Error:
            valid = False
        if not valid:
            problems.append("api_encryption_key is not 32 bytes of base64")
    if problems:
        sys.exit(f"{path} is not ready for the {variant} build:\n  " + "\n  ".join(problems))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("variant", choices=sorted(VARIANTS))
    parser.add_argument("-o", "--out", required=True, help="where to copy the factory image")
    parser.add_argument(
        "-s",
        "--substitution",
        nargs=2,
        action="append",
        default=[],
        metavar=("KEY", "VALUE"),
        help="pass a substitution to ESPHome, e.g. -s ota_password mypass",
    )
    args = parser.parse_args()

    if importlib.util.find_spec("esphome") is None:
        sys.exit(f"ESPHome is not installed for {sys.executable}.")
    if VARIANTS[args.variant]:
        check_secrets(args.variant)

    # Remove old images first so a stale one can never be copied.
    for old in BUILD_ROOT.rglob("firmware.factory.bin") if BUILD_ROOT.exists() else []:
        old.unlink()
    cmd = [sys.executable, "-m", "esphome"]
    for key, value in args.substitution:
        cmd += ["-s", key, value]
    cmd += ["compile", f"sbplug-{args.variant}.yaml"]
    result = subprocess.run(cmd, cwd=ESPHOME_DIR, check=False)
    images = sorted(BUILD_ROOT.rglob("firmware.factory.bin"), key=lambda p: p.stat().st_mtime)
    if result.returncode != 0 or not images:
        sys.exit("ESPHome build failed, nothing was copied.")
    image = images[-1]
    size = image.stat().st_size
    if size > WRITE_SIZE:
        sys.exit(f"The factory image is {size:,} bytes and the flasher can only write {WRITE_SIZE:,}.")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(image, out)
    md5 = hashlib.md5(out.read_bytes()).hexdigest()
    print(f"\n{args.variant}: {out}, {size:,} bytes, {WRITE_SIZE - size:,} bytes of headroom, md5 {md5}")


if __name__ == "__main__":
    main()
