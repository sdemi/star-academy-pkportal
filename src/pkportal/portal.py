"""Portal REST API client, media downloader, and incremental sync orchestration."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import httpx

from pkportal import auth
from pkportal.config import Config, ensure_runtime_dirs
from pkportal.metadata import VIDEO_EXTENSIONS, apply_album_date_metadata
from pkportal.state import StateDB

_STAR_PP_RE = re.compile(r"window\.StarPP\s*=\s*(\{.*?\});", re.DOTALL)
_INVALID_PATH_CHARS_RE = re.compile(r'[/\\:\x00-\x1f\x7f*?"<>|]+')

_MIME_EXT_MAP = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/gif": ".gif",
    "image/tiff": ".tiff",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-m4v": ".m4v",
    "video/3gpp": ".3gp",
}


@dataclass
class MediaItem:
    media_key: str
    album_key: str
    url: str
    filename: str
    media_type: str  # "photo" | "video"
    album_date: date
    album_index: int
    item_id: int = 0


@dataclass
class AlbumInfo:
    album_key: str
    url: str
    class_name: str
    season_name: str
    title: str
    album_date: date
    items: list[MediaItem] = field(default_factory=list)


def season_for_date(d: date) -> str:
    """Map a calendar date to the academic season label (e.g., Fall 2026, Winter 2026-2027)."""
    if d.month in (9, 10, 11):
        return f"Fall {d.year}"
    if d.month == 12:
        return f"Winter {d.year}-{d.year + 1}"
    if d.month in (1, 2):
        return f"Winter {d.year - 1}-{d.year}"
    if d.month in (3, 4, 5):
        return f"Spring {d.year}"
    return f"Summer {d.year}"


def sanitize_path_component(value: str) -> str:
    """Sanitize a string for safe use as a macOS folder or file name component."""
    cleaned = _INVALID_PATH_CHARS_RE.sub("-", str(value or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "untitled"


def build_album_dir(
    output_dir: Path,
    group_name: str,
    season_name: str,
    album_date: date,
    *,
    group_by_day_subfolder: bool = True,
    campus: str | None = None,
) -> Path:
    """Build destination directory: <output>/<Class - Season>[/<YYYY-MM-DD>]."""
    parts = [sanitize_path_component(campus)] if campus else []
    parts.extend([sanitize_path_component(group_name), sanitize_path_component(season_name)])
    season_dir = output_dir / " - ".join(parts)
    if group_by_day_subfolder:
        return season_dir / album_date.isoformat()
    return season_dir


def parse_content_disposition_filename(header_value: str | None) -> str | None:
    """Extract and sanitize the filename from a Content-Disposition header."""
    if not header_value:
        return None

    raw_name: str | None = None
    ext_match = re.search(
        r"filename\*\s*=\s*(?:[^']*'[^']*')?\"?([^;\"]+)\"?",
        header_value,
        re.IGNORECASE,
    )
    if ext_match:
        raw_name = unquote(ext_match.group(1).strip())
    else:
        plain_match = re.search(
            r'filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;\s]+)',
            header_value,
            re.IGNORECASE,
        )
        if plain_match:
            raw_name = (plain_match.group(1) or plain_match.group(2) or "").strip()

    if not raw_name:
        return None

    basename = Path(raw_name.replace("\\", "/")).name
    if not basename or basename in (".", ".."):
        return None
    sanitized = sanitize_path_component(basename)
    if not sanitized or sanitized in (".", "..", "untitled"):
        return None
    return sanitized


def _portal_base_url(portal_url: str) -> str:
    parts = urlsplit(portal_url)
    path = parts.path or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _guess_extension(content_type: str) -> str:
    if content_type in _MIME_EXT_MAP:
        return _MIME_EXT_MAP[content_type]
    guessed = mimetypes.guess_extension(content_type) or ".bin"
    if guessed == ".jpe":
        return ".jpg"
    return guessed


class PortalClient:
    """Authenticated HTTP client for the Star Preschool Portal WordPress REST API."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.portal_base_url = _portal_base_url(cfg.portal_url)
        self.client = self._build_httpx_client()
        self.pt: str = ""
        self.rest: str = ""
        self.nonce: str = ""
        self._auth_lock = threading.Lock()
        self._path_lock = threading.Lock()
        self._reserved_paths: set[Path] = set()

    def _build_httpx_client(self) -> httpx.Client:
        cookies = auth.load_httpx_cookies(self.cfg.storage_state_path)
        headers = {"User-Agent": auth.load_user_agent(self.cfg)}
        return httpx.Client(
            cookies=cookies,
            headers=headers,
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, read=120.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> PortalClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _post_with_retry(self, url: str, *, data: dict | None = None) -> httpx.Response:
        for attempt in range(3):
            try:
                resp = self.client.post(url, data=data)
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise RuntimeError(f"Network error contacting portal: {exc}") from exc
                time.sleep(0.5 * (2**attempt))
                continue

            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt < 2:
                    time.sleep(0.5 * (2**attempt))
                    continue
                raise RuntimeError(
                    f"Portal temporarily unavailable (HTTP {resp.status_code}). Try again shortly."
                )
            return resp
        raise RuntimeError("Portal temporarily unavailable.")

    def _fetch_session_tokens(self) -> bool:
        r_who = self._post_with_retry(self.portal_base_url, data={"sp_whoami": "1"})
        if r_who.status_code in (401, 403):
            return False
        if r_who.status_code != 200:
            return False
        try:
            who = r_who.json()
        except ValueError:
            return False

        if not isinstance(who, dict) or not (who.get("parent") or who.get("staff")):
            return False

        self.pt = str(who.get("pt") or "")

        r_app = self._post_with_retry(self.portal_base_url)
        if r_app.status_code != 200:
            return False
        match = _STAR_PP_RE.search(r_app.text)
        if not match:
            return False
        try:
            star_pp = json.loads(match.group(1))
        except ValueError:
            return False

        rest = str(star_pp.get("rest") or "").rstrip("/")
        nonce = str(star_pp.get("nonce") or "")
        if not rest or not nonce:
            return False

        self.rest = rest
        self.nonce = nonce
        return True

    def authenticate(self, *, relogin: bool = False) -> None:
        """Validate saved cookies or open Chrome interactively to sign in."""
        authenticated = False
        if not relogin and self.cfg.storage_state_path.exists():
            authenticated = self._fetch_session_tokens()

        if not authenticated:
            print(
                "Re-authenticating in Google Chrome..."
                if relogin
                else "Portal session missing or expired; opening Google Chrome to sign in..."
            )
            self.client.close()
            auth.interactive_login(self.cfg)
            self.client = self._build_httpx_client()
            authenticated = self._fetch_session_tokens()
            if not authenticated:
                raise RuntimeError("Portal authentication failed or was cancelled.")

        auth.merge_httpx_cookies_to_storage_state(self.client.cookies, self.cfg.storage_state_path)

    def api_get(self, path: str) -> dict:
        """Call a Star Preschool Portal REST GET endpoint using POST method override."""
        if not self.rest or not self.nonce:
            raise RuntimeError("PortalClient is not authenticated.")

        url = f"{self.rest}{path.replace('?', '&') if '?' in self.rest else path}"
        refreshed = False

        for attempt in range(3):
            headers = {
                "X-WP-Nonce": self.nonce,
                "Content-Type": "application/json",
            }
            if self.pt:
                headers["X-SP-Token"] = self.pt
                headers["X-HTTP-Method-Override"] = "GET"
            method = "POST" if self.pt else "GET"

            try:
                resp = self.client.request(method, url, headers=headers)
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise RuntimeError(f"Network error calling {path}: {exc}") from exc
                time.sleep(0.5 * (2**attempt))
                continue

            if resp.status_code in (401, 403) and not refreshed:
                refreshed = True
                with self._auth_lock:
                    if not self._fetch_session_tokens():
                        raise RuntimeError(f"Session expired while calling {path} (HTTP {resp.status_code}).")
                continue

            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(0.5 * (2**attempt))
                continue

            if resp.status_code != 200:
                raise RuntimeError(f"API request failed for {path} (HTTP {resp.status_code}).")

            try:
                data = resp.json()
            except ValueError as exc:
                raise RuntimeError(f"Invalid JSON returned from {path}.") from exc

            if not isinstance(data, dict):
                raise RuntimeError(f"Unexpected payload type returned from {path}.")
            return data

        raise RuntimeError(f"API request failed after retries for {path}.")

    def get_bootstrap(self) -> dict:
        return self.api_get("/bootstrap")

    def list_days(self, group_id: str | None = None) -> list[dict]:
        suffix = f"&group={quote(group_id)}" if group_id else ""
        return list(self.api_get(f"/gallery?days=1{suffix}").get("days") or [])

    def list_day_items(self, day_str: str, group_id: str) -> list[dict]:
        return list(self.api_get(f"/gallery?day={quote(day_str)}&group={quote(group_id)}").get("items") or [])

    def _reserve_unique_path(self, dest_dir: Path, filename: str, item_id: int) -> Path:
        with self._path_lock:
            candidate = dest_dir / filename
            if candidate in self._reserved_paths or (
                candidate.exists() and f"-{item_id}" not in candidate.stem
            ):
                stem = Path(filename).stem
                suffix = Path(filename).suffix
                candidate = dest_dir / f"{stem}_{item_id}{suffix}"
                counter = 2
                while candidate in self._reserved_paths or candidate.exists():
                    candidate = dest_dir / f"{stem}_{item_id}_{counter}{suffix}"
                    counter += 1
            self._reserved_paths.add(candidate)
            return candidate

    def download_media(
        self,
        item_id: int,
        dest_dir: Path,
        fallback_stem: str,
    ) -> tuple[Path, int, str, str]:
        """Download full-resolution media item atomically. Returns (path, size, sha256, media_type)."""
        canonical_url = f"{self.rest}/gallery/{item_id}/full"
        sep = "&" if "?" in canonical_url else "?"
        refreshed = False

        for attempt in range(3):
            with self._auth_lock:
                nonce = self.nonce
                pt = self.pt
            query = f"_wpnonce={quote(nonce)}"
            if pt:
                query += f"&sp_pt={quote(pt)}"
            query += "&download=1"
            dl_url = f"{canonical_url}{sep}{query}"

            headers = {"X-WP-Nonce": nonce}
            if pt:
                headers["X-SP-Token"] = pt

            tmp_path = dest_dir / f".part.{item_id}.{threading.get_ident()}.tmp"
            try:
                with self.client.stream("GET", dl_url, headers=headers) as resp:
                    if resp.status_code in (401, 403) and not refreshed:
                        refreshed = True
                        with self._auth_lock:
                            if not self._fetch_session_tokens():
                                raise RuntimeError(
                                    f"Session expired downloading item {item_id} (HTTP {resp.status_code})."
                                )
                        continue

                    if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                        time.sleep(0.5 * (2**attempt))
                        continue

                    if resp.status_code != 200:
                        raise RuntimeError(
                            f"Download failed for item {item_id} at {canonical_url} (HTTP {resp.status_code})."
                        )

                    content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if content_type in ("text/html", "application/json", "text/plain"):
                        raise RuntimeError(
                            f"Unexpected content-type '{content_type}' for item {item_id} at {canonical_url}."
                        )

                    filename = parse_content_disposition_filename(resp.headers.get("content-disposition"))
                    if not filename:
                        filename = f"{sanitize_path_component(fallback_stem)}{_guess_extension(content_type)}"

                    media_type = (
                        "video"
                        if (content_type.startswith("video/") or Path(filename).suffix.lower() in VIDEO_EXTENSIONS)
                        else "photo"
                    )

                    hasher = hashlib.sha256()
                    total_bytes = 0
                    with tmp_path.open("wb") as f:
                        for chunk in resp.iter_bytes(chunk_size=65536):
                            if not chunk:
                                continue
                            f.write(chunk)
                            hasher.update(chunk)
                            total_bytes += len(chunk)

                    final_path = self._reserve_unique_path(dest_dir, filename, item_id)
                    os.replace(tmp_path, final_path)
                    return final_path, total_bytes, hasher.hexdigest(), media_type

            except httpx.TransportError as exc:
                tmp_path.unlink(missing_ok=True)
                if attempt == 2:
                    raise RuntimeError(f"Network error downloading item {item_id} at {canonical_url}: {exc}") from exc
                time.sleep(0.5 * (2**attempt))
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise

        raise RuntimeError(f"Download failed after retries for item {item_id} at {canonical_url}.")


def _select_target_groups(boot: dict) -> list[dict]:
    groups = list(boot.get("groups") or [])
    mine = [g for g in groups if g.get("mine")]
    if mine:
        return mine
    me_group_ids = set(boot.get("me", {}).get("groups") or [])
    matched = [g for g in groups if g.get("id") in me_group_ids]
    return matched or groups


def sync_portal(
    cfg: Config,
    *,
    dry_run: bool = False,
    relogin: bool = False,
    redownload_missing: bool = False,
    limit_days: int | None = None,
    limit_files: int | None = None,
    workers: int = 4,
) -> dict:
    """Sync albums and media from the Star Academy PK Portal."""
    ensure_runtime_dirs(cfg)

    summary = {
        "status": "ok",
        "dry_run": dry_run,
        "output_dir": str(cfg.output_dir),
        "groups": [],
        "albums_checked": 0,
        "files_found": 0,
        "files_downloaded": 0,
        "files_skipped": 0,
        "files_failed": 0,
        "errors": [],
        "planned": [] if dry_run else None,
    }

    with StateDB(cfg.state_db_path) as db, PortalClient(cfg) as client:
        run_id = None if dry_run else db.start_run()
        try:
            client.authenticate(relogin=relogin)
            boot = client.get_bootstrap()
            target_groups = _select_target_groups(boot)
            group_names = [str(g.get("name") or g.get("id") or "") for g in target_groups]
            has_duplicate_group_names = len(set(group_names)) < len(group_names)

            remaining_limit = limit_files
            max_workers = max(1, int(workers))

            for grp in target_groups:
                group_id = str(grp.get("id") or "")
                group_name = str(grp.get("name") or group_id)
                campus = str(grp.get("campus") or "") if has_duplicate_group_names else None
                summary["groups"].append({"id": group_id, "name": group_name, "campus": grp.get("campus")})

                days = client.list_days(group_id)
                days.sort(key=lambda d: str(d.get("day") or ""))
                if limit_days is not None and limit_days >= 0:
                    days = days[-limit_days:] if limit_days > 0 else []

                for day_rec in days:
                    if remaining_limit is not None and remaining_limit <= 0:
                        break

                    day_str = str(day_rec.get("day") or "")
                    if not day_str:
                        continue
                    album_date = date.fromisoformat(day_str)
                    season_name = season_for_date(album_date)
                    album_key = f"sp:{group_id}:{day_str}"
                    album_url = f"{client.rest}/gallery?day={quote(day_str)}&group={quote(group_id)}"

                    items = client.list_day_items(day_str, group_id)
                    summary["albums_checked"] += 1
                    summary["files_found"] += len(items)

                    if not dry_run:
                        db.record_album(
                            album_key=album_key,
                            url=album_url,
                            class_name=group_name,
                            season_name=season_name,
                            title=day_str,
                            album_date=day_str,
                            item_count=len(items),
                        )

                    dest_dir = build_album_dir(
                        cfg.output_dir,
                        group_name,
                        season_name,
                        album_date,
                        group_by_day_subfolder=cfg.group_by_day_subfolder,
                        campus=campus,
                    )

                    pending: list[tuple[int, int]] = []
                    for idx, item in enumerate(items):
                        item_id = int(item["id"])
                        media_key = f"sp:{group_id}:{item_id}"
                        if db.has_media(media_key, verify_exists=redownload_missing):
                            summary["files_skipped"] += 1
                            continue
                        if remaining_limit is not None and len(pending) >= remaining_limit:
                            break
                        pending.append((idx, item_id))

                    if remaining_limit is not None:
                        remaining_limit -= len(pending)

                    if not pending:
                        continue

                    if dry_run:
                        summary["planned"].append(
                            {
                                "album_key": album_key,
                                "day": day_str,
                                "season": season_name,
                                "dest_dir": str(dest_dir),
                                "pending_count": len(pending),
                            }
                        )
                        continue

                    dest_dir.mkdir(parents=True, exist_ok=True)

                    def _process_one(album_index: int, item_id: int) -> dict:
                        fallback_stem = f"star-academy-{day_str}-{item_id}"
                        local_path, file_size, sha256_hex, media_type = client.download_media(
                            item_id, dest_dir, fallback_stem
                        )
                        meta_info = apply_album_date_metadata(
                            local_path,
                            album_date,
                            index=album_index,
                            tz_name=cfg.timezone,
                        )
                        return {
                            "album_index": album_index,
                            "item_id": item_id,
                            "media_key": f"sp:{group_id}:{item_id}",
                            "canonical_url": f"{client.rest}/gallery/{item_id}/full",
                            "local_path": local_path,
                            "file_size": file_size,
                            "sha256": sha256_hex,
                            "media_type": media_type,
                            "meta_info": meta_info,
                        }

                    with ThreadPoolExecutor(max_workers=max_workers) as pool:
                        futures = {
                            pool.submit(_process_one, album_idx, item_id): (album_idx, item_id)
                            for album_idx, item_id in pending
                        }
                        for fut in as_completed(futures):
                            album_idx, item_id = futures[fut]
                            try:
                                res = fut.result()
                                db.record_media(
                                    media_key=res["media_key"],
                                    album_key=album_key,
                                    media_url=res["canonical_url"],
                                    filename=res["local_path"].name,
                                    media_type=res["media_type"],
                                    album_date=day_str,
                                    album_index=res["album_index"],
                                    local_path=str(res["local_path"]),
                                    file_size=res["file_size"],
                                    sha256=res["sha256"],
                                    metadata_status="ok",
                                )
                                summary["files_downloaded"] += 1
                                print(
                                    f"Downloaded {res['local_path'].name} -> {dest_dir} "
                                    f"({res['meta_info']['target_datetime']})"
                                )
                            except Exception as exc:
                                summary["files_failed"] += 1
                                msg = f"Failed item {item_id} ({day_str}): {exc}"
                                summary["errors"].append(msg)
                                print(msg)

            auth.merge_httpx_cookies_to_storage_state(client.client.cookies, cfg.storage_state_path)
            if summary["files_failed"] > 0:
                summary["status"] = "partial_error"

            if run_id is not None:
                db.finish_run(
                    run_id,
                    status=summary["status"],
                    albums_checked=summary["albums_checked"],
                    files_downloaded=summary["files_downloaded"],
                    files_skipped=summary["files_skipped"],
                    error_message="\n".join(summary["errors"]) if summary["errors"] else None,
                )
        except Exception as exc:
            if run_id is not None:
                db.finish_run(
                    run_id,
                    status="error",
                    albums_checked=summary["albums_checked"],
                    files_downloaded=summary["files_downloaded"],
                    files_skipped=summary["files_skipped"],
                    error_message=str(exc),
                )
            raise

    if summary["planned"] is None:
        del summary["planned"]
    return summary
