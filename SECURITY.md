# Security

## What Switchback does, plainly

Switchback is a tool for converting SwitchBot Plug Minis you own to ESPHome. To do that it takes over the plug's firmware update. This is the full list of what it does on your computer and network.

**DNS redirect.** The stock plug downloads updates over plain HTTP from `wohand.com`. Switchback needs that name to resolve to your computer on the network the plug joins. Either:

- Switchback's built-in DNS responder answers on UDP port 53 of your computer's LAN address. It answers `wohand.com` and any name under it with your computer's address. It forwards every other query, unchanged, to one upstream resolver (`1.1.1.1` unless you set `--upstream-dns`), and returns the answer. It logs nothing and stores nothing. It only affects devices whose DHCP DNS server you point at your computer yourself.
- Or you add the two records to your own DNS server and run with `--no-dns`.

Switchback never changes your router, your DNS server, your computer's DNS settings or its hosts file. The one system change it makes is flushing your computer's own DNS cache (`ipconfig /flushdns`, `dscacheutil -flushcache` or `resolvectl flush-caches`) so its check sees fresh answers.

**Firmware push.** Switchback runs an HTTP server on TCP port 80. It serves exactly three things: the flasher (`app.bin`) for any `*.bin` request, `payload.md5`, and the ESPHome image (`payload.bin`). It also serves `/events`, a read-only list of which addresses fetched what, so a Bluetooth laptop can follow a conversion. It accepts no uploads and runs no commands.

**Bluetooth.** Switchback writes to SwitchBot's own GATT characteristic on Plug Minis it finds: the Wi-Fi details you give it, and the update trigger. It only touches a plug after you confirm (or pass `--yes`), and `--only <MAC>` limits it to one plug.

**Downloads.** Switchback downloads `SHA256SUMS` and the firmware files from this repository's GitHub Releases, over HTTPS, and refuses any file whose SHA-256 does not match. Verified files are cached in your user cache folder.

**Files it writes.** Besides that cache, `convert` appends one line per converted plug (time, name, both MAC addresses) to `switchback-plugs.csv` in your home folder. Nothing in it leaves your computer. `--no-log` turns it off.

**What it does not do.** It does not contact SwitchBot, does not collect or send telemetry, does not install a service, and does not keep running after you stop it.

## Why this is safe to publish

The DNS redirect and firmware push only work on a network you control, for devices you choose, and the redirect is something you set up and remove yourself. The plug's update path being plain HTTP is SwitchBot's design; Switchback uses it as intended for a device's owner.

## The bootstrap image

The ESPHome image Switchback writes has the API **without encryption** and the published OTA password `switchbota`, so one build can serve everyone. Until you flash your own config, anyone on the same network could talk to the plug's API or push an OTA update. Flash your own config, with your own key and password, as soon as the plug is up.

## Trusting the release files

- Every release file is built by GitHub Actions from the tagged commit, in this repository. Nothing is built or uploaded from a personal computer.
- Each file has a [build provenance attestation](https://docs.github.com/actions/security-for-github-actions/using-artifact-attestations). Check one with `gh attestation verify <file> -R WiFiGUY512/switchback`, or run `switchback firmware --attest`.
- `SHA256SUMS` protects against a broken or swapped download. The attestation proves where a file was built.
- There is no executable. Switchback is Python source you can read, installed with `uv` or `pipx`.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting: the **Security** tab of this repository, then **Report a vulnerability**. Do not open a public issue for a security problem. Expect a first answer within a week.
