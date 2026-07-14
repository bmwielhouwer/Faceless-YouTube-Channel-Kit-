"""Notifications via the Gmail API (Railway blocks SMTP ports).

Sends with the same Google OAuth credentials used for Drive — the refresh token
must include the gmail.send scope (re-run tools/gdrive_auth.py to add it). Falls
back to SMTP only if Gmail-API creds are absent.
"""
from __future__ import annotations

import base64
import logging
import smtplib
from email.message import EmailMessage
from email.mime.text import MIMEText

import requests

log = logging.getLogger("faceless_kit.notify")

TOKEN_URI = "https://oauth2.googleapis.com/token"
GMAIL_SEND = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


def _fmt_when(value) -> str:
    """Render an ISO scheduledFor timestamp as a friendly local string."""
    if not value:
        return ""
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(str(value))
        return dt.strftime("%a %b %-d, %-I:%M %p %Z").strip()
    except Exception:  # noqa: BLE001
        return str(value)


def send_video_ready(cfg, title, filename, location, copy=None, video_url=None,
                     thumbnail_path=None, youtube_url=None, tiktok_url=None,
                     tiktok_time=None, facebook_url=None, facebook_time=None,
                     shorts_urls=None) -> None:
    copy = copy or {}
    subject = f"New {cfg.channel_name} Video Ready — {title}"
    drive_link = location or "(see Google Drive)"
    backup = f"\nDirect download (backup): {video_url}" if video_url else ""
    live = f"\n🔴 LIVE ON YOUTUBE: {youtube_url}" if youtube_url else ""
    shorts = ""
    if shorts_urls:
        shorts = "\n📱 YOUTUBE SHORTS:\n" + "\n".join(f"   • {u}" for u in shorts_urls)
    tiktok = ""
    if tiktok_url:
        when = _fmt_when(tiktok_time)
        tiktok = f"\n🎵 TIKTOK (scheduled{' ' + when if when else ''}): {tiktok_url}"
    facebook_line = ""
    if facebook_url:
        when = _fmt_when(facebook_time)
        facebook_line = f"\n📘 FACEBOOK (scheduled{' ' + when if when else ''}): {facebook_url}"
    body = (
        f"Your video is ready. Everything you need is below.\n\n"
        f"FILE:         {filename}\n"
        f"GOOGLE DRIVE: {drive_link}{backup}{live}{shorts}{tiktok}{facebook_line}\n\n"
        f"================ YOUTUBE TITLE ================\n"
        f"{title}\n\n"
        f"============= YOUTUBE DESCRIPTION =============\n"
        f"{copy.get('youtube', '')}\n\n"
        f"================ YOUTUBE TAGS =================\n"
        f"{copy.get('tags', '')}\n\n"
        f"============== FACEBOOK CAPTION ===============\n"
        f"{copy.get('facebook', '')}\n\n"
        f"— {cfg.channel_name} automation"
    )
    attachments = [thumbnail_path] if thumbnail_path else None
    _send(cfg, subject, body, attachments)


def send_render_ready(cfg, title, filename, location, thumbnail_path=None) -> None:
    """Stage 1 notice: the video rendered, verified, and is queued for publishing."""
    subject = f"Rendered & verified — {title}"
    drive_link = location or "(see Google Drive)"
    body = (
        f"Stage 1 complete — this video rendered, passed verification, and is in "
        f"Google Drive. It will publish automatically at the scheduled morning hour "
        f"(YouTube + TikTok/Facebook via Zernio).\n\n"
        f"FILE:         {filename}\n"
        f"GOOGLE DRIVE: {drive_link}\n\n"
        f"Status is now 'Video Ready'. You'll get a second email with all the live "
        f"links once it's published.\n\n"
        f"— {cfg.channel_name} automation"
    )
    attachments = [thumbnail_path] if thumbnail_path else None
    _send(cfg, subject, body, attachments)


def _send(cfg, subject: str, body: str, attachments=None) -> None:
    to = cfg.notify_email
    if not to:
        log.warning("No NOTIFY_EMAIL set — skipping notification")
        return
    if cfg.google_client_id and cfg.google_client_secret and cfg.google_refresh_token:
        try:
            _send_gmail_api(cfg, subject, body, to, attachments)
            return
        except Exception as exc:  # noqa: BLE001
            log.error("Gmail API send failed (%s); trying SMTP", exc)
    _send_smtp(cfg, subject, body, to)


def send_error(cfg, title, error) -> None:
    _send(cfg, f"{cfg.channel_name} pipeline error — {title}", f"The pipeline failed:\n\n{error}")


def _access_token(cfg) -> str:
    resp = requests.post(TOKEN_URI, data={
        "client_id": cfg.google_client_id,
        "client_secret": cfg.google_client_secret,
        "refresh_token": cfg.google_refresh_token,
        "grant_type": "refresh_token",
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()["access_token"]


def _send_gmail_api(cfg, subject: str, body: str, to: str, attachments=None) -> None:
    token = _access_token(cfg)
    msg = EmailMessage()
    msg["To"] = to
    if cfg.gmail_address:
        msg["From"] = cfg.gmail_address
    msg["Subject"] = subject
    msg.set_content(body)
    for path in (attachments or []):
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            sub = "jpeg" if path.lower().endswith((".jpg", ".jpeg")) else "png"
            msg.add_attachment(data, maintype="image", subtype=sub,
                               filename=path.rsplit("/", 1)[-1])
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not attach %s: %s", path, exc)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    resp = requests.post(
        GMAIL_SEND,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"raw": raw},
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"{resp.status_code}: {resp.text[:300]}")
    log.info("Notification sent via Gmail API to %s", to)


def _send_smtp(cfg, subject: str, body: str, to: str) -> None:
    if not (cfg.gmail_address and cfg.gmail_app_password):
        log.warning("SMTP not configured — notification not sent")
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = cfg.gmail_address
    msg["To"] = to
    try:
        with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30) as server:
            server.login(cfg.gmail_address, cfg.gmail_app_password)
            server.sendmail(cfg.gmail_address, [to], msg.as_string())
        log.info("Notification sent via SMTP to %s", to)
    except Exception as exc:  # noqa: BLE001
        log.error("SMTP send failed: %s", exc)
