"""Generic LSL helpers shared by every wahoo_lsl outlet (CPS now, FTMS control events later)."""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pylsl

from . import __version__

SOFTWARE = "wahoo_lsl"
_NOT_ALNUM = re.compile(r"[^A-Za-z0-9]")


@dataclass(frozen=True)
class Channel:
    label: str
    unit: str
    type: str  # "measured" or "derived"
    description: str = ""


def sanitize(text: str) -> str:
    """Keep ASCII letters and digits only (safe for stream names and source ids)."""
    return _NOT_ALNUM.sub("", text)


def add_dict(parent: Any, name: str, mapping: Mapping[str, object]) -> Any:
    """Append <name> to an XML element with one text child per non-None entry."""
    node = parent.append_child(name)
    for key, value in mapping.items():
        if value is not None:
            node.append_child_value(key, str(value))
    return node


def add_channels(desc: Any, channels: Sequence[Channel]) -> None:
    """Channel metadata in the XDF convention: desc/channels/channel/{label,unit,type,description}."""
    node = desc.append_child("channels")
    for ch in channels:
        add_dict(node, "channel", {"label": ch.label, "unit": ch.unit, "type": ch.type, "description": ch.description})


def stream_info(
    name: str,
    type_: str,
    source_id: str,
    channels: Sequence[Channel],
    nominal_srate: float,
    *,
    fmt: str = "double64",
    acquisition: Mapping[str, object] | None = None,
) -> pylsl.StreamInfo:
    info = pylsl.StreamInfo(name, type_, len(channels), nominal_srate, fmt, source_id)
    desc = info.desc()
    add_dict(desc, "acquisition", {"software": SOFTWARE, "version": __version__, **(acquisition or {})})
    add_channels(desc, channels)
    return info
