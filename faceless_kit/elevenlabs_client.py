"""ElevenLabs text-to-speech client. The voice is chosen entirely via config."""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger("faceless_kit.elevenlabs")

API = "https://api.elevenlabs.io/v1/text-to-speech"

# Generic fallback voice — "Rachel", a public ElevenLabs preset available to every
# account (NOT account-specific). Set ELEVENLABS_VOICE_ID to use your own voice.
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"


class ElevenLabsClient:
    def __init__(
        self,
        api_key: str,
        voice_id: str | None = None,
        model_id: str = "eleven_multilingual_v2",
        stability: float = 0.5,
        similarity: float = 0.75,
        style: float = 0.35,
        speaker_boost: bool = True,
        speed: float = 1.0,
    ):
        self.api_key = api_key
        # Prefer an explicitly passed voice_id; otherwise read ELEVENLABS_VOICE_ID
        # from the environment, falling back to the built-in default.
        self.voice_id = voice_id or os.getenv("ELEVENLABS_VOICE_ID", DEFAULT_VOICE_ID)
        self.model_id = model_id
        self.voice_settings = {
            "stability": stability,
            "similarity_boost": similarity,
            "style": style,
            "use_speaker_boost": speaker_boost,
        }
        # Only include speed when changed — some models reject the field at 1.0.
        if speed and abs(speed - 1.0) > 1e-6:
            self.voice_settings["speed"] = speed

    def generate_with_timestamps(self, text: str, out_path: str) -> dict | None:
        """Generate the MP3 and return ElevenLabs character-level alignment.

        Writes the audio to ``out_path`` and returns the ``alignment`` dict
        (characters + start/end seconds) used to build word-timed captions.
        Falls back to a plain generation (returns None) if the endpoint fails.
        """
        import base64

        url = f"{API}/{self.voice_id}/with-timestamps"
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {
            "text": text,
            "model_id": self.model_id,
            "voice_settings": self.voice_settings,
        }
        log.info("Generating voiceover + timestamps (%d chars)", len(text))
        resp = requests.post(url, headers=headers, json=payload, timeout=600)
        if not resp.ok:
            log.error("ElevenLabs timestamps error (%s): %s — falling back to plain TTS",
                      resp.status_code, resp.text[:300])
            self.generate(text, out_path)
            return None
        data = resp.json()
        with open(out_path, "wb") as fh:
            fh.write(base64.b64decode(data["audio_base64"]))
        return data.get("alignment") or data.get("normalized_alignment")

    def generate(self, text: str, out_path: str) -> str:
        """Generate an MP3 voiceover and write it to ``out_path``."""
        url = f"{API}/{self.voice_id}"
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        payload = {
            "text": text,
            "model_id": self.model_id,
            "voice_settings": self.voice_settings,
        }
        log.info("Generating voiceover (%d chars) with voice %s", len(text), self.voice_id)
        resp = requests.post(url, headers=headers, json=payload, timeout=600)
        if not resp.ok:
            log.error("ElevenLabs error (%s): %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
        with open(out_path, "wb") as fh:
            fh.write(resp.content)
        log.info("Voiceover saved: %s (%d bytes)", out_path, len(resp.content))
        return out_path
