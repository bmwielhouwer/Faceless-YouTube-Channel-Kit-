"""End-to-end orchestration: Airtable script -> finished MP4 in cloud storage."""
from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import threading
from datetime import date, datetime

import requests

from . import audio_host, captions, ffmpeg_render, gdrive, notify, storage, thumbnail
from .airtable_client import AirtableClient
from .creatomate_client import CreatomateClient, build_modifications, build_source
from .elevenlabs_client import ElevenLabsClient

log = logging.getLogger("faceless_kit.pipeline")

# The Airtable "Zernio Posted" checkbox is the idempotency guard so a re-run of
# Stage 2 (the /publish endpoint or the morning cron) never creates duplicate
# TikTok/Facebook posts. Its field name is configurable (cfg.f_zernio_posted) so
# the guard works in any buyer's base.


def _today_str(cfg) -> str:
    """Today's date (YYYY-MM-DD) in the worker's configured timezone — used for the
    one-publish-per-day guard and the Upload Date stamp so the day boundary is
    consistent between writing and checking."""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(cfg.weekly_tz)).date().isoformat()
    except Exception:  # noqa: BLE001
        return datetime.utcnow().date().isoformat()


def _safe_filename(title: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 _-]", "", title).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned or "Video"


def _audio_duration(path: str) -> float | None:
    """Length of an MP3 in seconds, or None if it can't be read."""
    try:
        from mutagen.mp3 import MP3

        return float(MP3(path).info.length)
    except Exception as exc:  # noqa: BLE001 — duration is best-effort
        log.warning("Could not read voiceover duration: %s", exc)
        return None


def _download(url: str, dest: str) -> str:
    with requests.get(url, stream=True, timeout=600) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                fh.write(chunk)
    return dest


class Pipeline:
    def __init__(self, cfg):
        self.cfg = cfg
        # Serialize renders so the poll loop and the on-demand webhook never run
        # two ffmpeg encodes at once (which would risk OOM on the container).
        self._lock = threading.Lock()
        # Guards the one-queued-render-per-day claim so the Monday generation
        # render and the daily drain (and a same-day double poll) never double up.
        self._queue_lock = threading.Lock()
        self._last_queue_render_date: str | None = None
        self._render_note = ""
        self._broll_ids: list = []
        self.airtable = AirtableClient(cfg.airtable_token, cfg.airtable_base, cfg.airtable_table)
        self.eleven = ElevenLabsClient(
            cfg.eleven_key, cfg.eleven_voice, cfg.eleven_model,
            cfg.eleven_stability, cfg.eleven_similarity,
            cfg.eleven_style, cfg.eleven_speaker_boost, cfg.eleven_speed,
        )
        self.creatomate = CreatomateClient(
            cfg.creatomate_key, cfg.creatomate_template,
            cfg.creatomate_poll, cfg.creatomate_timeout,
        )

    # -- polling ---------------------------------------------------------------
    def find_pending(self) -> list[dict]:
        return self.airtable.find_by_status(self.cfg.f_status, self.cfg.status_trigger)

    def run_once(self) -> int:
        records = self.find_pending()
        if records:
            log.info("Found %d scripted record(s) to process", len(records))
        for record in records:
            self.process(record)
        return len(records)

    # -- staggered render queue ------------------------------------------------
    def render_next_queued(self) -> int:
        """Render at most ONE 'Queued' record per day (the oldest), so a week's
        worth of scripts generated on Monday render one-per-day across the week
        instead of all at once.

        Promotes the oldest Queued record to the trigger status (Scripted) and
        renders it to 'Video Ready'. The once-per-day claim is in-memory — the
        long-running worker holds it — so a same-day restart could render a second
        queued video, which is harmless because Stage 2 still publishes only one
        per day. Both the Monday generation render and the daily drain call this,
        and the internal claim keeps them from doubling up. Returns 1 if a render
        was started, else 0.
        """
        cfg = self.cfg
        if not cfg.status_queued:
            return 0
        today = _today_str(cfg)
        with self._queue_lock:
            if self._last_queue_render_date == today:
                return 0
            try:
                queued = self.airtable.find_by_status(cfg.f_status, cfg.status_queued)
            except Exception:  # noqa: BLE001
                log.exception("Queue: could not list Queued records")
                return 0
            if not queued:
                log.info("Queue: no Queued records to render today")
                return 0
            queued.sort(key=lambda r: r.get("createdTime", ""))  # oldest first
            rec = queued[0]
            # Claim the day only now that we have a record to render, so an empty
            # sweep never blocks a later same-day generation from rendering.
            self._last_queue_render_date = today
            try:
                self.airtable.set_status(rec["id"], cfg.f_status, cfg.status_trigger)
            except Exception:  # noqa: BLE001
                log.exception("Queue: could not promote %s; releasing the day", rec.get("id"))
                self._last_queue_render_date = None
                return 0
            log.info("Queue: %d queued; rendering the oldest one today (%s)",
                     len(queued), rec.get("id"))
        # Render off-thread (process serializes on its own lock) so the caller's
        # loop isn't blocked for the length of a render.
        threading.Thread(target=self.process, args=(rec,), daemon=True).start()
        return 1

    # -- render backends -------------------------------------------------------
    def _render_ffmpeg(self, workdir: str, filename: str, title: str, script: str) -> str:
        """Self-hosted render: voiceover + captions + motion graphics via ffmpeg."""
        cfg = self.cfg
        mp3 = os.path.join(workdir, "voiceover.mp3")
        alignment = self.eleven.generate_with_timestamps(script, mp3)
        audio_seconds = _audio_duration(mp3)
        if audio_seconds:
            log.info("Voiceover duration: %.1fs (%.1f min)", audio_seconds, audio_seconds / 60)

        # Content-aware B-roll, unique per video: exclude clips used in previous
        # videos (durable via Airtable) so every video looks visually distinct.
        self._broll_ids = []
        prior_used = set()
        if cfg.pexels_key:
            try:
                prior_used = self.airtable.recent_used_clips(cfg.f_used_clips)
                log.info("Excluding %d clips used in previous videos", len(prior_used))
            except Exception:  # noqa: BLE001
                log.exception("Could not read previously-used clips")
        broll_clips = self._content_aware_broll(workdir, script, alignment, audio_seconds, prior_used)

        out_mp4 = os.path.join(workdir, filename)
        ffmpeg_render.render_video(
            audio_path=mp3, audio_seconds=audio_seconds, script=script,
            alignment=alignment, out_path=out_mp4, accent=cfg.accent_color,
            music_url=cfg.music_url or None, music_volume=cfg.music_volume,
            watermark=(cfg.channel_name or "").upper(),
            logo_path=cfg.logo_path or None, intro_path=cfg.intro_path or None,
            broll_clips=broll_clips, scene_seconds=cfg.scene_seconds,
        )
        bits = [
            f"{len(broll_clips)} matched clips" if broll_clips else "gradient bg",
            "real music" if cfg.music_url else "synth music",
            "logo" if cfg.logo_path else "text watermark",
            "intro/outro" if cfg.intro_path else "no intro card",
        ]
        self._render_note = "ffmpeg: " + ", ".join(bits)
        return out_mp4

    def _content_aware_broll(self, workdir, script, alignment, audio_seconds, prior_used=None):
        """Return [(clip_path, duration), ...] of topic-matched Pexels clips.

        Walks the timeline picking a topic-relevant clip for each point, playing
        it once at its natural length. Excludes ``prior_used`` clip IDs (from past
        videos) and records this video's IDs in self._broll_ids.
        """
        cfg = self.cfg
        if not cfg.pexels_key:
            return []
        try:
            from . import broll, pexels

            total = float(audio_seconds or 180)
            words = (ffmpeg_render.words_from_alignment(alignment) if alignment
                     else ffmpeg_render._even_words(script, audio_seconds or 180))
            segments = broll.segment_script(words, cfg.scene_seconds)

            def topic_at(t: float) -> str:
                for text, s, e in segments:
                    if s <= t < e:
                        return broll.query_for_text(text)
                return broll.query_for_text(segments[-1][0]) if segments else "dark technology background"

            cap = max(5, cfg.clip_seconds)   # max seconds any single clip shows
            used = set(prior_used or set())  # exclude clips from previous videos too
            specs: list = []
            t, i, guard = 0.0, 0, 0
            while t < total - 0.5 and guard < 200:  # enough clips to cover 8-10 min
                guard += 1
                q = topic_at(t)
                result = (pexels.fetch_clip_for_query(cfg.pexels_key, q, used)
                          or pexels.fetch_clip_for_query(cfg.pexels_key, broll.random_fallback(), used))
                if not result:
                    break
                clip_id, url = result
                used.add(clip_id)
                self._broll_ids.append(clip_id)
                path = ffmpeg_render.download_one(url, os.path.join(workdir, f"clip_{i}.mp4"))
                i += 1
                if not path:
                    continue
                natural = ffmpeg_render.probe_duration(path) or cap
                # play it ONCE: never longer than its natural length (no loop)
                dur = min(natural, cap, total - t)
                if dur < 1.5:
                    dur = min(cap, total - t)
                specs.append((path, round(dur, 2)))
                t += dur
            if not specs:
                return []
            # If Pexels ran dry before covering the audio, extend the last clip.
            if t < total - 0.5:
                p, d = specs[-1]
                specs[-1] = (p, round(d + (total - t), 2))
            log.info("Content-aware B-roll: %d clips, %.0f/%.0fs covered (natural lengths)",
                     len(specs), t, total)
            return specs
        except Exception:  # noqa: BLE001 — B-roll is best-effort
            log.exception("Content-aware B-roll failed; using gradient background")
            return []

    def _render_creatomate(self, workdir: str, filename: str, title: str, script: str):
        """Legacy path: render via the Creatomate API (needs render credits)."""
        cfg = self.cfg
        mp3 = self.eleven.generate(script, os.path.join(workdir, "voiceover.mp3"))
        audio_seconds = _audio_duration(mp3)
        audio_url = audio_host.upload(mp3, cfg.audio_host)
        if cfg.render_mode == "source":
            payload = {"source": build_source(
                audio_url=audio_url, audio_seconds=audio_seconds, accent=cfg.accent_color,
                broll_urls=cfg.broll_urls, music_url=cfg.music_url,
                scene_seconds=cfg.scene_seconds, font=cfg.video_font,
                watermark=(cfg.channel_name or "").upper(),
            )}
        else:
            payload = {
                "template_id": cfg.creatomate_template, "output_format": "mp4",
                "modifications": build_modifications(
                    audio_url=audio_url, title=title, script=script,
                    audio_element=cfg.creatomate_audio_el,
                    subtitle_element=cfg.creatomate_subtitle_el,
                    title_element=cfg.creatomate_title_el,
                    audio_seconds=audio_seconds, extra=cfg.creatomate_extra,
                ),
            }
        mp4_url = self.creatomate.wait(self.creatomate.submit(payload))
        return _download(mp4_url, os.path.join(workdir, filename)), mp4_url

    def _make_and_upload_tiktok(self, local_mp4: str, base: str, workdir: str) -> None:
        """Cut 3 vertical 9:16 clips at 78s and save them to the TikTok and Reels
        Drive folders (YouTube Shorts get a separate 58s set elsewhere)."""
        cfg = self.cfg
        targets = [
            ("TikTok", cfg.gdrive_folder_tiktok),
            ("Reels", cfg.gdrive_folder_reels),
        ]
        try:
            duration = ffmpeg_render.probe_duration(local_mp4) or 0
            if duration < 30:
                return
            clips = ffmpeg_render.make_tiktok_clips(
                local_mp4, duration, workdir, base, cfg.tiktok_seconds, label="Clip")
            for path, name in clips:
                for label, folder in targets:
                    if not folder:
                        continue
                    try:
                        storage.save(path, f"{name}.mp4", cfg, folder_id=folder)
                    except Exception:  # noqa: BLE001
                        log.exception("Failed to upload clip %s to %s", name, label)
                try:
                    os.remove(path)
                except OSError:
                    pass
        except Exception:  # noqa: BLE001 — clips are a bonus; never fail the main video
            log.exception("Short-form clip generation failed")

    def _make_shorts_to_drive(self, local_mp4: str, base: str, workdir: str) -> list[str]:
        """Cut 3 vertical 58s clips (under the 60s Shorts limit), verify each, and
        save to the Shorts Drive folder. Returns their Drive file ids (Stage 2
        uploads them to YouTube). No posting here."""
        cfg = self.cfg
        ids: list[str] = []
        try:
            duration = ffmpeg_render.probe_duration(local_mp4) or 0
            if duration < 30:
                return ids
            clips = ffmpeg_render.make_tiktok_clips(
                local_mp4, duration, workdir, base, cfg.youtube_shorts_seconds,
                label="Short")
            for path, name in clips:
                if not ffmpeg_render.verify_playable(path):
                    log.warning("Short %s failed verification — skipping", name)
                    continue
                try:
                    _, fid = gdrive.upload2(path, f"{name}.mp4", cfg,
                                            folder_id=cfg.gdrive_folder_shorts)
                    if fid:
                        ids.append(fid)
                except Exception:  # noqa: BLE001
                    log.exception("Failed to upload Short %s to Drive", name)
                try:
                    os.remove(path)
                except OSError:
                    pass
        except Exception:  # noqa: BLE001 — shorts are a bonus; never fail the video
            log.exception("Shorts generation failed")
        return ids

    def _make_vertical_to_drive(self, local_mp4: str, base: str, workdir: str) -> str | None:
        """Render the 9:16 vertical (full) used for the TikTok post, verify it, and
        save it to the TikTok Drive folder. Returns its Drive file id."""
        cfg = self.cfg
        try:
            vpath = os.path.join(workdir, f"{base}_vertical.mp4")
            if not ffmpeg_render.make_vertical_centercrop(local_mp4, vpath):
                return None
            if not ffmpeg_render.verify_playable(vpath):
                log.warning("Vertical crop failed verification")
                return None
            _, fid = gdrive.upload2(vpath, f"{base}_vertical.mp4", cfg,
                                    folder_id=cfg.gdrive_folder_tiktok)
            try:
                os.remove(vpath)
            except OSError:
                pass
            return fid
        except Exception:  # noqa: BLE001
            log.exception("Vertical render/upload failed")
            return None

    # -- per-record ------------------------------------------------------------
    def process(self, record: dict) -> None:
        """Render one record. Serialized so poll + webhook never overlap."""
        with self._lock:
            self._do_process(record)

    def _fetch_thumbnail(self, media: dict, title: str, workdir: str) -> str | None:
        """Local thumbnail JPG for the YouTube upload: download the one saved to
        Drive at render time (media['thumbnail']); if absent (older records),
        regenerate it locally. Returns a path or None."""
        cand = os.path.join(workdir, "thumb.jpg")
        tid = media.get("thumbnail")
        if tid:
            try:
                if gdrive.download(tid, cand, self.cfg):
                    return cand
            except Exception:  # noqa: BLE001
                log.exception("Could not download thumbnail %s from Drive", tid)
        try:
            if thumbnail.generate(title, cand, self.cfg.logo_path or None):
                return cand
        except Exception:  # noqa: BLE001
            log.exception("Thumbnail regeneration failed")
        return None

    # -- Stage 2: publishing ---------------------------------------------------
    def publish_ready(self) -> int:
        """Publish every 'Video Ready' record: upload to YouTube, schedule the
        TikTok/Facebook posts via Zernio (from the rendered files), mark Posted,
        and email the live links. Returns the count processed."""
        cfg = self.cfg
        try:
            ready = self.airtable.find_by_status(cfg.f_status, cfg.status_done)
        except Exception:  # noqa: BLE001
            log.exception("Publish: could not list Video Ready records")
            return 0
        if not ready:
            log.info("Publish: no Video Ready records to publish")
            return 0
        log.info("Publish: %d Video Ready record(s) to publish", len(ready))
        for rec in ready:
            with self._lock:
                try:
                    self._publish_record(rec)
                except Exception:  # noqa: BLE001 — isolate per record
                    log.exception("Publish failed for %s", rec.get("id"))
        return len(ready)

    def publish_oldest(self) -> int:
        """Publish only the single OLDEST 'Video Ready' record (one per call) so the
        channel gets at most one new video per scheduled run. Returns 1 if a record
        was published, else 0."""
        cfg = self.cfg
        try:
            ready = self.airtable.find_by_status(cfg.f_status, cfg.status_done)
        except Exception:  # noqa: BLE001
            log.exception("Publish: could not list Video Ready records")
            return 0
        if not ready:
            log.info("Publish: no Video Ready records to publish")
            return 0
        ready.sort(key=lambda r: r.get("createdTime", ""))  # oldest first
        rec = ready[0]
        log.info("Publish: %d Video Ready; publishing the oldest (%s)", len(ready), rec.get("id"))
        with self._lock:
            try:
                return 1 if self._publish_record(rec) else 0
            except Exception:  # noqa: BLE001
                log.exception("Publish failed for %s", rec.get("id"))
                return 0

    def published_today(self) -> bool:
        """True if any record is already marked Posted carrying today's Upload Date
        (worker tz). Reads Airtable so the one-per-day guard survives restarts."""
        cfg = self.cfg
        if not cfg.f_upload_date:
            return False
        today = _today_str(cfg)
        try:
            posted = self.airtable.find_by_status(cfg.f_status, cfg.status_published)
        except Exception:  # noqa: BLE001
            log.exception("published_today check failed")
            return False
        for r in posted:
            d = (r.get("fields", {}) or {}).get(cfg.f_upload_date)
            if d and str(d).startswith(today):
                return True
        return False

    def _publish_record(self, record: dict) -> bool:
        cfg = self.cfg
        rec_id = record["id"]
        # Claim: re-fetch and only publish if still Video Ready (idempotent).
        try:
            fresh = self.airtable.get_record(rec_id)
            if fresh:
                record = fresh
        except Exception:  # noqa: BLE001
            log.exception("Publish: could not re-fetch %s", rec_id)
        fields = record.get("fields", {})
        cur = fields.get(cfg.f_status)
        if isinstance(cur, dict):
            cur = cur.get("name")
        if cur != cfg.status_done:
            log.info("Publish: skipping %s — status=%r (not Video Ready)", rec_id, cur)
            return False

        title = (fields.get(cfg.f_title) or "Untitled").strip()
        script = (fields.get(cfg.f_script) or "").strip()
        content_type = fields.get(cfg.f_content_type)
        if isinstance(content_type, dict):
            content_type = content_type.get("name")
        is_youtube = (content_type or "YouTube").strip().lower() == "youtube"

        media = {}
        raw = fields.get(cfg.f_media_ids)
        if raw:
            try:
                media = json.loads(raw)
            except Exception:  # noqa: BLE001
                log.warning("Publish: bad Media IDs JSON on %s", rec_id)
        main_id = media.get("main") or gdrive.extract_file_id(fields.get(cfg.f_drive_link))
        vertical_id = media.get("tiktok_vertical")
        shorts_ids = media.get("shorts") or []

        log.info("=== Publishing: %s ===", title)
        workdir = tempfile.mkdtemp(prefix="vvpub_")
        try:
            copy = captions.draft(cfg, title, script)
            youtube_url = fields.get(cfg.f_youtube_link)

            # YouTube main upload — download the verified file from Drive first.
            # Idempotent: skip if this record already has a YouTube link so a
            # re-publish never creates a duplicate long-form upload.
            if (cfg.youtube_upload and cfg.platform_enabled("youtube")
                    and is_youtube and main_id and not youtube_url):
                try:
                    from . import youtube as yt
                    mp = os.path.join(workdir, "main.mp4")
                    if gdrive.download(main_id, mp, cfg):
                        tags = [t.strip() for t in copy.get("tags", "").split(",") if t.strip()]
                        thumb_local = self._fetch_thumbnail(media, title, workdir)
                        youtube_url = yt.upload(mp, title, copy["youtube"], tags, cfg,
                                                thumbnail_path=thumb_local)
                        os.remove(mp)
                except Exception:  # noqa: BLE001
                    log.exception("YouTube main upload failed")
            elif youtube_url and is_youtube:
                log.info("YouTube main upload skipped (idempotent) — already at %s", youtube_url)

            # YouTube Shorts — idempotent per clip: a Drive-id -> URL map persisted
            # in the Media IDs JSON means a re-publish skips Shorts already uploaded
            # (and still retries any that previously failed).
            shorts_done = media.get("shorts_youtube")
            if not isinstance(shorts_done, dict):
                shorts_done = {}
            shorts_urls: list[str] = []
            if (cfg.youtube_upload and cfg.youtube_shorts_upload
                    and cfg.platform_enabled("youtube") and is_youtube):
                for idx, sid in enumerate(shorts_ids, 1):
                    if sid in shorts_done:
                        log.info("YouTube Short %d skipped (idempotent) — already uploaded", idx)
                        prev = shorts_done.get(sid)
                        if prev and str(prev).startswith("http"):
                            shorts_urls.append(prev)
                        continue
                    try:
                        from . import youtube as yt
                        sp = os.path.join(workdir, f"short_{idx}.mp4")
                        if gdrive.download(sid, sp, cfg):
                            st = f"{title} (Part {idx}) #Shorts"[:100]
                            u = yt.upload(sp, st, f"{title}\n\n#Shorts", [], cfg)
                            if u:
                                shorts_urls.append(u)
                                shorts_done[sid] = u
                            os.remove(sp)
                    except Exception:  # noqa: BLE001
                        log.exception("YouTube Short %d upload failed", idx)
            media["shorts_youtube"] = shorts_done

            # Zernio: schedule TikTok + Facebook, both from the 9:16 vertical so the
            # Facebook post is a true vertical Reel (not the 16:9 long-form), via
            # proxy URLs.
            # Idempotent: if this record was already posted to Zernio, skip the whole
            # block so a re-run of Stage 2 never creates duplicate TikTok/Facebook posts.
            social: dict = {}
            zernio_already = bool(fields.get(cfg.f_zernio_posted))
            if cfg.zernio_api_key and is_youtube and zernio_already:
                log.info("Zernio publish skipped (idempotent) — already posted")
            elif cfg.zernio_api_key and is_youtube:
                from . import zernio as zpost
                yt_link = youtube_url or cfg.channel_url or "the channel"
                bullets = captions.social_bullets(cfg, title, script)
                caption = captions.build_social_caption(cfg, title, yt_link, bullets)
                # TikTok gets a short, curiosity-driven, non-promotional caption
                # (no affiliate link, no dollar/income claims); Facebook keeps the
                # full brand caption above.
                tt_caption = captions.tiktok_caption(cfg, title, script)
                schedule = self.airtable.posting_schedule(cfg.posting_schedule_table)
                social = zpost.schedule_from_ids(cfg, caption, schedule,
                                                 tiktok_file_id=vertical_id,
                                                 facebook_file_id=vertical_id,
                                                 tiktok_caption=tt_caption)

            # Mark Posted + persist publishing metadata. Stamp the Upload Date so the
            # one-publish-per-day guard knows a video went out today.
            update = {cfg.f_status: cfg.status_published, cfg.f_facebook: copy["facebook"]}
            if cfg.f_upload_date:
                update[cfg.f_upload_date] = _today_str(cfg)
            if cfg.f_youtube_desc:
                update[cfg.f_youtube_desc] = copy["youtube"]
            if cfg.f_tags:
                update[cfg.f_tags] = copy.get("tags", "")
            if cfg.f_youtube_link and youtube_url:
                update[cfg.f_youtube_link] = youtube_url
            # Persist the uploaded-Shorts map (and any newly-set main id) so a
            # re-publish is idempotent and won't duplicate YouTube uploads.
            if cfg.f_media_ids:
                update[cfg.f_media_ids] = json.dumps(media)
            # Stamp the Zernio idempotency guard ONLY when both the TikTok and
            # Facebook posts succeeded, so a partial failure can be retried next run.
            if social.get("tiktok_url") and social.get("facebook_url"):
                update[cfg.f_zernio_posted] = True
            self.airtable.update_fields(rec_id, update)

            notify.send_video_ready(
                cfg, title, f"{_safe_filename(title)}.mp4", fields.get(cfg.f_drive_link),
                copy=copy, youtube_url=youtube_url, shorts_urls=shorts_urls,
                tiktok_url=social.get("tiktok_url"), tiktok_time=social.get("tiktok_time"),
                facebook_url=social.get("facebook_url"), facebook_time=social.get("facebook_time"))
            log.info("=== PUBLISHED (Posted): %s ===", title)
            return True
        finally:
            _cleanup(workdir)

    def _do_process(self, record: dict) -> None:
        cfg = self.cfg
        rec_id = record["id"]

        # Atomic claim (inside the lock): re-fetch the record and only render if it
        # is still 'Scripted'. This makes the daily 7am poll (Lane 1) and the
        # on-demand webhook (Lane 2) safe to both target the same record — whoever
        # claims it first renders it once; the other sees a non-Scripted status and
        # skips, so there's never a double render.
        try:
            fresh = self.airtable.get_record(rec_id)
            if fresh:
                record = fresh
        except Exception:  # noqa: BLE001
            log.exception("Could not re-fetch %s; using provided data", rec_id)
        fields = record.get("fields", {})
        cur = fields.get(cfg.f_status)
        if isinstance(cur, dict):
            cur = cur.get("name")
        if cur != cfg.status_trigger:
            log.info("Skipping %s — status=%r (already claimed/rendered by the other lane)",
                     rec_id, cur)
            return

        title = (fields.get(cfg.f_title) or "Untitled").strip()
        script = (fields.get(cfg.f_script) or "").strip()

        if not script:
            log.warning("Record %s (%s) has no script — marking error", rec_id, title)
            self.airtable.set_status(rec_id, cfg.f_status, cfg.status_error)
            return

        log.info("=== Processing: %s ===", title)
        # Lock immediately so the next poll won't pick it up again.
        self.airtable.set_status(rec_id, cfg.f_status, cfg.status_processing)

        workdir = tempfile.mkdtemp(prefix="vv_")
        try:
            filename = f"{_safe_filename(title)}_{date.today():%Y-%m-%d}.mp4"
            mp4_url = None  # backup download link (Creatomate path only)

            content_type = fields.get(cfg.f_content_type)
            if isinstance(content_type, dict):
                content_type = content_type.get("name")
            folder_id = cfg.folder_for(content_type)

            self._render_note = ""
            location = None
            main_file_id = None

            # Resume path: if this record was orphaned AFTER the render (its Drive
            # link is already set), don't burn CPU re-rendering — pull the finished
            # MP4 back from Drive and resume the posting tail.
            existing_link = fields.get(cfg.f_drive_link)
            existing_id = gdrive.extract_file_id(existing_link) if existing_link else None
            local_mp4 = None
            if existing_id and (cfg.storage_provider or "gdrive").lower() == "gdrive":
                cand = os.path.join(workdir, filename)
                if gdrive.download(existing_id, cand, cfg):
                    local_mp4 = cand
                    location = existing_link
                    main_file_id = existing_id
                    log.info("Resuming posting from existing Drive render (no re-render): %s", title)

            if not local_mp4:
                if cfg.render_backend == "ffmpeg":
                    local_mp4 = self._render_ffmpeg(workdir, filename, title, script)
                else:
                    local_mp4, mp4_url = self._render_creatomate(workdir, filename, title, script)

            # 5. Save the main video to the Drive subfolder for its Content Type
            if not location:
                if (cfg.storage_provider or "gdrive").lower() == "gdrive":
                    location, main_file_id = gdrive.upload2(local_mp4, filename, cfg, folder_id=folder_id)
                else:
                    location = storage.save(local_mp4, filename, cfg, folder_id=folder_id)
                # Persist the Drive link immediately so a crash in the (heavy)
                # posting tail can resume from here instead of re-rendering.
                if cfg.f_drive_link and location:
                    try:
                        self.airtable.update_fields(rec_id, {cfg.f_drive_link: location})
                    except Exception:  # noqa: BLE001
                        log.exception("Could not persist Drive link early")

            # 5b. Verify the main render is a complete, playable file before we
            #     mark it ready. Catches truncated/half-written renders.
            if not ffmpeg_render.verify_playable(local_mp4):
                raise RuntimeError("Main render failed verification (incomplete/corrupt file)")

            is_youtube = (content_type or "YouTube").strip().lower() == "youtube"
            media_ids: dict = {"main": main_file_id, "tiktok_vertical": None, "shorts": []}

            # 5c. Archival 78s TikTok + Reels clips (Drive only).
            if cfg.tiktok_enabled and cfg.render_backend == "ffmpeg" and is_youtube:
                self._make_and_upload_tiktok(local_mp4, _safe_filename(title), workdir)

            # 5d. Build the assets Stage 2 will post — 9:16 vertical (TikTok) and the
            #     58s Shorts set — verify them, and record their Drive ids.
            if cfg.render_backend == "ffmpeg" and is_youtube:
                media_ids["tiktok_vertical"] = self._make_vertical_to_drive(
                    local_mp4, _safe_filename(title), workdir)
                media_ids["shorts"] = self._make_shorts_to_drive(
                    local_mp4, _safe_filename(title), workdir)

            # 5e. Thumbnail -> Drive (record its id so Stage 2 can apply it to the
            #     YouTube video via thumbnails.set) + email attachment.
            thumb_path = None
            try:
                tp = os.path.join(workdir, f"{_safe_filename(title)}_thumbnail.jpg")
                if thumbnail.generate(title, tp, cfg.logo_path or None):
                    thumb_path = tp
                    tname = f"{_safe_filename(title)}_thumbnail.jpg"
                    if (cfg.storage_provider or "gdrive").lower() == "gdrive":
                        _, tid = gdrive.upload2(tp, tname, cfg, folder_id=folder_id)
                        if tid:
                            media_ids["thumbnail"] = tid
                    else:
                        storage.save(tp, tname, cfg, folder_id=folder_id)
            except Exception:  # noqa: BLE001 — thumbnail is a bonus
                log.exception("Thumbnail generation/upload failed")

            # 6. Stage 1 complete: mark Video Ready and store the asset ids. NO
            #    posting here — the Stage 2 publisher posts from these verified
            #    files at the scheduled morning hour.
            update = {cfg.f_status: cfg.status_done}
            if cfg.f_drive_link and location:
                update[cfg.f_drive_link] = location
            if cfg.f_media_ids:
                update[cfg.f_media_ids] = json.dumps(media_ids)
            if cfg.f_used_clips and getattr(self, "_broll_ids", None):
                update[cfg.f_used_clips] = ",".join(str(x) for x in self._broll_ids)
            if cfg.f_notes and self._render_note:
                update[cfg.f_notes] = self._render_note
            self.airtable.update_fields(rec_id, update)

            # 7. Notify that the render is done and queued for morning publishing.
            notify.send_render_ready(cfg, title, filename, location, thumbnail_path=thumb_path)
            log.info("=== RENDERED & VERIFIED (Video Ready): %s -> %s ===", title, location)
        except Exception as exc:  # noqa: BLE001 — isolate failures per record
            log.exception("Pipeline failed for %s", title)
            # Record the reason in Airtable so failures are diagnosable without
            # shell/log access to the host.
            reason = f"[{datetime.now():%Y-%m-%d %H:%M} UTC] {type(exc).__name__}: {exc}"
            try:
                update = {cfg.f_status: cfg.status_error}
                if cfg.f_notes:
                    update[cfg.f_notes] = reason[:900]
                self.airtable.update_fields(rec_id, update)
            except Exception:  # noqa: BLE001
                log.error("Could not write error status/notes on %s", rec_id)
            notify.send_error(cfg, title, str(exc))
        finally:
            _cleanup(workdir)


def _cleanup(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)
