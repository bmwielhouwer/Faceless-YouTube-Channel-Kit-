#!/usr/bin/env python3
"""Faceless YouTube Kit automation — continuous runner + on-demand webhook.

Usage:
  python run.py            # poll Airtable forever (default) + serve /trigger
  python run.py --once     # process the current queue once and exit
  python run.py --check    # validate config + connectivity, then exit

On-demand trigger (no need to wait for the next poll):
  POST http://<host>/trigger
       header  X-Trigger-Token: <TRIGGER_TOKEN>
       body    {"record_id": "rec..."}   or   {"title": "My Video Title"}
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from config import load_config
from faceless_kit.pipeline import Pipeline


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def _start_server(pipeline: Pipeline, cfg) -> None:
    """Bind GET / (health) and POST /trigger (on-demand render) on $PORT.

    The health endpoint keeps the host (Railway) from stopping the container.
    /trigger fires a render immediately in a background thread (serialized by the
    pipeline lock so it never overlaps a poll render).
    """
    port = int(os.getenv("PORT", "8080"))
    log = logging.getLogger("faceless_kit")

    class _Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _check_media_token(self, qs) -> bool:
            provided = qs.get("token", [None])[0] or self.headers.get("X-Media-Token")
            return not cfg.media_token or provided == cfg.media_token

        def _media_id(self, path) -> str:
            fid = path[len("/media/"):].split("/")[0]
            return fid.rsplit(".", 1)[0] if "." in fid else fid

        def do_GET(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path.startswith("/media/"):
                self._serve_media(parsed)
                return
            self._send(200, {"status": "alive", "service": "faceless-youtube-kit"})

        def do_HEAD(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if not parsed.path.startswith("/media/"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            if not self._check_media_token(qs):
                self.send_response(401)
                self.end_headers()
                return
            from faceless_kit import gdrive
            fid = self._media_id(parsed.path)
            size = gdrive.file_size(fid, cfg)
            # A real render is never 0 bytes; treat "no size" as gone so a probing
            # client (e.g. Zernio at schedule time) fails fast instead of getting a
            # 200 here and a 502 on the follow-up GET.
            if not size:
                log.error("Media HEAD: Drive file %s unavailable (deleted/0 bytes)", fid)
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()

        def _serve_media(self, parsed):
            """Stream a rendered video straight from Drive (authenticated) so Zernio
            fetches the raw bytes instantly — no Drive preview-processing wait."""
            qs = urllib.parse.parse_qs(parsed.query)
            if not self._check_media_token(qs):
                self._send(401, {"error": "unauthorized"})
                return
            from faceless_kit import gdrive
            fid = self._media_id(parsed.path)
            upstream = gdrive.open_stream(fid, cfg, self.headers.get("Range"))
            if upstream is None:
                log.error("Media proxy: no response from Drive for %s", fid)
                self._send(502, {"error": "media upstream unavailable"})
                return
            if upstream.status_code in (404, 403):
                # File was deleted/cleaned up or lost access — report it truthfully
                # so the failure is unambiguous (this is the Facebook 'unable to
                # fetch' cause when a post outlives its Drive file).
                log.error("Media proxy: Drive file %s gone (HTTP %s) — deleted/cleaned up",
                          fid, upstream.status_code)
                upstream.close()
                self._send(404, {"error": "media file not found"})
                return
            if upstream.status_code not in (200, 206):
                log.error("Media proxy fetch failed (%s) for %s", upstream.status_code, fid)
                upstream.close()
                self._send(502, {"error": "media not available"})
                return
            try:
                self.send_response(upstream.status_code)
                self.send_header("Content-Type", "video/mp4")
                for h in ("Content-Length", "Content-Range"):
                    v = upstream.headers.get(h)
                    if v:
                        self.send_header(h, v)
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                for chunk in upstream.iter_content(chunk_size=262144):
                    if chunk:
                        self.wfile.write(chunk)
            except Exception:  # noqa: BLE001 — client hangup mid-stream is normal
                pass
            finally:
                upstream.close()

        def do_POST(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.rstrip("/")
            if path not in ("/trigger", "/generate-weekly", "/publish", ""):
                self._send(404, {"error": "not found"})
                return
            qs = urllib.parse.parse_qs(parsed.query)
            length = int(self.headers.get("Content-Length") or 0)
            body = {}
            if length:
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    body = {}

            provided = (self.headers.get("X-Trigger-Token")
                        or qs.get("token", [None])[0] or body.get("token"))
            if cfg.trigger_token and provided != cfg.trigger_token:
                self._send(401, {"error": "unauthorized"})
                return

            # Weekly auto-scripting: write scripts for the top trending topics.
            if path == "/generate-weekly":
                count = body.get("count") or qs.get("count", [None])[0]
                count = int(count) if count else None

                def _weekly():
                    from faceless_kit import weekly
                    weekly.generate_weekly(cfg, pipeline.airtable, count)
                    # Render ONE immediately; the rest stay Queued and drain
                    # one-per-day via the daily queue (Lane 3).
                    pipeline.render_next_queued()

                threading.Thread(target=_weekly, daemon=True).start()
                log.info("Weekly script generation started")
                self._send(202, {"status": "weekly generation started"})
                return

            # Stage 2 — publish all 'Video Ready' records now (manual/testing).
            if path == "/publish":
                threading.Thread(target=pipeline.publish_ready, daemon=True).start()
                log.info("Manual publish run started")
                self._send(202, {"status": "publish started"})
                return

            record_id = qs.get("record_id", [None])[0] or body.get("record_id")
            title = qs.get("title", [None])[0] or body.get("title")
            try:
                if record_id:
                    record = pipeline.airtable.get_record(record_id)
                elif title:
                    matches = pipeline.airtable.find_by_field(cfg.f_title, title)
                    record = matches[0] if matches else None
                else:
                    self._send(400, {"error": "provide record_id or title"})
                    return
            except Exception as exc:  # noqa: BLE001
                self._send(502, {"error": f"airtable lookup failed: {exc}"})
                return

            if not record:
                self._send(404, {"error": "record not found"})
                return

            threading.Thread(target=pipeline.process, args=(record,), daemon=True).start()
            log.info("On-demand trigger accepted for %s", record["id"])
            self._send(202, {"status": "accepted", "record_id": record["id"]})

        def log_message(self, *_args):
            pass

    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    except OSError as exc:
        log.warning("HTTP server not started: %s", exc)
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("HTTP server (health + /trigger) listening on :%s", port)


def _start_weekly_scheduler(pipeline, cfg) -> None:
    """Daemon thread: every Monday (weekly_day) at/after weekly_hour in weekly_tz,
    research trending topics and script the week's videos. Restart-safe via an
    Airtable recent-record check, and independent of the poll interval.
    """
    if not cfg.weekly_enabled:
        return
    log = logging.getLogger("faceless_kit")

    def _loop():
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(cfg.weekly_tz)
        except Exception as exc:  # noqa: BLE001 — bad tz / missing tzdata
            log.warning("Weekly scheduler disabled (timezone error: %s)", exc)
            return
        last_week = None
        while True:
            try:
                now = datetime.now(tz)
                iso = now.isocalendar()
                wk = (iso[0], iso[1])
                if (now.weekday() == cfg.weekly_day and now.hour >= cfg.weekly_hour
                        and wk != last_week):
                    last_week = wk
                    recent = 0
                    try:
                        recent = pipeline.airtable.count_recent(18)
                    except Exception:  # noqa: BLE001
                        log.exception("Weekly dedup check failed")
                    if recent >= cfg.weekly_count:
                        log.info("Weekly: %d records created recently — already ran; skipping.", recent)
                    else:
                        log.info("Weekly scheduled run starting (%s)", now.isoformat())
                        from faceless_kit import weekly
                        weekly.generate_weekly(cfg, pipeline.airtable)
                        # Render ONE immediately; the remaining scripts stay
                        # Queued and the daily queue (Lane 3) drains them one per
                        # day so the week's videos don't all render at once.
                        pipeline.render_next_queued()
            except Exception:  # noqa: BLE001 — keep the scheduler alive
                log.exception("Weekly scheduler error")
            time.sleep(300)

    threading.Thread(target=_loop, daemon=True).start()
    log.info("Weekly scheduler armed: %s %02d:00 %s",
             ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][cfg.weekly_day % 7],
             cfg.weekly_hour, cfg.weekly_tz)


def main() -> int:
    parser = argparse.ArgumentParser(description="Faceless YouTube Kit video automation")
    parser.add_argument("--once", action="store_true", help="process the queue once and exit")
    parser.add_argument("--check", action="store_true", help="validate config and exit")
    args = parser.parse_args()

    _setup_logging()
    log = logging.getLogger("faceless_kit")

    try:
        cfg = load_config()
    except RuntimeError as exc:
        log.error(str(exc))
        return 2

    pipeline = Pipeline(cfg)

    if args.check:
        log.info("Config OK. Checking Airtable connectivity...")
        pending = pipeline.find_pending()
        log.info("Airtable reachable. %d record(s) currently '%s'.", len(pending), cfg.status_trigger)
        return 0

    if args.once:
        pipeline.run_once()
        return 0

    _start_server(pipeline, cfg)            # Lane 2 — on-demand webhook (+ health)
    _start_weekly_scheduler(pipeline, cfg)  # Mon 7am — research + script the week

    # Resume any render orphaned by a previous crash (record stuck in Processing),
    # immediately — independent of the daily schedule.
    _resume_orphans(pipeline, cfg)

    # Lane 1 — fixed daily cron: fire run_once() at DAILY_POLL_HOUR (wall clock,
    # in WEEKLY_TZ) once per day. Anchored to the clock, NOT a rolling sleep, so
    # it always fires at the set hour regardless of when the worker last ran or
    # restarted. Independent of the webhook.
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(cfg.weekly_tz)
    except Exception as exc:  # noqa: BLE001
        from zoneinfo import ZoneInfo
        log.warning("Bad WEEKLY_TZ (%s); using UTC for the daily poll", exc)
        tz = ZoneInfo("UTC")

    from datetime import datetime
    log.info("Lane 1 armed: daily poll at %02d:00 %s (fixed wall-clock schedule).",
             cfg.daily_poll_hour, cfg.weekly_tz)
    if cfg.publish_enabled:
        log.info("Stage 2 armed: publish ONE video/day (oldest Video Ready) at "
                 "%02d:00 %s, with a %ds catch-up sweep that runs only if nothing "
                 "was published yet today.", cfg.publish_hour, cfg.weekly_tz,
                 cfg.publish_sweep_seconds)
    if cfg.status_queued:
        log.info("Lane 3 armed: render ONE queued video/day (oldest) at %02d:00 %s, "
                 "so a week's scripts render one-per-day instead of all at once.",
                 cfg.daily_poll_hour, cfg.weekly_tz)
    last_run_date = None
    last_pub_date = None     # day on which Stage 2 work is done (published or confirmed)
    last_8am_date = None     # day the fixed 08:00 attempt already fired
    last_queue_date = None   # day the staggered queue drain already attempted
    last_sweep = 0.0
    while True:
        try:
            now = datetime.now(tz)
            today = now.date()
            if now.hour == cfg.daily_poll_hour and last_run_date != today:
                last_run_date = today
                log.info("Lane 1 daily poll firing at %s", now.isoformat())
                found = pipeline.run_once()
                log.info("Lane 1 processed %d Scripted record(s).", found)
            # Lane 3 — staggered queue drain: render exactly ONE 'Queued' video per
            # day (the oldest) so a Monday batch of scripts renders across the week
            # rather than all at once. render_next_queued() enforces one-per-day
            # internally too, so the Monday generation render and this drain never
            # double up.
            if (cfg.status_queued and now.hour == cfg.daily_poll_hour
                    and last_queue_date != today):
                last_queue_date = today
                if pipeline.render_next_queued():
                    log.info("Lane 3: started today's queued render.")
            # Stage 2 — publish exactly ONE video per day (the oldest Video Ready
            # record). The 08:00 run does it; if that slot is missed (e.g. the
            # render ran long) the hourly sweep catches up — but ONLY while nothing
            # has been published today. Once a video goes out, we idle till tomorrow.
            if cfg.publish_enabled and last_pub_date != today:
                due_8am = now.hour == cfg.publish_hour and last_8am_date != today
                due_sweep = (cfg.publish_sweep_seconds > 0
                             and time.monotonic() - last_sweep >= cfg.publish_sweep_seconds)
                if due_8am or due_sweep:
                    if due_8am:
                        last_8am_date = today
                    if due_sweep:
                        last_sweep = time.monotonic()
                    via = "08:00 run" if due_8am else "catch-up sweep"
                    if pipeline.published_today():
                        # A video already went out today (this run, manual, or pre-restart).
                        last_pub_date = today
                        log.info("Stage 2 (%s): already published today — idle until tomorrow.", via)
                    elif pipeline.publish_oldest():
                        last_pub_date = today
                        log.info("Stage 2 (%s): published today's video.", via)
                    # else: nothing Video Ready yet — leave the day open so the next
                    # sweep retries once a render completes.
        except Exception:  # noqa: BLE001 — keep the daemon alive
            log.exception("Daily loop error")
        time.sleep(45)


def _resume_orphans(pipeline, cfg) -> None:
    log = logging.getLogger("faceless_kit")
    try:
        orphans = pipeline.airtable.find_by_status(cfg.f_status, cfg.status_processing)
    except Exception:  # noqa: BLE001
        log.exception("Orphan check failed")
        return
    if not orphans:
        return
    log.info("Resuming %d orphaned render(s) from a previous crash", len(orphans))

    def _job():
        for rec in orphans:
            try:
                pipeline.airtable.set_status(rec["id"], cfg.f_status, cfg.status_trigger)
                pipeline.process(rec)  # re-claims (Scripted) and renders
            except Exception:  # noqa: BLE001
                log.exception("Failed to resume %s", rec["id"])

    threading.Thread(target=_job, daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
