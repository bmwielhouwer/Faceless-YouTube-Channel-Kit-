"""Upload the voiceover MP3 to a public URL so Creatomate can fetch it.

Creatomate renders by downloading assets from URLs, so the locally generated
MP3 must be reachable over HTTP first.  Two lightweight, no-account providers
are supported; swap in your own bucket later if you prefer permanence.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger("faceless_kit.audio_host")


def upload(local_path: str, provider: str = "tmpfiles") -> str:
    provider = (provider or "tmpfiles").lower()
    if provider == "tmpfiles":
        return _tmpfiles(local_path)
    if provider == "0x0":
        return _zero_x_zero(local_path)
    raise ValueError(f"Unknown AUDIO_HOST_PROVIDER: {provider}")


def _tmpfiles(local_path: str) -> str:
    with open(local_path, "rb") as fh:
        resp = requests.post(
            "https://tmpfiles.org/api/v1/upload",
            files={"file": fh},
            timeout=120,
        )
    resp.raise_for_status()
    page_url = resp.json()["data"]["url"]
    # Convert the human page URL into a direct-download URL:
    #   https://tmpfiles.org/12345/file.mp3 -> https://tmpfiles.org/dl/12345/file.mp3
    direct = page_url.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1)
    log.info("Audio hosted at %s", direct)
    return direct


def _zero_x_zero(local_path: str) -> str:
    with open(local_path, "rb") as fh:
        resp = requests.post(
            "https://0x0.st",
            files={"file": fh},
            headers={"User-Agent": "faceless-youtube-kit-pipeline/1.0"},
            timeout=120,
        )
    resp.raise_for_status()
    url = resp.text.strip()
    log.info("Audio hosted at %s", url)
    return url
