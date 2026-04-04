"""
Source registry.

Each source plugs into the shared control/audio pipeline by implementing the
Source protocol (see base.py). The registry below is the single place that
new sources need to be wired in.
"""
from typing import Dict

from .base import Source, Station
from .fm import FmSource
from .nws import NwsSource
from .repeaters import RepeaterSource

_SOURCES: Dict[str, Source] = {
    NwsSource.key: NwsSource(),
    RepeaterSource.key: RepeaterSource(),
    FmSource.key: FmSource(),
}


def get(key: str) -> Source:
    """Return the source registered under `key`, or raise KeyError."""
    return _SOURCES[key]


def all_sources() -> Dict[str, Source]:
    return dict(_SOURCES)


__all__ = ["Source", "Station", "get", "all_sources"]
