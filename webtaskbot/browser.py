"""Launching the browser with a persistent profile.

The profile lives in `.wtb/profiles/<name>` so sessions (logins, cookies)
survive between `wtb record` and `wtb run`: log in once by hand and the
bot finds you already logged in. We prefer the Chrome or Edge that is
already installed; Playwright's own Chromium is the fallback.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Playwright

CHANNELS = ("chrome", "msedge", None)   # None = Playwright's bundled Chromium


class BrowserLaunchError(Exception):
    pass


def launch(
    pw: Playwright,
    profile_dir: Path,
    headless: bool = False,
    channel: Optional[str] = None,
    slow_mo: int = 0,
    downloads_dir: Optional[Path] = None,
) -> BrowserContext:
    profile_dir.mkdir(parents=True, exist_ok=True)
    channels = [channel] if channel else list(CHANNELS)
    errors = []
    for ch in channels:
        try:
            ctx = pw.chromium.launch_persistent_context(
                str(profile_dir),
                channel=None if ch in (None, "chromium") else ch,
                headless=headless,
                slow_mo=slow_mo,
                viewport=None if not headless else {"width": 1366, "height": 900},
                no_viewport=not headless,
                accept_downloads=True,
                downloads_path=str(downloads_dir) if downloads_dir else None,
                args=["--start-maximized"] if not headless else [],
                locale="es-ES",
            )
            return ctx
        except PlaywrightError as exc:
            errors.append(f"{ch or 'chromium'}: {str(exc).splitlines()[0]}")
    raise BrowserLaunchError(
        "no pude abrir ningún navegador.\n  " + "\n  ".join(errors) +
        "\nInstala Chrome, o ejecuta:  python -m playwright install chromium"
    )
