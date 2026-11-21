# Changelog

All notable changes are listed here. Versions follow [Semantic Versioning](https://semver.org/).

## [0.4.0] - unreleased

A converted plug now boots the full hardware profile, so it reports power and energy right away. No second flash is needed before it is useful.

### Added

- `firmware/esphome/plug-mini.yaml`: the plug's hardware as one public package. Relay output, button, both LEDs, BL0937 power monitoring with calibration and range checks, lifetime and daily energy, chip temperature. No Wi-Fi, API, OTA, names or secrets. Use it as a remote package to build your own config.
- Power, current and voltage are measured every 2 seconds but sent to Home Assistant only on a real change (1 W, 0.05 A, 1 V) plus a heartbeat. Below 1.5 W the plug reads 0. The raw 2 second values stay available on the plug (`id(power)` and so on) for lambdas.
- Device Builder and Home Assistant offer **Adopt** for a converted plug (ESPHome project `wifiguy512.switchback-plug-mini`).
- `firmware/esphome/addon-ble-proxy.yaml`: an optional Bluetooth proxy with passive scanning, to add over OTA. It is not in any payload.
- The relay is latched through clean restarts, so an OTA update does not blip the load.

### Changed

- `sbplug-common.yaml` now includes `plug-mini.yaml` and adds the bootstrap defaults: the "Relay" switch, the button scripts (short press toggles, hold 10 seconds to factory reset), an sntp time source and the captive portal. The relay switch keeps its name and id. The blue LED shows Wi-Fi and Home Assistant state instead of ESPHome's status pattern.
- The button's short press now accepts presses up to 2.9 seconds (was 1 second).

## [0.3.0] - 2026-11-18

First release from this repository, with fresh history.

### Changed

- Switchback is now a Python package with a `switchback` command, installed with `uv tool install` or `pipx install`. There is no exe any more.
- Firmware is no longer bundled. Switchback downloads it from GitHub Releases and verifies each file against `SHA256SUMS`. Every release file is built by GitHub Actions and has a build provenance attestation.
- The update server is now Python (standard library), so Node and Docker are no longer needed. `switchback serve` replaces the old server folder.
- Subcommands: `check`, `convert` (alias `flash`), `serve`, `scan`, `provision`, `firmware`, `label`.
- The ESPHome configs live in `firmware/esphome` and work as ESPHome remote packages. The OTA password is a substitution (default `switchbota`).
- The label printer and the conversion log are off unless you ask for them.

### Fixed

- Bluetooth Wi-Fi provisioning now pages the SSID and password at 11 bytes per write. Before, a name or password longer than 11 bytes was cut and filled from the plug's buffer, so the plug looked for a network that did not exist.
- The built-in DNS only redirects `wohand.com` and names under it, not any name that merely ends in those letters.

### Added

- Status handling while the plug joins Wi-Fi: Switchback stops early, with a clear reason, when the plug cannot find the network (status 30 or 40) or joins but gets no address (status 50).
- If the plug refuses the update trigger, Switchback tries other version bytes (0D, 0E). If all are refused it explains that the plug runs newer firmware and points to the app re-onboard path.
- `--only` targets one plug by Bluetooth MAC, even in programming mode.
- `switchback check` reports ports, DNS, Bluetooth and firmware without changing anything.

### Notes

- Direct mode is experimental. Portal mode is the proven path.
