---
name: star-academy-google-photos
description: >-
  Sync new daily photos and videos from the Star Academy PK Portal with album-date
  EXIF metadata and import newly downloaded files into Google Photos albums. Use when
  the user asks to sync, fetch, re-run, or import Star Academy photos into Google
  Photos, check Google Photos upload status, or set up Google Photos OAuth.
---

# Star Academy -> Google Photos Import

Run this workflow from the repository root containing `./pkportal` and `data/state.db`.

> **Environment note:** Syncing from the portal, uploading to Google Photos, and reading `~/Pictures/Star Academy iCloud` require network and filesystem access outside the default sandbox.

## 1. Check Auth and Sync Status

Check Google Photos OAuth status and pending item counts:

```bash
./pkportal gphotos status
./pkportal status
```

- If `client_secret_exists` is `false`, walk the user through [the OAuth setup guide](./references/gphotos-oauth-setup.md) and install their downloaded Desktop OAuth JSON:
  ```bash
  ./pkportal gphotos auth --client-secret /path/to/client_secret.json
  ```
- If `client_secret_exists` is `true` but `token_exists` or `token_valid` is `false`, authorize or refresh in the browser:
  ```bash
  ./pkportal gphotos auth
  ```

## 2. Preview Pending Work (Optional Dry Run)

Preview both portal downloads and pending Google Photos uploads without downloading or uploading:

```bash
./pkportal sync --gphotos --dry-run
```

Or preview Google Photos uploads only for files already recorded in `data/state.db`:

```bash
./pkportal gphotos upload --dry-run
```

## 3. Run Full Sync + Google Photos Import

Fetch new daily media from the portal, apply album-date EXIF/XMP/QuickTime + filesystem timestamps, and upload unuploaded items into their `Class - Season` Google Photos album (e.g., `Group D - Fall 2026`):

```bash
./pkportal sync --gphotos
```

- If the portal session cookie expired, Chrome opens automatically for re-login and resumes syncing.
- Optional overrides: `--gphotos-album-title "<title>"` or `--gphotos-no-album`.

## 4. Upload-Only (Already Synced Locally)

When files are already downloaded and recorded in `data/state.db`, push only unuploaded items:

```bash
./pkportal gphotos upload
```

Useful filters and flags:
- `--day YYYY-MM-DD` — upload items from a specific album day
- `--class-name "Group D"` / `--season "Fall 2026"` — filter by class or season
- `--album-title "<title>"` or `--no-album` — override target album or skip album placement
- `--limit N` — upload at most `N` pending items
- `--workers N` — concurrent raw byte uploads (default `4`)
- `--dry-run` — show planned album groupings and counts without API calls

## 5. Verify and Report

1. Inspect the JSON output from `sync --gphotos` or `gphotos upload`, then confirm database counts:
   ```bash
   ./pkportal gphotos status
   ```
2. Verify `pending_count` is `0` (or matches expected filtered/unuploaded items).
3. Report to the user:
   - Newly downloaded portal files (`files_downloaded` / `files_skipped`)
   - Newly uploaded Google Photos files (`files_uploaded`) and album title(s) + `product_url`
   - Any `files_missing` or `errors`. If local files were moved or deleted (`files_missing > 0`), restore them with:
     ```bash
     ./pkportal sync --redownload-missing
     ```

## Operational Notes

- **Secrets stay local:** `data/gphotos_client_secret.json` and `data/gphotos_token.json` are stored with `0600` permissions inside `data/`, which is gitignored — never commit or print raw token contents.
- **Album permissions:** Under Google Photos Library API rules (`photoslibrary.appendonly` and `photoslibrary.readonly.appcreateddata`), `pkportal` can create and append to app-created albums (such as `Group D - Fall 2026`), not pre-existing manually created albums in Google Photos.
