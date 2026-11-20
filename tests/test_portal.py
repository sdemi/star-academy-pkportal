"""Unit and integration tests for portal.py."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import struct
import subprocess
import zlib
from datetime import date
from pathlib import Path

import httpx
import pytest

import pkportal.portal as portal_mod
from pkportal.config import Config
from pkportal.metadata import find_exiftool
from pkportal.portal import (
    PortalClient,
    build_album_dir,
    parse_content_disposition_filename,
    season_for_date,
    sync_portal,
)


def _make_sample_jpeg_bytes(tmp_dir: Path) -> bytes:
    png_path = tmp_dir / "_src.png"
    jpg_path = tmp_dir / "_src.jpg"

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack("!I", len(data))
            + tag
            + data
            + struct.pack("!I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack("!IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x00\x00\xff")
    png_path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b""))

    res = subprocess.run(
        ["/usr/bin/sips", "-s", "format", "jpeg", str(png_path), "--out", str(jpg_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    del res
    data = jpg_path.read_bytes()
    png_path.unlink(missing_ok=True)
    jpg_path.unlink(missing_ok=True)
    return data


def test_season_for_date() -> None:
    assert season_for_date(date(2026, 9, 1)) == "Fall 2026"
    assert season_for_date(date(2026, 11, 18)) == "Fall 2026"
    assert season_for_date(date(2026, 12, 5)) == "Winter 2026-2027"
    assert season_for_date(date(2027, 1, 15)) == "Winter 2026-2027"
    assert season_for_date(date(2027, 2, 28)) == "Winter 2026-2027"
    assert season_for_date(date(2027, 3, 1)) == "Spring 2027"
    assert season_for_date(date(2027, 5, 31)) == "Spring 2027"
    assert season_for_date(date(2027, 6, 1)) == "Summer 2027"
    assert season_for_date(date(2027, 8, 31)) == "Summer 2027"


def test_build_album_dir_and_sanitization(tmp_path: Path) -> None:
    out = tmp_path / "Star Academy"
    nested = build_album_dir(out, "Group D", "Fall 2026", date(2026, 11, 18))
    assert nested == out / "Group D - Fall 2026" / "2026-11-18"

    flat = build_album_dir(out, "Group D", "Fall 2026", date(2026, 11, 18), group_by_day_subfolder=False)
    assert flat == out / "Group D - Fall 2026"

    campus_dir = build_album_dir(out, "Group D", "Fall 2026", date(2026, 11, 18), campus="Watertown")
    assert campus_dir == out / "Watertown - Group D - Fall 2026" / "2026-11-18"

    dirty = build_album_dir(out, "Group: A/B", "Fall\\2026", date(2026, 11, 18))
    assert dirty == out / "Group- A-B - Fall-2026" / "2026-11-18"


def test_parse_content_disposition_filename() -> None:
    assert (
        parse_content_disposition_filename('attachment; filename="star-academy-2026-11-18-1013.jpg"')
        == "star-academy-2026-11-18-1013.jpg"
    )
    assert (
        parse_content_disposition_filename("attachment; filename*=UTF-8''hello%20world%201.jpg")
        == "hello world 1.jpg"
    )
    assert parse_content_disposition_filename('attachment; filename="../../evil.jpg"') == "evil.jpg"
    assert parse_content_disposition_filename(None) is None


def test_sync_portal_incremental_and_late_additions(tmp_path: Path, monkeypatch) -> None:
    sample_jpeg = _make_sample_jpeg_bytes(tmp_path)
    server_items: list[dict] = [
        {"id": 1013, "group": "TD", "day": "2026-11-18"},
        {"id": 1014, "group": "TD", "day": "2026-11-18"},
    ]

    class FakePortalClient:
        def __init__(self, cfg: Config) -> None:
            self.cfg = cfg
            self.rest = "https://staracademyboston.com/wp-json/star-preschool/v1"
            self.client = type("DummyClient", (), {"cookies": None})()

        def __enter__(self) -> FakePortalClient:
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def authenticate(self, *, relogin: bool = False) -> None:
            del relogin

        def get_bootstrap(self) -> dict:
            return {
                "groups": [
                    {"id": "TD", "name": "Group D", "campus": "Watertown", "mine": True},
                ]
            }

        def list_days(self, group_id: str | None = None) -> list[dict]:
            del group_id
            return [{"day": "2026-11-18", "group": "TD", "count": len(server_items)}]

        def list_day_items(self, day_str: str, group_id: str) -> list[dict]:
            assert day_str == "2026-11-18"
            assert group_id == "TD"
            return list(server_items)

        def download_media(
            self,
            item_id: int,
            dest_dir: Path,
            fallback_stem: str,
        ) -> tuple[Path, int, str, str]:
            del fallback_stem
            filename = f"star-academy-2026-11-18-{item_id}.jpg"
            dest = dest_dir / filename
            dest.write_bytes(sample_jpeg)
            digest = hashlib.sha256(sample_jpeg).hexdigest()
            return dest, len(sample_jpeg), digest, "photo"

    monkeypatch.setattr(portal_mod, "PortalClient", FakePortalClient)
    monkeypatch.setattr(portal_mod.auth, "merge_httpx_cookies_to_storage_state", lambda c, p: None)

    cfg = Config(
        output_dir=tmp_path / "Pictures" / "Star Academy",
        app_support_dir=tmp_path / "AppSupport",
        logs_dir=tmp_path / "Logs",
    )

    # 1. Dry run must create zero output files and zero DB album/media rows
    dry = sync_portal(cfg, dry_run=True)
    assert dry["files_found"] == 2
    assert dry["files_downloaded"] == 0
    assert not cfg.output_dir.exists()
    conn = sqlite3.connect(str(cfg.state_db_path))
    assert conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0] == 0
    conn.close()

    # 2. First sync downloads both photos and applies metadata
    first = sync_portal(cfg, workers=2)
    assert first["status"] == "ok"
    assert first["files_downloaded"] == 2
    assert first["files_skipped"] == 0
    assert first["files_failed"] == 0

    day_dir = cfg.output_dir / "Group D - Fall 2026" / "2026-11-18"
    p1 = day_dir / "star-academy-2026-11-18-1013.jpg"
    p2 = day_dir / "star-academy-2026-11-18-1014.jpg"
    assert p1.exists() and p2.exists()

    exiftool = find_exiftool()
    tags = json.loads(
        subprocess.run(
            [exiftool, "-j", "-DateTimeOriginal", "-OffsetTimeOriginal", str(p1), str(p2)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    assert tags[0]["DateTimeOriginal"] == "2026:11:18 12:00:00"
    assert tags[0]["OffsetTimeOriginal"] == "-05:00"
    assert tags[1]["DateTimeOriginal"] == "2026:11:18 12:00:01"
    assert tags[1]["OffsetTimeOriginal"] == "-05:00"

    conn = sqlite3.connect(str(cfg.state_db_path))
    urls = [row[0] for row in conn.execute("SELECT media_url FROM media_items").fetchall()]
    assert len(urls) == 2
    assert all("sp_pt" not in u and "_wpnonce" not in u for u in urls)
    conn.close()

    # 3. Second sync skips everything
    second = sync_portal(cfg)
    assert second["files_downloaded"] == 0
    assert second["files_skipped"] == 2

    # 4. Late upload on the same day downloads only the new 3rd item with index=2 -> 12:00:02
    server_items.append({"id": 1016, "group": "TD", "day": "2026-11-18"})
    third = sync_portal(cfg)
    assert third["files_downloaded"] == 1
    assert third["files_skipped"] == 2
    p3 = day_dir / "star-academy-2026-11-18-1016.jpg"
    assert p3.exists()
    tag3 = json.loads(
        subprocess.run(
            [exiftool, "-j", "-DateTimeOriginal", str(p3)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )[0]
    assert tag3["DateTimeOriginal"] == "2026:11:18 12:00:02"


def test_authenticate_does_not_open_chrome_on_503_maintenance(tmp_path: Path, monkeypatch) -> None:
    cfg = Config(
        output_dir=tmp_path / "Pictures" / "Star Academy",
        app_support_dir=tmp_path / "AppSupport",
        logs_dir=tmp_path / "Logs",
    )
    cfg.app_support_dir.mkdir(parents=True, exist_ok=True)
    cfg.storage_state_path.write_text('{"cookies": [], "origins": []}', encoding="utf-8")

    chrome_called = False

    def fake_login(*args, **kwargs):
        nonlocal chrome_called
        chrome_called = True
        return []

    monkeypatch.setattr(portal_mod.auth, "interactive_login", fake_login)
    monkeypatch.setattr(portal_mod.time, "sleep", lambda _s: None)

    transport = httpx.MockTransport(
        lambda request: httpx.Response(503, text="<html><title>Site is offline</title></html>")
    )
    with PortalClient(cfg) as pc:
        pc.client.close()
        pc.client = httpx.Client(transport=transport)
        with pytest.raises(RuntimeError, match="HTTP 503"):
            pc.authenticate()

    assert chrome_called is False


def test_cli_reports_runtime_error_cleanly(monkeypatch, capsys) -> None:
    from pkportal import cli

    monkeypatch.setattr(
        cli.portal,
        "sync_portal",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("Portal temporarily unavailable (HTTP 503).")),
    )
    code = cli.main(["sync", "--dry-run"])
    captured = capsys.readouterr()
    assert code == 1
    assert "Error: Portal temporarily unavailable (HTTP 503)." in captured.err


def test_project_local_paths_and_schedule_plist() -> None:
    from pkportal import schedule
    from pkportal.config import PROJECT_ROOT

    cfg = Config()
    assert cfg.output_dir == Path.home() / "Pictures" / "Star Academy iCloud"
    assert cfg.app_support_dir == PROJECT_ROOT / "data"
    assert cfg.logs_dir == PROJECT_ROOT / "data" / "logs"
    assert cfg.storage_state_path == PROJECT_ROOT / "data" / "storage_state.json"

    py_bin = schedule.default_pkportal_bin()
    assert py_bin == PROJECT_ROOT / ".venv" / "bin" / "python"
    plist = schedule.build_plist_dict(cfg, py_bin, hour=18, minute=30)
    assert plist["ProgramArguments"] == [str(py_bin), "-m", "pkportal.cli", "sync"]
    assert plist["WorkingDirectory"] == str(PROJECT_ROOT)
    assert plist["StandardOutPath"] == str(PROJECT_ROOT / "data" / "logs" / "launchd.out.log")
