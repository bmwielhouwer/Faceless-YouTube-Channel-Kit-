"""Save the finished MP4 to OneDrive.

Two modes:
  * local  — copy into a folder synced by the OneDrive desktop app (simplest,
             zero-auth; the desktop client handles the cloud upload).
  * graph  — upload directly via the Microsoft Graph API (for headless servers
             with no OneDrive sync client).
"""
from __future__ import annotations

import logging
import os
import shutil

import requests

log = logging.getLogger("faceless_kit.onedrive")


def save(local_path: str, filename: str, cfg) -> str:
    if cfg.onedrive_mode == "graph":
        return _save_graph(local_path, filename, cfg)
    return _save_local(local_path, filename, cfg)


def _save_local(local_path: str, filename: str, cfg) -> str:
    os.makedirs(cfg.onedrive_local_dir, exist_ok=True)
    dest = os.path.join(cfg.onedrive_local_dir, filename)
    shutil.copy2(local_path, dest)
    log.info("Saved MP4 to OneDrive (local sync): %s", dest)
    return dest


def _graph_token(cfg) -> str:
    resp = requests.post(
        f"https://login.microsoftonline.com/{cfg.ms_tenant}/oauth2/v2.0/token",
        data={
            "client_id": cfg.ms_client_id,
            "client_secret": cfg.ms_client_secret,
            "refresh_token": cfg.ms_refresh_token,
            "grant_type": "refresh_token",
            "scope": "https://graph.microsoft.com/Files.ReadWrite.All offline_access",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _save_graph(local_path: str, filename: str, cfg) -> str:
    token = _graph_token(cfg)
    folder = cfg.ms_drive_folder.strip("/")
    upload_path = f"{folder}/{filename}"
    url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{upload_path}:/content"
    with open(local_path, "rb") as fh:
        resp = requests.put(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"},
            data=fh,
            timeout=600,
        )
    if not resp.ok:
        log.error("Graph upload error (%s): %s", resp.status_code, resp.text[:500])
    resp.raise_for_status()
    web_url = resp.json().get("webUrl", upload_path)
    log.info("Uploaded MP4 to OneDrive (Graph): %s", web_url)
    return web_url
