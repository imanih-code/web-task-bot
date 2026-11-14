"""Reading the input table (Excel / CSV) and writing the results table."""
from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from webtaskbot.template import format_cell


@dataclass
class Table:
    path: Path
    columns: List[str]
    rows: List[Dict[str, Any]]      # data rows only, header excluded

    def __len__(self) -> int:
        return len(self.rows)


class DataError(Exception):
    pass


def _clean_header(values: Sequence[Any]) -> List[str]:
    cols: List[str] = []
    seen: Dict[str, int] = {}
    for i, v in enumerate(values):
        name = format_cell(v) or f"Columna{i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        cols.append(name)
    # drop trailing auto-named empty columns
    while cols and cols[-1].startswith("Columna") and values[len(cols) - 1] in (None, ""):
        cols.pop()
    return cols


def _is_empty_row(values: Sequence[Any]) -> bool:
    return all(v is None or (isinstance(v, str) and not v.strip()) for v in values)


def load_table(path: str | Path, sheet: Optional[str] = None) -> Table:
    p = Path(path)
    if not p.exists():
        raise DataError(f"no existe el archivo de datos: {p}")
    suffix = p.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        return _load_xlsx(p, sheet)
    if suffix in (".csv", ".txt", ".tsv"):
        return _load_csv(p)
    if suffix == ".xls":
        raise DataError("el formato .xls (Excel 97-2003) no está soportado: guárdalo como .xlsx o .csv")
    raise DataError(f"formato no soportado: {suffix} (usa .xlsx o .csv)")


def _load_xlsx(p: Path, sheet: Optional[str]) -> Table:
    from openpyxl import load_workbook

    wb = load_workbook(p, read_only=True, data_only=True)
    try:
        if sheet:
            if sheet not in wb.sheetnames:
                raise DataError(f"la hoja '{sheet}' no existe. Hojas: {', '.join(wb.sheetnames)}")
            ws = wb[sheet]
        else:
            ws = wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        header = None
        for values in it:
            if not _is_empty_row(values):
                header = list(values)
                break
        if header is None:
            raise DataError("la hoja está vacía")
        cols = _clean_header(header)
        rows = []
        for values in it:
            values = list(values)[: len(cols)]
            if _is_empty_row(values):
                continue
            values += [None] * (len(cols) - len(values))
            rows.append(dict(zip(cols, values)))
        return Table(p, cols, rows)
    finally:
        wb.close()


def _read_text(p: Path) -> str:
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise DataError("no pude leer la codificación del CSV")


def _load_csv(p: Path) -> Table:
    text = _read_text(p)
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.reader(text.splitlines(), delimiter=delimiter)
    header = None
    for values in reader:
        if not _is_empty_row(values):
            header = values
            break
    if header is None:
        raise DataError("el CSV está vacío")
    cols = _clean_header(header)
    rows = []
    for values in reader:
        if _is_empty_row(values):
            continue
        values = [v.strip() for v in values[: len(cols)]]
        values += [""] * (len(cols) - len(values))
        rows.append(dict(zip(cols, values)))
    return Table(p, cols, rows)


def parse_row_selection(spec: Optional[str], total: int) -> List[int]:
    """'1-10,15,20-' -> 1-based row numbers within [1, total]."""
    if not spec:
        return list(range(1, total + 1))
    chosen: List[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            start = int(a) if a.strip() else 1
            end = int(b) if b.strip() else total
        else:
            start = end = int(part)
        for n in range(max(start, 1), min(end, total) + 1):
            if n not in chosen:
                chosen.append(n)
    return chosen


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------
STATUS_COLUMNS = ["_fila", "_estado", "_error", "_intentos", "_segundos", "_captura"]


class ResultsWriter:
    """Appends one line per processed row to a CSV (crash-safe, opens in
    Excel thanks to the BOM and ';' separator) and writes an .xlsx copy at
    the end."""

    def __init__(self, run_dir: Path, data_columns: List[str], extra_columns: List[str]) -> None:
        self.run_dir = run_dir
        self.columns = STATUS_COLUMNS[:2] + data_columns + extra_columns + STATUS_COLUMNS[2:]
        self.csv_path = run_dir / "resultados.csv"
        self.xlsx_path = run_dir / "resultados.xlsx"
        self._rows: List[Dict[str, Any]] = []
        with self.csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
            csv.writer(fh, delimiter=";").writerow(self.columns)

    def add(self, record: Dict[str, Any]) -> None:
        self._rows.append(record)
        with self.csv_path.open("a", newline="", encoding="utf-8-sig") as fh:
            csv.writer(fh, delimiter=";").writerow(
                [format_cell(record.get(c)) for c in self.columns]
            )

    def finish(self) -> Path:
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = "Resultados"
        ws.append(self.columns)
        for r in self._rows:
            ws.append([r.get(c) if not isinstance(r.get(c), (list, dict)) else str(r.get(c))
                       for c in self.columns])
        ws.freeze_panes = "A2"
        wb.save(self.xlsx_path)
        return self.xlsx_path


def read_previous_ok_rows(results_csv: Path) -> List[int]:
    ok: List[int] = []
    try:
        text = results_csv.read_text(encoding="utf-8-sig")
    except OSError:
        return ok
    reader = csv.DictReader(text.splitlines(), delimiter=";")
    for rec in reader:
        if rec.get("_estado") == "OK":
            try:
                ok.append(int(rec["_fila"]))
            except (KeyError, ValueError):
                pass
    return ok
