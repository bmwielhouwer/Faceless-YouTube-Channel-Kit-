"""Upload a finished video to YouTube via the Data API v3 (resumable upload).

Uses the same Google OAuth credentials as Drive/Gmail — the refresh token must
include the youtube.upload scope (re-run tools/gdrive_auth.py to add it). Returns
the video URL.
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger("faceless_kit.youtube")

TOKEN_URI = "https://oauth2.googleapis.com/token"
UPLOAD = ("https://www.googleapis.com/upload/youtube/v3/videos"
          "?uploadType=resumable&part=snippet,status")


def _token_info(cfg) -> dict:
    """Exchange the refresh token for an access token. Returns the full token
    response, which includes the granted ``scope`` string — a refresh-token grant
    only carries the scopes originally consented to (Google ignores any requested
    ``scope`` here), so this is how we detect a token minted without YouTube."""
    resp = requests.post(TOKEN_URI, data={
        "client_id": cfg.google_client_id,
        "client_secret": cfg.google_client_secret,
        "refresh_token": cfg.google_refresh_token,
        "grant_type": "refresh_token",
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()


def upload(local_path: str, title: str, description: str, tags, cfg,
           thumbnail_path: str | None = None) -> str | None:
    """Upload the MP4 to YouTube; return its watch URL (or None on failure).

    If ``thumbnail_path`` is given, the JPG is applied to the video via
    thumbnails.set immediately after the upload completes.
    """
    if not (cfg.google_client_id and cfg.google_client_secret and cfg.google_refresh_token):
        log.warning("Google creds missing — cannot upload to YouTube")
        return None

    info = _token_info(cfg)
    token = info.get("access_token")
    granted = info.get("scope", "") or ""
    # Fail fast with an actionable message if the refresh token lacks the upload
    # scope — otherwise YouTube returns an opaque 403 Forbidden on the upload call.
    if "youtube" not in granted:
        log.error(
            "YOUTUBE_REFRESH_TOKEN is missing the youtube.upload scope "
            "(granted scopes: %s). This token was minted before YouTube was "
            "enabled. Re-run `python -m tools.gdrive_auth` to re-consent (it now "
            "requests youtube.upload), then update YOUTUBE_REFRESH_TOKEN in your host.",
            granted or "(none)")
        return None
    size = os.path.getsize(local_path)
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "tags": [t for t in (tags or []) if t][:30],
            "categoryId": cfg.youtube_category,
        },
        "status": {
            "privacyStatus": cfg.youtube_privacy,
            "selfDeclaredMadeForKids": False,
            # AI/altered-content disclosure — auto-checks the synthetic-media box
            # so every upload is disclosed without manual selection in Studio.
            "containsSyntheticMedia": True,
        },
    }
    init = requests.post(
        UPLOAD,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/*",
            "X-Upload-Content-Length": str(size),
        },
        json=body,
        timeout=60,
    )
    if not init.ok:
        log.error("YouTube upload init failed (%s): %s", init.status_code, init.text[:400])
        if init.status_code == 403:
            log.error("403 with the youtube scope present usually means the "
                      "YouTube Data API v3 is not enabled in the Google Cloud "
                      "project, or the Google account has no YouTube channel.")
        init.raise_for_status()
    session_url = init.headers["Location"]

    with open(local_path, "rb") as fh:
        put = requests.put(session_url, headers={"Content-Type": "video/mp4"}, data=fh, timeout=2400)
    if not put.ok:
        log.error("YouTube upload failed (%s): %s", put.status_code, put.text[:400])
        put.raise_for_status()
    result = put.json()
    vid = result.get("id")
    url = f"https://youtu.be/{vid}" if vid else None
    # The insert response (part=snippet,status) carries the status YouTube actually
    # assigned — log it so the channel-visibility question is answerable from logs
    # alone, with no extra OAuth scope needed.
    status = result.get("status", {}) if isinstance(result, dict) else {}
    returned_privacy = status.get("privacyStatus")
    upload_status = status.get("uploadStatus")
    log.info("Uploaded to YouTube: %s (requested privacy=%s, returned privacy=%s, uploadStatus=%s)",
             url, cfg.youtube_privacy, returned_privacy or "?", upload_status or "?")
    if returned_privacy and returned_privacy != cfg.youtube_privacy:
        log.warning("YouTube OVERRODE privacy: requested %r but the video is %r. "
                    "Videos uploaded via the API are force-locked to private until "
                    "the Google Cloud project passes YouTube's API compliance audit "
                    "(apply via the API Services panel in Cloud Console).",
                    cfg.youtube_privacy, returned_privacy)
    if upload_status in ("rejected", "failed"):
        log.error("YouTube uploadStatus=%s — reason: %s", upload_status,
                  status.get("rejectionReason") or status.get("failureReason") or "unknown")
    # Apply the custom thumbnail to the freshly-uploaded video.
    if vid and thumbnail_path and os.path.exists(thumbnail_path):
        set_thumbnail(vid, thumbnail_path, token)
    return url


THUMBNAIL_SET = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"


def set_thumbnail(video_id: str, thumb_path: str, token: str) -> bool:
    """Apply a JPG thumbnail to an uploaded video via thumbnails.set. Returns
    True on success. Requires a verified channel (custom thumbnails are a
    verified-account feature) — a 403 here means the channel isn't verified."""
    try:
        with open(thumb_path, "rb") as fh:
            r = requests.post(
                f"{THUMBNAIL_SET}?videoId={video_id}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"},
                data=fh, timeout=120)
        if r.ok:
            log.info("YouTube thumbnail set for %s", video_id)
            return True
        log.error("YouTube thumbnail set failed (%s): %s — custom thumbnails need a "
                  "verified channel.", r.status_code, r.text[:300])
    except Exception:  # noqa: BLE001
        log.exception("YouTube thumbnail set error for %s", video_id)
    return False
