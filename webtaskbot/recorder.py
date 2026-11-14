"""Recording: open a browser, let the person do the task once, and turn
what they did into steps."""
from __future__ import annotations

import re
import time
import unicodedata
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Frame, Page

from webtaskbot.model import describe_step

JS_PATH = Path(__file__).parent / "js" / "recorder.js"

# A page load this long after the last recorded action was not caused by
# that action: the person typed a URL or used a bookmark.
NAVIGATION_GRACE = 4.0


def _slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").upper()
    return text or "CLAVE"


class Recorder:
    def __init__(
        self,
        ctx: BrowserContext,
        start_url: str,
        on_step: Optional[Callable[[int, Dict[str, Any]], None]] = None,
    ) -> None:
        self.ctx = ctx
        self.start_url = start_url
        self.steps: List[Dict[str, Any]] = []
        self.stopped = False
        self.mode = "record"
        self._events: Deque[Tuple[float, Any, Any, Dict[str, Any]]] = deque()
        self._last_action_t = 0.0
        self._on_step = on_step or (lambda i, s: None)
        self._step_meta: List[Dict[str, Any]] = []   # parallel to steps (not saved)

        ctx.expose_binding("__wtb_record", self._on_binding)
        ctx.add_init_script(path=str(JS_PATH))
        ctx.on("close", lambda *_: setattr(self, "stopped", True))
        self.page: Page = ctx.pages[0] if ctx.pages else ctx.new_page()

    # ----------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._add({"action": "goto", "url": self.start_url}, page=self.page)
        try:
            self.page.goto(self.start_url, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            print(f"  aviso: la página inicial tardó o falló al cargar ({str(exc).splitlines()[0]})")
        self._last_action_t = time.monotonic()

    def pump(self, wait_ms: int = 150) -> None:
        """Let the browser run for a moment, then process what happened."""
        pages = self._open_pages()
        if not pages:
            self.stopped = True
            return
        try:
            pages[0].wait_for_timeout(wait_ms)
        except PlaywrightError:
            if not self._open_pages():
                self.stopped = True
        self._process()

    def run(self) -> List[Dict[str, Any]]:
        self.start()
        try:
            while not self.stopped:
                self.pump()
        except KeyboardInterrupt:
            self.stopped = True
        return self.finish()

    def finish(self) -> List[Dict[str, Any]]:
        """Flush half-typed fields and return the steps."""
        for page in self._open_pages():
            for frame in page.frames:
                try:
                    frame.evaluate("() => window.__wtbFlush && window.__wtbFlush()")
                except PlaywrightError:
                    pass
        try:
            pages = self._open_pages()
            if pages:
                pages[0].wait_for_timeout(100)
        except PlaywrightError:
            pass
        self._process()
        return self.steps

    # -------------------------------------------------------------- events
    def _on_binding(self, source: Dict[str, Any], payload: Dict[str, Any]) -> None:
        self._events.append((time.monotonic(), source.get("page"), source.get("frame"), payload))

    def _open_pages(self) -> List[Page]:
        try:
            return [p for p in self.ctx.pages if not p.is_closed()]
        except PlaywrightError:
            return []

    def _broadcast(self, script: str, arg: Any) -> None:
        for page in self._open_pages():
            for frame in page.frames:
                try:
                    frame.evaluate(script, arg)
                except PlaywrightError:
                    pass

    def _process(self) -> None:
        changed = False
        while self._events:
            t, page, frame, payload = self._events.popleft()
            kind = payload.get("type")
            if kind == "stop":
                self.stopped = True
                continue
            if kind == "set_mode":
                self.mode = payload.get("mode", "record")
                self._broadcast("m => window.__wtbSetMode && window.__wtbSetMode(m)", self.mode)
                continue
            if kind == "mode_done":
                self.mode = "record"
                self._broadcast("m => window.__wtbSetMode && window.__wtbSetMode(m)", self.mode)
                continue
            if kind == "pageload":
                self._on_pageload(t, page, payload)
                continue
            step = self._step_from(page, frame, payload)
            if step is not None:
                self._last_action_t = t
                self._add(step, page)
                changed = True
        if changed:
            self._broadcast("n => window.__wtbSetCount && window.__wtbSetCount(n)", len(self.steps))

    def _on_pageload(self, t: float, page: Page, payload: Dict[str, Any]) -> None:
        if t - self._last_action_t < NAVIGATION_GRACE:
            return
        nav = payload.get("navType")
        if nav == "back_forward":
            self._last_action_t = t
            self._add({"action": "back"}, page)
        elif nav == "navigate":
            url = payload.get("url")
            last = self.steps[-1] if self.steps else {}
            if url and not (last.get("action") == "goto" and last.get("url") == url):
                self._last_action_t = t
                self._add({"action": "goto", "url": url}, page)

    # --------------------------------------------------------------- steps
    def _frame_chain(self, page: Page, frame: Optional[Frame]) -> List[Dict[str, Any]]:
        if frame is None or page is None or frame == page.main_frame:
            return []
        chain: List[Dict[str, Any]] = []
        f = frame
        while f is not None and f.parent_frame is not None:
            entry: Optional[Dict[str, Any]] = None
            try:
                handle = f.frame_element()
                d = f.parent_frame.evaluate(
                    "el => window.__wtbDescribe ? window.__wtbDescribe(el) : null", handle
                )
                if d:
                    entry = {"desc": d.get("desc"), "find": d.get("candidates") or []}
            except PlaywrightError:
                entry = None
            if not entry or not entry["find"]:
                find = []
                if f.name:   # Playwright reports the name, or the id when unnamed
                    find.append({"css": f'iframe[name="{f.name}"], iframe[id="{f.name}"]'})
                path = re.sub(r"^[a-z]+://[^/]+", "", f.url.split("?")[0])
                if path and path != "/":
                    find.append({"css": f'iframe[src*="{path.rsplit("/", 1)[-1]}"]'})
                find.append({"css": "iframe"})
                entry = {"desc": f"marco {f.name or f.url}", "find": find}
            chain.insert(0, entry)
            f = f.parent_frame
        return chain

    def _target(self, page: Page, frame: Optional[Frame], d: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not d:
            return None
        tgt: Dict[str, Any] = {"desc": d.get("desc", ""), "find": d.get("candidates") or []}
        frames = d.get("frames")
        if frames is None:
            frames = self._frame_chain(page, frame)
        if frames:
            tgt["frames"] = frames
        return tgt

    def _step_from(self, page: Page, frame: Optional[Frame], p: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        kind = p.get("type")
        target = self._target(page, frame, p.get("target"))
        if kind in ("click", "check", "uncheck", "wait_for") and target:
            return {"action": kind, "target": target}
        if kind == "fill" and target:
            if p.get("password"):
                name = _slug(re.sub(r'^\w+\s*"?|"$', "", target["desc"]) or "CLAVE")
                return {"action": "fill", "target": target, "value": "{{secret:%s}}" % name}
            return {"action": "fill", "target": target, "value": p.get("value") or ""}
        if kind == "select" and target:
            values, labels = p.get("values") or [], p.get("labels") or []
            step = {"action": "select", "target": target,
                    "value": values[0] if len(values) == 1 else values,
                    "label": labels[0] if len(labels) == 1 else labels}
            if p.get("optional"):
                step["optional"] = True
            return step
        if kind == "upload" and target:
            files = p.get("files") or []
            if not files:
                return None
            return {"action": "upload", "target": target, "files": files}
        if kind == "press":
            step = {"action": "press", "key": p.get("key")}
            if target:
                step["target"] = target
            return step
        if kind == "assert_text" and target:
            return {"action": "assert_text", "target": target, "text": p.get("text") or ""}
        if kind == "extract" and target:
            return {"action": "extract", "target": target, "column": p.get("column") or "dato"}
        return None

    def _same_target(self, a: Dict[str, Any], b: Dict[str, Any]) -> bool:
        ta, tb = a.get("target") or {}, b.get("target") or {}
        return ta.get("find") == tb.get("find") and ta.get("frames") == tb.get("frames") \
            and a.get("tab", 0) == b.get("tab", 0)

    def _add(self, step: Dict[str, Any], page: Optional[Page]) -> None:
        try:
            idx = self.ctx.pages.index(page) if page is not None else 0
        except (ValueError, PlaywrightError):
            idx = 0
        if idx:
            step["tab"] = idx
        last = self.steps[-1] if self.steps else None
        # Successive edits of the same field collapse into one fill.
        if last and step["action"] == "fill" and last.get("action") == "fill" and self._same_target(last, step):
            last["value"] = step["value"]
            self._on_step(len(self.steps), last)
            return
        # A select re-chosen immediately replaces the previous choice.
        if last and step["action"] == "select" and last.get("action") == "select" and self._same_target(last, step):
            self.steps[-1] = step
            self._on_step(len(self.steps), step)
            return
        self.steps.append(step)
        self._on_step(len(self.steps), step)


def print_step(i: int, step: Dict[str, Any]) -> None:
    print(f"  {i:>3}. {describe_step(step)}")
