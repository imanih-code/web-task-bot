"""Waiting for the page to "calm down" after each action.

Element lookup already waits for what the next step needs; this adds a
general safety net for apps that render the element early but are still
busy (saving, re-rendering, showing a spinner over everything):

1. no document/XHR/fetch request in flight for a short quiet period,
2. no visible loading indicator (spinners, aria-busy, progress bars).

Both are capped so a page that polls forever cannot hang the bot.
"""
from __future__ import annotations

import time
from typing import Dict, List

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Request

TRACKED_TYPES = {"document", "xhr", "fetch"}
QUIET_PERIOD = 0.35
DEFAULT_BUSY = [
    "[aria-busy='true']",
    ".spinner", ".spinner-border", ".spinner-grow", ".loading", ".loader",
    ".MuiCircularProgress-root", ".MuiLinearProgress-root", ".ant-spin-spinning",
    ".blockUI", ".v-progress-circular--indeterminate", ".el-loading-mask",
    ".p-progress-spinner", ".k-loading-mask", ".dataTables_processing",
    "[role='progressbar']:not([aria-valuenow])",
]

_BUSY_JS = """
(selectors) => {
  for (const sel of selectors) {
    let nodes;
    try { nodes = document.querySelectorAll(sel); } catch (e) { continue; }
    for (const el of nodes) {
      if (el.closest('[data-wtb-toolbar]')) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) continue;
      const st = getComputedStyle(el);
      if (st.visibility === 'hidden' || st.display === 'none' || parseFloat(st.opacity) === 0) continue;
      return sel;
    }
  }
  return null;
}
"""


class NetworkWatcher:
    """Counts in-flight requests per page."""

    def __init__(self) -> None:
        self._inflight: Dict[int, set] = {}
        self._last_change: Dict[int, float] = {}

    def attach(self, page: Page) -> None:
        key = id(page)
        if key in self._inflight:
            return
        self._inflight[key] = set()
        self._last_change[key] = time.monotonic()

        def started(req: Request) -> None:
            if req.resource_type in TRACKED_TYPES:
                self._inflight[key].add(req)
                self._last_change[key] = time.monotonic()

        def ended(req: Request) -> None:
            if req in self._inflight[key]:
                self._inflight[key].discard(req)
                self._last_change[key] = time.monotonic()

        page.on("request", started)
        page.on("requestfinished", ended)
        page.on("requestfailed", ended)

    def quiet(self, page: Page) -> bool:
        key = id(page)
        if not self._inflight.get(key):
            return time.monotonic() - self._last_change.get(key, 0.0) >= QUIET_PERIOD
        return False


def settle(page: Page, watcher: NetworkWatcher, max_wait: float, busy_selectors: List[str]) -> List[str]:
    """Returns warnings (e.g. 'still busy after N s'); never raises for
    slowness, only stops waiting."""
    warnings: List[str] = []
    deadline = time.monotonic() + max_wait
    selectors = DEFAULT_BUSY + list(busy_selectors or [])
    try:
        page.wait_for_load_state("domcontentloaded", timeout=max_wait * 1000)
    except PlaywrightError:
        pass
    busy_seen = None
    while time.monotonic() < deadline:
        if page.is_closed():
            return warnings
        net_ok = watcher.quiet(page)
        try:
            busy_seen = page.evaluate(_BUSY_JS, selectors)
        except PlaywrightError:
            busy_seen = "navegando"   # context destroyed mid-navigation
        if net_ok and not busy_seen:
            return warnings
        page.wait_for_timeout(100)
    if busy_seen:
        warnings.append(f"la página seguía ocupada ({busy_seen}) tras {max_wait:.0f} s; continúo")
    return warnings
