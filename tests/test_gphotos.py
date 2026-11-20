"""Unit and integration tests for gphotos.py and Google Photos CLI integration."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import stat
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

import pkportal.cli as cli_mod
import pkportal.gphotos as gphotos_mod
from pkportal.config import Config
from pkportal.gphotos import GooglePhotosClient, authorize_gphotos, load_client_secret, upload_to_gphotos
from pkportal.state import StateDB


def _write_installed_secret(path: Path) -> Path:
    payload = {
        "installed": {
            "client_id": "test-client-id.apps.googleusercontent.com",
            "client_secret": "test-client-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _make_cfg(tmp_path: Path) -> Config:
    return Config(
        output_dir=tmp_path / "Pictures",
        app_support_dir=tmp_path / "data",
        logs_dir=tmp_path / "data" / "logs",
    )


def _seed_auth(cfg: Config, *, expires_at: int = 9999999999, access_token: str = "tok-initial") -> None:
    cfg.app_support_dir.mkdir(parents=True, exist_ok=True)
    _write_installed_secret(cfg.gphotos_client_secret_path)
    cfg.gphotos_token_path.write_text(
        json.dumps(
            {
                "access_token": access_token,
                "refresh_token": "refresh-initial",
                "token_type": "Bearer",
                "scope": " ".join(gphotos_mod.GPHOTOS_SCOPES),
                "expires_at": expires_at,
                "token_uri": gphotos_mod.DEFAULT_TOKEN_URI,
                "client_id": "test-client-id.apps.googleusercontent.com",
            }
        ),
        encoding="utf-8",
    )


def _seed_media_items(
    cfg: Config,
    count: int,
    *,
    class_name: str = "Group D",
    season_name: str = "Fall 2026",
    album_date: str = "2026-11-18",
    create_files: bool = True,
) -> list[Path]:
    media_dir = cfg.output_dir / f"{class_name} - {season_name}" / album_date
    media_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    with StateDB(cfg.state_db_path) as db:
        album_key = f"sp:TD:{album_date}"
        db.record_album(
            album_key=album_key,
            url=f"https://example.com/{album_date}",
            class_name=class_name,
            season_name=season_name,
            title=album_date,
            album_date=album_date,
            item_count=count,
        )
        for i in range(count):
            fname = f"star-academy-{album_date}-{1000 + i:04d}.jpg"
            fpath = media_dir / fname
            if create_files:
                fpath.write_bytes(f"jpeg-bytes-{i}".encode("utf-8"))
            db.record_media(
                media_key=f"sp:TD:{1000 + i}",
                album_key=album_key,
                media_url=f"https://example.com/full/{1000 + i}",
                filename=fname,
                media_type="photo",
                album_date=album_date,
                album_index=i,
                local_path=str(fpath),
                file_size=len(f"jpeg-bytes-{i}"),
                sha256=f"sha-{i}",
                metadata_status="ok",
            )
            paths.append(fpath)
    return paths


def test_load_client_secret_validation(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(RuntimeError, match="not found"):
        load_client_secret(missing)

    web_secret = tmp_path / "web.json"
    web_secret.write_text(
        json.dumps({"web": {"client_id": "id", "client_secret": "sec"}}),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="Desktop app"):
        load_client_secret(web_secret)

    good = _write_installed_secret(tmp_path / "good.json")
    loaded = load_client_secret(good)
    assert loaded["client_id"] == "test-client-id.apps.googleusercontent.com"
    assert loaded["client_secret"] == "test-client-secret"


def test_authorize_gphotos_pkce_loopback(tmp_path: Path, monkeypatch) -> None:
    cfg = _make_cfg(tmp_path)
    src_secret = _write_installed_secret(tmp_path / "client_secret.json")

    captured_auth: dict[str, str] = {}
    created_servers: list[object] = []

    class _SocketPairHTTPServer:
        def __init__(self, server_address: tuple[str, int], handler_cls: type) -> None:
            del server_address
            self.server_address = ("127.0.0.1", 54321)
            self.handler_cls = handler_cls
            self.timeout = 1.0
            self.requests_queue: list[bytes] = []
            self.responses_seen: list[str] = []
            self.closed = False
            created_servers.append(self)

        def handle_request(self) -> None:
            if not self.requests_queue:
                return
            raw_req = self.requests_queue.pop(0)
            client_sock, server_sock = socket.socketpair()
            try:
                client_sock.sendall(raw_req)
                client_sock.shutdown(socket.SHUT_WR)
                self.handler_cls(server_sock, ("127.0.0.1", 0), self)
            finally:
                server_sock.close()
            chunks: list[bytes] = []
            try:
                while True:
                    data = client_sock.recv(4096)
                    if not data:
                        break
                    chunks.append(data)
            finally:
                client_sock.close()
            self.responses_seen.append(b"".join(chunks).decode("utf-8", errors="replace"))

        def server_close(self) -> None:
            self.closed = True

    monkeypatch.setattr(gphotos_mod, "HTTPServer", _SocketPairHTTPServer)

    def fake_browser_open(url: str) -> bool:
        parsed = urlsplit(url)
        qs = parse_qs(parsed.query)
        captured_auth["redirect_uri"] = qs["redirect_uri"][0]
        captured_auth["state"] = qs["state"][0]
        captured_auth["code_challenge"] = qs["code_challenge"][0]
        captured_auth["code_challenge_method"] = qs["code_challenge_method"][0]
        captured_auth["scope"] = qs["scope"][0]

        srv = created_servers[-1]
        srv.requests_queue.append(b"GET /favicon.ico HTTP/1.1\r\nHost: 127.0.0.1:54321\r\n\r\n")
        srv.requests_queue.append(
            f"GET /callback?code=auth-code-123&state={captured_auth['state']} HTTP/1.1\r\n"
            "Host: 127.0.0.1:54321\r\n\r\n".encode("ascii")
        )
        return True

    monkeypatch.setattr(gphotos_mod.webbrowser, "open", fake_browser_open)

    token_requests: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://oauth2.googleapis.com/token"
        body_params = {k: v[0] for k, v in parse_qs(request.read().decode("utf-8")).items()}
        token_requests.append(body_params)
        return httpx.Response(
            200,
            json={
                "access_token": "secret-access-token",
                "refresh_token": "secret-refresh-token",
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": " ".join(gphotos_mod.GPHOTOS_SCOPES),
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as mock_http:
        res = authorize_gphotos(cfg, client_secret_path=src_secret, timeout_seconds=10, http_client=mock_http)

    assert len(created_servers) == 1
    srv = created_servers[0]
    assert srv.closed is True
    assert len(srv.responses_seen) == 2
    assert srv.responses_seen[0].startswith("HTTP/1.0 404")
    assert srv.responses_seen[1].startswith("HTTP/1.0 200")

    assert captured_auth["code_challenge_method"] == "S256"
    assert set(captured_auth["scope"].split()) == set(gphotos_mod.GPHOTOS_SCOPES)
    assert len(token_requests) == 1
    verifier = token_requests[0]["code_verifier"]
    expected_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    )
    assert expected_challenge == captured_auth["code_challenge"]
    assert token_requests[0]["code"] == "auth-code-123"

    assert res["has_refresh_token"] is True
    assert "secret-access-token" not in json.dumps(res)
    assert "secret-refresh-token" not in json.dumps(res)

    assert stat.S_IMODE(cfg.gphotos_client_secret_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(cfg.gphotos_token_path.stat().st_mode) == 0o600


def test_token_refresh_preserves_refresh_token_and_401_retry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(gphotos_mod.time, "sleep", lambda _s: None)
    cfg = _make_cfg(tmp_path)
    _seed_auth(cfg, expires_at=0, access_token="expired-tok")

    calls: list[tuple[str, str]] = []

    def handler_expired(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if str(request.url) == gphotos_mod.DEFAULT_TOKEN_URI:
            return httpx.Response(200, json={"access_token": "fresh-tok-1", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer fresh-tok-1"
        return httpx.Response(200, json={"albums": []})

    with httpx.Client(transport=httpx.MockTransport(handler_expired)) as http_1:
        with GooglePhotosClient(cfg, http_client=http_1) as gp:
            assert gp.list_app_albums() == []

    saved = json.loads(cfg.gphotos_token_path.read_text(encoding="utf-8"))
    assert saved["access_token"] == "fresh-tok-1"
    assert saved["refresh_token"] == "refresh-initial"

    # Now test 401 on API call with supposedly non-expired access token triggers forced refresh
    _seed_auth(cfg, expires_at=9999999999, access_token="revoked-tok")
    album_attempts = {"n": 0}

    def handler_401(request: httpx.Request) -> httpx.Response:
        if str(request.url) == gphotos_mod.DEFAULT_TOKEN_URI:
            return httpx.Response(200, json={"access_token": "fresh-tok-2", "expires_in": 3600})
        album_attempts["n"] += 1
        if album_attempts["n"] == 1:
            assert request.headers["Authorization"] == "Bearer revoked-tok"
            return httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}})
        assert request.headers["Authorization"] == "Bearer fresh-tok-2"
        return httpx.Response(200, json={"albums": [{"id": "a1", "title": "Test"}]})

    with httpx.Client(transport=httpx.MockTransport(handler_401)) as http_2:
        with GooglePhotosClient(cfg, http_client=http_2) as gp:
            albums = gp.list_app_albums()
            assert len(albums) == 1
            assert album_attempts["n"] == 2


def test_ensure_album_pagination_cache_and_create(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    _seed_auth(cfg)
    requests_seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append((request.method, str(request.url)))
        if request.method == "GET" and str(request.url).startswith(gphotos_mod.ALBUMS_URL):
            qs = parse_qs(urlsplit(str(request.url)).query)
            assert qs["excludeNonAppCreatedData"] == ["true"]
            if "pageToken" not in qs:
                return httpx.Response(
                    200,
                    json={
                        "albums": [{"id": "alb-0", "title": "Other", "isWriteable": True}],
                        "nextPageToken": "page-2",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "albums": [
                        {
                            "id": "alb-existing",
                            "title": "Existing Album",
                            "isWriteable": True,
                            "productUrl": "https://photos.google.com/album/alb-existing",
                        }
                    ]
                },
            )
        if request.method == "POST" and str(request.url) == gphotos_mod.ALBUMS_URL:
            body = json.loads(request.read().decode("utf-8"))
            assert body == {"album": {"title": "Brand New Album"}}
            return httpx.Response(
                200,
                json={
                    "id": "alb-new",
                    "title": "Brand New Album",
                    "isWriteable": True,
                    "productUrl": "https://photos.google.com/album/alb-new",
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    with StateDB(cfg.state_db_path) as db, httpx.Client(transport=httpx.MockTransport(handler)) as mock_http:
        with GooglePhotosClient(cfg, http_client=mock_http) as gp:
            alb_id, url = gp.ensure_album("Existing Album", db)
            assert alb_id == "alb-existing"
            assert url == "https://photos.google.com/album/alb-existing"
            assert len(requests_seen) == 2

            # Second lookup hits DB cache with zero HTTP calls
            alb_id2, _ = gp.ensure_album("Existing Album", db)
            assert alb_id2 == "alb-existing"
            assert len(requests_seen) == 2

            # Missing title creates new album and caches it (after scanning app-created albums)
            new_id, new_url = gp.ensure_album("Brand New Album", db)
            assert new_id == "alb-new"
            assert new_url == "https://photos.google.com/album/alb-new"
            assert len(requests_seen) == 5


def test_upload_to_gphotos_dry_run_and_55_items_chunked_idempotent(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    _seed_media_items(cfg, 55)

    # Dry run requires no credentials/token and makes zero HTTP calls
    dry = upload_to_gphotos(cfg, dry_run=True)
    assert dry["status"] == "ok"
    assert dry["dry_run"] is True
    assert dry["files_pending"] == 55
    assert dry["files_uploaded"] == 0
    assert dry["planned"] == [
        {"album_title": "Group D - Fall 2026", "pending_count": 55, "days": ["2026-11-18"]}
    ]

    _seed_auth(cfg)
    upload_headers: list[dict[str, str]] = []
    batch_chunks: list[list[dict]] = []
    counter = {"n": 0}
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "GET" and url.startswith(gphotos_mod.ALBUMS_URL):
            return httpx.Response(200, json={"albums": []})
        if request.method == "POST" and url == gphotos_mod.ALBUMS_URL:
            return httpx.Response(
                200,
                json={
                    "id": "gp-alb-group-d",
                    "title": "Group D - Fall 2026",
                    "productUrl": "https://photos.google.com/album/gp-alb-group-d",
                },
            )
        if request.method == "POST" and url == gphotos_mod.UPLOAD_URL:
            raw_bytes = request.read()
            assert raw_bytes.startswith(b"jpeg-bytes-")
            upload_headers.append(dict(request.headers))
            with lock:
                idx = counter["n"]
                counter["n"] += 1
            return httpx.Response(200, text=f"upload-tok-{idx}-{request.headers['x-goog-upload-file-name']}")
        if request.method == "POST" and url == gphotos_mod.BATCH_CREATE_URL:
            body = json.loads(request.read().decode("utf-8"))
            assert body["albumId"] == "gp-alb-group-d"
            items = body["newMediaItems"]
            batch_chunks.append(items)
            results = []
            for j, entry in enumerate(items):
                tok = entry["simpleMediaItem"]["uploadToken"]
                fname = entry["simpleMediaItem"]["fileName"]
                results.append(
                    {
                        "uploadToken": tok,
                        "status": {"message": "Success"},
                        "mediaItem": {
                            "id": f"gp-media-{fname}",
                            "productUrl": f"https://photos.google.com/photo/{fname}",
                        },
                    }
                )
            return httpx.Response(200, json={"newMediaItemResults": results})
        raise AssertionError(f"Unexpected HTTP call: {request.method} {url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as mock_http:
        res = upload_to_gphotos(cfg, workers=4, http_client=mock_http)

    assert res["status"] == "ok"
    assert res["files_pending"] == 55
    assert res["files_uploaded"] == 55
    assert res["files_missing"] == 0
    assert res["files_failed"] == 0
    assert "planned" not in res
    assert len(upload_headers) == 55
    assert all(h["x-goog-upload-protocol"] == "raw" for h in upload_headers)
    assert all(h["x-goog-upload-content-type"] == "image/jpeg" for h in upload_headers)
    assert all(h["content-type"] == "application/octet-stream" for h in upload_headers)

    assert [len(c) for c in batch_chunks] == [50, 5]
    submitted_names = [
        entry["simpleMediaItem"]["fileName"] for chunk in batch_chunks for entry in chunk
    ]
    assert submitted_names == [f"star-academy-2026-11-18-{1000 + i:04d}.jpg" for i in range(55)]

    with StateDB(cfg.state_db_path) as db:
        assert db.get_gphotos_stats() == {"albums_count": 1, "uploaded_count": 55, "pending_count": 0}

    # Re-run must be idempotent with zero HTTP requests
    def fail_if_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Should not make HTTP calls when nothing pending: {request.url}")

    with httpx.Client(transport=httpx.MockTransport(fail_if_called)) as noop_http:
        res2 = upload_to_gphotos(cfg, http_client=noop_http)
    assert res2["status"] == "ok"
    assert res2["files_pending"] == 0
    assert res2["files_uploaded"] == 0


def test_upload_to_gphotos_partial_failure_and_missing_file(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    paths = _seed_media_items(cfg, 3)
    paths[0].unlink()  # Missing on disk
    _seed_auth(cfg)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "GET" and url.startswith(gphotos_mod.ALBUMS_URL):
            return httpx.Response(200, json={"albums": [{"id": "alb-1", "title": "Group D - Fall 2026"}]})
        if request.method == "POST" and url == gphotos_mod.UPLOAD_URL:
            request.read()
            fname = request.headers["x-goog-upload-file-name"]
            return httpx.Response(200, text=f"tok-{fname}")
        if request.method == "POST" and url == gphotos_mod.BATCH_CREATE_URL:
            body = json.loads(request.read().decode("utf-8"))
            results = []
            for entry in body["newMediaItems"]:
                tok = entry["simpleMediaItem"]["uploadToken"]
                fname = entry["simpleMediaItem"]["fileName"]
                if "1002" in fname:
                    results.append(
                        {
                            "uploadToken": tok,
                            "status": {"code": 3, "message": "Invalid image"},
                        }
                    )
                else:
                    results.append(
                        {
                            "uploadToken": tok,
                            "status": {"code": 0, "message": "Success"},
                            "mediaItem": {"id": f"gp-{fname}", "productUrl": "https://photos.google.com/p/1"},
                        }
                    )
            return httpx.Response(200, json={"newMediaItemResults": results})
        raise AssertionError(f"Unexpected call: {request.method} {url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as mock_http:
        res = upload_to_gphotos(cfg, workers=2, http_client=mock_http)

    assert res["status"] == "partial_error"
    assert res["files_pending"] == 3
    assert res["files_missing"] == 1
    assert res["files_uploaded"] == 1
    assert res["files_failed"] == 1

    with StateDB(cfg.state_db_path) as db:
        assert db.has_gphotos_upload("sp:TD:1001") is True
        assert db.has_gphotos_upload("sp:TD:1000") is False
        assert db.has_gphotos_upload("sp:TD:1002") is False
        assert [row["media_key"] for row in db.list_pending_gphotos_media()] == [
            "sp:TD:1000",
            "sp:TD:1002",
        ]


def test_cli_gphotos_commands_and_sync_gphotos_dry_run(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = _make_cfg(tmp_path)
    _seed_media_items(cfg, 2)
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)

    code = cli_mod.main(["gphotos", "status"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["client_secret_exists"] is False
    assert out["token_exists"] is False
    assert out["pending_count"] == 2
    assert out["uploaded_count"] == 0

    code2 = cli_mod.main(["gphotos", "upload", "--dry-run"])
    out2 = json.loads(capsys.readouterr().out)
    assert code2 == 0
    assert out2["dry_run"] is True
    assert out2["files_pending"] == 2
    assert out2["planned"][0]["album_title"] == "Group D - Fall 2026"

    monkeypatch.setattr(
        cli_mod.portal,
        "sync_portal",
        lambda *a, **kw: {
            "status": "ok",
            "dry_run": True,
            "output_dir": str(cfg.output_dir),
            "groups": [],
            "albums_checked": 1,
            "files_found": 2,
            "files_downloaded": 0,
            "files_skipped": 2,
            "files_failed": 0,
            "errors": [],
            "planned": [],
        },
    )
    code3 = cli_mod.main(["sync", "--gphotos", "--dry-run"])
    out3 = json.loads(capsys.readouterr().out)
    assert code3 == 0
    assert out3["gphotos"]["dry_run"] is True
    assert out3["gphotos"]["files_pending"] == 2
