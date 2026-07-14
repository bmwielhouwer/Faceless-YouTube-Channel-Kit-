"""Pexels Video API client — fetches dark/tech B-roll clip URLs."""
from __future__ import annotations

import logging
import random

import requests

log = logging.getLogger("faceless_kit.pexels")

SEARCH = "https://api.pexels.com/videos/search"

DEFAULT_QUERIES = [
    "dark technology background",
    "laptop coding dark",
    "data dashboard analytics",
    "digital network connection",
    "city at night aerial",
    "code screen programming",
    "server room",
    "circuit board macro",
    "cyber security dark",
    "futuristic interface",
]


def _best_mp4(video: dict, target_w: int = 1920) -> str | None:
    files = [
        f for f in video.get("video_files", [])
        if f.get("file_type") == "video/mp4" and f.get("link") and (f.get("width") or 0) >= 1280
    ]
    if not files:
        return None
    # Prefer the file closest to (but not far above) 1080p to keep encoding light.
    files.sort(key=lambda f: abs((f.get("width") or 0) - target_w))
    return files[0]["link"]


def fetch_clip_urls(api_key: str, count: int, queries: list[str] | None = None) -> list[str]:
    """Return up to ``count`` direct MP4 URLs for dark/tech B-roll."""
    queries = queries or DEFAULT_QUERIES
    session = requests.Session()
    session.headers["Authorization"] = api_key
    urls: list[str] = []
    for q in queries:
        if len(urls) >= count:
            break
        try:
            r = session.get(
                SEARCH,
                params={"query": q, "per_page": 8, "orientation": "landscape", "size": "medium"},
                timeout=30,
            )
            if not r.ok:
                log.warning("Pexels search '%s' failed (%s)", q, r.status_code)
                continue
            for v in r.json().get("videos", []):
                link = _best_mp4(v)
                if link and link not in urls:
                    urls.append(link)
                    if len(urls) >= count:
                        break
        except Exception as exc:  # noqa: BLE001 — never let B-roll fetching break a render
            log.warning("Pexels search '%s' error: %s", q, exc)
    log.info("Pexels returned %d B-roll clip URLs", len(urls))
    return urls[:count]


def fetch_clip_for_query(api_key: str, query: str, used_ids: set | None = None):
    """Return a (clip_id, mp4_url) for ``query`` that isn't in ``used_ids``.

    Pulls a RANDOM result page and picks a RANDOM clip from it (not the top
    result), so the same query yields different footage on different videos.
    """
    used_ids = used_ids or set()
    for attempt in range(2):
        page = random.randint(1, 4) if attempt == 0 else 1
        try:
            r = requests.get(
                SEARCH,
                headers={"Authorization": api_key},
                params={"query": query, "per_page": 20, "orientation": "landscape", "page": page},
                timeout=30,
            )
            if not r.ok:
                log.warning("Pexels query '%s' p%d failed (%s)", query, page, r.status_code)
                continue
            candidates = []
            for v in r.json().get("videos", []):
                vid, link = v.get("id"), _best_mp4(v)
                if vid and link and vid not in used_ids:
                    candidates.append((vid, link))
            if candidates:
                return random.choice(candidates)
        except Exception as exc:  # noqa: BLE001
            log.warning("Pexels query '%s' error: %s", query, exc)
            return None
    return None
