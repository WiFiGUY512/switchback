# Contributing

Thanks for helping. Bug reports with a log from `switchback convert` are the most useful thing you can send. Leave out your Wi-Fi details and MAC addresses if you prefer.

## Set up

```bash
git clone https://github.com/WiFiGUY512/switchback
cd switchback
uv sync
uv run switchback --help
```

## Checks

CI runs these on Windows, Linux and macOS. Run them before you open a pull request:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

The tests need no plug and no Bluetooth. They cover the Wi-Fi paging, status parsing, the image patcher, the DNS responder, the update server and release verification.

## Layout

| Path | What |
| --- | --- |
| `src/switchback/` | The Python package and the `switchback` command |
| `firmware/esphome/` | ESPHome configs for the payloads (`sbplug-common.yaml` is the hardware) |
| `firmware/flasher/` | The plug-side flasher, ESP-IDF v4.4 for esp32c3 |
| `scripts/build_payload.py` | Builds one payload and checks it fits the flasher |
| `.github/workflows/` | CI and the release build |

## Building firmware yourself

The flasher (needs ESP-IDF v4.4):

```bash
cd firmware/flasher
idf.py set-target esp32c3
idf.py build
```

The output is `build/switchbota.bin`. It keeps upstream's project name on purpose.

A payload (needs the ESPHome version pinned in `.github/workflows/release.yml`):

```bash
pip install esphome==2026.5.1
python scripts/build_payload.py portal -o out/sbplug-portal.bin
python scripts/build_payload.py direct -o out/sbplug-direct.bin
```

Put `app.bin` and the payloads in one folder and test with `switchback convert --firmware-dir out`. The image has to stay under 0x171000 bytes, which is why the payloads have no Bluetooth components.

## Releases

Maintainers bump `version` in `pyproject.toml`, add a `CHANGELOG.md` entry, and push a tag `vX.Y.Z` that matches. The release workflow builds everything, writes `SHA256SUMS`, attests each file and opens a draft release. Review it, then publish.

## Rules for every change

- No binaries in git. Firmware comes from the release workflow.
- No personal or site details in tracked files: no home network names, addresses, hostnames or device MACs. Use `192.0.2.x` (documentation addresses) and `02:00:00:...` MACs in examples.
- Pin every GitHub Action to a full commit SHA.
- Plain, short sentences in docs.

By contributing you agree your work is licensed under GPL-3.0, the project's license.
