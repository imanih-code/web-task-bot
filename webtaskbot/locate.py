"""Turning a recorded target back into a live element, robustly.

A target carries several candidate locators, best first. At run time we
poll all of them until one resolves to exactly one element (or exactly one
*visible* element), which both waits for slow pages and survives small
changes to the site: if the id changed but the button text did not, the
role/text candidate still finds it.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Union

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import FrameLocator, Locator, Page

Scope = Union[Page, FrameLocator]


class ElementNotFound(Exception):
    pass


def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build(scope: Scope, cand: Dict[str, Any]) -> Optional[Locator]:
    if "testid" in cand:
        return scope.locator(f'[{cand.get("attr", "data-testid")}="{_q(str(cand["testid"]))}"]')
    if "id" in cand:
        return scope.locator(f'[id="{_q(str(cand["id"]))}"]')
    if "role" in cand:
        if cand.get("name"):
            return scope.get_by_role(cand["role"], name=str(cand["name"]), exact=True)
        return scope.get_by_role(cand["role"])
    if "label" in cand:
        return scope.get_by_label(str(cand["label"]), exact=True)
    if "placeholder" in cand:
        return scope.get_by_placeholder(str(cand["placeholder"]), exact=True)
    if "text" in cand:
        return scope.get_by_text(str(cand["text"]), exact=True)
    if "css" in cand:
        return scope.locator(str(cand["css"]))
    if "xpath" in cand:
        return scope.locator("xpath=" + str(cand["xpath"]))
    return None


def _try_unique(scope: Scope, candidates: List[Dict[str, Any]], allow_first: bool) -> Optional[Locator]:
    fallback = None
    for cand in candidates:
        loc = build(scope, cand)
        if loc is None:
            continue
        try:
            n = loc.count()
            if n == 1:
                return loc
            if n > 1:
                visible = loc.filter(visible=True)
                if visible.count() == 1:
                    return visible
                if fallback is None and visible.count() > 1:
                    fallback = visible.first
        except PlaywrightError:
            continue
    return fallback if allow_first else None


def _resolve_scope(page: Page, frames: List[Dict[str, Any]], allow_first: bool) -> Optional[Scope]:
    scope: Scope = page
    for frame_target in frames:
        loc = _try_unique(scope, frame_target.get("find") or [], allow_first)
        if loc is None:
            return None
        scope = loc.content_frame
    return scope


def resolve(page: Page, target: Dict[str, Any], timeout: float) -> Locator:
    """Wait up to `timeout` seconds for the target to appear."""
    candidates = target.get("find") or []
    frames = target.get("frames") or []
    deadline = time.monotonic() + timeout
    # During the first part of the wait we insist on a unique match; near
    # the end we accept the first visible of several, rather than failing.
    lenient_after = time.monotonic() + timeout * 0.6
    delay = 0.1
    while True:
        lenient = time.monotonic() >= lenient_after
        scope = _resolve_scope(page, frames, lenient)
        if scope is not None:
            loc = _try_unique(scope, candidates, lenient)
            if loc is not None:
                return loc
        if time.monotonic() >= deadline:
            raise ElementNotFound(
                f"no encontré {target.get('desc') or 'el elemento'} después de {timeout:.0f} s"
            )
        page.wait_for_timeout(int(delay * 1000))
        delay = min(delay * 1.5, 0.5)


def exists(page: Page, target: Dict[str, Any]) -> bool:
    scope = _resolve_scope(page, target.get("frames") or [], True)
    return scope is not None and _try_unique(scope, target.get("find") or [], True) is not None
