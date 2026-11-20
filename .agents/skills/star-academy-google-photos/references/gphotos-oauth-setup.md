# Google Photos Desktop OAuth Setup (One-Time)

This project uses the **Google Photos Library API** with a personal **Desktop app** OAuth 2.0 client. Setup takes ~5 minutes in Google Cloud Console and stores credentials locally inside `data/` (`0600` permissions, gitignored).

## 1. Create a Google Cloud Project & Enable the API

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create a new project (e.g., `star-academy-photos-sync`) or select an existing personal project.
2. Go to **APIs & Services -> Library**.
3. Search for **Photos Library API** and click **Enable**.

## 2. Configure the OAuth Consent Screen

1. Go to **Google Auth platform** (or **APIs & Services -> OAuth consent screen**).
2. Choose **External** as the User Type (unless using a Google Workspace Internal org) and fill in the required app name and contact email.
3. Under **Audience / Test users**, click **Add users** and add your Google account email address (required while the app stays in Testing mode — no app verification needed for personal use).
4. Under **Data Access / Scopes**, add these two scopes:
   - `https://www.googleapis.com/auth/photoslibrary.appendonly`
   - `https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata`

## 3. Create a Desktop App OAuth Client ID

1. Go to **Clients** (or **APIs & Services -> Credentials -> Create Credentials -> OAuth client ID**).
2. Set **Application type** to **Desktop app** (do **not** choose *Web application*).
3. Name it (e.g., `pkportal-desktop`) and click **Create**.
4. Click **Download JSON** to save `client_secret_....json` (typically into `~/Downloads/`).

## 4. Authorize `pkportal`

Run this from the project root, passing the downloaded JSON file:

```bash
./pkportal gphotos auth --client-secret ~/Downloads/client_secret_....json
```

This validates and copies the client secret to `data/gphotos_client_secret.json` (`0600`), opens your browser for Google sign-in and consent, exchanges the authorization code using PKCE, and saves refresh/access tokens to `data/gphotos_token.json` (`0600`).

Confirm authorization and pending counts:

```bash
./pkportal gphotos status
```

## Notes on Scopes & Refresh Tokens

- Google's `photoslibrary.appendonly` + `photoslibrary.readonly.appcreateddata` scopes let `pkportal` upload media, create albums (e.g., `Group D - Fall 2026`), and reuse albums it created, while preventing access to unrelated personal photos.
- Testing-mode OAuth refresh tokens for **External** apps can expire after 7 days; if a refresh fails later, rerun `./pkportal gphotos auth` (no need to re-pass `--client-secret`).
