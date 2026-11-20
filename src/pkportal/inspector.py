"""Interactive browser inspector to capture authenticated DOM and network patterns."""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError, Page, Response, sync_playwright

from pkportal.auth import launch_chrome_context, save_storage_state_atomic, summarize_cookies
from pkportal.config import Config, ensure_runtime_dirs

_MAX_RESPONSE_BODY_BYTES = 256 * 1024
_INTERESTING_RESOURCE_TYPES = {"document", "xhr", "fetch", "image", "media"}

_DOM_EXTRACT_JS = """
() => {
  const headings = Array.from(document.querySelectorAll('h1, h2, h3, h4'))
    .map(el => ({ tag: el.tagName, text: (el.innerText || '').trim().slice(0, 200) }))
    .filter(x => x.text.length > 0)
    .slice(0, 100);

  const links = Array.from(document.querySelectorAll('a[href]'))
    .map(a => ({
      text: (a.innerText || '').trim().slice(0, 150),
      href: a.href,
      cls: a.className || ''
    }))
    .slice(0, 500);

  const images = Array.from(document.querySelectorAll('img'))
    .map(img => {
      const parentLink = img.closest('a[href]');
      return {
        src: img.currentSrc || img.getAttribute('src') || '',
        data_src: img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || '',
        srcset: (img.getAttribute('srcset') || '').slice(0, 400),
        alt: (img.getAttribute('alt') || '').slice(0, 150),
        cls: img.className || '',
        naturalWidth: img.naturalWidth,
        naturalHeight: img.naturalHeight,
        parentHref: parentLink ? parentLink.href : null,
        parentAttrs: parentLink ? {
          cls: parentLink.className || '',
          data_fancybox: parentLink.getAttribute('data-fancybox'),
          data_src: parentLink.getAttribute('data-src'),
          download: parentLink.getAttribute('download')
        } : null
      };
    })
    .slice(0, 500);

  const videos = Array.from(document.querySelectorAll('video, video source, iframe'))
    .map(el => ({
      tag: el.tagName,
      src: el.getAttribute('src') || el.currentSrc || '',
      poster: el.getAttribute('poster') || '',
      type: el.getAttribute('type') || '',
      cls: el.className || ''
    }))
    .slice(0, 200);

  const buttons = Array.from(document.querySelectorAll('button, input[type="submit"], [role="button"]'))
    .map(el => ({
      tag: el.tagName,
      text: (el.innerText || el.getAttribute('value') || el.getAttribute('aria-label') || '').trim().slice(0, 120),
      id: el.id || '',
      cls: el.className || ''
    }))
    .slice(0, 150);

  const forms = Array.from(document.querySelectorAll('form'))
    .map(f => ({
      action: f.getAttribute('action') || '',
      method: f.getAttribute('method') || '',
      id: f.id || '',
      cls: f.className || '',
      inputs: Array.from(f.querySelectorAll('input, select')).map(i => ({
        tag: i.tagName,
        type: i.getAttribute('type') || '',
        name: i.getAttribute('name') || '',
        id: i.id || '',
        placeholder: i.getAttribute('placeholder') || ''
      }))
    }))
    .slice(0, 30);

  return {
    url: location.href,
    title: document.title,
    htmlLength: document.documentElement ? document.documentElement.outerHTML.length : 0,
    headings,
    links,
    images,
    videos,
    buttons,
    forms
  };
}
"""


def _slugify(value: str, max_len: int = 50) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return (slug[:max_len] or "page").strip("-")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_set_cookie_names(response: Response) -> list[str]:
    names: list[str] = []
    try:
        headers_arr = response.headers_array()
        for item in headers_arr:
            if item.get("name", "").lower() == "set-cookie":
                cookie_name = item.get("value", "").split("=", 1)[0].strip()
                if cookie_name:
                    names.append(cookie_name)
    except PlaywrightError:
        pass
    return names


def run_inspect(cfg: Config) -> Path:
    """Open Chrome interactively, capture DOM/network while user browses Photos, save cookies."""
    ensure_runtime_dirs(cfg)
    inspect_dir = cfg.inspect_dir
    inspect_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(inspect_dir, 0o700)
    pages_dir = inspect_dir / "pages"
    responses_dir = inspect_dir / "responses"

    for old in (pages_dir, responses_dir):
        if old.exists():
            shutil.rmtree(old)
    for fname in ("network.jsonl", "summary.json", "cookies_summary.json"):
        (inspect_dir / fname).unlink(missing_ok=True)

    pages_dir.mkdir(parents=True, exist_ok=True)
    responses_dir.mkdir(parents=True, exist_ok=True)

    network_log_path = inspect_dir / "network.jsonl"
    portal_host = urlparse(cfg.portal_url).netloc

    page_seq = 0
    resp_seq = 0
    last_sigs: dict[int, str] = {}
    captured_pages: list[dict] = []
    network_entries: list[dict] = []
    attached_pages: set[int] = set()

    def _save_cookies() -> list[dict]:
        try:
            save_storage_state_atomic(context, cfg.storage_state_path)
        except PlaywrightError:
            pass
        summary = summarize_cookies(cfg.storage_state_path)
        (inspect_dir / "cookies_summary.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
        return summary

    def _snapshot_page(page: Page, reason: str, *, force: bool = False) -> None:
        nonlocal page_seq
        try:
            if page.is_closed():
                return
            meta = page.evaluate(_DOM_EXTRACT_JS)
            html = page.content()
        except PlaywrightError:
            return

        sig = (
            f"{meta.get('url')}|{meta.get('htmlLength')}|"
            f"{len(meta.get('images', []))}|{len(meta.get('links', []))}|"
            f"{len(meta.get('videos', []))}"
        )
        pid = id(page)
        if not force and last_sigs.get(pid) == sig:
            return
        last_sigs[pid] = sig

        page_seq += 1
        parsed = urlparse(meta.get("url", ""))
        slug = _slugify(f"{parsed.path}_{parsed.query}" or meta.get("title", "page"))
        stem = f"{page_seq:03d}_{slug}"
        html_file = pages_dir / f"{stem}.html"
        meta_file = pages_dir / f"{stem}.meta.json"

        meta["captured_at"] = _now_iso()
        meta["reason"] = reason
        meta["frames"] = [f.url for f in page.frames]
        meta["html_file"] = str(html_file.name)

        html_file.write_text(html, encoding="utf-8")
        meta_file.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        captured_pages.append(
            {
                "seq": page_seq,
                "reason": reason,
                "url": meta.get("url"),
                "title": meta.get("title"),
                "html_file": html_file.name,
                "meta_file": meta_file.name,
                "image_count": len(meta.get("images", [])),
                "video_count": len(meta.get("videos", [])),
                "link_count": len(meta.get("links", [])),
            }
        )

    def _on_response(response: Response) -> None:
        nonlocal resp_seq
        try:
            req = response.request
            rtype = req.resource_type
            url = response.url
            parsed = urlparse(url)
            same_origin = parsed.netloc == portal_host
            if rtype not in _INTERESTING_RESOURCE_TYPES and not same_origin:
                return
            # Skip static font/css/js noise from same origin unless xhr/fetch/document
            if same_origin and rtype in {"stylesheet", "script", "font"}:
                return

            headers = response.headers
            ctype = headers.get("content-type", "")
            entry = {
                "timestamp": _now_iso(),
                "url": url,
                "method": req.method,
                "status": response.status,
                "resource_type": rtype,
                "content_type": ctype,
                "location": headers.get("location"),
                "set_cookie_names": _extract_set_cookie_names(response),
            }

            should_save_body = (
                rtype in {"xhr", "fetch"}
                or "json" in ctype.lower()
                or ("admin-ajax.php" in url or "wp-json" in url)
            )
            if should_save_body:
                try:
                    raw_body = response.body()
                    if len(raw_body) <= _MAX_RESPONSE_BODY_BYTES:
                        resp_seq += 1
                        resp_name = f"{resp_seq:03d}_{_slugify(parsed.path)}.txt"
                        (responses_dir / resp_name).write_bytes(raw_body)
                        entry["body_file"] = resp_name
                        entry["body_bytes"] = len(raw_body)
                except PlaywrightError:
                    pass

            network_entries.append(entry)
            with network_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except PlaywrightError:
            pass

    def _attach_page(page: Page) -> None:
        pid = id(page)
        if pid in attached_pages:
            return
        attached_pages.add(pid)
        page.on("domcontentloaded", lambda _p: _snapshot_page(page, "domcontentloaded"))
        page.on("load", lambda _p: (_save_cookies(), _snapshot_page(page, "load")))
        page.on(
            "framenavigated",
            lambda frame: _snapshot_page(page, "framenavigated")
            if frame == page.main_frame
            else None,
        )
        page.on("response", _on_response)

    with sync_playwright() as p:
        context = launch_chrome_context(p, cfg, headless=False)
        context.on("page", _attach_page)
        for existing in context.pages:
            _attach_page(existing)

        first_page = context.pages[0] if context.pages else context.new_page()
        _attach_page(first_page)

        print("Inspector running in Google Chrome.")
        print(f"1. Sign in at {cfg.portal_url}")
        print("2. Navigate to Photos and open 1-2 daily albums (and any video/lightbox if present).")
        print("3. Close the Chrome window when finished.")

        try:
            first_page.goto(cfg.portal_url, wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightError:
            pass
        _snapshot_page(first_page, "initial", force=True)
        _save_cookies()

        try:
            while context.pages:
                for pg in list(context.pages):
                    _snapshot_page(pg, "poll")
                _save_cookies()
                active = context.pages[0] if context.pages else None
                if active is None:
                    break
                try:
                    active.wait_for_timeout(2000)
                except PlaywrightError:
                    continue
        except KeyboardInterrupt:
            pass
        finally:
            cookie_summary = _save_cookies()
            try:
                context.close()
            except PlaywrightError:
                pass

    xhr_endpoints = sorted(
        {
            f"{e['method']} {urlparse(e['url']).scheme}://{urlparse(e['url']).netloc}{urlparse(e['url']).path}"
            for e in network_entries
            if e["resource_type"] in {"xhr", "fetch"} or "admin-ajax.php" in e["url"] or "wp-json" in e["url"]
        }
    )
    media_hosts = sorted(
        {
            urlparse(e["url"]).netloc
            for e in network_entries
            if e["resource_type"] in {"image", "media"}
        }
    )

    summary = {
        "captured_at": _now_iso(),
        "portal_url": cfg.portal_url,
        "inspect_dir": str(inspect_dir),
        "pages_captured": len(captured_pages),
        "pages": captured_pages,
        "network_events": len(network_entries),
        "xhr_endpoints": xhr_endpoints,
        "media_hosts": media_hosts,
        "cookies_count": len(cookie_summary),
        "cookies": cookie_summary,
    }
    summary_path = inspect_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"Capture complete. Summary written to {summary_path}")
    print(f"Pages captured: {len(captured_pages)}, Network events: {len(network_entries)}, Cookies saved: {len(cookie_summary)}")
    return summary_path
