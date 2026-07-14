"""Auto-post each finished video to TikTok AND Facebook via Zernio.

Flow (per platform):
  1. GET  https://zernio.com/api/v1/accounts  -> discover the connected TikTok
     account ID and your Facebook page ID (env vars override).
  2. Upload the platform's media file to Zernio (TikTok = 9:16 crop, Facebook =
     full 16:9) -> a hosted media URL.
  3. POST https://zernio.com/api/v1/posts with the shared brand caption and a
     ``scheduledFor`` timestamp computed from the Airtable Posting Schedule
     table, so Brian can retune times in Airtable without touching code.

Set ZERNIO_API_KEY to enable. Posting never raises into the render path — any
failure is logged and the render/email still completes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

log = logging.getLogger("faceless_kit.zernio")

API = "https://zernio.com/api/v1"
PRIVACY = "PUBLIC_TO_EVERYONE"

# Sensible fallbacks if the Posting Schedule table can't be read.
_FALLBACK = {
    "tiktok": {"weekday": "7:00 PM ET", "weekend": "9:00 AM ET", "tz": "America/New_York"},
    "facebook": {"weekday": "12:00 PM ET", "weekend": "11:00 AM ET", "tz": "America/New_York"},
}


def _get(obj, *keys):
    for k in keys:
        if isinstance(obj, dict) and obj.get(k):
            return obj[k]
        v = getattr(obj, k, None)
        if v:
            return v
    return None


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #
def _parse_clock(text: str):
    """'7:00 PM ET' -> datetime.time (drops a trailing tz token like ET/PT)."""
    s = (text or "").strip()
    if not s:
        return None
    parts = s.rsplit(" ", 1)
    if len(parts) == 2 and parts[1].isalpha() and parts[1].upper() not in ("AM", "PM"):
        s = parts[0].strip()
    for fmt in ("%I:%M %p", "%I %p", "%H:%M"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    return None


def compute_scheduled_for(entry: dict, now: datetime | None = None) -> str | None:
    """Next upcoming optimal time as an ISO-8601 timestamp with offset.

    Picks the weekday or weekend time based on the *target* day; if today's slot
    has already passed, rolls to tomorrow (re-checking weekend vs weekday).
    """
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(entry.get("tz") or "America/New_York")
    except Exception:  # noqa: BLE001
        tz = None
    now = now or datetime.now(tz)
    if now.tzinfo is None and tz is not None:
        now = now.replace(tzinfo=tz)

    for day_offset in (0, 1):
        target = (now + timedelta(days=day_offset)).date()
        is_weekend = target.weekday() >= 5  # Sat=5, Sun=6
        clock = _parse_clock(entry.get("weekend" if is_weekend else "weekday"))
        if clock is None:
            continue
        cand = datetime.combine(target, clock, tzinfo=tz)
        if cand > now + timedelta(minutes=2):
            return cand.isoformat()
    return None


def _schedule_for(platform: str, schedule: dict) -> str | None:
    entry = (schedule or {}).get(platform) or _FALLBACK.get(platform, {})
    return compute_scheduled_for(entry)


# --------------------------------------------------------------------------- #
# Zernio REST helpers
# --------------------------------------------------------------------------- #
def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}


def _accounts(api_key: str) -> list[dict]:
    import requests

    resp = requests.get(f"{API}/accounts", headers=_headers(api_key), timeout=45)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        data = data.get("data") or data.get("accounts") or data.get("results") or []
    return data if isinstance(data, list) else []


def _match_account(accounts: list[dict], platform: str) -> str | None:
    for a in accounts:
        plat = str(_get(a, "platform", "type", "provider") or "").lower()
        if platform in plat:
            return _get(a, "accountId", "id", "_id", "pageId")
    return None


def _proxy_url(cfg, file_id: str | None) -> str | None:
    """URL that serves the file from THIS worker's /media endpoint (instant,
    raw bytes straight from Drive — no Drive preview-processing dependency)."""
    base = (getattr(cfg, "public_base_url", "") or "").rstrip("/")
    if not base or not file_id:
        return None
    tok = f"?token={cfg.media_token}" if getattr(cfg, "media_token", "") else ""
    return f"{base}/media/{file_id}.mp4{tok}"


def _host_media(cfg, path: str, filename: str) -> str | None:
    """Make the rendered video fetchable by Zernio.

    Zernio caps direct binary uploads at 4MB (413 on a 190MB render). We upload to
    Drive (archival) and serve the bytes through the worker's /media proxy — which
    returns them instantly via the Drive API, sidestepping Drive's slow preview
    processing and large-file download interstitial. Falls back to a public Drive
    link only if the worker has no public base URL configured.
    """
    from . import gdrive
    _, fid = gdrive.upload2(path, filename, cfg, folder_id=cfg.gdrive_folder_tiktok)
    purl = _proxy_url(cfg, fid)
    if purl:
        return purl
    # Fallback: public Drive URL (needs Drive to finish processing first).
    try:
        gdrive.share_public(fid, cfg)
        gdrive.wait_until_ready(fid, cfg)
        return gdrive.direct_download_url(fid)
    except Exception:  # noqa: BLE001
        log.exception("Drive public-URL fallback failed for %s", filename)
        return None


def _create_post(api_key: str, *, content: str, media_url: str, platform: str,
                 account_id: str, scheduled_for: str | None,
                 platform_specific: dict | None = None) -> str | None:
    import requests

    entry: dict = {"platform": platform, "accountId": account_id}
    if platform_specific:
        entry["platformSpecificContent"] = platform_specific
    payload: dict = {
        "content": content[:2200],
        # URL media source (no binary upload -> no 4MB limit). Send both the
        # mediaItems and mediaUrls shapes for API compatibility. Zernio's
        # mediaItems.type is the media KIND ("video"/"image"), not the source —
        # the URL is carried by the "url" field. (Sending "url" here is rejected
        # with 400 "mediaItems.0.type is invalid".)
        "mediaItems": [{"type": "video", "url": media_url}],
        "mediaUrls": [media_url],
        "platforms": [entry],
    }
    if scheduled_for:
        payload["scheduledFor"] = scheduled_for
    else:
        payload["publishNow"] = True

    resp = requests.post(f"{API}/posts", headers=_headers(api_key),
                         json=payload, timeout=90)
    if not resp.ok:
        log.error("Zernio %s post failed (%s): %s", platform, resp.status_code,
                  resp.text[:300])
        return None
    body = resp.json()
    if isinstance(body, dict):
        body = body.get("data", body)
    url = _get(body, "url", "postUrl", "permalink", "link")
    if not url:
        results = _get(body, "results", "platformResults") or []
        for r in (results if isinstance(results, list) else []):
            if platform in str(_get(r, "platform") or "").lower():
                url = _get(r, "url", "postUrl", "permalink")
                break
    log.info("Scheduled %s post via Zernio (%s): %s",
             platform, _get(body, "id", "_id") or "ok", url or "(link pending)")
    return url or "(scheduled — link pending)"


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def _media_url_for_id(cfg, file_id: str | None) -> str | None:
    """Worker proxy URL for a Drive file id; falls back to a public Drive link."""
    if not file_id:
        return None
    u = _proxy_url(cfg, file_id)
    if u:
        return u
    from . import gdrive
    try:
        gdrive.share_public(file_id, cfg)
        gdrive.wait_until_ready(file_id, cfg)
        return gdrive.direct_download_url(file_id)
    except Exception:  # noqa: BLE001
        log.exception("Public Drive fallback failed for %s", file_id)
        return None


def schedule_from_ids(cfg, caption: str, schedule: dict,
                      tiktok_file_id: str | None,
                      facebook_file_id: str | None,
                      tiktok_caption: str | None = None) -> dict:
    """Stage 2 publisher: schedule TikTok + Facebook from already-rendered Drive
    files, serving each via the worker media proxy. ``caption`` is used for
    Facebook; TikTok uses ``tiktok_caption`` when given (a short, non-promotional
    caption) and falls back to ``caption`` otherwise. Returns
    {tiktok_url, tiktok_time, facebook_url, facebook_time}. Never raises."""
    result = {"tiktok_url": None, "tiktok_time": None,
              "facebook_url": None, "facebook_time": None}
    if not cfg.zernio_api_key:
        return result
    try:
        accounts = []
        try:
            accounts = _accounts(cfg.zernio_api_key)
        except Exception:  # noqa: BLE001
            log.exception("Could not list Zernio accounts")
        tiktok_id = cfg.zernio_tiktok_account_id or _match_account(accounts, "tiktok")
        facebook_id = cfg.zernio_facebook_account_id or _match_account(accounts, "facebook")

        # Honor the configured PLATFORMS list — skip any platform not included.
        want_tiktok = cfg.platform_enabled("tiktok")
        want_facebook = cfg.platform_enabled("facebook")

        if tiktok_file_id and tiktok_id and want_tiktok:
            media_url = _media_url_for_id(cfg, tiktok_file_id)
            when = _schedule_for("tiktok", schedule)
            if media_url:
                result["tiktok_url"] = _create_post(
                    cfg.zernio_api_key, content=tiktok_caption or caption,
                    media_url=media_url, platform="tiktok", account_id=tiktok_id,
                    scheduled_for=when, platform_specific={"privacyLevel": PRIVACY})
                result["tiktok_time"] = when
        elif tiktok_file_id and not want_tiktok:
            log.info("TikTok not in PLATFORMS — skipping TikTok post.")
        elif tiktok_file_id:
            log.error("No TikTok account in Zernio — set ZERNIO_TIKTOK_ACCOUNT_ID.")

        if facebook_file_id and facebook_id and want_facebook:
            media_url = _media_url_for_id(cfg, facebook_file_id)
            when = _schedule_for("facebook", schedule)
            if media_url:
                result["facebook_url"] = _create_post(
                    cfg.zernio_api_key, content=caption, media_url=media_url,
                    platform="facebook", account_id=facebook_id, scheduled_for=when)
                result["facebook_time"] = when
        elif facebook_file_id and not want_facebook:
            log.info("Facebook not in PLATFORMS — skipping Facebook post.")
        elif facebook_file_id:
            log.error("No Facebook page in Zernio — set ZERNIO_FACEBOOK_ACCOUNT_ID.")
    except Exception:  # noqa: BLE001 — posting must never crash the publisher
        log.exception("Zernio schedule_from_ids failed")
    return result


def post_to_socials(tiktok_path: str | None, facebook_path: str | None,
                    caption: str, schedule: dict, cfg,
                    facebook_file_id: str | None = None) -> dict:
    """Schedule the video to TikTok (9:16) and Facebook (16:9).

    If ``facebook_file_id`` is given (the main video's Drive id), Facebook reuses
    that already-uploaded file instead of uploading the full 190MB again.

    Returns {tiktok_url, tiktok_time, facebook_url, facebook_time} (values may be
    None). Never raises.
    """
    result = {"tiktok_url": None, "tiktok_time": None,
              "facebook_url": None, "facebook_time": None}
    if not cfg.zernio_api_key:
        return result

    try:
        accounts = []
        try:
            accounts = _accounts(cfg.zernio_api_key)
        except Exception:  # noqa: BLE001
            log.exception("Could not list Zernio accounts")

        tiktok_id = cfg.zernio_tiktok_account_id or _match_account(accounts, "tiktok")
        facebook_id = cfg.zernio_facebook_account_id or _match_account(accounts, "facebook")

        # Honor the configured PLATFORMS list — skip any platform not included.
        want_tiktok = cfg.platform_enabled("tiktok")
        want_facebook = cfg.platform_enabled("facebook")

        import os
        base = os.path.splitext(os.path.basename(facebook_path or tiktok_path or "video"))[0]

        # ---- TikTok (9:16) ----
        if tiktok_path and tiktok_id and want_tiktok:
            when = _schedule_for("tiktok", schedule)
            media_url = _host_media(cfg, tiktok_path, f"{base}_tiktok_9x16.mp4")
            if media_url:
                result["tiktok_url"] = _create_post(
                    cfg.zernio_api_key, content=caption, media_url=media_url,
                    platform="tiktok", account_id=tiktok_id, scheduled_for=when,
                    platform_specific={"privacyLevel": PRIVACY})
                result["tiktok_time"] = when
        elif tiktok_path and not want_tiktok:
            log.info("TikTok not in PLATFORMS — skipping TikTok post.")
        elif tiktok_path:
            log.error("No TikTok account in Zernio — connect it or set "
                      "ZERNIO_TIKTOK_ACCOUNT_ID.")

        # ---- Facebook (full 16:9) ----
        if not want_facebook and (facebook_path or facebook_file_id):
            log.info("Facebook not in PLATFORMS — skipping Facebook post.")
        elif (facebook_path or facebook_file_id) and facebook_id:
            when = _schedule_for("facebook", schedule)
            if facebook_file_id and _proxy_url(cfg, facebook_file_id):
                # Reuse the already-uploaded main video via the worker proxy.
                media_url = _proxy_url(cfg, facebook_file_id)
            elif facebook_file_id:
                # No worker base URL — fall back to a public Drive link.
                from . import gdrive
                try:
                    gdrive.share_public(facebook_file_id, cfg)
                    gdrive.wait_until_ready(facebook_file_id, cfg)
                    media_url = gdrive.direct_download_url(facebook_file_id)
                except Exception:  # noqa: BLE001
                    log.exception("Could not reuse main Drive file for Facebook")
                    media_url = _host_media(cfg, facebook_path, f"{base}_facebook_16x9.mp4")
            else:
                media_url = _host_media(cfg, facebook_path, f"{base}_facebook_16x9.mp4")
            if media_url:
                result["facebook_url"] = _create_post(
                    cfg.zernio_api_key, content=caption, media_url=media_url,
                    platform="facebook", account_id=facebook_id, scheduled_for=when)
                result["facebook_time"] = when
        elif facebook_path:
            log.error("No Facebook page in Zernio — connect your Facebook page "
                      "or set ZERNIO_FACEBOOK_ACCOUNT_ID.")
    except Exception:  # noqa: BLE001 — posting must never fail the render
        log.exception("Zernio social posting failed")
    return result
