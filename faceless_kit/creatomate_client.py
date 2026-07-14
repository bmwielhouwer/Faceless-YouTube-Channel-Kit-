"""Creatomate render client — builds the finished MP4 from a JSON source."""
from __future__ import annotations

import logging
import math
import time

import requests

log = logging.getLogger("faceless_kit.creatomate")

API = "https://api.creatomate.com/v1"


class CreatomateError(RuntimeError):
    pass


class CreatomateClient:
    def __init__(self, api_key: str, template_id: str = "", poll_interval: int = 30, timeout: int = 1800):
        self.template_id = template_id
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        )

    def submit(self, payload: dict) -> str:
        """Submit any render payload (template- or source-based); return its id."""
        log.info("Submitting Creatomate render (%s)",
                 "source" if "source" in payload else f"template {self.template_id}")
        resp = self.session.post(f"{API}/renders", json=payload, timeout=120)
        if not resp.ok:
            log.error("Creatomate render error (%s): %s", resp.status_code, resp.text[:800])
        resp.raise_for_status()
        data = resp.json()
        render = data[0] if isinstance(data, list) else data
        render_id = render["id"]
        log.info("Render queued: %s (status=%s)", render_id, render.get("status"))
        return render_id

    def render(self, modifications: dict) -> str:
        """Template-based render (kept for the legacy path)."""
        return self.submit({
            "template_id": self.template_id,
            "output_format": "mp4",
            "modifications": modifications,
        })


    def wait(self, render_id: str) -> str:
        """Poll until the render succeeds; return the output URL."""
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            resp = self.session.get(f"{API}/renders/{render_id}", timeout=30)
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status")
            log.info("Render %s status: %s", render_id, status)
            if status == "succeeded":
                url = data.get("url")
                if not url:
                    raise CreatomateError("Render succeeded but no URL returned")
                return url
            if status in ("failed", "cancelled"):
                raise CreatomateError(
                    f"Render {status}: {data.get('error_message', 'unknown error')}"
                )
            time.sleep(self.poll_interval)
        raise CreatomateError(f"Render timed out after {self.timeout}s")


def build_modifications(
    *,
    audio_url: str,
    title: str,
    script: str,
    audio_element: str,
    subtitle_element: str,
    title_element: str,
    audio_seconds: float | None = None,
    extra: dict | None = None,
) -> dict:
    """Map pipeline data onto the template's named elements.

    Element names depend on YOUR Creatomate template — discover them with
    ``python -m tools.inspect_creatomate_template`` and set the matching env
    vars (CREATOMATE_AUDIO_ELEMENT, CREATOMATE_SUBTITLE_ELEMENT, ...).
    """
    mods: dict = {}
    if audio_element:
        mods[f"{audio_element}.source"] = audio_url
        # Force the element to play the full voiceover. Without this, Creatomate
        # keeps the template's original audio duration and clips the new file,
        # which (with an Auto composition) caps the whole video's length.
        if audio_seconds:
            mods[f"{audio_element}.duration"] = round(audio_seconds, 2)
    if subtitle_element:
        # Turn the text element into auto-transcribed, timed subtitles driven by
        # the voiceover audio. (Do NOT also set static .text, or Creatomate would
        # show the whole script as one block instead of word-timed captions.)
        mods[f"{subtitle_element}.transcript_source"] = audio_element
        if audio_seconds:
            mods[f"{subtitle_element}.duration"] = round(audio_seconds, 2)
    if title_element:
        mods[f"{title_element}.text"] = title
    if extra:
        mods.update(extra)
    return mods


_RECT = "M 0 0 L 100 0 L 100 100 L 0 100 Z"  # full-box rectangle path
_DARK_SCENES = ["#04121f", "#071a2b", "#0a0a14", "#02131c", "#0b0e16", "#081522"]


def build_source(
    *,
    audio_url: str,
    audio_seconds: float | None,
    accent: str = "#1FA2FF",
    broll_urls: list[str] | None = None,
    music_url: str = "",
    scene_seconds: int = 35,
    font: str = "Montserrat",
    watermark: str = "",
) -> dict:
    """Build a complete, template-less Creatomate composition.

    Produces a full-length 1080p video whose duration follows the voiceover:
    dark scenes that change every ``scene_seconds`` (real dark-tech stock clips
    if ``broll_urls`` are supplied, otherwise self-contained dark gradient
    scenes that need no external assets), electric-blue accents, the voiceover,
    optional ambient music, auto-transcribed captions, and a watermark.
    """
    duration = round(audio_seconds or 180.0, 2)
    scenes = max(1, math.ceil(duration / scene_seconds))
    elements: list[dict] = []

    # --- Track 1: background scenes (change every ~scene_seconds) ---
    if broll_urls:
        for i in range(scenes):
            elements.append({
                "type": "video", "track": 1, "source": broll_urls[i % len(broll_urls)],
                "duration": scene_seconds, "fit": "cover", "volume": "0%", "loop": True,
                "animations": [{"type": "fade", "duration": 1.0}],
            })
    else:
        for i in range(scenes):
            elements.append({
                "type": "shape", "track": 1, "path": _RECT,
                "width": "100%", "height": "100%",
                "x_alignment": "50%", "y_alignment": "50%",
                "fill_color": _DARK_SCENES[i % len(_DARK_SCENES)],
                "duration": scene_seconds,
                "animations": [{"type": "fade", "duration": 1.2}],
            })

    # --- Track 2: electric-blue accent lines (full length) ---
    for y in ("9%", "79%"):
        elements.append({
            "type": "shape", "track": 2, "path": _RECT,
            "width": "100%", "height": "0.45%",
            "x_alignment": "50%", "y_alignment": y, "fill_color": accent,
            "time": 0, "duration": duration,
        })

    # --- Track 3: voiceover ---
    elements.append({
        "type": "audio", "track": 3, "name": "Voiceover", "source": audio_url,
        "time": 0, "duration": duration, "volume": "100%",
    })

    # --- Track 4: ambient music (optional) ---
    if music_url:
        elements.append({
            "type": "audio", "track": 4, "source": music_url,
            "time": 0, "duration": duration, "volume": "18%", "loop": True,
        })

    # --- Track 5: auto captions, white bold bottom-center, blue highlight ---
    elements.append({
        "type": "text", "track": 5, "transcript_source": "Voiceover",
        "transcript_effect": "highlight", "transcript_color": accent,
        "transcript_maximum_length": 36,
        "time": 0, "duration": duration,
        "width": "86%", "x_alignment": "50%", "y_alignment": "86%",
        "font_family": font, "font_weight": "700", "font_size": "5.2 vmin",
        "fill_color": "#FFFFFF", "stroke_color": "#000000", "stroke_width": "0.5 vmin",
    })

    # --- Track 6: watermark, top-left ---
    elements.append({
        "type": "text", "track": 6, "text": watermark,
        "time": 0, "duration": duration,
        "x": "4%", "y": "6%", "x_alignment": "0%", "y_alignment": "0%",
        "font_family": font, "font_weight": "700", "font_size": "2.6 vmin",
        "fill_color": accent,
    })

    return {
        "output_format": "mp4",
        "width": 1920, "height": 1080, "frame_rate": 30,
        "duration": duration, "fill_color": "#05060A",
        "elements": elements,
    }
