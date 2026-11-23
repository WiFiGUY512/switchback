"""The switchback command."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import ipaddress
import os
import platform
import socket
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from switchback import __version__, dns, firmware, image, label, wifiscan
from switchback import protocol as p
from switchback.server import FirmwareServer, Payloads

# Every converted plug is recorded here unless --log or --no-log says otherwise.
DEFAULT_LOG = Path.home() / "switchback-plugs.csv"

EXPERIMENTAL_DIRECT = (
    "Direct mode is experimental. Its Wi-Fi patch checks out, but it has not yet converted a real plug. "
    "If the plug cannot join, it opens its own hotspot instead, the same as portal mode."
)


def say(message: str = "") -> None:
    print(message, flush=True)


def local_ip() -> str:
    """The address other machines reach this one on. Nothing is sent to find it."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))  # TEST-NET-1, never routed; only picks the outgoing interface
        return str(s.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def port_free(port: int, kind: int, bind: str = "") -> str | None:
    """None if we can bind the port, otherwise why not."""
    s = socket.socket(socket.AF_INET, kind)
    try:
        s.bind((bind, port))
        return None
    except PermissionError:
        return "permission denied"
    except OSError as error:
        return error.strerror or str(error)
    finally:
        s.close()


def port_hint() -> str:
    if sys.platform.startswith("linux"):
        return "On Linux, ports below 1024 need root: run with sudo, or stop whatever holds the port."
    if sys.platform.startswith("win"):
        return "Close whatever uses the port. Windows may also ask once to allow the firewall."
    return "Close whatever uses the port."


# ---------------------------------------------------------------- firmware


def load_firmware(args: argparse.Namespace) -> firmware.FirmwareSet:
    if args.firmware_dir:
        return firmware.from_folder(Path(args.firmware_dir), log=say)
    return firmware.fetch(args.firmware_tag, log=say)


def load_payloads(args: argparse.Namespace) -> Payloads:
    fw = load_firmware(args)
    say(f"Firmware: {fw.source} ({'checksums verified' if fw.verified else 'NOT verified'})")
    portal = fw.portal
    if getattr(args, "payload", None):
        portal = Path(args.payload)
        say(f"Using your own payload instead of the portal build: {portal}")
    return Payloads.from_files(fw.app, portal, fw.direct)


# ---------------------------------------------------------------- prompts


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def ask_password(prompt: str = "Wi-Fi password: ") -> str:
    env = os.environ.get("SWITCHBACK_WIFI_PASS")
    if env is not None:
        return env
    while True:
        first = getpass.getpass(prompt)
        if not first:
            return first
        if getpass.getpass("Type it again: ") == first:
            return first
        say("They do not match. Try again.")


def choose_mode(args: argparse.Namespace) -> str:
    if args.mode:
        return str(args.mode)
    say("How should the plug get on Wi-Fi after it is converted?")
    say("  [1] portal  The plug opens a hotspot named sbplug-xxxxxx. You pick its Wi-Fi in a browser.")
    say("  [2] direct  The plug joins the Wi-Fi you type in now. (experimental)")
    return "direct" if ask("Choose 1 or 2 [1]: ").startswith("2") else "portal"


def pick_wifi() -> str:
    """Offer the Wi-Fi networks in range. Returns "" when the user should type a name instead."""
    networks, hidden = wifiscan.usable(wifiscan.scan())
    if not networks:
        return ""
    networks = networks[:15]
    say("Wi-Fi networks this computer can see:")
    for number, net in enumerate(networks, 1):
        note = "  (this computer is on it)" if net.current else ""
        say(f"  [{number}] {net.ssid}{note}")
    say("  [0] another one (type its name)")
    if hidden:
        say(f"  Left out: {hidden} network(s) that are 5 GHz only. The plug cannot join those.")
    while True:
        answer = ask("Choose a number [1]: ") or "1"
        if answer == "0":
            return ""
        if answer.isdigit() and 1 <= int(answer) <= len(networks):
            return networks[int(answer) - 1].ssid
        say(f"Type a number from 0 to {len(networks)}.")


def wifi_details(args: argparse.Namespace, mode: str) -> tuple[str, str]:
    ssid = args.wifi_ssid
    if not ssid:
        if mode == "direct":
            say("Which Wi-Fi should the plug join? It keeps this one. 2.4 GHz only.")
        else:
            say("The plug needs a Wi-Fi just to download the firmware. It does not keep it.")
            say("Use a plain 2.4 GHz network whose DNS sends wohand.com to the server.")
        ssid = pick_wifi() or ask("Wi-Fi name (SSID): ")
    if not ssid:
        return "", ""
    password = args.wifi_pass if args.wifi_pass is not None else ask_password(f"Password for {ssid}: ")
    return ssid, password


# ---------------------------------------------------------------- servers


def start_servers(args: argparse.Namespace, payloads: Payloads, ip: str, on_event: Any) -> FirmwareServer | None:
    try:
        server = FirmwareServer(payloads, port=args.http_port, on_event=on_event).start()
    except OSError as error:
        say(f"Could not serve on port {args.http_port}: {error}. {port_hint()}")
        return None
    say(f"Firmware server: http://{ip}:{server.port}/")
    if args.no_dns:
        say(f"Built-in DNS is off. Send wohand.com and www.wohand.com to {ip} in your own DNS.")
    else:
        try:
            dns.DnsServer(ip, args.dns_port, upstream=args.upstream_dns, bind=ip).start()
            say(f"Built-in DNS: {ip}:{args.dns_port}. It answers wohand.com with {ip} and forwards the rest.")
        except OSError as error:
            say(f"Could not start the built-in DNS on port {args.dns_port}: {error}.")
            say(f"Send wohand.com to {ip} in your own DNS instead, or free port {args.dns_port}.")
    return server


def report_dns(ip: str, args: argparse.Namespace) -> str | None:
    """Say whether wohand.com points here, and how to fix it if not. Returns what it resolves to."""
    dns.flush_cache()
    seen = dns.system_lookup("www.wohand.com")
    if seen == ip:
        say(f"DNS: www.wohand.com resolves to this computer ({ip}). Good.")
        return seen
    say(f"DNS: on this computer, www.wohand.com resolves to {seen or 'nothing'}, not {ip}.")
    if is_private(seen):
        say(f"  {seen} is another machine on your network, so a redirect is already in place and points THERE.")
        say("  A plug on a network with that redirect downloads from that machine, not from this computer.")
        return seen
    say("  That is fine if only the plugs' network uses the redirect. The plug must get this answer:")
    say(f"    wohand.com and www.wohand.com -> {ip}")
    say("  Ways to do it (the README has steps for common routers and DNS servers):")
    say(f"    1. Point the DHCP DNS server of the plugs' network at {ip} (the built-in DNS answers).")
    say(f"    2. Or add those two names as local records to {ip} in your own DNS server.")
    return seen


def is_private(address: str | None) -> bool:
    try:
        if address is None:
            return False
        parsed = ipaddress.ip_address(address)
        return parsed.is_private and not parsed.is_loopback
    except ValueError:
        return False


def switchback_server_at(address: str) -> bool:
    """True if a Switchback firmware server answers at this address ("<md5> <size>" on /payload.md5)."""
    try:
        with urllib.request.urlopen(f"http://{address}/payload.md5", timeout=3) as response:
            fields = response.read(100).decode("ascii", errors="replace").split()
    except (OSError, ValueError):
        return False
    return len(fields) == 2 and len(fields[0]) == 32 and fields[1].isdigit()


# ---------------------------------------------------------------- commands


def cmd_check(args: argparse.Namespace) -> int:
    problems = 0
    say(f"Switchback {__version__}, Python {platform.python_version()}, {platform.system()} {platform.release()}")
    try:
        from importlib.metadata import version

        import bleak  # noqa: F401  (loads the OS Bluetooth backend)

        say(f"Bluetooth library: bleak {version('bleak')}")
    except Exception as error:  # pragma: no cover - depends on the OS stack
        say(f"Bluetooth library: FAILED to load ({error})")
        problems += 1
    if args.ble:
        try:
            from switchback import ble

            plugs = asyncio.run(ble.scan(5.0, quiet=True))
            say(f"Bluetooth scan: OK, {len(plugs)} stock Plug Mini(s) in range")
        except Exception as error:
            say(f"Bluetooth scan: FAILED ({type(error).__name__}: {error})")
            problems += 1

    ip = args.ip or local_ip()
    say(f"This computer: {ip}")
    http_why = port_free(args.http_port, socket.SOCK_STREAM)
    say(f"Port {args.http_port}/tcp (firmware server): {'free' if http_why is None else 'NOT available, ' + http_why}")
    if http_why:
        problems += 1
        say(f"  {port_hint()}")
    if not args.no_dns:
        dns_why = port_free(args.dns_port, socket.SOCK_DGRAM, ip if ip != "127.0.0.1" else "")
        say(f"Port {args.dns_port}/udp (built-in DNS): {'free' if dns_why is None else 'NOT available, ' + dns_why}")
        if dns_why:
            say("  Run with --no-dns and redirect wohand.com in your own DNS, or free the port.")
    report_dns(ip, args)

    if args.firmware or args.firmware_dir:
        try:
            payloads = load_payloads(args)
            say(f"  app.bin {len(payloads.app)} bytes, portal payload {len(payloads.portal)} bytes")
            if payloads.direct:
                say(f"  direct payload {len(payloads.direct)} bytes, Wi-Fi patch self-check passed")
        except (firmware.FirmwareError, image.ImageError, OSError) as error:
            say(f"Firmware: FAILED ({error})")
            problems += 1
    else:
        say("Firmware: not checked. Add --firmware to download and verify it.")

    say("All good." if not problems else f"{problems} problem(s) found.")
    return 1 if problems else 0


def cmd_firmware(args: argparse.Namespace) -> int:
    fw = load_firmware(args)
    files = [fw.app, fw.portal] + ([fw.direct] if fw.direct else [])
    for path in files:
        say(f"  {path}  sha256 {firmware.sha256_file(path)}")
    Payloads.from_files(fw.app, fw.portal, fw.direct)
    say("Image checks passed.")
    if args.attest:
        for path in files:
            result = firmware.verify_attestation(path)
            if result is None:
                say("The gh CLI is not installed, so attestations were not checked.")
                break
            say(f"  attestation {path.name}: {'OK' if result else 'FAILED'}")
            if not result:
                return 1
    return 0


def _on_event_printer(entry: dict[str, Any]) -> None:
    if entry["event"] == "other":
        return
    extra = f" after {entry['seconds']}s" if "seconds" in entry else ""
    who = entry["mac"] or "unknown MAC"
    say(f"{time.strftime('%H:%M:%S')}  {entry['event']} from {entry['ip']} ({who}){extra}")
    if entry["event"] == "payload_done":
        say("           The plug has the ESPHome image. Leave it powered for at least a minute.")


def cmd_serve(args: argparse.Namespace) -> int:
    payloads = load_payloads(args)
    if args.mode == "direct":
        say(EXPERIMENTAL_DIRECT)
        ssid, password = wifi_details(args, "direct")
        payloads.select("direct", ssid, password)
    say(f"Serving the {payloads.mode} payload: {len(payloads.payload)} bytes, md5 {payloads.md5}")
    ip = args.ip or local_ip()
    if not start_servers(args, payloads, ip, _on_event_printer):
        return 1
    report_dns(ip, args)
    say("Waiting for plugs. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        say("Stopped.")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    from switchback import ble

    plugs = asyncio.run(ble.scan(args.scan_time, args.only))
    if not plugs:
        say("No stock Plug Minis found. Converted plugs no longer show up here.")
    for plug in plugs:
        say(f"{plug}  ->  {plug.name}")
    return 0


async def _pick_plug(args: argparse.Namespace, handled: set[str]) -> Any:
    plug, _ = await _find_plugs(args, handled)
    return plug


async def _find_plugs(args: argparse.Namespace, handled: set[str]) -> tuple[Any, int]:
    """The nearest stock plug not handled yet, and how many are in range."""
    from switchback import ble

    plugs = [plug for plug in await ble.scan(args.scan_time, args.only) if plug.ble_mac not in handled]
    return (plugs[0] if plugs else None), len(plugs)


def name_plug(plug: Any, payloads: Payloads, mode: str, ssid: str, password: str, typed: str) -> None:
    """Patch the name the user typed into the payload, or reset the payload to the unnamed one."""
    hostname = image.hostname_from(typed)
    if not hostname:
        payloads.select(mode, ssid, password)
        return
    friendly = typed.strip()
    while len(friendly.encode("utf-8")) > 63:
        friendly = friendly[:-1]
    payloads.select(mode, ssid, password, name=hostname, friendly_name=friendly)
    plug.name = hostname
    say(f"It will be {hostname!r}, shown as {friendly!r}.")


def convert_question(plug: Any, count: int) -> str:
    if count == 1:
        return f"Found 1 plug. It will become {plug.name}. Convert it? [y/N] "
    return f"Found {count} plugs. The nearest will become {plug.name}. Convert it? [y/N] "


def cmd_provision(args: argparse.Namespace) -> int:
    from switchback import ble

    ssid, password = wifi_details(args, "direct")
    if not ssid:
        say("No Wi-Fi name given.")
        return 1

    async def run() -> int:
        plug = await _pick_plug(args, set())
        if plug is None:
            say("No stock Plug Mini found.")
            return 1
        say(f"Setting Wi-Fi on {plug}")
        await ble.provision(plug.device, ssid, password)
        return 0

    try:
        return asyncio.run(run())
    except ble.Failed as error:
        say(f"FAILED: {error}")
        return 1


def cmd_convert(args: argparse.Namespace) -> int:
    from switchback import ble, convert

    if args.name and args.count > 1:
        say("--name names one plug. Leave it out with --count and you are asked for each plug.")
        return 1
    mode = choose_mode(args)
    if mode == "direct":
        say(EXPERIMENTAL_DIRECT)
    ssid, password = wifi_details(args, mode)
    if not ssid:
        say("No Wi-Fi name given, nothing to do.")
        return 1

    feed: convert.Feed
    payloads: Payloads | None = None
    if args.server:
        feed = convert.HttpFeed(args.server)
        try:
            say(feed.describe())
        except (OSError, ValueError) as error:
            say(f"The server at {args.server} is not answering: {error}")
            return 1
        if mode == "direct":
            say("With --server, the server decides the payload. Start it with 'switchback serve --mode direct'.")
    else:
        payloads = load_payloads(args)
        if mode == "direct":
            try:
                payloads.select("direct", ssid, password)
            except (ValueError, image.ImageError) as error:
                say(f"Could not prepare the direct payload ({error}). Using portal mode.")
                mode = "portal"
        ip = args.ip or local_ip()
        server = start_servers(args, payloads, ip, None)
        if not server:
            return 1
        seen = report_dns(ip, args)
        feed = convert.LocalFeed(server)
        say(f"Serving the {feed.describe()}")
        if seen != ip and is_private(seen):
            assert seen is not None
            if switchback_server_at(seen):
                say(f"A Switchback server is answering at {seen}. The plug will take its firmware from there,")
                say("in that server's mode, whatever was chosen here.")
                if args.yes or ask(f"Follow the server at {seen} instead of this computer? [Y/n] ").lower() != "n":
                    server.close()
                    payloads = None  # that server decides the payload, so no name can be patched in here
                    feed = convert.HttpFeed(f"http://{seen}")
                    say(feed.describe())
                    if mode == "direct":
                        say("Treating this run as portal mode: the plug will open its hotspot unless that")
                        say("server was started with 'switchback serve --mode direct'.")
                        mode = "portal"
            else:
                say(f"Nothing at {seen} looks like a Switchback server, so the plug would download nothing.")
                say(f"Fix the redirect so wohand.com and www.wohand.com point to {ip}, then run this again.")
                if args.yes or ask("Carry on anyway? [y/N] ").lower() != "y":
                    return 1

    opts = convert.Options(
        mode=mode,
        wifi_ssid=ssid,
        wifi_pass=password,
        fw_byte=args.fw_byte,
        stage1_timeout=args.stage1_timeout,
        timeout=args.timeout,
        domain=args.domain,
        log_file=None if args.no_log else Path(args.log) if args.log else DEFAULT_LOG,
        printer=args.printer,
    )
    say("Keep each plug powered the whole time. Unplugging during the write can brick it.")

    async def run() -> int:
        handled: set[str] = set()
        done = 0
        while done < args.count:
            say("Looking for stock Plug Minis over Bluetooth ...")
            plug, count = await _find_plugs(args, handled)
            if plug is None:
                say("No stock Plug Minis in range." if not handled else "No more stock Plug Minis in range.")
                break
            handled.add(plug.ble_mac)
            if not args.yes and (await asyncio.to_thread(ask, convert_question(plug, count))).lower() != "y":
                continue
            if payloads is not None and payloads.can_name(mode):
                typed = args.name or ""
                if not typed and not args.yes:
                    typed = await asyncio.to_thread(ask, f"Name for this plug (Enter keeps {plug.name}): ")
                try:
                    name_plug(plug, payloads, mode, ssid, password, typed)
                except (ValueError, image.ImageError) as error:
                    say(f"Could not use that name ({error}). It stays {plug.name}.")
                    payloads.select(mode, ssid, password)
            elif args.name:
                say(f"This firmware or server cannot take a name. It stays {plug.name}.")
            try:
                await convert.convert(plug, feed, opts)
                done += 1
            except ble.DeadTrigger as error:
                say(f"\nNOT CONVERTED: {error}")
                if args.count > 1:
                    continue
                return 2
            except ble.Failed as error:
                say(f"\nFAILED: {error}")
                return 1
        say(f"Converted {done} plug(s).")
        return 0

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        say("\nStopped. A plug that is mid-flash carries on by itself. Leave it powered.")
        return 130


def cmd_label(args: argparse.Namespace) -> int:
    if args.test:
        ble_mac, wifi_mac = "02:00:00:00:00:02", "02:00:00:00:00:00"
    else:
        ble_mac = p.normalize_mac(args.ble) or ""
        if not ble_mac:
            say("Give --ble <MAC> (or --test).")
            return 1
        wifi_mac = p.normalize_mac(args.wifi) or p.mac_offset(ble_mac, -2)
    zpl = label.render(ble_mac, wifi_mac)
    if not args.printer:
        print(zpl)
        return 0
    label.send(args.printer, zpl)
    say(f"Label sent to {args.printer}")
    return 0


# ---------------------------------------------------------------- parser


def _hex_byte(text: str) -> int:
    value = int(text, 16)
    if not 0 <= value <= 0xFF:
        raise argparse.ArgumentTypeError("a hex byte, 00 to FF")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchback",
        description="Convert a SwitchBot Plug Mini (W1901400/W1901401) to ESPHome. No app, no cloud.",
        epilog="This tool redirects wohand.com, SwitchBot's update host, to your own computer so a plug "
        "you own downloads ESPHome instead of SwitchBot firmware. See SECURITY.md.",
    )
    parser.add_argument("--version", action="version", version=f"switchback {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    fw = argparse.ArgumentParser(add_help=False)
    fw.add_argument("--firmware-tag", help="release to download firmware from (default: this version, or 'latest')")
    fw.add_argument("--firmware-dir", help="use firmware files from this folder instead of a release")
    fw.add_argument("--payload", help="serve this ESPHome factory image (your own build) instead of the portal one")

    net = argparse.ArgumentParser(add_help=False)
    net.add_argument("--ip", help="this computer's address to hand out (default: detected)")
    net.add_argument("--http-port", type=int, default=80, help="firmware server port (the plug always uses 80)")
    net.add_argument("--dns-port", type=int, default=53)
    net.add_argument("--no-dns", action="store_true", help="do not run the built-in DNS responder")
    net.add_argument("--upstream-dns", default="1.1.1.1", help="where the built-in DNS forwards other names")

    wifi = argparse.ArgumentParser(add_help=False)
    wifi.add_argument("--wifi-ssid", help="Wi-Fi name for the plug (asked for if omitted)")
    wifi.add_argument(
        "--wifi-pass",
        help="Wi-Fi password. Safer: leave it out to be asked, or set SWITCHBACK_WIFI_PASS",
    )

    blearg = argparse.ArgumentParser(add_help=False)
    blearg.add_argument("--only", help="only touch the plug with this Bluetooth MAC")
    blearg.add_argument("--scan-time", type=float, default=10.0, help="seconds per Bluetooth scan")

    c = sub.add_parser("check", parents=[fw, net], help="check this computer, ports, DNS and (optionally) firmware")
    c.add_argument("--ble", action="store_true", help="also run a short Bluetooth scan")
    c.add_argument("--firmware", action="store_true", help="also download and verify the firmware")
    c.set_defaults(func=cmd_check)

    f = sub.add_parser("firmware", parents=[fw], help="download and verify the firmware, show where it is")
    f.add_argument("--attest", action="store_true", help="also verify GitHub build attestations (needs gh)")
    f.set_defaults(func=cmd_firmware)

    s = sub.add_parser(
        "serve", parents=[fw, net, wifi], help="run only the fake update server and DNS (for the app re-onboard path)"
    )
    s.add_argument("--mode", choices=["portal", "direct"], default="portal")
    s.set_defaults(func=cmd_serve)

    sc = sub.add_parser("scan", parents=[blearg], help="list stock Plug Minis in Bluetooth range")
    sc.set_defaults(func=cmd_scan)

    pr = sub.add_parser("provision", parents=[blearg, wifi], help="only set a stock plug's Wi-Fi over Bluetooth")
    pr.set_defaults(func=cmd_provision)

    cv = sub.add_parser(
        "convert", aliases=["flash"], parents=[fw, net, wifi, blearg], help="convert plugs to ESPHome (all steps)"
    )
    cv.add_argument("--mode", choices=["portal", "direct"], help="portal (proven) or direct (experimental)")
    cv.add_argument("--server", help="use a 'switchback serve' on another box, e.g. http://192.0.2.10")
    cv.add_argument("--count", type=int, default=1, help="how many plugs to convert this run")
    cv.add_argument("--name", help="name for the plug, e.g. 'Garage Freezer' (asked for if omitted; one plug only)")
    cv.add_argument("--yes", action="store_true", help="do not ask before each plug")
    cv.add_argument(
        "--fw-byte",
        type=_hex_byte,
        default=p.DEFAULT_FW_BYTE,
        help="version byte for the OTA trigger, hex (default 0C). 0D and 0E are tried if it is refused.",
    )
    cv.add_argument("--stage1-timeout", type=float, default=900.0)
    cv.add_argument("--timeout", type=float, default=600.0, help="seconds allowed after stage 1")
    cv.add_argument("--domain", help="DNS suffix such as .lan, to find converted plugs across VLANs")
    cv.add_argument(
        "--log",
        help=f"CSV file that records each converted plug (default: {DEFAULT_LOG.name} in your home folder)",
    )
    cv.add_argument("--no-log", action="store_true", help="do not record converted plugs")
    cv.add_argument("--printer", help="ZPL label printer as host:port (off unless given)")
    cv.set_defaults(func=cmd_convert)

    lb = sub.add_parser("label", help="print (or show) a ZPL label for a plug")
    lb.add_argument("--printer", help="ZPL printer as host:port. Without it the ZPL is printed here.")
    lb.add_argument("--ble", help="Bluetooth MAC")
    lb.add_argument("--wifi", help="Wi-Fi MAC (default: Bluetooth MAC minus 2)")
    lb.add_argument("--test", action="store_true", help="a sample label")
    lb.set_defaults(func=cmd_label)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        code = args.func(args)
    except (firmware.FirmwareError, image.ImageError) as error:
        say(f"FAILED: {error}")
        code = 1
    except KeyboardInterrupt:
        say("\nStopped.")
        code = 130
    sys.exit(code)
