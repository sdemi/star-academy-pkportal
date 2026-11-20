"""EXIF/XMP/QuickTime and macOS filesystem timestamp writer via exiftool."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".3gp"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff", ".gif"}

_EXIF_DT_RE = re.compile(
    r"^(?P<y>\d{4}):(?P<m>\d{2}):(?P<d>\d{2})\s+"
    r"(?P<H>\d{2}):(?P<M>\d{2}):(?P<S>\d{2})"
    r"(?:\.\d+)?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?$"
)


def find_exiftool() -> str:
    """Locate exiftool binary on PATH or common Homebrew/macOS locations."""
    for candidate in (
        shutil.which("exiftool"),
        "/opt/homebrew/bin/exiftool",
        "/usr/local/bin/exiftool",
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("exiftool not found. Install it via: brew install exiftool")


def _parse_exif_datetime(raw: str | None, offset_hint: str | None, default_tz: ZoneInfo) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    if raw.startswith("0000:00:00"):
        return None
    match = _EXIF_DT_RE.match(raw)
    if not match:
        return None

    year = int(match.group("y"))
    month = int(match.group("m"))
    day = int(match.group("d"))
    hour = int(match.group("H"))
    minute = int(match.group("M"))
    second = int(match.group("S"))
    tz_str = match.group("tz") or (offset_hint.strip() if isinstance(offset_hint, str) and offset_hint.strip() else None)

    try:
        if tz_str:
            if tz_str == "Z":
                tzinfo = timezone.utc
            else:
                sign = 1 if tz_str[0] == "+" else -1
                digits = tz_str[1:].replace(":", "")
                tz_h = int(digits[:2])
                tz_m = int(digits[2:4])
                tzinfo = timezone(timedelta(hours=sign * tz_h, minutes=sign * tz_m))
            return datetime(year, month, day, hour, minute, second, tzinfo=tzinfo)
        return datetime(year, month, day, hour, minute, second, tzinfo=default_tz)
    except ValueError:
        return None


def read_existing_embedded_datetime(file_path: Path, tz_name: str = "America/New_York") -> datetime | None:
    """Read existing EXIF/XMP/QuickTime capture datetime from a media file via exiftool."""
    exiftool = find_exiftool()
    cmd = [
        exiftool,
        "-j",
        "-api",
        "QuickTimeUTC=1",
        "-DateTimeOriginal",
        "-OffsetTimeOriginal",
        "-CreateDate",
        "-CreationDate",
        "-MediaCreateDate",
        "-XMP:DateCreated",
        "-PNG:CreationTime",
        "-MIMEType",
        str(file_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0 or not res.stdout.strip():
        return None
    try:
        items = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None
    if not items:
        return None

    data = items[0]
    default_tz = ZoneInfo(tz_name)
    offset_orig = data.get("OffsetTimeOriginal")

    for key in (
        "DateTimeOriginal",
        "CreationDate",
        "DateCreated",
        "CreationTime",
        "MediaCreateDate",
        "CreateDate",
    ):
        dt = _parse_exif_datetime(data.get(key), offset_orig if key == "DateTimeOriginal" else None, default_tz)
        if dt is not None:
            return dt
    return None


def resolve_target_datetime(
    file_path: Path,
    album_date: date,
    index: int,
    tz_name: str = "America/New_York",
    existing_dt: datetime | None = None,
) -> tuple[datetime, bool]:
    """Compute target localized datetime, keeping original time-of-day if on album_date."""
    del file_path  # Reserved for future extension-specific heuristics; kept in signature.
    tz = ZoneInfo(tz_name)
    if existing_dt is not None:
        aware_existing = existing_dt if existing_dt.tzinfo is not None else existing_dt.replace(tzinfo=tz)
        localized = aware_existing.astimezone(tz)
        if localized.date() == album_date:
            return localized, True

    noon = datetime(album_date.year, album_date.month, album_date.day, 12, 0, 0, tzinfo=tz)
    return noon + timedelta(seconds=index), False


def _format_offset(dt: datetime) -> str:
    offset = dt.strftime("%z")  # e.g. -0500
    return f"{offset[:3]}:{offset[3:]}"


def _is_video(file_path: Path) -> bool:
    return file_path.suffix.lower() in VIDEO_EXTENSIONS


def apply_album_date_metadata(
    file_path: Path,
    album_date: date,
    index: int = 0,
    tz_name: str = "America/New_York",
) -> dict:
    """Write album-date embedded metadata (EXIF/XMP/QuickTime) and macOS file dates."""
    exiftool = find_exiftool()
    existing_dt = read_existing_embedded_datetime(file_path, tz_name=tz_name)
    target_dt, preserved = resolve_target_datetime(
        file_path,
        album_date,
        index,
        tz_name=tz_name,
        existing_dt=existing_dt,
    )

    naive_str = target_dt.strftime("%Y:%m:%d %H:%M:%S")
    offset_str = _format_offset(target_dt)
    tz_str = f"{naive_str}{offset_str}"

    args = [exiftool, "-overwrite_original", "-m"]
    if _is_video(file_path):
        args.extend(
            [
                "-api",
                "QuickTimeUTC=1",
                f"-QuickTime:CreateDate={tz_str}",
                f"-QuickTime:ModifyDate={tz_str}",
                f"-QuickTime:TrackCreateDate={tz_str}",
                f"-QuickTime:MediaCreateDate={tz_str}",
                f"-Keys:CreationDate={tz_str}",
            ]
        )
    else:
        args.extend(
            [
                f"-DateTimeOriginal={naive_str}",
                f"-CreateDate={naive_str}",
                f"-ModifyDate={naive_str}",
                f"-OffsetTimeOriginal={offset_str}",
                f"-OffsetTimeDigitized={offset_str}",
                f"-OffsetTime={offset_str}",
                f"-XMP:DateCreated={tz_str}",
            ]
        )
        if file_path.suffix.lower() == ".png":
            args.append(f"-PNG:CreationTime={tz_str}")

    args.extend(
        [
            f"-FileCreateDate={tz_str}",
            f"-FileModifyDate={tz_str}",
            str(file_path),
        ]
    )

    res = subprocess.run(args, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(
            f"exiftool failed ({res.returncode}) for {file_path}: {res.stderr.strip() or res.stdout.strip()}"
        )

    epoch = target_dt.timestamp()
    os.utime(file_path, (epoch, epoch))

    return {
        "target_datetime": target_dt.isoformat(),
        "preserved_existing_time": preserved,
        "exiftool_output": res.stdout.strip(),
    }
