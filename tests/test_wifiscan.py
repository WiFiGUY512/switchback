from switchback import wifiscan
from switchback.cli import convert_question, is_private

NETSH = """
Interface name : Wi-Fi
There are 4 networks currently visible.

SSID 1 : Home Net
    Network type            : Infrastructure
    Authentication          : WPA2-Personal
    Encryption              : CCMP
    BSSID 1                 : aa:bb:cc:00:00:01
         Signal             : 61%
         Radio type         : 802.11ax
         Band               : 5 GHz
         Channel            : 149
    BSSID 2                 : aa:bb:cc:00:00:02
         Signal             : 88%
         Radio type         : 802.11n
         Band               : 2.4 GHz
         Channel            : 6

SSID 2 : Fast: Only
    Network type            : Infrastructure
    Authentication          : WPA3-Personal
    Encryption              : CCMP
    BSSID 1                 : aa:bb:cc:00:00:03
         Signal             : 70%
         Radio type         : 802.11ax
         Channel            : 36

SSID 3 :
    Network type            : Infrastructure
    BSSID 1                 : aa:bb:cc:00:00:04
         Signal             : 99%
         Channel            : 1

SSID 4 : IoT
    Network type            : Infrastructure
    BSSID 1                 : aa:bb:cc:00:00:05
         Signal             : 40%
         Channel            : 11
"""

NETSH_INTERFACES = """
    Name                   : Wi-Fi
    State                  : connected
    SSID                   : Home Net
    BSSID                  : aa:bb:cc:00:00:02
    Channel                : 6
"""


def by_name(networks):
    return {n.ssid: n for n in networks}


def test_netsh_networks():
    nets = by_name(wifiscan.parse_netsh_networks(NETSH))
    assert set(nets) == {"Home Net", "Fast: Only", "IoT"}  # the hidden one is dropped
    assert nets["Home Net"].band24 is True and nets["Home Net"].signal == 88
    assert nets["Fast: Only"].band24 is False
    assert nets["IoT"].band24 is True and nets["IoT"].signal == 40


def test_netsh_without_bssid_lines_keeps_the_names():
    nets = by_name(wifiscan.parse_netsh_networks("SSID 1 : Alpha\n    Network type : Infrastructure\nSSID 2 : Beta\n"))
    assert set(nets) == {"Alpha", "Beta"}
    assert nets["Alpha"].band24 is None


def test_netsh_german_channel_word():
    text = "SSID 1 : Netz\n    BSSID 1 : aa:bb:cc:00:00:01\n         Signal : 50%\n         Kanal : 3\n"
    assert wifiscan.parse_netsh_networks(text)[0].band24 is True


def test_netsh_current():
    assert wifiscan.parse_netsh_current(NETSH_INTERFACES) == "Home Net"
    assert wifiscan.parse_netsh_current("    State : disconnected\n") is None


def test_nmcli():
    text = "yes:82:2437 MHz:Home Net\nno:60:5180 MHz:Home Net\nno:55:5745 MHz:Fast\\: Only\nno:90:2412 MHz:\n"
    nets = by_name(wifiscan.parse_nmcli(text))
    assert set(nets) == {"Home Net", "Fast: Only"}
    assert nets["Home Net"].current and nets["Home Net"].band24 is True and nets["Home Net"].signal == 82
    assert nets["Fast: Only"].band24 is False


def test_networksetup():
    assert wifiscan.parse_networksetup("Current Wi-Fi Network: Home Net\n") == "Home Net"
    assert wifiscan.parse_networksetup("You are not associated with an AirPort network.\n") is None


def test_usable_drops_5ghz_only_and_orders_current_first():
    nets = wifiscan._order(wifiscan.parse_netsh_networks(NETSH))
    for net in nets:
        net.current = net.ssid == "IoT"
    keep, left_out = wifiscan.usable(wifiscan._order(nets))
    assert [n.ssid for n in keep] == ["IoT", "Home Net"]
    assert left_out == 1


def test_scan_never_raises(monkeypatch):
    monkeypatch.setattr(wifiscan, "_run", lambda cmd: "")
    assert wifiscan.scan() == []


class FakePlug:
    name = "sbplug-a1b2c3"


def test_convert_question():
    assert convert_question(FakePlug(), 1) == "Found 1 plug. It will become sbplug-a1b2c3. Convert it? [y/N] "
    assert convert_question(FakePlug(), 3).startswith("Found 3 plugs. The nearest will become sbplug-a1b2c3.")


def test_is_private():
    assert is_private("192.168.20.84") and is_private("10.1.10.96")
    assert not is_private("3.171.153.12") and not is_private("127.0.0.1") and not is_private(None)
