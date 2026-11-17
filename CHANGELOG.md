# Changelog

All notable changes are listed here. Versions follow [Semantic Versioning](https://semver.org/).

## [0.3.0] - unreleased

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
