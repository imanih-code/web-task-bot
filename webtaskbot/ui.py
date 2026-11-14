"""Tiny terminal helpers (ANSI colours, enabled on Windows 10+)."""
from __future__ import annotations

import os
import sys

_COLOURS = {"red": "31", "green": "32", "yellow": "33", "blue": "34", "bold": "1", "dim": "2"}
_enabled = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

if _enabled and os.name == "nt":
    os.system("")  # turns on VT processing in the Windows console


def c(colour: str, text: str) -> str:
    if not _enabled:
        return text
    return f"\033[{_COLOURS[colour]}m{text}\033[0m"


def ask_yes_no(question: str, default: bool = False) -> bool:
    suffix = " [S/n] " if default else " [s/N] "
    try:
        answer = input(question + suffix).strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("s", "si", "sí", "y", "yes")
