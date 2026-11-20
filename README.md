# Star Academy PK Portal Photo Sync

CLI tool to log in to `https://staracademyboston.com/pkportal/`, persist session cookies, download new daily photos and videos, and stamp embedded EXIF/XMP/QuickTime + macOS file creation/modification dates from each album's date.

## Requirements

- macOS with Google Chrome (`/Applications/Google Chrome.app`)
- `exiftool` (`brew install exiftool`)
- Python 3.11+

## Output Structure

By default, media is saved into `~/Pictures/Star Academy iCloud/` organized by **Class - Season** and day:

```text
~/Pictures/Star Academy iCloud/
  Group D - Fall 2026/
    2026-11-16/
      star-academy-2026-11-16-219.jpg
    2026-11-17/
      ...
    2026-11-18/
      star-academy-2026-11-18-1013.jpg
```

You can also disable daily subfolders (`pkportal config set --flat-season`) or override the root folder (`--output <dir>` / `pkportal config set --output <dir>`).

## Embedded Metadata

- **Date source:** Each album's date is read from the portal's WordPress REST API `day` field (not from the HTML `<title>`).
- **Timestamp logic:** Preserves an item's existing embedded capture time-of-day if it already matches the album date; otherwise stamps `12:00:00` plus album item index seconds in the configured timezone (`America/New_York`, configurable via `--timezone`) to keep files in album order.
- **Embedded tags written (`exiftool`):**
  - **Photos:** `DateTimeOriginal`, `CreateDate`, `ModifyDate`, `OffsetTimeOriginal`, `OffsetTimeDigitized`, `OffsetTime`, `XMP:DateCreated` (plus `PNG:CreationTime` for `.png`).
  - **Videos:** `QuickTime:CreateDate`, `QuickTime:ModifyDate`, `QuickTime:TrackCreateDate`, `QuickTime:MediaCreateDate`, `Keys:CreationDate`.
  - **Filesystem dates:** macOS `FileCreateDate`, `FileModifyDate`, and `os.utime()`.
- **What is not included:** Saved files contain **no GPS/location coordinates**, camera EXIF, or caption/keyword tags (the portal strips original camera EXIF/GPS before serving media, and `pkportal` only writes timestamps). No `.xmp` or `.json` sidecar files are saved inside the photo folders — sync and upload state is tracked in `data/state.db`.

## Session & Auto-Relogin

- `pkportal login` opens Google Chrome to the portal, lets you sign in with your email + 6-digit code, saves cookies under `data/storage_state.json` (`0600`) inside this project folder, and closes automatically once authenticated.
- `pkportal sync` reuses saved cookies without opening Chrome. If your session expires (including during a scheduled run), it automatically opens Google Chrome so you can re-authenticate and continues syncing.

## Commands

Run `./pkportal` from the project root (or `pkportal` when `.venv` is activated):

```bash
pkportal login                           # Open Chrome to log in and save cookies
pkportal sync                            # Download new photos/videos and apply metadata
pkportal sync --dry-run                  # Preview pending albums/files without downloading
pkportal sync --output ~/Pictures/Custom # Override output folder for one run
pkportal sync --relogin                  # Force Chrome login before syncing
pkportal sync --redownload-missing       # Re-download files removed from disk
pkportal sync --workers 4                # Set concurrent download workers
pkportal sync --limit-days 2             # Only check the most recent N days
pkportal sync --limit-files 10           # Cap newly downloaded files in this run
pkportal status                          # Show cookie metadata, sync stats, Google Photos status, exiftool, schedule
pkportal schedule install --hour 18 --minute 30
pkportal schedule uninstall
pkportal schedule status
pkportal config show
pkportal config set --output "~/Pictures/Star Academy iCloud"
pkportal config set --flat-season        # Or --day-subfolders (default)
pkportal inspect                         # Capture portal DOM/network diagnostics
```

Runtime state (Chrome profile, portal cookies, Google Photos credentials `data/gphotos_client_secret.json` and `data/gphotos_token.json` at mode `0600`, SQLite DB `data/state.db`, config, logs) and the Python virtualenv (`.venv/`) are stored inside this project folder under `data/` (gitignored) and `.venv/`.

## Google Photos Import & Workspace Skill

`pkportal` can upload newly synced media into app-created Google Photos albums (defaulting to `<Class> - <Season>`, e.g. `Group D - Fall 2026`), tracking albums and uploaded items (`gphotos_albums` and `gphotos_uploads` in `data/state.db`) so reruns stay idempotent:

```bash
pkportal gphotos auth --client-secret ~/Downloads/client_secret_....json [--timeout 300]  # One-time Desktop OAuth setup
pkportal gphotos status                                                                   # Check OAuth & pending upload counts
pkportal gphotos upload --dry-run                                                         # Preview pending Google Photos uploads
pkportal gphotos upload [--class-name "Group D"] [--season "Fall 2026"] [--day YYYY-MM-DD] [--album-title "..." | --no-album] [--limit N] [--workers 4]
pkportal sync --gphotos [--gphotos-album-title "..." | --gphotos-no-album]                # Sync from portal + upload new items
```

- **OAuth scopes & album rules:** Uses Desktop OAuth + PKCE with `https://www.googleapis.com/auth/photoslibrary.appendonly` and `https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata`, which lets `pkportal` create and append to app-created albums (not pre-existing manually created Google Photos albums).
- **Batching:** Streams raw bytes concurrently to `/v1/uploads` and registers items in chronological batches of up to 50 via `mediaItems:batchCreate`.
- **Workspace skill:** Includes the Antigravity skill at `.agents/skills/star-academy-google-photos/SKILL.md` and the one-time Google Cloud Desktop OAuth guide at `.agents/skills/star-academy-google-photos/references/gphotos-oauth-setup.md`.

## Tests

```bash
.venv/bin/pytest
```
