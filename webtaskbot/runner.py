"""Running a bot once per row of the data table."""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from playwright.sync_api import BrowserContext, Error as PlaywrightError, FileChooser, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from webtaskbot import browser, locate, template
from webtaskbot.data import ResultsWriter, Table, parse_row_selection, read_previous_ok_rows
from webtaskbot.model import Bot, describe_step
from webtaskbot.settle import NetworkWatcher, settle
from webtaskbot.ui import c


class StepError(Exception):
    pass


class StopRun(Exception):
    pass


@dataclass
class RunOptions:
    rows: Optional[str] = None
    resume: bool = False
    retries: int = 1
    headless: bool = False
    slow_mo: int = 0
    step_mode: bool = False
    stop_on_error: bool = False
    files_dir: Optional[Path] = None
    channel: Optional[str] = None
    profile: str = "default"


@dataclass
class RunSummary:
    run_dir: Path
    ok: int = 0
    errors: int = 0
    skipped: int = 0
    interrupted: bool = False
    results_xlsx: Optional[Path] = None
    failed_rows: List[int] = field(default_factory=list)


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


class Runner:
    def __init__(
        self,
        bot: Bot,
        table: Table,
        opts: RunOptions,
        home: Path,
        secrets: Optional[template.Secrets] = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.bot = bot
        self.table = table
        self.opts = opts
        self.home = home
        self.secrets = secrets or template.Secrets()
        self.log = log
        self.files_dir = Path(opts.files_dir or bot.setting("files_dir") or table.path.parent)
        self.timeout = float(bot.setting("timeout"))
        self.date_format = str(bot.setting("date_format"))
        self.watcher = NetworkWatcher()
        self._chooser: Optional[FileChooser] = None
        self.ctx: Optional[BrowserContext] = None
        self.run_dir: Optional[Path] = None
        self._warned: set = set()

    # ------------------------------------------------------------ checks
    def preflight(self, row_numbers: List[int]) -> List[str]:
        """Problems that would make rows fail, found before opening a browser."""
        problems: List[str] = []
        missing_cols = [col for col in self.bot.required_columns()
                        if template.find_column(self.table.columns, col) is None]
        if missing_cols:
            problems.append(
                "columnas que usa el bot pero no están en los datos: " + ", ".join(missing_cols)
                + "\n    columnas disponibles: " + ", ".join(self.table.columns)
            )
            return problems
        dummy = template.Secrets(ask=lambda name: "x")
        for n in row_numbers:
            row = self.table.rows[n - 1]
            for i, step in enumerate(self.bot.steps, 1):
                try:
                    if step.get("action") == "upload":
                        for f in self._files_for(step, row, dummy):
                            if not f.exists():
                                problems.append(f"registro {n}, paso {i}: no existe el archivo {f}")
                    for key in ("value", "label", "text", "url", "save_as"):
                        v = step.get(key)
                        for item in (v if isinstance(v, list) else [v]):
                            if isinstance(item, str):
                                template.render(item, row, dummy, self.date_format)
                except template.TemplateError as exc:
                    problems.append(f"registro {n}, paso {i}: {exc}")
        return problems

    def selected_rows(self) -> List[int]:
        rows = parse_row_selection(self.opts.rows, len(self.table))
        if self.opts.resume:
            done = set(self.previous_ok_rows())
            rows = [r for r in rows if r not in done]
        return rows

    def previous_ok_rows(self) -> List[int]:
        base = self.home / "runs" / self.bot.name
        data_key = str(self.table.path.resolve())
        ok: List[int] = []
        if not base.exists():
            return ok
        for meta_path in base.glob("*/meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if meta.get("data") == data_key:
                ok.extend(read_previous_ok_rows(meta_path.parent / "resultados.csv"))
        return sorted(set(ok))

    # --------------------------------------------------------------- run
    def run(self, row_numbers: List[int]) -> RunSummary:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_dir = self.home / "runs" / self.bot.name / stamp
        (self.run_dir / "capturas").mkdir(parents=True, exist_ok=True)
        (self.run_dir / "meta.json").write_text(json.dumps({
            "bot": self.bot.name,
            "bot_file": str(self.bot.path) if self.bot.path else None,
            "data": str(self.table.path.resolve()),
            "started": dt.datetime.now().isoformat(timespec="seconds"),
            "rows": row_numbers,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        summary = RunSummary(self.run_dir)
        writer = ResultsWriter(self.run_dir, self.table.columns, self.bot.extract_columns())

        # Ask for secrets up front so the run is unattended afterwards.
        for name in self.bot.required_secrets():
            self.secrets.get(name)

        total = len(row_numbers)
        with sync_playwright() as pw:
            self.ctx = browser.launch(
                pw,
                self.home / ".wtb" / "profiles" / self.opts.profile,
                headless=self.opts.headless,
                channel=self.opts.channel,
                slow_mo=self.opts.slow_mo,
                downloads_dir=self.run_dir / "descargas",
            )
            self.ctx.set_default_timeout(self.timeout * 1000)
            for p in self.ctx.pages:
                self._wire_page(p)
            self.ctx.on("page", self._wire_page)
            try:
                for pos, n in enumerate(row_numbers, 1):
                    row = self.table.rows[n - 1]
                    record = self._run_row(n, row, pos, total)
                    writer.add(record)
                    if record["_estado"] == "OK":
                        summary.ok += 1
                    elif record["_estado"] == "OMITIDO":
                        summary.skipped += 1
                    else:
                        summary.errors += 1
                        summary.failed_rows.append(n)
                        if self.opts.stop_on_error:
                            self.log(c("yellow", "  Parando: --stop-on-error"))
                            break
            except (KeyboardInterrupt, StopRun):
                summary.interrupted = True
                self.log(c("yellow", "\n  Interrumpido. Los registros ya procesados quedan guardados."))
            finally:
                summary.results_xlsx = writer.finish()
                try:
                    self.ctx.close()
                except PlaywrightError:
                    pass
        return summary

    def _wire_page(self, page: Page) -> None:
        self.watcher.attach(page)
        page.on("filechooser", self._on_chooser)
        page.on("dialog", self._on_dialog)

    def _on_chooser(self, chooser: FileChooser) -> None:
        self._chooser = chooser

    def _on_dialog(self, dialog) -> None:
        try:
            if self.bot.setting("on_dialog") == "dismiss":
                dialog.dismiss()
            else:
                dialog.accept()
        except PlaywrightError:
            pass

    def _label_for(self, row: Dict[str, Any]) -> str:
        vals = [template.format_cell(v) for v in row.values() if template.format_cell(v)]
        return " · ".join(vals[:2])[:50]

    def _run_row(self, n: int, row: Dict[str, Any], pos: int, total: int) -> Dict[str, Any]:
        record: Dict[str, Any] = {"_fila": n, **row}
        attempts = max(1, self.opts.retries + 1)
        started = time.monotonic()
        last_error = ""
        for attempt in range(1, attempts + 1):
            extracted: Dict[str, Any] = {}
            try:
                self._reset_tabs()
                for i, step in enumerate(self.bot.steps, 1):
                    self._run_step(i, step, row, extracted)
                record.update(extracted)
                record.update({"_estado": "OK", "_error": "", "_intentos": attempt})
                break
            except (KeyboardInterrupt, StopRun):
                raise
            except _SkipRow:
                record.update({"_estado": "OMITIDO", "_error": "omitido a mano", "_intentos": attempt})
                break
            except Exception as exc:  # noqa: BLE001 - any failure fails only this row
                last_error = _friendly_error(exc)
                shot = self._screenshot(n, attempt)
                record.update({"_estado": "ERROR", "_error": last_error,
                               "_intentos": attempt, "_captura": shot})
                if attempt < attempts:
                    self.log(c("yellow", f"           intento {attempt} falló: {last_error} — reintento"))
        record["_segundos"] = round(time.monotonic() - started, 1)
        status = record["_estado"]
        colour = {"OK": "green", "ERROR": "red"}.get(status, "yellow")
        line = f"  [{pos:>{len(str(total))}}/{total}] registro {n:<4} {c(colour, f'{status:<7}')} " \
               f"{record['_segundos']:>5.1f}s  {self._label_for(row)}"
        self.log(line)
        if status == "ERROR":
            self.log(c("red", f"           {last_error}"))
        return record

    def _reset_tabs(self) -> None:
        pages = [p for p in self.ctx.pages if not p.is_closed()]
        if not pages:
            self.ctx.new_page()
            return
        for extra in pages[1:]:
            try:
                extra.close()
            except PlaywrightError:
                pass
        self._chooser = None

    def _screenshot(self, n: int, attempt: int) -> str:
        pages = [p for p in self.ctx.pages if not p.is_closed()] if self.ctx else []
        if not pages:
            return ""
        path = self.run_dir / "capturas" / f"registro-{n:04d}-intento{attempt}.png"
        try:
            pages[-1].screenshot(path=str(path))
            return str(path)
        except PlaywrightError:
            return ""

    # -------------------------------------------------------------- steps
    def _page(self, step: Dict[str, Any]) -> Page:
        idx = int(step.get("tab", 0))
        deadline = time.monotonic() + self.timeout
        while True:
            pages = [p for p in self.ctx.pages if not p.is_closed()]
            if len(pages) > idx:
                page = pages[idx]
                if idx:
                    page.bring_to_front()
                return page
            if time.monotonic() > deadline:
                raise StepError(f"no se abrió la pestaña {idx + 1} que esperaba el paso")
            (pages[0] if pages else self.ctx.new_page()).wait_for_timeout(200)

    def _files_for(self, step: Dict[str, Any], row: Dict[str, Any], secrets=None) -> List[Path]:
        files = step.get("files") or []
        if isinstance(files, str):
            files = [files]
        out = []
        for f in files:
            rendered = template.render(f, row, secrets or self.secrets, self.date_format)
            p = Path(rendered)
            out.append(p if p.is_absolute() else self.files_dir / p)
        return out

    def _render_target(self, tgt: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
        """Targets may hold placeholders (data-driven clicks)."""
        def render_cands(cands):
            out = []
            for cand in cands or []:
                out.append({k: (self._render(v, row) if isinstance(v, str) and "{{" in v else v)
                            for k, v in cand.items()})
            return out

        rendered = dict(tgt)
        rendered["find"] = render_cands(tgt.get("find"))
        if tgt.get("frames"):
            rendered["frames"] = [dict(f, find=render_cands(f.get("find"))) for f in tgt["frames"]]
        if "{{" in str(tgt.get("desc", "")):
            rendered["desc"] = self._render(tgt["desc"], row)
        return rendered

    def _render(self, value: Any, row: Dict[str, Any]) -> Any:
        if isinstance(value, list):
            return [template.render(v, row, self.secrets, self.date_format) for v in value]
        return template.render(value, row, self.secrets, self.date_format)

    def _run_step(self, i: int, step: Dict[str, Any], row: Dict[str, Any], extracted: Dict[str, Any]) -> None:
        self._current_row = row
        if self.opts.step_mode:
            self._pause_before(i, step)
        page = self._page(step)
        timeout = float(step.get("timeout", self.timeout))
        try:
            self._do(step, page, row, extracted, timeout)
        except locate.ElementNotFound as exc:
            if step.get("optional"):
                return
            raise StepError(f"paso {i} ({describe_step(step)}): {exc}") from None
        except (PlaywrightTimeout, PlaywrightError, StepError, template.TemplateError) as exc:
            if step.get("optional"):
                return
            raise StepError(f"paso {i} ({describe_step(step)}): {_friendly_error(exc)}") from None
        if step["action"] not in ("sleep", "extract", "assert_text", "wait_for"):
            for w in settle(page, self.watcher, float(self.bot.setting("settle_timeout")),
                            self.bot.setting("busy_selectors") or []):
                if w not in self._warned:
                    self._warned.add(w)
                    self.log(c("yellow", f"           aviso: {w}"))

    def _pause_before(self, i: int, step: Dict[str, Any]) -> None:
        page = self._page(step)
        if step.get("target"):
            try:
                locate.resolve(page, self._render_target(step["target"], self._current_row),
                               min(self.timeout, 5)).highlight()
            except Exception:  # noqa: BLE001 - highlighting is best-effort
                pass
        answer = input(f"    paso {i}: {describe_step(step)}  [Enter=ejecutar, s=saltar registro, q=salir] ")
        if answer.strip().lower() == "q":
            raise StopRun()
        if answer.strip().lower() == "s":
            raise _SkipRow()

    def _do(self, step: Dict[str, Any], page: Page, row: Dict[str, Any],
            extracted: Dict[str, Any], timeout: float) -> None:
        a = step["action"]
        ms = timeout * 1000

        def target():
            return locate.resolve(page, self._render_target(step["target"], row), timeout)

        if a == "goto":
            page.goto(self._render(step["url"], row), wait_until="domcontentloaded", timeout=ms)
        elif a == "click":
            target().click(timeout=ms)
        elif a == "dblclick":
            target().dblclick(timeout=ms)
        elif a == "hover":
            target().hover(timeout=ms)
        elif a in ("fill", "type"):
            value = self._render(step["value"], row)
            loc = target()
            if a == "type":
                loc.fill("", timeout=ms)
                loc.press_sequentially(value, delay=40, timeout=ms)
            else:
                loc.fill(value, timeout=ms)
                self._verify_fill(loc, value, ms)
        elif a == "select":
            self._select(target(), step, row, ms)
        elif a in ("check", "uncheck"):
            loc = target()
            try:
                loc.set_checked(a == "check", timeout=min(ms, 5000))
            except PlaywrightError:
                loc.set_checked(a == "check", force=True, timeout=ms)
        elif a == "upload":
            paths = self._files_for(step, row)
            for p in paths:
                if not p.exists():
                    raise StepError(f"no existe el archivo a subir: {p}")
            if self._chooser is not None and not self._chooser.page.is_closed():
                self._chooser.set_files([str(p) for p in paths], timeout=ms)
                self._chooser = None
            else:
                target().set_input_files([str(p) for p in paths], timeout=ms)
        elif a == "press":
            if step.get("target"):
                target().press(step["key"], timeout=ms)
            else:
                page.keyboard.press(step["key"])
        elif a == "wait_for":
            state = step.get("state", "visible")
            if step.get("target"):
                if state == "hidden":
                    if locate.exists(page, step["target"]):
                        target().wait_for(state="hidden", timeout=ms)
                else:
                    target().wait_for(state="visible", timeout=ms)
            elif step.get("text"):
                text = self._render(step["text"], row)
                loc = page.get_by_text(text).first
                loc.wait_for(state="hidden" if state == "hidden" else "visible", timeout=ms)
            elif step.get("url"):
                frag = self._render(step["url"], row)
                page.wait_for_url(lambda u: frag in u, timeout=ms)
        elif a == "assert_text":
            expected = _norm_text(self._render(step["text"], row))
            loc = target()
            deadline = time.monotonic() + timeout
            actual = ""
            while True:
                actual = _norm_text(_read(loc))
                if expected.casefold() in actual.casefold():
                    break
                if time.monotonic() > deadline:
                    raise StepError(f"esperaba encontrar {expected!r} pero decía {actual[:120]!r}")
                page.wait_for_timeout(250)
        elif a == "extract":
            loc = target()
            extracted[step["column"]] = _norm_text(_read(loc))
        elif a == "download":
            dest = Path(self._render(step["save_as"], row))
            if not dest.is_absolute():
                dest = self.run_dir / "descargas" / dest
            dest.parent.mkdir(parents=True, exist_ok=True)
            with page.expect_download(timeout=ms) as info:
                target().click(timeout=ms)
            info.value.save_as(str(dest))
            if step.get("column"):
                extracted[step["column"]] = str(dest)
        elif a == "back":
            page.go_back(wait_until="domcontentloaded", timeout=ms)
        elif a == "reload":
            page.reload(wait_until="domcontentloaded", timeout=ms)
        elif a == "sleep":
            page.wait_for_timeout(float(step["seconds"]) * 1000)
        else:
            raise StepError(f"acción desconocida: {a}")

    def _verify_fill(self, loc, value: str, ms: float) -> None:
        """Some fields (masks, React-controlled inputs) ignore a direct value
        set; if the field doesn't hold what we wrote, type it key by key."""
        try:
            current = loc.input_value(timeout=2000)
        except PlaywrightError:
            return  # contenteditable or similar: nothing to compare
        if current == value or _digits(current) == _digits(value) and _digits(value):
            return
        loc.fill("", timeout=ms)
        loc.press_sequentially(value, delay=35, timeout=ms)

    def _select(self, loc, step: Dict[str, Any], row: Dict[str, Any], ms: float) -> None:
        errors = []
        for key in ("value", "label"):
            if step.get(key) in (None, "", []):
                continue
            wanted = self._render(step[key], row)
            try:
                if key == "value":
                    loc.select_option(value=wanted, timeout=min(ms, 5000))
                else:
                    loc.select_option(label=wanted, timeout=min(ms, 5000))
                return
            except PlaywrightError as exc:
                errors.append(f"{key}={wanted!r}")
        # last resort: case-insensitive match on the visible option text
        wanted = self._render(step.get("label") or step.get("value"), row)
        wanted_list = wanted if isinstance(wanted, list) else [wanted]
        options = loc.locator("option").all_inner_texts()
        matches = [o for o in options if _norm_text(o).casefold() in
                   [_norm_text(w).casefold() for w in wanted_list]]
        if matches:
            loc.select_option(label=matches, timeout=ms)
            return
        raise StepError(f"la lista no tiene la opción {' / '.join(errors) or wanted!r}")


class _SkipRow(Exception):
    pass


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _read(loc) -> str:
    try:
        tag = loc.evaluate("el => el.tagName", timeout=2000)
    except PlaywrightError:
        tag = ""
    if tag in ("INPUT", "TEXTAREA", "SELECT"):
        return loc.input_value()
    return loc.inner_text()


def _friendly_error(exc: BaseException) -> str:
    msg = str(exc).strip()
    # Playwright errors carry a long call log after the first line.
    first = msg.split("\n")[0]
    first = re.sub(r"^(Locator|Page|Frame|ElementHandle)\.\w+: ", "", first)
    if isinstance(exc, PlaywrightTimeout) or "Timeout" in first and "exceeded" in first:
        return "tiempo de espera agotado: " + first
    return first or exc.__class__.__name__
