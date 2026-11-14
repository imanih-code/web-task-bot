"""`{{Columna}}` placeholders with optional filters.

    {{Nombre}}                     -> value of column "Nombre"
    {{Nombre | upper}}             -> filters: upper, lower, title, strip, digits, int
    {{Fecha | date:%Y-%m-%d}}      -> format a date cell
    {{Telefono | default:-}}       -> fallback when the cell is empty
    {{secret:CLAVE}}               -> asked once at run time (or WTB_SECRET_CLAVE)
"""
from __future__ import annotations

import datetime as dt
import getpass
import os
import re
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

PLACEHOLDER = re.compile(r"\{\{\s*(.+?)\s*\}\}")

DEFAULT_DATE_FORMAT = "%d/%m/%Y"


class TemplateError(Exception):
    pass


def format_cell(value: Any, date_format: str = DEFAULT_DATE_FORMAT) -> str:
    """How a raw spreadsheet cell becomes text in a form field."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "SI" if value else "NO"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, dt.datetime):
        if value.time() == dt.time(0, 0):
            return value.strftime(date_format)
        return value.strftime(date_format + " %H:%M")
    if isinstance(value, dt.date):
        return value.strftime(date_format)
    return str(value).strip()


def _as_date(value: Any) -> Optional[dt.datetime]:
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day)
    text = str(value).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _apply_filter(name: str, arg: Optional[str], raw: Any, text: str) -> str:
    if name == "upper":
        return text.upper()
    if name == "lower":
        return text.lower()
    if name == "title":
        return text.title()
    if name == "strip":
        return text.strip()
    if name == "digits":
        return re.sub(r"\D", "", text)
    if name == "int":
        try:
            return str(int(float(text.replace(",", "."))))
        except ValueError:
            raise TemplateError(f"'{text}' no es un número")
    if name == "default":
        return text if text else (arg or "")
    if name == "date":
        d = _as_date(raw if raw is not None else text)
        if d is None:
            if not text:
                return ""
            raise TemplateError(f"'{text}' no es una fecha reconocible")
        return d.strftime(arg or DEFAULT_DATE_FORMAT)
    raise TemplateError(f"filtro desconocido: {name}")


def find_column(columns: Iterable[str], wanted: str) -> Optional[str]:
    cols = list(columns)
    if wanted in cols:
        return wanted
    low = wanted.strip().lower()
    for c in cols:
        if c.strip().lower() == low:
            return c
    return None


def placeholders(text: str) -> List[str]:
    return PLACEHOLDER.findall(text or "")


def referenced_columns(text: str) -> List[str]:
    out = []
    for expr in placeholders(text):
        head = expr.split("|")[0].strip()
        if not head.startswith("secret:"):
            out.append(head)
    return out


def referenced_secrets(text: str) -> List[str]:
    out = []
    for expr in placeholders(text):
        head = expr.split("|")[0].strip()
        if head.startswith("secret:"):
            out.append(head[len("secret:"):].strip())
    return out


class Secrets:
    """Secrets are never written to disk: env var or asked once per run."""

    def __init__(self, ask: Callable[[str], str] | None = None) -> None:
        self._cache: Dict[str, str] = {}
        self._ask = ask or (lambda name: getpass.getpass(f"Valor para el secreto '{name}': "))

    def get(self, name: str) -> str:
        if name not in self._cache:
            env = os.environ.get(f"WTB_SECRET_{name.upper()}")
            self._cache[name] = env if env is not None else self._ask(name)
        return self._cache[name]


def render(
    text: Any,
    row: Mapping[str, Any],
    secrets: Optional[Secrets] = None,
    date_format: str = DEFAULT_DATE_FORMAT,
) -> str:
    if text is None:
        return ""
    text = str(text)

    def repl(m: re.Match) -> str:
        parts = [p.strip() for p in m.group(1).split("|")]
        head, filters = parts[0], parts[1:]
        if head.startswith("secret:"):
            if secrets is None:
                raise TemplateError("este paso necesita un secreto pero no hay de dónde leerlo")
            raw: Any = secrets.get(head[len("secret:"):].strip())
            value = str(raw)
        else:
            col = find_column(row.keys(), head)
            if col is None:
                raise TemplateError(
                    f"la columna '{head}' no existe. Columnas disponibles: {', '.join(row.keys())}"
                )
            raw = row[col]
            value = format_cell(raw, date_format)
        for f in filters:
            name, _, arg = f.partition(":")
            value = _apply_filter(name.strip(), arg if _ else None, raw, value)
            raw = value
        return value

    return PLACEHOLDER.sub(repl, text)
