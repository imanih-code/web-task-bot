"""Turning recorded literal values into `{{Columna}}` placeholders.

The trick that makes recording pleasant: record the task using the data of
the FIRST row of your spreadsheet. Every value you typed that matches a
cell of that row is replaced by a reference to its column, so the bot
fills in the right value for every other row.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any, Dict, List, Mapping, Optional, Tuple

from webtaskbot.template import PLACEHOLDER, format_cell

MIN_SUBSTRING = 3


@dataclass
class MapReport:
    mapped: List[Tuple[int, str, str]] = field(default_factory=list)      # step, before, after
    literal: List[Tuple[int, str]] = field(default_factory=list)          # step, value kept as is
    ambiguous: List[Tuple[int, str, List[str]]] = field(default_factory=list)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().casefold()


def _date_variants(value: Any) -> List[Tuple[str, str]]:
    """(rendered text, filter suffix) pairs for a date cell."""
    if isinstance(value, dt.datetime):
        d = value
    elif isinstance(value, dt.date):
        d = dt.datetime(value.year, value.month, value.day)
    else:
        return []
    fmts = ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y", "%d/%m/%y", "%d.%m.%Y"]
    return [(d.strftime(f), f" | date:{f}") for f in fmts]


def _candidates(row: Mapping[str, Any], date_format: str) -> List[Tuple[str, str]]:
    """(text as it would appear in a form, placeholder) for every cell."""
    out: List[Tuple[str, str]] = []
    for col, raw in row.items():
        text = format_cell(raw, date_format)
        if text:
            out.append((text, "{{%s}}" % col))
        for variant, suffix in _date_variants(raw):
            if variant != text:
                out.append((variant, "{{%s%s}}" % (col, suffix)))
        # Text cells that look like dates (CSV input) also get variants.
        if isinstance(raw, str):
            for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
                try:
                    d = dt.datetime.strptime(raw.strip(), fmt)
                except ValueError:
                    continue
                for variant, suffix in _date_variants(d):
                    if _norm(variant) != _norm(text):
                        out.append((variant, "{{%s%s}}" % (col, suffix)))
    return out


def map_value(value: str, row: Mapping[str, Any], date_format: str) -> Tuple[str, List[str]]:
    """Returns (templated value, ambiguous column names)."""
    if not value or PLACEHOLDER.search(value):
        return value, []
    cands = _candidates(row, date_format)
    exact = [ph for text, ph in cands if _norm(text) == _norm(value)]
    if exact:
        uniq = list(dict.fromkeys(exact))
        if len(uniq) > 1 and len(value.strip()) < MIN_SUBSTRING:
            return value, uniq      # "1" matches several columns: don't guess
        return uniq[0], uniq[1:] if len(uniq) > 1 else []

    # Partial: "Ana López" from Nombre=Ana + Apellido=López. Longest first so
    # "Ana María" wins over "Ana".
    result = value
    replaced = False
    for text, ph in sorted(cands, key=lambda c: -len(c[0])):
        if len(text) < MIN_SUBSTRING or "|" in ph:
            continue
        pattern = re.compile(r"(?<![\w{])" + re.escape(text) + r"(?![\w}])", re.IGNORECASE)
        # only replace outside placeholders already inserted
        pieces = re.split(r"(\{\{.*?\}\})", result)
        new_pieces = []
        for piece in pieces:
            if piece.startswith("{{"):
                new_pieces.append(piece)
            else:
                new_piece, n = pattern.subn(ph, piece)
                replaced = replaced or n > 0
                new_pieces.append(new_piece)
        result = "".join(new_pieces)
    return (result if replaced else value), []


def map_file(name: str, row: Mapping[str, Any], date_format: str) -> str:
    """'12345678.pdf' with DNI=12345678 -> '{{DNI}}.pdf'."""
    p = PurePath(name)
    templated, _ = map_value(name, row, date_format)
    if templated != name:
        return templated
    stem_t, _ = map_value(p.stem, row, date_format)
    if stem_t != p.stem:
        return stem_t + p.suffix
    return name


TARGET_TEXT_KEYS = ("name", "text", "label")
TARGET_ACTIONS = ("click", "dblclick", "check", "uncheck", "hover")


def map_target(step: Dict[str, Any], row: Mapping[str, Any], date_format: str) -> Optional[str]:
    """Clicks whose element text comes from the data ("Parcial" radio, a
    link with the employee's name, a custom-dropdown option) become
    data-driven: candidates that mention a cell are templated, and the
    ones tied to row 1's specific element (id, css) are dropped.
    Returns the templated text, or None when nothing matched."""
    tgt = step.get("target") or {}
    new_find = []
    mapped_text = None
    for cand in tgt.get("find") or []:
        c2 = dict(cand)
        for k in TARGET_TEXT_KEYS:
            v = c2.get(k)
            if isinstance(v, str) and len(v.strip()) >= MIN_SUBSTRING:
                after, amb = map_value(v, row, date_format)
                if after != v and not amb:
                    c2[k] = after
                    mapped_text = mapped_text or after
        new_find.append(c2)
    if mapped_text is None:
        return None
    tgt["find"] = [c2 for c2 in new_find if any("{{" in str(v) for v in c2.values())]
    tgt["desc"] = re.sub(r'"[^"]*"', f'"{mapped_text}"', tgt.get("desc", ""), count=1) or mapped_text
    return mapped_text


def automap(
    steps: List[Dict[str, Any]], row: Mapping[str, Any], date_format: str
) -> MapReport:
    report = MapReport()
    for i, step in enumerate(steps, 1):
        action = step.get("action")
        if action in TARGET_ACTIONS:
            before = (step.get("target") or {}).get("desc", "")
            after = map_target(step, row, date_format)
            if after:
                report.mapped.append((i, before, f"elemento {after}"))
            continue
        if action in ("fill", "type", "assert_text"):
            key = "value" if action != "assert_text" else "text"
            before = str(step.get(key) or "")
            after, amb = map_value(before, row, date_format)
            if amb and after == before:
                report.ambiguous.append((i, before, amb))
            elif after != before:
                step[key] = after
                report.mapped.append((i, before, after))
            elif before and action != "assert_text":
                report.literal.append((i, before))
        elif action == "select":
            # Prefer the visible label: it is what usually comes from the sheet.
            for key in ("label", "value"):
                v = step.get(key)
                if isinstance(v, str) and v:
                    after, _ = map_value(v, row, date_format)
                    if after != v:
                        step[key] = after
                        report.mapped.append((i, v, after))
                        # The other field is a literal from row 1: drop it
                        # so other rows never fall back to row 1's option.
                        step.pop("value" if key == "label" else "label", None)
                        break
            else:
                report.literal.append((i, str(step.get("label") or step.get("value"))))
        elif action == "upload":
            files = step.get("files") or []
            new_files = []
            for f in files:
                nf = map_file(str(f), row, date_format)
                if nf != f:
                    report.mapped.append((i, str(f), nf))
                else:
                    report.literal.append((i, str(f)))
                new_files.append(nf)
            step["files"] = new_files
    return report
