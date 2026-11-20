"""Google Photos Library API OAuth, album management, and idempotent uploader."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import secrets
import tempfile
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from pkportal.config import Config, ensure_runtime_dirs
from pkportal.state import StateDB

GPHOTOS_SCOPES = (
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
)
DEFAULT_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://photoslibrary.googleapis.com/v1/uploads"
ALBUMS_URL = "https://photoslibrary.googleapis.com/v1/albums"
BATCH_CREATE_URL = "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate"
BATCH_CREATE_MAX = 50
_UPLOAD_CHUNK_SIZE = 262144

_EXT_MIME_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
    ".3gp": "video/3gpp",
}


def _guess_mime_type(file_path: Path) -> str:
    ext = file_path.suffix.lower()
    if ext in _EXT_MIME_MAP:
        return _EXT_MIME_MAP[ext]
    guessed, _ = mimetypes.guess_type(str(file_path))
    return guessed or "application/octet-stream"


def _write_secret_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically with 0600 permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def load_client_secret(path: Path) -> dict[str, str]:
    """Load and validate a Google Cloud Desktop OAuth client_secret JSON file."""
    path = Path(path).expanduser()
    if not path.exists():
        raise RuntimeError(
            f"Google Photos OAuth client secret not found at {path}. "
            "Download a 'Desktop app' OAuth client JSON from Google Cloud Console "
            "and install it via: ./pkportal gphotos auth --client-secret /path/to/client_secret.json"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Failed to read Google OAuth client secret at {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise RuntimeError(f"Invalid Google OAuth client secret format in {path}.")

    installed = raw.get("installed")
    if not isinstance(installed, dict):
        if isinstance(raw.get("web"), dict):
            raise RuntimeError(
                f"Client secret at {path} is a 'Web application' OAuth client. "
                "Create and download a 'Desktop app' OAuth client ID in Google Cloud Console instead."
            )
        raise RuntimeError(
            f"Invalid Google OAuth client secret in {path}: expected top-level 'installed' object."
        )

    client_id = str(installed.get("client_id") or "").strip()
    client_secret = str(installed.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise RuntimeError(
            f"Invalid Google OAuth client secret in {path}: missing 'client_id' or 'client_secret'."
        )

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": str(installed.get("auth_uri") or DEFAULT_AUTH_URI).strip() or DEFAULT_AUTH_URI,
        "token_uri": str(installed.get("token_uri") or DEFAULT_TOKEN_URI).strip() or DEFAULT_TOKEN_URI,
    }


def install_client_secret(src_path: Path, cfg: Config) -> Path:
    """Validate and copy a Desktop OAuth client secret JSON into cfg.gphotos_client_secret_path."""
    src = Path(src_path).expanduser()
    load_client_secret(src)
    ensure_runtime_dirs(cfg)
    raw = json.loads(src.read_text(encoding="utf-8"))
    _write_secret_json(cfg.gphotos_client_secret_path, raw)
    return cfg.gphotos_client_secret_path


def _make_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_album_title(class_name: str | None, season_name: str | None) -> str:
    """Build default Google Photos album title from class and season names."""
    parts = [str(p).strip() for p in (class_name, season_name) if p and str(p).strip()]
    return " - ".join(parts) if parts else "Star Academy"


def authorize_gphotos(
    cfg: Config,
    *,
    client_secret_path: Path | None = None,
    timeout_seconds: int = 300,
    open_browser: bool = True,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Run loopback Desktop OAuth + PKCE flow and save refresh/access tokens with 0600 mode."""
    ensure_runtime_dirs(cfg)
    if client_secret_path is not None:
        install_client_secret(client_secret_path, cfg)

    client_cfg = load_client_secret(cfg.gphotos_client_secret_path)
    code_verifier, code_challenge = _make_pkce_pair()
    expected_state = secrets.token_urlsafe(32)
    callback_box: dict[str, dict[str, list[str]] | None] = {"params": None}

    class _CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Not found")
                return

            callback_box["params"] = parse_qs(parsed.query, keep_blank_values=True)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Google Photos authorization received.</h2>"
                b"<p>You can close this tab and return to the terminal.</p></body></html>"
            )

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            del format, args

    server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.timeout = 1.0
    try:
        port = server.server_address[1]
        redirect_uri = f"http://127.0.0.1:{port}/callback"
        query = urlencode(
            {
                "response_type": "code",
                "client_id": client_cfg["client_id"],
                "redirect_uri": redirect_uri,
                "scope": " ".join(GPHOTOS_SCOPES),
                "access_type": "offline",
                "prompt": "consent",
                "state": expected_state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        auth_url = f"{client_cfg['auth_uri']}?{query}"
        print(f"Authorize Google Photos access by opening:\n{auth_url}")
        if open_browser:
            webbrowser.open(auth_url)

        deadline = time.monotonic() + max(1, int(timeout_seconds))
        while callback_box["params"] is None and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    params = callback_box["params"]
    if params is None:
        raise RuntimeError(f"Timed out after {timeout_seconds}s waiting for Google OAuth callback.")

    if "error" in params:
        err = params["error"][0] if params["error"] else "unknown_error"
        desc = params.get("error_description", [""])[0]
        raise RuntimeError(f"Google OAuth authorization failed: {err}" + (f" ({desc})" if desc else ""))

    got_state = (params.get("state") or [""])[0]
    if not secrets.compare_digest(got_state, expected_state):
        raise RuntimeError("Google OAuth state mismatch; authorization aborted.")

    code = (params.get("code") or [""])[0]
    if not code:
        raise RuntimeError("Google OAuth callback did not include an authorization code.")

    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=30.0)
    try:
        resp = client.post(
            client_cfg["token_uri"],
            data={
                "code": code,
                "client_id": client_cfg["client_id"],
                "client_secret": client_cfg["client_secret"],
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            },
        )
    except httpx.TransportError as exc:
        raise RuntimeError(f"Network error exchanging OAuth code: {exc}") from exc
    finally:
        if owns_client:
            client.close()

    if resp.status_code != 200:
        raise RuntimeError(f"OAuth token exchange failed (HTTP {resp.status_code}): {resp.text}")

    try:
        token_resp = resp.json()
    except ValueError as exc:
        raise RuntimeError("Invalid JSON received from OAuth token endpoint.") from exc

    access_token = str(token_resp.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("OAuth token response missing access_token.")

    refresh_token = str(token_resp.get("refresh_token") or "").strip()
    if not refresh_token and cfg.gphotos_token_path.exists():
        try:
            prev = json.loads(cfg.gphotos_token_path.read_text(encoding="utf-8"))
            if isinstance(prev, dict) and prev.get("client_id") == client_cfg["client_id"]:
                refresh_token = str(prev.get("refresh_token") or "").strip()
        except (OSError, json.JSONDecodeError):
            pass

    now = int(time.time())
    expires_in = int(token_resp.get("expires_in") or 3600)
    expires_at = now + expires_in
    token_data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": str(token_resp.get("token_type") or "Bearer"),
        "scope": str(token_resp.get("scope") or " ".join(GPHOTOS_SCOPES)),
        "expires_at": expires_at,
        "token_uri": client_cfg["token_uri"],
        "client_id": client_cfg["client_id"],
        "obtained_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_secret_json(cfg.gphotos_token_path, token_data)

    return {
        "token_path": str(cfg.gphotos_token_path),
        "scopes": token_data["scope"].split(),
        "expires_at": expires_at,
        "has_refresh_token": bool(refresh_token),
    }


def gphotos_auth_status(cfg: Config) -> dict[str, Any]:
    """Return non-secret Google Photos OAuth credential and token status metadata."""
    client_secret_exists = cfg.gphotos_client_secret_path.exists()
    token_exists = cfg.gphotos_token_path.exists()
    token_valid = False
    has_refresh_token = False
    token_expires_at: int | float | None = None
    scopes: list[str] = []

    if token_exists:
        try:
            raw = json.loads(cfg.gphotos_token_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if isinstance(raw, dict):
            token_expires_at = raw.get("expires_at")
            has_refresh_token = bool(str(raw.get("refresh_token") or "").strip())
            scope_str = str(raw.get("scope") or "").strip()
            scopes = scope_str.split() if scope_str else []
            has_access = bool(str(raw.get("access_token") or "").strip())
            not_expired = (
                isinstance(token_expires_at, (int, float))
                and float(token_expires_at) > time.time() + 60
            )
            token_valid = (has_access and not_expired) or has_refresh_token

    return {
        "client_secret_exists": client_secret_exists,
        "client_secret_path": str(cfg.gphotos_client_secret_path),
        "token_exists": token_exists,
        "token_path": str(cfg.gphotos_token_path),
        "token_valid": token_valid,
        "has_refresh_token": has_refresh_token,
        "token_expires_at": token_expires_at,
        "scopes": scopes,
    }


def _iter_file_chunks(file_path: Path, chunk_size: int = _UPLOAD_CHUNK_SIZE) -> Iterator[bytes]:
    with file_path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            yield chunk


class GooglePhotosClient:
    """Authenticated HTTP client for the Google Photos Library API."""

    def __init__(self, cfg: Config, *, http_client: httpx.Client | None = None) -> None:
        self.cfg = cfg
        self.client_cfg = load_client_secret(cfg.gphotos_client_secret_path)
        if not cfg.gphotos_token_path.exists():
            raise RuntimeError(
                f"Google Photos OAuth token not found at {cfg.gphotos_token_path}. "
                "Authorize first with: ./pkportal gphotos auth"
            )
        try:
            token_data = json.loads(cfg.gphotos_token_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Failed to read Google Photos token at {cfg.gphotos_token_path}: {exc}") from exc
        if not isinstance(token_data, dict) or not token_data.get("access_token"):
            raise RuntimeError(
                f"Google Photos token at {cfg.gphotos_token_path} is invalid. Re-run: ./pkportal gphotos auth"
            )
        self._token: dict[str, Any] = token_data
        self._token_lock = threading.Lock()
        self._owns_client = http_client is None
        self.client = http_client or httpx.Client(
            timeout=httpx.Timeout(30.0, read=120.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> GooglePhotosClient:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        del exc_type, exc, tb
        self.close()

    def _is_token_fresh_locked(self) -> bool:
        access_token = str(self._token.get("access_token") or "").strip()
        expires_at = self._token.get("expires_at")
        return bool(
            access_token
            and isinstance(expires_at, (int, float))
            and time.time() < float(expires_at) - 120
        )

    def ensure_access_token(self, *, force_refresh: bool = False, failed_token: str | None = None) -> str:
        """Return a valid OAuth access token, refreshing with refresh_token when needed."""
        with self._token_lock:
            current = str(self._token.get("access_token") or "").strip()
            if force_refresh and failed_token is not None and current and current != failed_token:
                if self._is_token_fresh_locked():
                    return current
            if not force_refresh and self._is_token_fresh_locked():
                return current

            refresh_token = str(self._token.get("refresh_token") or "").strip()
            if not refresh_token:
                raise RuntimeError(
                    "Google Photos access token expired and no refresh_token is available. "
                    "Re-authorize via: ./pkportal gphotos auth"
                )

            token_uri = str(self._token.get("token_uri") or self.client_cfg["token_uri"])
            try:
                resp = self.client.post(
                    token_uri,
                    data={
                        "client_id": self.client_cfg["client_id"],
                        "client_secret": self.client_cfg["client_secret"],
                        "refresh_token": refresh_token,
                        "grant_type": "refresh_token",
                    },
                )
            except httpx.TransportError as exc:
                raise RuntimeError(f"Network error refreshing Google Photos token: {exc}") from exc

            if resp.status_code != 200:
                raise RuntimeError(
                    f"Google Photos token refresh failed (HTTP {resp.status_code}). "
                    "Re-authorize via: ./pkportal gphotos auth"
                )
            try:
                payload = resp.json()
            except ValueError as exc:
                raise RuntimeError("Invalid JSON received from Google OAuth refresh endpoint.") from exc

            new_access = str(payload.get("access_token") or "").strip()
            if not new_access:
                raise RuntimeError("Google OAuth refresh response missing access_token.")

            new_refresh = str(payload.get("refresh_token") or "").strip() or refresh_token
            expires_in = int(payload.get("expires_in") or 3600)
            self._token.update(
                {
                    "access_token": new_access,
                    "refresh_token": new_refresh,
                    "token_type": str(payload.get("token_type") or self._token.get("token_type") or "Bearer"),
                    "scope": str(payload.get("scope") or self._token.get("scope") or " ".join(GPHOTOS_SCOPES)),
                    "expires_at": int(time.time()) + expires_in,
                    "token_uri": token_uri,
                    "client_id": self.client_cfg["client_id"],
                }
            )
            _write_secret_json(self.cfg.gphotos_token_path, self._token)
            return new_access

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        content_factory: Callable[[], Any] | None = None,
    ) -> httpx.Response:
        refreshed = False
        attempt = 0
        while True:
            token = self.ensure_access_token()
            req_headers = dict(headers or {})
            req_headers["Authorization"] = f"Bearer {token}"
            content = content_factory() if content_factory is not None else None

            try:
                resp = self.client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=req_headers,
                    content=content,
                )
            except httpx.TransportError as exc:
                if attempt >= 2:
                    raise RuntimeError(f"Network error calling {url}: {exc}") from exc
                time.sleep(0.5 * (2**attempt))
                attempt += 1
                continue

            if resp.status_code == 401 and not refreshed:
                refreshed = True
                self.ensure_access_token(force_refresh=True, failed_token=token)
                continue

            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                delay = 0.5 * (2**attempt)
                if resp.status_code == 429:
                    ra = resp.headers.get("Retry-After")
                    if ra and ra.strip().isdigit():
                        delay = min(max(float(ra.strip()), delay), 30.0)
                time.sleep(delay)
                attempt += 1
                continue

            return resp

    def list_app_albums(self) -> list[dict[str, Any]]:
        """List writable albums created by this OAuth client."""
        albums: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "excludeNonAppCreatedData": "true",
                "pageSize": "50",
            }
            if page_token:
                params["pageToken"] = page_token
            resp = self._request_with_retry("GET", ALBUMS_URL, params=params)
            if resp.status_code != 200:
                raise RuntimeError(f"Failed to list Google Photos albums (HTTP {resp.status_code}): {resp.text}")
            data = resp.json()
            albums.extend(data.get("albums") or [])
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return albums

    def create_album(self, title: str) -> dict[str, Any]:
        """Create a new Google Photos album owned by this app."""
        resp = self._request_with_retry(
            "POST",
            ALBUMS_URL,
            json_body={"album": {"title": title}},
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Failed to create Google Photos album '{title}' (HTTP {resp.status_code}): {resp.text}"
            )
        return resp.json()

    def ensure_album(self, title: str, db: StateDB) -> tuple[str, str | None]:
        """Return (gphotos_album_id, product_url), reusing DB cache or app-created albums."""
        cached = db.get_gphotos_album(title)
        if cached:
            return str(cached["gphotos_album_id"]), cached.get("product_url")

        for album in self.list_app_albums():
            if album.get("title") == title and album.get("isWriteable", True) and album.get("id"):
                album_id = str(album["id"])
                product_url = album.get("productUrl")
                db.record_gphotos_album(album_title=title, gphotos_album_id=album_id, product_url=product_url)
                return album_id, product_url

        created = self.create_album(title)
        album_id = str(created.get("id") or "").strip()
        if not album_id:
            raise RuntimeError(f"Google Photos create_album response for '{title}' missing id.")
        product_url = created.get("productUrl")
        db.record_gphotos_album(album_title=title, gphotos_album_id=album_id, product_url=product_url)
        return album_id, product_url

    def upload_bytes(self, file_path: Path) -> str:
        """Stream raw file bytes to Google Photos /v1/uploads and return uploadToken."""
        file_size = file_path.stat().st_size
        mime_type = _guess_mime_type(file_path)
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(file_size),
            "X-Goog-Upload-Content-Type": mime_type,
            "X-Goog-Upload-Protocol": "raw",
        }
        try:
            file_path.name.encode("ascii")
        except UnicodeEncodeError:
            pass
        else:
            headers["X-Goog-Upload-File-Name"] = file_path.name

        resp = self._request_with_retry(
            "POST",
            UPLOAD_URL,
            headers=headers,
            content_factory=lambda: _iter_file_chunks(file_path),
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Google Photos byte upload failed for {file_path.name} (HTTP {resp.status_code}): {resp.text}"
            )
        token = resp.text.strip()
        if not token:
            raise RuntimeError(f"Google Photos byte upload returned empty uploadToken for {file_path.name}.")
        return token

    def batch_create(self, album_id: str | None, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Create Google Photos media items from uploadTokens in batches of up to 50."""
        all_results: list[dict[str, Any]] = []
        for idx in range(0, len(items), BATCH_CREATE_MAX):
            chunk = items[idx : idx + BATCH_CREATE_MAX]
            body: dict[str, Any] = {"newMediaItems": chunk}
            if album_id is not None:
                body["albumId"] = album_id
            resp = self._request_with_retry("POST", BATCH_CREATE_URL, json_body=body)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Google Photos mediaItems:batchCreate failed (HTTP {resp.status_code}): {resp.text}"
                )
            data = resp.json()
            all_results.extend(data.get("newMediaItemResults") or [])
        return all_results


def upload_to_gphotos(
    cfg: Config,
    *,
    dry_run: bool = False,
    album_title: str | None = None,
    no_album: bool = False,
    class_name: str | None = None,
    season_name: str | None = None,
    day: str | None = None,
    limit: int | None = None,
    workers: int = 4,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Upload locally synced media not yet recorded in gphotos_uploads to Google Photos."""
    ensure_runtime_dirs(cfg)

    summary: dict[str, Any] = {
        "status": "ok",
        "dry_run": dry_run,
        "albums": [],
        "files_pending": 0,
        "files_uploaded": 0,
        "files_missing": 0,
        "files_failed": 0,
        "errors": [],
        "planned": [] if dry_run else None,
    }

    with StateDB(cfg.state_db_path) as db:
        pending = db.list_pending_gphotos_media(
            class_name=class_name,
            season_name=season_name,
            day=day,
        )
        if limit is not None:
            pending = pending[: max(0, int(limit))]
        summary["files_pending"] = len(pending)

        readable_items: list[dict[str, Any]] = []
        for item in pending:
            local_path = Path(item["local_path"])
            try:
                local_path.stat()
            except FileNotFoundError:
                summary["files_missing"] += 1
                summary["errors"].append(
                    f"Missing local file {local_path} (run './pkportal sync --redownload-missing' to restore)"
                )
                continue
            except PermissionError as exc:
                summary["files_failed"] += 1
                summary["errors"].append(f"Permission denied reading {local_path}: {exc}")
                continue
            except OSError as exc:
                summary["files_failed"] += 1
                summary["errors"].append(f"Cannot read {local_path}: {exc}")
                continue
            readable_items.append(item)

        grouped: dict[str | None, list[dict[str, Any]]] = {}
        for item in readable_items:
            target_title = (
                None
                if no_album
                else (album_title or build_album_title(item.get("class_name"), item.get("season_name")))
            )
            grouped.setdefault(target_title, []).append(item)

        if dry_run:
            for target_title, items in grouped.items():
                summary["planned"].append(
                    {
                        "album_title": target_title,
                        "pending_count": len(items),
                        "days": sorted({str(it["album_date"]) for it in items}),
                    }
                )
            if summary["files_failed"] > 0:
                summary["status"] = "partial_error"
            return summary

        if not readable_items:
            if summary["files_failed"] > 0:
                summary["status"] = "partial_error"
            del summary["planned"]
            return summary

        max_workers = max(1, int(workers))
        with GooglePhotosClient(cfg, http_client=http_client) as client:
            for target_title, items in grouped.items():
                gphotos_album_id: str | None = None
                product_url: str | None = None
                if target_title is not None:
                    gphotos_album_id, product_url = client.ensure_album(target_title, db)

                uploaded_by_order: dict[int, tuple[dict[str, Any], str]] = {}
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futures = {
                        pool.submit(client.upload_bytes, Path(item["local_path"])): (order_idx, item)
                        for order_idx, item in enumerate(items)
                    }
                    for fut in as_completed(futures):
                        order_idx, item = futures[fut]
                        try:
                            upload_token = fut.result()
                            uploaded_by_order[order_idx] = (item, upload_token)
                        except FileNotFoundError:
                            summary["files_missing"] += 1
                            summary["errors"].append(
                                f"Missing local file {item['local_path']} "
                                "(run './pkportal sync --redownload-missing' to restore)"
                            )
                        except Exception as exc:
                            summary["files_failed"] += 1
                            summary["errors"].append(
                                f"Failed byte upload for {item['filename']} ({item['media_key']}): {exc}"
                            )

                ordered_uploads = [uploaded_by_order[i] for i in sorted(uploaded_by_order)]
                album_uploaded = 0

                for chunk_start in range(0, len(ordered_uploads), BATCH_CREATE_MAX):
                    chunk = ordered_uploads[chunk_start : chunk_start + BATCH_CREATE_MAX]
                    batch_payload = [
                        {
                            "description": (
                                f"{target_title or build_album_title(it.get('class_name'), it.get('season_name'))} "
                                f"({it['album_date']})"
                            ),
                            "simpleMediaItem": {
                                "uploadToken": tok,
                                "fileName": it["filename"],
                            },
                        }
                        for it, tok in chunk
                    ]
                    try:
                        results = client.batch_create(gphotos_album_id, batch_payload)
                    except Exception as exc:
                        summary["files_failed"] += len(chunk)
                        summary["errors"].append(f"batchCreate error: {exc}")
                        continue

                    results_by_token = {
                        str(r.get("uploadToken")): r
                        for r in results
                        if isinstance(r, dict) and r.get("uploadToken")
                    }
                    for idx, (item, tok) in enumerate(chunk):
                        res = results_by_token.get(tok)
                        if res is None and idx < len(results) and isinstance(results[idx], dict):
                            res = results[idx]
                        if not isinstance(res, dict):
                            summary["files_failed"] += 1
                            summary["errors"].append(
                                f"batchCreate missing result for {item['filename']} ({item['media_key']})"
                            )
                            continue

                        status = res.get("status") or {}
                        media_item = res.get("mediaItem") or {}
                        code = status.get("code")
                        media_id = str(media_item.get("id") or "").strip()
                        if code in (None, 0) and media_id:
                            db.record_gphotos_upload(
                                media_key=item["media_key"],
                                gphotos_media_id=media_id,
                                gphotos_album_id=gphotos_album_id,
                                album_title=target_title,
                                product_url=media_item.get("productUrl"),
                            )
                            summary["files_uploaded"] += 1
                            album_uploaded += 1
                        else:
                            err_msg = status.get("message") or f"code={code}"
                            summary["files_failed"] += 1
                            summary["errors"].append(
                                f"batchCreate failed for {item['filename']} ({item['media_key']}): {err_msg}"
                            )

                summary["albums"].append(
                    {
                        "title": target_title,
                        "id": gphotos_album_id,
                        "product_url": product_url,
                        "uploaded": album_uploaded,
                    }
                )

    if summary["files_failed"] > 0:
        summary["status"] = "partial_error"
    del summary["planned"]
    return summary
