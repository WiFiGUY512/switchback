"""Switchback: convert a SwitchBot Plug Mini to ESPHome. No app, no cloud."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("switchback")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0.0.0"

__all__ = ["__version__"]
