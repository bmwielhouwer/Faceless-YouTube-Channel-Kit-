"""Storage dispatcher — where the finished MP4 lands.

Selected by STORAGE_PROVIDER:
  * gdrive   -> Google Drive (default; native upload to your account)
  * onedrive -> OneDrive (local sync or Microsoft Graph, see onedrive.py)
  * local    -> just copy into a local directory
"""
from __future__ import annotations

import logging
import os
import shutil

from . import gdrive, onedrive

log = logging.getLogger("faceless_kit.storage")


def save(local_path: str, filename: str, cfg, folder_id: str | None = None) -> str:
    provider = (cfg.storage_provider or "gdrive").lower()
    if provider == "gdrive":
        return gdrive.upload(local_path, filename, cfg, folder_id=folder_id)
    if provider == "onedrive":
        return onedrive.save(local_path, filename, cfg)
    if provider == "local":
        os.makedirs(cfg.onedrive_local_dir, exist_ok=True)
        dest = os.path.join(cfg.onedrive_local_dir, filename)
        shutil.copy2(local_path, dest)
        log.info("Saved MP4 locally: %s", dest)
        return dest
    raise ValueError(f"Unknown STORAGE_PROVIDER: {provider}")
