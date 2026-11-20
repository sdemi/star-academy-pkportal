"""Browser session authentication and cookie persistence."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Callable

import httpx
from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page, sync_playwright

from pkportal.config import Config, ensure_runtime_dirs

CHROME_APP_PATH = Path("/Applications/Google Chrome.app")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def save_session_meta(cfg: Config, user_agent: str) -> None:
    """Persist non-secret session metadata (e.g., User-Agent) with 0600 permissions."""
    if user_agent:
        _write_json_atomic_private(cfg.session_meta_path, {"user_agent": user_agent})


def load_user_agent(cfg: Config) -> str:
    """Load the saved browser User-Agent or return DEFAULT_USER_AGENT."""
    if not cfg.session_meta_path.exists():
        return DEFAULT_USER_AGENT
    try:
        data = json.loads(cfg.session_meta_path.read_text(encoding="utf-8"))
        ua = str(data.get("user_agent") or "").strip()
        return ua or DEFAULT_USER_AGENT
    except (OSError, json.JSONDecodeError):
        return DEFAULT_USER_AGENT


def is_portal_authenticated_in_page(page: Page) -> bool:
    """Check whether the open Chrome tab holds an authenticated Star Preschool Portal session."""
    try:
        if page.is_closed():
            return False
        if "staracademyboston.com" not in (page.url or ""):
            return False
        res = page.evaluate(
            """async () => {
                try {
                    const r = await fetch('/pkportal/', {
                        method: 'POST',
                        credentials: 'same-origin',
                        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                        body: 'sp_whoami=1'
                    });
                    if (!r.ok) return null;
                    return await r.json();
                } catch (e) {
                    return null;
                }
            }"""
        )
        return bool(isinstance(res, dict) and (res.get("parent") or res.get("staff")))
    except PlaywrightError:
        return False


def _write_json_atomic_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def save_storage_state_atomic(context: BrowserContext, path: Path) -> dict:
    """Persist Playwright storage_state (cookies + origins) atomically with 0600 permissions."""
    state = context.storage_state()
    _write_json_atomic_private(path, state)
    return state


def summarize_cookies(storage_state_path: Path) -> list[dict]:
    """Return sanitized cookie metadata without secret cookie values."""
    if not storage_state_path.exists():
        return []
    try:
        state = json.loads(storage_state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    summary: list[dict] = []
    for c in state.get("cookies", []):
        summary.append(
            {
                "name": c.get("name"),
                "domain": c.get("domain"),
                "path": c.get("path"),
                "expires": c.get("expires"),
                "httpOnly": c.get("httpOnly"),
                "secure": c.get("secure"),
                "sameSite": c.get("sameSite"),
            }
        )
    return summary


def load_httpx_cookies(storage_state_path: Path) -> httpx.Cookies:
    """Load saved Playwright cookies into an httpx.Cookies jar."""
    jar = httpx.Cookies()
    if not storage_state_path.exists():
        return jar
    state = json.loads(storage_state_path.read_text(encoding="utf-8"))
    for c in state.get("cookies", []):
        name = c.get("name")
        value = c.get("value")
        if name is None or value is None:
            continue
        jar.set(
            name,
            value,
            domain=c.get("domain", ""),
            path=c.get("path", "/"),
        )
    return jar


def merge_httpx_cookies_to_storage_state(cookies: httpx.Cookies, storage_state_path: Path) -> None:
    """Merge updated/rolling cookies from httpx back into storage_state.json atomically."""
    if storage_state_path.exists():
        try:
            state = json.loads(storage_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {"cookies": [], "origins": []}
    else:
        state = {"cookies": [], "origins": []}

    existing: dict[tuple[str, str, str], dict] = {}
    for c in state.get("cookies", []):
        key = (c.get("name", ""), c.get("domain", ""), c.get("path", "/"))
        existing[key] = c

    for cookie in cookies.jar:
        key = (cookie.name, cookie.domain, cookie.path)
        prev = existing.get(key, {})
        existing[key] = {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path,
            "expires": float(cookie.expires) if cookie.expires is not None else prev.get("expires", -1),
            "httpOnly": prev.get("httpOnly", False),
            "secure": bool(cookie.secure),
            "sameSite": prev.get("sameSite", "Lax"),
        }

    state["cookies"] = list(existing.values())
    _write_json_atomic_private(storage_state_path, state)


def launch_chrome_context(playwright, cfg: Config, *, headless: bool = False) -> BrowserContext:
    """Launch persistent Google Chrome context storing profile outside cloud sync."""
    ensure_runtime_dirs(cfg)
    if not CHROME_APP_PATH.exists():
        raise RuntimeError(
            f"Google Chrome was not found at {CHROME_APP_PATH}. Please install Google Chrome."
        )
    try:
        return playwright.chromium.launch_persistent_context(
            user_data_dir=str(cfg.chrome_profile_dir),
            channel="chrome",
            headless=headless,
            no_viewport=True,
        )
    except PlaywrightError as exc:
        raise RuntimeError(
            f"Failed to launch Google Chrome via Playwright: {exc}"
        ) from exc


def interactive_login(
    cfg: Config,
    is_logged_in_fn: Callable[[Page], bool] | None = None,
    *,
    auto_close_on_auth: bool = True,
) -> list[dict]:
    """Open Chrome interactively so the user can sign in and persist cookies."""
    ensure_runtime_dirs(cfg)
    effective_check = is_logged_in_fn
    if effective_check is None and auto_close_on_auth:
        effective_check = is_portal_authenticated_in_page

    with sync_playwright() as p:
        context = launch_chrome_context(p, cfg, headless=False)
        page = context.pages[0] if context.pages else context.new_page()

        def _try_save() -> None:
            try:
                if context.pages:
                    save_storage_state_atomic(context, cfg.storage_state_path)
            except PlaywrightError:
                pass

        page.on("load", lambda _p: _try_save())

        try:
            page.goto(cfg.portal_url, wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightError:
            pass
        _try_save()

        try:
            ua = page.evaluate("() => navigator.userAgent")
            if isinstance(ua, str) and ua.strip():
                save_session_meta(cfg, ua.strip())
        except PlaywrightError:
            pass

        print(f"Opened Chrome to {cfg.portal_url}")
        if effective_check is None:
            print("Sign in inside Chrome, then close the Chrome window to save your session.")
        else:
            print("Sign in inside Chrome; the session will save and close automatically once authenticated.")

        try:
            while context.pages:
                active = context.pages[0]
                _try_save()
                if effective_check is not None:
                    try:
                        if effective_check(active):
                            try:
                                active.wait_for_timeout(1000)
                            except PlaywrightError:
                                pass
                            _try_save()
                            break
                    except PlaywrightError:
                        pass
                try:
                    active.wait_for_timeout(2000)
                except PlaywrightError:
                    # Page closed while waiting; loop re-checks context.pages.
                    continue
        except KeyboardInterrupt:
            _try_save()
        finally:
            _try_save()
            try:
                context.close()
            except PlaywrightError:
                pass

    return summarize_cookies(cfg.storage_state_path)
