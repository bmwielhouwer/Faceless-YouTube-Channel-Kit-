"""Self-hosted video renderer using ffmpeg — no external render service.

Composes a finished 1080p MP4 entirely on the worker:
  * animated dark + electric-blue gradient background
  * the ElevenLabs voiceover, full length
  * burned-in word-timed captions (white bold, bottom-center)
  * a channel-name watermark, top-left (from CHANNEL_NAME)
  * optional ambient music mixed under the voiceover

This removes any dependency on a paid render API (and its credits/limits).
Requires a full ffmpeg with libfreetype (drawtext) + libass (subtitles); the
Docker image installs it via apt.
"""
from __future__ import annotations

import logging
import math
import os
import re
import shutil
import subprocess

log = logging.getLogger("faceless_kit.ffmpeg")

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]


def _ffmpeg_bin() -> str:
    if os.getenv("FFMPEG_BIN"):
        return os.getenv("FFMPEG_BIN")
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return "ffmpeg"


def _ffprobe_bin() -> str | None:
    return os.getenv("FFPROBE_BIN") or shutil.which("ffprobe")


def verify_playable(path: str) -> bool:
    """Confirm a file is a complete, decodable video (a valid video stream + a
    real duration). Uses ffprobe when available, else parses ``ffmpeg -i``.

    Catches truncated/half-written uploads before they're marked Video Ready.
    """
    if not (os.path.exists(path) and os.path.getsize(path) > 1024):
        log.warning("verify_playable: missing/tiny file %s", path)
        return False
    probe = _ffprobe_bin()
    if probe:
        try:
            r = subprocess.run(
                [probe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-show_entries", "format=duration",
                 "-of", "default=nw=1", path],
                capture_output=True, text=True, timeout=120)
            out = r.stdout or ""
            dur = 0.0
            for line in out.splitlines():
                if line.startswith("duration="):
                    try:
                        dur = float(line.split("=", 1)[1])
                    except ValueError:
                        pass
            if r.returncode == 0 and "codec_name=" in out and dur > 0:
                return True
            log.warning("verify_playable(ffprobe) failed for %s: rc=%s dur=%s",
                        path, r.returncode, dur)
            return False
        except Exception:  # noqa: BLE001
            log.exception("ffprobe verify error; falling back to ffmpeg -i")
    # Fallback: ffmpeg -i parses container/stream info to stderr.
    try:
        ff = _ffmpeg_bin()
        r = subprocess.run([ff, "-hide_banner", "-i", path],
                           capture_output=True, text=True, timeout=120)
        s = r.stderr or ""
        ok = ("Video:" in s) and ("Duration:" in s) and ("Duration: N/A" not in s)
        if not ok:
            log.warning("verify_playable(ffmpeg) failed for %s", path)
        return ok
    except Exception:  # noqa: BLE001
        log.exception("ffmpeg verify error for %s", path)
        return False


def _font_bold() -> str:
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return _FONT_CANDIDATES[0]


# Dark + electric-blue color grade applied to all B-roll so bright stock clips
# (e.g. money on a white background) match the dark premium brand.
_BROLL_GRADE = (
    "eq=brightness=-0.13:saturation=0.80:contrast=1.05,"
    "colorbalance=rs=-0.06:bs=0.13:bm=0.07:rh=-0.07:bh=0.13,"
    "vignette=PI/4.6"
)


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)")


def probe_duration(path: str) -> float | None:
    """Return a media file's duration in seconds (via ffmpeg), or None."""
    try:
        p = subprocess.run([_ffmpeg_bin(), "-i", path], capture_output=True, text=True, timeout=60)
        m = _DURATION_RE.search(p.stderr or "")
        if m:
            h, mn, s = m.groups()
            return int(h) * 3600 + int(mn) * 60 + float(s)
    except Exception:  # noqa: BLE001
        pass
    return None


def download_one(url: str, dest: str) -> str | None:
    """Download a single clip; return its path or None on failure."""
    import requests

    try:
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in r.iter_content(1 << 16):
                    fh.write(chunk)
        return dest if os.path.getsize(dest) > 10000 else None
    except Exception as exc:  # noqa: BLE001
        log.warning("B-roll download failed (%s): %s", url, exc)
        return None


def build_broll_background(specs, out_path):
    """Sequence ``specs`` = [(clip_path, duration_seconds), ...] into one bg video.

    Each clip plays for its segment's duration (looped if shorter), cover-cropped
    to 1080p, normalised to identical codec params (low memory), then concatenated
    so the footage tracks the narration. Returns the path, or None on failure.
    """
    if not specs:
        return None
    ff = _ffmpeg_bin()
    workdir = os.path.dirname(out_path) or "."
    segments = []
    try:
        seen_clips = set()
        for i, (clip, dur) in enumerate(specs):
            dur = max(2.0, round(float(dur), 2))
            seg = os.path.join(workdir, f"seg_{i}.mp4")
            cmd = [
                ff, "-y", "-hide_banner", "-loglevel", "error",
                "-stream_loop", "-1", "-t", str(dur), "-i", clip, "-an",
                "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,"
                       "crop=1920:1080,setsar=1,fps=30,format=yuv420p," + _BROLL_GRADE,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-threads", "2", "-x264-params", "ref=1:bframes=0", seg,
            ]
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if p.returncode != 0 or not os.path.exists(seg):
                log.warning("B-roll segment %d failed: %s", i, (p.stderr or "")[-200:])
                continue
            segments.append(seg)
            seen_clips.add(clip)
        # Free the source clips now that segments are encoded (keeps disk low).
        for c in seen_clips:
            try:
                os.remove(c)
            except OSError:
                pass
        if not segments:
            return None
        listfile = os.path.join(workdir, "broll_list.txt")
        with open(listfile, "w") as fh:
            for s in segments:
                fh.write(f"file '{s}'\n")
        concat = [ff, "-y", "-hide_banner", "-loglevel", "error",
                  "-f", "concat", "-safe", "0", "-i", listfile, "-c", "copy", out_path]
        p = subprocess.run(concat, capture_output=True, text=True, timeout=600)
        if p.returncode != 0 or not os.path.exists(out_path):
            log.warning("B-roll concat failed: %s", (p.stderr or "")[-200:])
            return None
        # Free the per-segment files now that they're concatenated.
        for s in segments:
            try:
                os.remove(s)
            except OSError:
                pass
        log.info("Built content-aware B-roll background from %d segments", len(segments))
        return out_path
    except Exception as exc:  # noqa: BLE001
        log.warning("B-roll background build error: %s", exc)
        return None



def _hex_to_ass_colour(hex_rgb: str) -> str:
    """#RRGGBB -> ASS &HBBGGRR&."""
    h = hex_rgb.lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}".upper()


# ---- caption timing ---------------------------------------------------------

def words_from_alignment(alignment: dict) -> list[tuple[str, float, float]]:
    """Convert ElevenLabs character alignment into (word, start, end) tuples."""
    if not alignment:
        return []
    chars = alignment.get("characters") or []
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    words: list[tuple[str, float, float]] = []
    cur, c_start, c_end = "", None, None
    for ch, s, e in zip(chars, starts, ends):
        if ch.isspace():
            if cur:
                words.append((cur, c_start, c_end))
                cur, c_start = "", None
        else:
            if not cur:
                c_start = s
            cur += ch
            c_end = e
    if cur:
        words.append((cur, c_start, c_end))
    return words


def _even_words(script: str, duration: float) -> list[tuple[str, float, float]]:
    """Fallback: spread the script's words evenly across the duration."""
    toks = script.split()
    if not toks or duration <= 0:
        return []
    step = duration / len(toks)
    return [(w, i * step, (i + 1) * step) for i, w in enumerate(toks)]


def _group_lines(words, max_words=7, max_gap=0.8):
    """Group words into caption lines (a few words each)."""
    lines = []
    cur, start, end = [], None, None
    for w, s, e in words:
        if not cur:
            start = s
        if cur and (len(cur) >= max_words or (s - end) > max_gap):
            lines.append((" ".join(cur), start, end))
            cur, start = [], s
        cur.append(w)
        end = e
    if cur:
        lines.append((" ".join(cur), start, end))
    return lines


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(words, out_path: str, accent: str = "#1FA2FF") -> str:
    lines = _group_lines(words)
    outline = _hex_to_ass_colour(accent)
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1920\n"
        "PlayResY: 1080\n"
        "WrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
        "Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cap,DejaVu Sans,58,&H00FFFFFF,{outline},&H64000000,-1,0,1,3,2,2,120,120,120,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    for text, start, end in lines:
        if start is None or end is None or end <= start:
            continue
        events.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Cap,,0,0,0,,{_ass_escape(text)}"
        )
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(header + "\n".join(events) + "\n")
    return out_path


# ---- rendering --------------------------------------------------------------

_ENC = [
    "-r", "30", "-ar", "44100", "-ac", "2",
    # H.264 + AAC + yuv420p + faststart => plays inline in Drive, fetches in
    # Zernio, and uploads cleanly to YouTube. faststart is added when the final
    # file is muxed (concat/finalize) so the moov atom sits at the front.
    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
    "-profile:v", "main", "-level", "4.0",
    "-maxrate", "6M", "-bufsize", "12M",
    "-threads", "2",
    "-pix_fmt", "yuv420p", "-g", "120", "-c:a", "aac", "-b:a", "128k",
]
_COVER = ("scale=1920:1080:force_original_aspect_ratio=increase,"
          "crop=1920:1080,setsar=1,fps=30,format=yuv420p")
_MUSIC_EXPR = ("aevalsrc=0.34*sin(2*PI*130.81*t)+0.28*sin(2*PI*196.00*t)+"
               "0.22*sin(2*PI*261.63*t)+0.16*sin(2*PI*155.56*t):s=44100:c=stereo:d={d}")


def _run(cmd: list[str], what: str) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffmpeg {what} timed out")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg {what} failed (rc={proc.returncode}): {(proc.stderr or '')[-500:]}")


def _music_args(music_url: str | None, seconds: float, vol: float):
    """Return (input_args, audio_filter_template) for the music bed.

    Everything is resampled to 44.1 kHz so amix never matches to a lower-rate
    voiceover (which would dull the music / risk pitch artifacts).
    """
    if music_url:
        return (["-stream_loop", "-1", "-i", music_url],
                "[{idx}:a]aresample=44100,volume=" + str(vol) + "[m]")
    expr = _MUSIC_EXPR.format(d=math.ceil(seconds))
    # Audible midrange dark-ambient pad (small speakers can't reproduce sub-bass).
    return (["-f", "lavfi", "-i", expr],
            "[{idx}:a]aresample=44100,lowpass=f=950,tremolo=f=0.12:d=0.5,volume=" + str(vol) + "[m]")


def _encode_card(card_path, out_path, fade_out, music_url, vol, seconds=3.0):
    """Encode a 3s branded card (intro/outro) with fades + music."""
    ff = _ffmpeg_bin()
    music_in, music_filt = _music_args(music_url, seconds, vol)
    fade = "fade=t=in:st=0:d=1" if not fade_out else "fade=t=in:st=0:d=0.8,fade=t=out:st=2.2:d=0.8"
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
           "-loop", "1", "-t", str(seconds), "-i", card_path] + music_in + [
        "-filter_complex",
        f"[0:v]{_COVER},{fade}[v];" + music_filt.format(idx=1),
        "-map", "[v]", "-map", "[m]", "-t", str(seconds)] + _ENC + [out_path]
    _run(cmd, "card")
    return out_path


def _concat(parts, out_path, workdir):
    ff = _ffmpeg_bin()
    listfile = os.path.join(workdir, "concat.txt")
    with open(listfile, "w") as fh:
        for p in parts:
            fh.write(f"file '{p}'\n")
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
           "-i", listfile, "-c", "copy", "-movflags", "+faststart", out_path]
    _run(cmd, "concat")
    return out_path


# Fit the full 16:9 source inside a 9:16 1080x1920 frame: scale to 1080 wide
# (preserving aspect ratio) then pad the top/bottom with black bars. Shows the
# entire frame with no crop/zoom (letterboxed).
_VERTICAL_FIT = "scale=1080:-2,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=30,format=yuv420p"


def make_vertical_centercrop(source: str, out_path: str) -> str | None:
    """Fit a 16:9 video into a 9:16 1080x1920 frame with black top/bottom bars
    (full frame shown, no crop/zoom)."""
    ff = _ffmpeg_bin()
    cmd = [
        ff, "-y", "-hide_banner", "-loglevel", "error", "-i", source,
        "-vf", _VERTICAL_FIT,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-profile:v", "main", "-level", "4.0", "-maxrate", "6M", "-bufsize", "12M",
        "-threads", "2", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out_path,
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if p.returncode == 0 and os.path.exists(out_path):
            return out_path
        log.warning("Vertical center-crop failed: %s", (p.stderr or "")[-200:])
    except Exception as exc:  # noqa: BLE001
        log.warning("Vertical center-crop error: %s", exc)
    return None


def make_tiktok_clips(source: str, duration: float, workdir: str, base: str,
                      length: int = 78, label: str = "TikTok"):
    """Cut 3 vertical 1080x1920 clips (hook / middle / close) from a 16:9 video.

    Returns [(path, name), ...]. Each clip fits the full 16:9 frame into 9:16 with
    black top/bottom bars (no crop/zoom). ``label`` controls the filename suffix so
    different-length sets (e.g. 78s TikTok vs 58s Shorts) don't collide.
    """
    ff = _ffmpeg_bin()
    L = float(min(length, max(20.0, duration)))
    starts = [0.0, max(0.0, duration / 2 - L / 2), max(0.0, duration - L)]
    clips = []
    for i, start in enumerate(starts, 1):
        name = f"{base}_{label}_{i}"
        out = os.path.join(workdir, f"{name}.mp4")
        cmd = [
            ff, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", str(round(start, 2)), "-i", source, "-t", str(round(L, 2)),
            "-vf", _VERTICAL_FIT, "-map", "0:v:0", "-map", "0:a?",
            "-r", "30", "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-profile:v", "main", "-level", "4.0",
            "-maxrate", "6M", "-bufsize", "12M", "-threads", "2", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out,
        ]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            if p.returncode == 0 and os.path.exists(out):
                clips.append((out, name))
            else:
                log.warning("TikTok clip %d failed: %s", i, (p.stderr or "")[-200:])
        except Exception as exc:  # noqa: BLE001
            log.warning("TikTok clip %d error: %s", i, exc)
    log.info("Generated %d/3 TikTok clips", len(clips))
    return clips


def render_video(
    *,
    audio_path: str,
    audio_seconds: float | None,
    script: str,
    alignment: dict | None,
    out_path: str,
    accent: str = "#1FA2FF",
    music_url: str | None = None,
    music_volume: float = 0.2,
    watermark: str = "",
    logo_path: str | None = None,
    intro_path: str | None = None,
    broll_clips: list | None = None,
    scene_seconds: int = 35,
) -> str:
    duration = round(audio_seconds or 180.0, 2)
    workdir = os.path.dirname(out_path) or "."
    ass_path = os.path.join(workdir, "captions.ass")
    accent_hex = "0x" + accent.lstrip("#")

    words = words_from_alignment(alignment) if alignment else []
    if not words:
        log.warning("No alignment from ElevenLabs; using evenly-spaced captions")
        words = _even_words(script, duration)
    build_ass(words, ass_path, accent)

    # Background: content-aware B-roll if available, else animated gradient.
    bg_video = None
    if broll_clips:
        bg_video = build_broll_background(
            broll_clips, os.path.join(workdir, "background.mp4")
        )
    grad = (
        f"gradients=s=1920x1080:c0=0x03101e:c1=0x0a2a4a:c2=0x000308:"
        f"x0=120:y0=80:x1=1800:y1=1000:nb_colors=3:speed=0.006:d={math.ceil(duration)}"
    )

    ff = _ffmpeg_bin()
    main = os.path.join(workdir, "main.mp4")

    # --- inputs: background, voiceover, music, (logo) ---
    if bg_video:
        inputs = ["-i", bg_video]
    else:
        inputs = ["-f", "lavfi", "-i", grad]
    inputs += ["-i", audio_path]
    music_in, music_filt = _music_args(music_url, duration, music_volume)
    inputs += music_in
    music_idx = 2
    logo_idx = None
    if logo_path:
        inputs += ["-loop", "1", "-i", logo_path]
        logo_idx = 3

    # --- video chain: cover -> watermark (logo or text) -> captions ---
    ass_escaped = ass_path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    fp = [f"[0:v]{_COVER}[bg]"]
    if logo_idx is not None:
        fp.append(f"[{logo_idx}:v]scale=-1:150,colorkey=0x000000:0.30:0.10,"
                  f"format=yuva420p,colorchannelmixer=aa=0.7[lg]")
        fp.append("[bg][lg]overlay=48:40[wm]")
    elif watermark:
        # Escape ffmpeg drawtext metacharacters in the channel name.
        wm = watermark.replace("\\", "\\\\").replace("'", "’").replace(":", "\\:")
        dt = (f"drawtext=fontfile='{_font_bold()}':text='{wm}':x=72:y=56:"
              f"fontsize=40:fontcolor={accent_hex}:alpha=0.92:"
              f"shadowcolor=0x000000:shadowx=2:shadowy=2")
        fp.append(f"[bg]{dt}[wm]")
    else:
        fp.append("[bg]null[wm]")
    fp.append(f"[wm]subtitles='{ass_escaped}'[v]")
    # audio: voiceover (resampled to 44.1k) + music bed; voiceover stays full
    fp.append("[1:a]aresample=44100[vo]")
    fp.append(music_filt.format(idx=music_idx))
    fp.append("[vo][m]amix=inputs=2:duration=first:normalize=0[a]")

    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error"] + inputs + [
        "-filter_complex", ";".join(fp), "-map", "[v]", "-map", "[a]",
        "-t", str(duration)] + _ENC + [main]
    log.info("Rendering main segment (%.1fs, %d captions, logo=%s, broll=%s)...",
             duration, len(words), bool(logo_path), bool(bg_video))
    _run(cmd, "main")

    # --- intro/outro branded cards, if the card asset is available ---
    if intro_path and os.path.exists(intro_path):
        intro = _encode_card(intro_path, os.path.join(workdir, "intro.mp4"),
                             False, music_url, music_volume)
        outro = _encode_card(intro_path, os.path.join(workdir, "outro.mp4"),
                             True, music_url, music_volume)
        _concat([intro, main, outro], out_path, workdir)
    else:
        # finalize the main segment (add faststart) as the output
        _run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", main,
              "-c", "copy", "-movflags", "+faststart", out_path], "finalize")

    log.info("ffmpeg render complete: %s", out_path)
    return out_path

