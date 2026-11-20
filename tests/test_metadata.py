"""Unit and exiftool integration tests for metadata.py."""

from __future__ import annotations

import json
import os
import struct
import subprocess
import zlib
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pkportal.metadata import (
    apply_album_date_metadata,
    find_exiftool,
    read_existing_embedded_datetime,
    resolve_target_datetime,
)


def _make_minimal_png(path: Path) -> None:
    """Create a valid 1x1 RGBA PNG using Python standard library."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack("!I", len(data))
            + tag
            + data
            + struct.pack("!I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack("!IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x00\x00\xff")
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png_bytes)


def _make_minimal_jpeg(path: Path) -> None:
    """Create a valid 1x1 JPEG via macOS sips from a minimal PNG."""
    png_tmp = path.with_suffix(".src.png")
    _make_minimal_png(png_tmp)
    res = subprocess.run(
        ["/usr/bin/sips", "-s", "format", "jpeg", str(png_tmp), "--out", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    png_tmp.unlink(missing_ok=True)
    assert res.returncode == 0, f"sips failed: {res.stderr}"


def test_resolve_target_datetime_est_and_edt(tmp_path: Path) -> None:
    dummy = tmp_path / "photo.jpg"

    dt_est, preserved_est = resolve_target_datetime(dummy, date(2026, 11, 18), 5, "America/New_York")
    assert preserved_est is False
    assert dt_est.isoformat() == "2026-11-18T12:00:05-05:00"

    dt_edt, preserved_edt = resolve_target_datetime(dummy, date(2026, 7, 15), 2, "America/New_York")
    assert preserved_edt is False
    assert dt_edt.isoformat() == "2026-07-15T12:00:02-04:00"


def test_resolve_target_datetime_preserves_same_day_time(tmp_path: Path) -> None:
    dummy = tmp_path / "photo.jpg"
    ny = ZoneInfo("America/New_York")

    same_day = datetime(2026, 11, 18, 9, 42, 11, tzinfo=ny)
    dt, preserved = resolve_target_datetime(dummy, date(2026, 11, 18), 0, "America/New_York", existing_dt=same_day)
    assert preserved is True
    assert dt.isoformat() == "2026-11-18T09:42:11-05:00"

    # UTC timestamp that falls on 2026-11-18 in America/New_York (2026-11-19 02:30Z == 2026-11-18 21:30-05:00)
    utc_same_ny_day = datetime(2026, 11, 19, 2, 30, 0, tzinfo=timezone.utc)
    dt_utc, preserved_utc = resolve_target_datetime(
        dummy, date(2026, 11, 18), 0, "America/New_York", existing_dt=utc_same_ny_day
    )
    assert preserved_utc is True
    assert dt_utc.isoformat() == "2026-11-18T21:30:00-05:00"

    different_day = datetime(2026, 11, 10, 9, 42, 11, tzinfo=ny)
    dt_other, preserved_other = resolve_target_datetime(
        dummy, date(2026, 11, 18), 4, "America/New_York", existing_dt=different_day
    )
    assert preserved_other is False
    assert dt_other.isoformat() == "2026-11-18T12:00:04-05:00"


def test_apply_metadata_jpeg_integration(tmp_path: Path) -> None:
    img = tmp_path / "sample.jpg"
    _make_minimal_jpeg(img)

    album_day = date(2026, 11, 18)
    result = apply_album_date_metadata(img, album_day, index=3, tz_name="America/New_York")
    assert result["preserved_existing_time"] is False
    assert result["target_datetime"] == "2026-11-18T12:00:03-05:00"

    exiftool = find_exiftool()
    read_back = subprocess.run(
        [exiftool, "-j", "-DateTimeOriginal", "-OffsetTimeOriginal", "-XMP:DateCreated", str(img)],
        capture_output=True,
        text=True,
        check=True,
    )
    tags = json.loads(read_back.stdout)[0]
    assert tags["DateTimeOriginal"] == "2026:11:18 12:00:03"
    assert tags["OffsetTimeOriginal"] == "-05:00"
    assert str(tags["DateCreated"]).startswith("2026:11:18 12:00:03")

    expected_epoch = datetime(2026, 11, 18, 12, 0, 3, tzinfo=ZoneInfo("America/New_York")).timestamp()
    st = os.stat(img)
    assert abs(st.st_mtime - expected_epoch) <= 1.0
    if hasattr(st, "st_birthtime"):
        assert abs(st.st_birthtime - expected_epoch) <= 1.0

    # Write a specific morning time on the same day and confirm apply preserves it
    subprocess.run(
        [
            exiftool,
            "-overwrite_original",
            "-DateTimeOriginal=2026:11:18 09:15:30",
            "-OffsetTimeOriginal=-05:00",
            str(img),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    existing = read_existing_embedded_datetime(img, tz_name="America/New_York")
    assert existing is not None
    result2 = apply_album_date_metadata(img, album_day, index=9, tz_name="America/New_York")
    assert result2["preserved_existing_time"] is True
    assert result2["target_datetime"] == "2026-11-18T09:15:30-05:00"


def test_apply_metadata_png_integration(tmp_path: Path) -> None:
    png = tmp_path / "sample.png"
    _make_minimal_png(png)

    album_day = date(2026, 7, 15)
    result = apply_album_date_metadata(png, album_day, index=1, tz_name="America/New_York")
    assert result["preserved_existing_time"] is False
    assert result["target_datetime"] == "2026-07-15T12:00:01-04:00"

    exiftool = find_exiftool()
    read_back = subprocess.run(
        [exiftool, "-j", "-DateTimeOriginal", "-OffsetTimeOriginal", "-PNG:CreationTime", str(png)],
        capture_output=True,
        text=True,
        check=True,
    )
    tags = json.loads(read_back.stdout)[0]
    assert tags["DateTimeOriginal"] == "2026:07:15 12:00:01"

    expected_epoch = datetime(2026, 7, 15, 12, 0, 1, tzinfo=ZoneInfo("America/New_York")).timestamp()
    st = os.stat(png)
    assert abs(st.st_mtime - expected_epoch) <= 1.0
    if hasattr(st, "st_birthtime"):
        assert abs(st.st_birthtime - expected_epoch) <= 1.0
