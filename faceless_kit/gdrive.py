"""Upload the finished MP4 directly to Google Drive (resumable upload).

Headless-friendly: the worker authenticates with a long-lived OAuth refresh
token (minted once via ``python -m tools.gdrive_auth``). Files are owned by your
own Google account and land in the configured folder. No Microsoft / Azure.
"""
from __future__ import annotations

import json
import logging
import os

import requests

log = logging.getLogger("faceless_kit.gdrive")

TOKEN_URI = "https://oauth2.googleapis.com/token"
RESUMABLE_INIT = (
    "https://www.googleapis.com/upload/drive/v3/files"
    "?uploadType=resumable&supportsAllDrives=true&fields=id,webViewLink,name,size,mimeType"
)


def _access_token(cfg) -> str:
    resp = requests.post(
        TOKEN_URI,
        data={
            "client_id": cfg.google_client_id,
            "client_secret": cfg.google_client_secret,
            "refresh_token": cfg.google_refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if not resp.ok:
        log.error("Google token refresh failed (%s): %s", resp.status_code, resp.text[:500])
    resp.raise_for_status()
    return resp.json()["access_token"]


def upload(local_path: str, filename: str, cfg, folder_id: str | None = None) -> str:
    """Upload ``local_path`` as ``filename`` into the given Drive folder
    (defaults to the configured root folder). Returns a shareable view link."""
    link, _ = upload2(local_path, filename, cfg, folder_id=folder_id)
    return link


def upload2(local_path: str, filename: str, cfg, folder_id: str | None = None) -> tuple[str, str | None]:
    """Like :func:`upload` but also returns the Drive file id (for reuse/resume)."""
    info = _do_upload(local_path, filename, cfg, folder_id=folder_id)
    fid = info.get("id")
    link = info.get("webViewLink") or f"https://drive.google.com/file/d/{fid}/view"
    log.info("Uploaded MP4 to Google Drive: %s", link)
    return link, fid


def extract_file_id(link_or_id: str | None) -> str | None:
    """Pull a Drive file id out of a view link (or pass an id straight through)."""
    if not link_or_id:
        return None
    s = str(link_or_id)
    if "/" not in s and len(s) > 10:
        return s
    import re
    m = re.search(r"/d/([A-Za-z0-9_-]+)", s) or re.search(r"[?&]id=([A-Za-z0-9_-]+)", s)
    return m.group(1) if m else None


def wait_until_ready(file_id: str, cfg, timeout: int | None = None,
                     interval: int | None = None) -> bool:
    """Block until Drive finishes server-side processing of an uploaded video.

    Drive returns upload 'success' as soon as bytes land, but the file can't be
    previewed or reliably served through its share URL until processing completes.
    Polls the file's metadata until it has videoMediaMetadata / a thumbnail (or
    the timeout elapses). Returns True if confirmed ready. Never raises.
    """
    import time as _time
    timeout = timeout if timeout is not None else getattr(cfg, "media_ready_timeout", 300)
    interval = interval if interval is not None else getattr(cfg, "media_ready_interval", 10)
    url = (f"https://www.googleapis.com/drive/v3/files/{file_id}"
           "?fields=id,trashed,hasThumbnail,thumbnailLink,videoMediaMetadata"
           "&supportsAllDrives=true")
    deadline = _time.time() + timeout
    try:
        token = _access_token(cfg)
    except Exception:  # noqa: BLE001
        return False
    while True:
        try:
            r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
            if r.status_code == 401:
                token = _access_token(cfg)
            elif r.ok:
                d = r.json()
                if d.get("trashed"):
                    log.warning("Drive file %s is trashed — not ready", file_id)
                    return False
                vmd = d.get("videoMediaMetadata") or {}
                if vmd.get("durationMillis") or d.get("hasThumbnail"):
                    log.info("Drive file %s processed and ready for remote fetch", file_id)
                    return True
        except Exception:  # noqa: BLE001
            log.exception("Drive readiness poll error for %s", file_id)
        if _time.time() >= deadline:
            log.warning("Drive file %s not confirmed ready after %ss; proceeding anyway",
                        file_id, timeout)
            return False
        _time.sleep(interval)


def open_stream(file_id: str, cfg, range_header: str | None = None):
    """Open an authenticated streaming GET of the original file bytes (alt=media).

    Serves the bytes the instant the upload finished — no dependency on Drive's
    async preview processing. Returns a streaming ``requests.Response`` (caller
    iterates and closes) or None on failure. Forwards a Range header if given.

    Retries once with a fresh token on a transient upstream status (expired token,
    rate limit, 5xx) so a momentary Drive hiccup at post time doesn't fail the
    fetch. Permanent statuses (404 deleted, 403) are returned as-is for the caller
    to map to the right HTTP response.
    """
    url = (f"https://www.googleapis.com/drive/v3/files/{file_id}"
           "?alt=media&supportsAllDrives=true")
    transient = {401, 408, 429, 500, 502, 503, 504}
    last = None
    for attempt in range(2):
        try:
            token = _access_token(cfg)  # fresh token per attempt
            headers = {"Authorization": f"Bearer {token}"}
            if range_header:
                headers["Range"] = range_header
            resp = requests.get(url, headers=headers, stream=True, timeout=900)
            if resp.status_code in (200, 206):
                return resp
            if resp.status_code in transient and attempt == 0:
                log.warning("Drive stream %s for %s — retrying with fresh token",
                            resp.status_code, file_id)
                resp.close()
                last = resp
                continue
            return resp  # permanent (404/403/etc.) — let the caller decide
        except Exception:  # noqa: BLE001
            log.exception("Drive open_stream failed for %s (attempt %d)", file_id, attempt)
            last = None
    return last


def file_size(file_id: str, cfg) -> int:
    try:
        token = _access_token(cfg)
        r = requests.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}"
            "?fields=size&supportsAllDrives=true",
            headers={"Authorization": f"Bearer {token}"}, timeout=30)
        if r.ok:
            return int(r.json().get("size") or 0)
    except Exception:  # noqa: BLE001
        pass
    return 0


def download(file_id: str, dest_path: str, cfg) -> str | None:
    """Download an app-owned Drive file by id to ``dest_path`` (authenticated)."""
    try:
        token = _access_token(cfg)
        url = (f"https://www.googleapis.com/drive/v3/files/{file_id}"
               "?alt=media&supportsAllDrives=true")
        with requests.get(url, headers={"Authorization": f"Bearer {token}"},
                          stream=True, timeout=900) as r:
            if not r.ok:
                log.error("Drive download failed (%s): %s", r.status_code, r.text[:300])
                return None
            with open(dest_path, "wb") as fh:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        fh.write(chunk)
        return dest_path if os.path.getsize(dest_path) > 0 else None
    except Exception:  # noqa: BLE001
        log.exception("Drive download error for %s", file_id)
        return None


def upload_public(local_path: str, filename: str, cfg, folder_id: str | None = None) -> str | None:
    """Upload a file, make it 'anyone with the link can view', and return a
    direct-download URL suitable as a remote media source (e.g. for Zernio).

    Returns None on failure. The direct-download host bypasses Drive's large-file
    virus-scan interstitial so third parties fetch the raw bytes, not an HTML page.
    """
    try:
        info = _do_upload(local_path, filename, cfg, folder_id=folder_id)
        file_id = info.get("id")
        if not file_id:
            return None
        share_public(file_id, cfg)
        # Let Drive finish processing before anything fetches the file.
        wait_until_ready(file_id, cfg)
        url = direct_download_url(file_id)
        log.info("Shared %s publicly for remote fetch: %s", filename, url)
        return url
    except Exception:  # noqa: BLE001
        log.exception("Public Drive share failed for %s", filename)
        return None


def share_public(file_id: str, cfg) -> None:
    """Grant 'anyone with the link' reader access to an app-created file."""
    token = _access_token(cfg)
    resp = requests.post(
        f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions"
        "?supportsAllDrives=true",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=json.dumps({"role": "reader", "type": "anyone"}),
        timeout=30,
    )
    # 200 = created, 409-ish already-shared states are fine.
    if not resp.ok and resp.status_code not in (409,):
        log.error("Drive share failed (%s): %s", resp.status_code, resp.text[:300])
        resp.raise_for_status()


def direct_download_url(file_id: str) -> str:
    """Public direct-download URL that returns raw bytes for large files."""
    return (f"https://drive.usercontent.google.com/download"
            f"?id={file_id}&export=download&confirm=t")


def _do_upload(local_path: str, filename: str, cfg, folder_id: str | None = None) -> dict:
    """Resumable upload; returns the Drive file info dict (id, webViewLink, name)."""
    if not (cfg.google_client_id and cfg.google_client_secret and cfg.google_refresh_token):
        raise RuntimeError(
            "Google Drive not configured. Run `python -m tools.gdrive_auth` and set "
            "YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET / YOUTUBE_REFRESH_TOKEN."
        )

    token = _access_token(cfg)
    # Set mimeType explicitly so Drive treats it as a previewable video even if
    # content sniffing would otherwise guess application/octet-stream.
    metadata: dict = {"name": filename, "mimeType": "video/mp4"}
    parent = folder_id or cfg.gdrive_folder_id
    if parent:
        metadata["parents"] = [parent]

    size = os.path.getsize(local_path)

    # 1. Open a resumable session.
    init = requests.post(
        RESUMABLE_INIT,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(size),
        },
        data=json.dumps(metadata),
        timeout=60,
    )
    if not init.ok:
        log.error("Drive resumable init failed (%s): %s", init.status_code, init.text[:500])
    init.raise_for_status()
    session_url = init.headers["Location"]

    # 2. Stream the file in one shot (requests sets Content-Length from the file).
    with open(local_path, "rb") as fh:
        put = requests.put(
            session_url,
            headers={"Content-Type": "video/mp4"},
            data=fh,
            timeout=900,
        )
    if not put.ok:
        log.error("Drive upload failed (%s): %s", put.status_code, put.text[:500])
    put.raise_for_status()
    info = put.json()
    # Verify Drive received the whole file — a truncated upload (e.g. the worker
    # killed mid-PUT) is the classic cause of a file that won't preview/download.
    remote = int(info.get("size") or 0)
    if remote and remote != size:
        raise RuntimeError(
            f"Drive upload truncated for {filename}: sent {size} bytes, Drive stored "
            f"{remote}. Treating as failed so it isn't posted as a broken file.")
    log.info("Drive upload verified: %s (%d bytes)", filename, remote or size)
    return info
