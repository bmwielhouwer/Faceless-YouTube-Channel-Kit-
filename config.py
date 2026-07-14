"""Central configuration for the Faceless YouTube Kit automation pipeline.

Everything niche-specific, account-specific, secret, or tunable is read from
environment variables (loaded from a local ``.env`` in development, or from your
host's dashboard in production). Nothing sensitive and nothing niche-specific is
hard-coded here — that is what makes this Kit reusable for any channel/niche.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _get(key: str, default: str | None = None) -> str | None:
    val = os.getenv(key, default)
    if val is not None:
        val = val.strip()
    return val or default


def _first(*keys_then_default) -> str | None:
    """First non-empty value among several env keys; last arg is the default.

    Lets a variable be renamed (e.g. AIRTABLE_PAT) while still honoring an older
    name (AIRTABLE_API_TOKEN) if that's what the buyer already set.
    """
    *keys, default = keys_then_default
    for k in keys:
        v = _get(k)
        if v:
            return v
    return default


def _req(key: str, *aliases) -> str:
    val = _first(key, *aliases, None)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {key}. "
            f"Copy .env.example to .env and fill it in."
        )
    return val


def _float(key: str, default: float) -> float:
    try:
        return float(_get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _int(key: str, default: int) -> int:
    try:
        return int(_get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _bool(key: str, default: bool) -> bool:
    return _get(key, str(default)).lower() in ("1", "true", "yes", "on")


def _asset_path(env_key: str, filename: str) -> str:
    """Resolve a brand asset: env override, else faceless_kit/assets/<file> if it
    exists, else empty (feature stays off until the asset is provided)."""
    override = _get(env_key)
    if override and os.path.exists(override):
        return override
    repo_asset = os.path.join(os.path.dirname(__file__), "faceless_kit", "assets", filename)
    return repo_asset if os.path.exists(repo_asset) else ""


def _json(key: str, default: dict) -> dict:
    raw = _get(key)
    if not raw:
        return default
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else default
    except json.JSONDecodeError:
        return default


def _list(key: str, default: str) -> list:
    raw = _get(key, default) or ""
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _ratio(key: str, default: str) -> tuple[int, int]:
    """Parse 'N/D' (e.g. '5/7') into (numerator, denominator). Falls back to the
    default on anything unparseable so the script generator never crashes on a
    typo."""
    raw = (_get(key, default) or default).strip()
    m = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", raw)
    if m:
        num, den = int(m.group(1)), int(m.group(2))
        if 0 < num <= den:
            return num, den
    dnum, dden = default.split("/")
    return int(dnum), int(dden)


def _schedule_hours(default_render: int = 7, default_publish: int = 8) -> tuple[int, int]:
    """Parse POSTING_SCHEDULE (e.g. 'render 07:00, publish 08:00') into
    (render_hour, publish_hour), both in the buyer's local timezone. Anything the
    buyer omits falls back to the sensible 7am-render / 8am-publish default."""
    raw = _get("POSTING_SCHEDULE", "") or ""
    render_h, publish_h = default_render, default_publish
    for label, is_render in (("render", True), ("publish", False)):
        m = re.search(rf"{label}\D*(\d{{1,2}})", raw, re.I)
        if m:
            hour = max(0, min(23, int(m.group(1))))
            if is_render:
                render_h = hour
            else:
                publish_h = hour
    return render_h, publish_h


def _public_base() -> str:
    """Public URL of this worker — explicit override, else Railway's domain."""
    explicit = _get("PUBLIC_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    dom = _get("RAILWAY_PUBLIC_DOMAIN") or _get("RAILWAY_STATIC_URL")
    if dom:
        dom = dom.replace("https://", "").replace("http://", "").rstrip("/")
        return f"https://{dom}"
    return ""


# Niche defaults — deliberately generic. A buyer overrides these to make the Kit
# theirs; left unset, the pipeline still runs with a neutral, non-branded voice.
_DEFAULT_NICHE = "your channel's niche"
_DEFAULT_CHANNEL = "Faceless Channel"
_DEFAULT_STYLE = (
    "Write in a clear, confident, engaging voice. Open with a strong hook, deliver "
    "genuine value with concrete examples and specifics, and close with a call to "
    "subscribe. No filler."
)


@dataclass
class Config:
    # ---- Niche / customization (make the Kit yours) --------------------------
    niche_topic: str = field(default_factory=lambda: _get("NICHE_TOPIC", _DEFAULT_NICHE))
    channel_name: str = field(default_factory=lambda: _get("CHANNEL_NAME", _DEFAULT_CHANNEL))
    script_style_prompt: str = field(default_factory=lambda: _get("SCRIPT_STYLE_PROMPT", _DEFAULT_STYLE))
    # How many of every N generated titles should be head-to-head comparisons.
    comparison_ratio: tuple = field(default_factory=lambda: _ratio("COMPARISON_BIAS_RATIO", "5/7"))
    # Which platforms to publish to. Any platform not listed here is skipped.
    platforms: list = field(default_factory=lambda: _list("PLATFORMS", "youtube,tiktok,facebook"))
    # Optional affiliate/referral link appended to descriptions/captions when set.
    affiliate_link: str = field(default_factory=lambda: _get("AFFILIATE_LINK", ""))

    # ---- Airtable -----------------------------------------------------------
    airtable_token: str = field(default_factory=lambda: _req("AIRTABLE_PAT", "AIRTABLE_API_TOKEN"))
    airtable_base: str = field(default_factory=lambda: _req("AIRTABLE_BASE_ID"))
    airtable_table: str = field(default_factory=lambda: _req("AIRTABLE_VIDEO_LIBRARY_TABLE_ID", "AIRTABLE_TABLE_ID"))
    f_title: str = field(default_factory=lambda: _get("AIRTABLE_TITLE_FIELD", "Video Title"))
    f_script: str = field(default_factory=lambda: _get("AIRTABLE_SCRIPT_FIELD", "ElevenLabs Script"))
    f_status: str = field(default_factory=lambda: _get("AIRTABLE_STATUS_FIELD", "Status"))
    f_facebook: str = field(default_factory=lambda: _get("AIRTABLE_FACEBOOK_FIELD", "Facebook Caption"))
    f_youtube_desc: str = field(default_factory=lambda: _get("AIRTABLE_YOUTUBE_DESC_FIELD", "YouTube Description"))
    f_tags: str = field(default_factory=lambda: _get("AIRTABLE_TAGS_FIELD", "YouTube Tags"))
    f_used_clips: str = field(default_factory=lambda: _get("AIRTABLE_USED_CLIPS_FIELD", "Used Clips"))
    f_notes: str = field(default_factory=lambda: _get("AIRTABLE_NOTES_FIELD", "Notes"))
    status_trigger: str = field(default_factory=lambda: _get("AIRTABLE_TRIGGER_STATUS", "Scripted"))
    status_processing: str = field(default_factory=lambda: _get("AIRTABLE_PROCESSING_STATUS", "Processing"))
    status_done: str = field(default_factory=lambda: _get("AIRTABLE_DONE_STATUS", "Video Ready"))
    status_published: str = field(default_factory=lambda: _get("AIRTABLE_PUBLISHED_STATUS", "Posted"))
    status_error: str = field(default_factory=lambda: _get("AIRTABLE_ERROR_STATUS", "Error"))
    # Staggered-render queue: a week's scripts are created in this state; the
    # render queue promotes exactly ONE per day to the trigger status so videos
    # render (and then publish) one-per-day instead of all at once.
    status_queued: str = field(default_factory=lambda: _get("AIRTABLE_QUEUED_STATUS", "Queued"))
    f_media_ids: str = field(default_factory=lambda: _get("AIRTABLE_MEDIA_IDS_FIELD", "Media IDs"))
    # Checkbox field flipped once a record's TikTok + Facebook posts both succeed —
    # the Zernio idempotency guard so a re-run never double-posts. Read & written
    # by field NAME so it works in any base (no base-specific field id).
    f_zernio_posted: str = field(default_factory=lambda: _get("AIRTABLE_ZERNIO_POSTED_FIELD", "Zernio Posted"))
    # Date stamped when a record is published (Posted). Drives the one-publish-per-day
    # guard: the scheduler/sweep skip the rest of the day once a record carries today.
    f_upload_date: str = field(default_factory=lambda: _get("AIRTABLE_UPLOAD_DATE_FIELD", "Upload Date"))
    # Stage 2 — Publishing: a separate scheduled job posts all 'Video Ready'
    # records each morning (wall-clock hour in the posting timezone).
    publish_enabled: bool = field(default_factory=lambda: _bool("PUBLISH_ENABLED", True))
    publish_hour: int = field(default_factory=lambda: _int("PUBLISH_HOUR", _schedule_hours()[1]))
    # Recurring safety sweep: re-run the publisher this often (seconds, default
    # hourly) so a record that becomes 'Video Ready' off the fixed publish hour
    # (a late/long render or a manual trigger) is published the same day instead
    # of being stranded until tomorrow. Set 0 to disable the sweep.
    publish_sweep_seconds: int = field(default_factory=lambda: _int("PUBLISH_SWEEP_SECONDS", 3600))
    poll_interval: int = field(default_factory=lambda: _int("POLL_INTERVAL_SECONDS", 300))
    # Lane 1 — fixed daily cron: process all Scripted records at this wall-clock
    # hour (in the posting timezone) every day, anchored to the clock.
    daily_poll_hour: int = field(default_factory=lambda: _int("DAILY_POLL_HOUR", _schedule_hours()[0]))
    # Shared secret required by the on-demand /trigger webhook (recommended).
    trigger_token: str = field(default_factory=lambda: _get("TRIGGER_TOKEN", ""))

    # ---- ElevenLabs ---------------------------------------------------------
    eleven_key: str = field(default_factory=lambda: _req("ELEVENLABS_API_KEY"))
    eleven_voice: str = field(default_factory=lambda: _get("ELEVENLABS_VOICE_ID", ""))
    eleven_model: str = field(default_factory=lambda: _get("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2"))
    eleven_stability: float = field(default_factory=lambda: _float("ELEVENLABS_STABILITY", 0.5))
    eleven_similarity: float = field(default_factory=lambda: _float("ELEVENLABS_SIMILARITY", 0.75))
    eleven_style: float = field(default_factory=lambda: _float("ELEVENLABS_STYLE", 0.35))
    eleven_speaker_boost: bool = field(default_factory=lambda: _bool("ELEVENLABS_SPEAKER_BOOST", True))
    # Delivery speed (0.7 slow … 1.0 normal … 1.2 fast). Lower this if the voice
    # sounds rushed. Only sent to ElevenLabs when != 1.0.
    eleven_speed: float = field(default_factory=lambda: _float("ELEVENLABS_SPEED", 1.0))

    # ---- Creatomate (legacy/optional — default render backend is ffmpeg) ----
    creatomate_key: str = field(default_factory=lambda: _get("CREATOMATE_API_KEY", ""))
    creatomate_template: str = field(default_factory=lambda: _get("CREATOMATE_TEMPLATE_ID", ""))
    creatomate_poll: int = field(default_factory=lambda: _int("CREATOMATE_POLL_INTERVAL", 30))
    creatomate_timeout: int = field(default_factory=lambda: _int("CREATOMATE_RENDER_TIMEOUT", 1800))
    creatomate_audio_el: str = field(default_factory=lambda: _get("CREATOMATE_AUDIO_ELEMENT", "Voiceover"))
    creatomate_subtitle_el: str = field(default_factory=lambda: _get("CREATOMATE_SUBTITLE_ELEMENT", "Subtitles"))
    creatomate_title_el: str = field(default_factory=lambda: _get("CREATOMATE_TITLE_ELEMENT", "Title"))
    creatomate_extra: dict = field(default_factory=lambda: _json("CREATOMATE_EXTRA_MODIFICATIONS", {}))
    # Render backend: "ffmpeg" (self-hosted, no credits) or "creatomate".
    render_backend: str = field(default_factory=lambda: _get("RENDER_BACKEND", "ffmpeg"))
    # Pexels stock B-roll (optional). If set, real stock clips replace the
    # gradient background.
    pexels_key: str = field(default_factory=lambda: _get("PEXELS_API_KEY", ""))
    # Source-mode (template-less) rendering — the long-form generator.
    render_mode: str = field(default_factory=lambda: _get("CREATOMATE_RENDER_MODE", "source"))
    # Thumbnail + video accent color. Default is an electric blue; override with
    # THUMBNAIL_ACCENT_COLOR to match your channel's brand.
    accent_color: str = field(default_factory=lambda: _first("THUMBNAIL_ACCENT_COLOR", "CREATOMATE_ACCENT_COLOR", "#1FA2FF"))
    scene_seconds: int = field(default_factory=lambda: _int("CREATOMATE_SCENE_SECONDS", 35))
    video_font: str = field(default_factory=lambda: _get("CREATOMATE_FONT", "Montserrat"))
    # Music: MUSIC_URL, else a committed faceless_kit/assets/music.mp3, else a
    # built-in procedural ambient pad.
    music_url: str = field(default_factory=lambda: _get("MUSIC_URL", "") or _asset_path("MUSIC_FILE", "music.mp3"))
    music_volume: float = field(default_factory=lambda: _float("MUSIC_VOLUME", 0.2))
    # Visible B-roll clip length — footage changes roughly this often.
    clip_seconds: int = field(default_factory=lambda: _int("CLIP_SECONDS", 13))
    # Brand assets (drop your own under faceless_kit/assets/, or point env at them).
    logo_path: str = field(default_factory=lambda: _asset_path("LOGO_PATH", "logo.png"))
    intro_path: str = field(default_factory=lambda: _asset_path("INTRO_PATH", "intro.png"))
    broll_urls: list = field(default_factory=lambda: [
        u.strip() for u in (_get("CREATOMATE_BROLL_URLS", "") or "").split(",") if u.strip()
    ])

    # ---- Audio hosting ------------------------------------------------------
    audio_host: str = field(default_factory=lambda: _get("AUDIO_HOST_PROVIDER", "tmpfiles"))

    # ---- Storage (where the finished MP4 lands) -----------------------------
    storage_provider: str = field(default_factory=lambda: _get("STORAGE_PROVIDER", "gdrive"))

    # ---- Google / YouTube OAuth ---------------------------------------------
    # One Google OAuth client (Desktop app) powers Drive, Gmail, AND the YouTube
    # upload. The refresh token must include the drive, gmail.send, and
    # youtube.upload scopes (mint it with `python -m tools.gdrive_auth`).
    google_client_id: str = field(default_factory=lambda: _first("YOUTUBE_CLIENT_ID", "GOOGLE_CLIENT_ID", ""))
    google_client_secret: str = field(default_factory=lambda: _first("YOUTUBE_CLIENT_SECRET", "GOOGLE_CLIENT_SECRET", ""))
    google_refresh_token: str = field(default_factory=lambda: _first("YOUTUBE_REFRESH_TOKEN", "GOOGLE_REFRESH_TOKEN", ""))
    youtube_channel_id: str = field(default_factory=lambda: _get("YOUTUBE_CHANNEL_ID", ""))
    gdrive_folder_id: str = field(default_factory=lambda: _get("GDRIVE_FOLDER_ID", ""))
    # Platform subfolders inside the root Drive folder (routed by Content Type).
    # Default to the single root folder so a buyer can start with just GDRIVE_FOLDER_ID.
    gdrive_folder_youtube: str = field(default_factory=lambda: _first("GDRIVE_FOLDER_YOUTUBE", "GDRIVE_FOLDER_ID", ""))
    gdrive_folder_tiktok: str = field(default_factory=lambda: _first("GDRIVE_FOLDER_TIKTOK", "GDRIVE_FOLDER_ID", ""))
    gdrive_folder_shorts: str = field(default_factory=lambda: _first("GDRIVE_FOLDER_SHORTS", "GDRIVE_FOLDER_ID", ""))
    gdrive_folder_reels: str = field(default_factory=lambda: _first("GDRIVE_FOLDER_REELS", "GDRIVE_FOLDER_ID", ""))
    f_content_type: str = field(default_factory=lambda: _get("AIRTABLE_CONTENT_TYPE_FIELD", "Content Type"))
    f_drive_link: str = field(default_factory=lambda: _get("AIRTABLE_DRIVE_LINK_FIELD", "Google Drive Link"))
    # Auto short-form (TikTok/Reels) clips from each long-form render.
    tiktok_enabled: bool = field(default_factory=lambda: _bool("TIKTOK_CLIPS", True))
    tiktok_seconds: int = field(default_factory=lambda: _int("TIKTOK_CLIP_SECONDS", 78))

    # Auto-upload to YouTube (needs the youtube.upload scope + API enabled).
    youtube_upload: bool = field(default_factory=lambda: _bool("YOUTUBE_UPLOAD", True))
    youtube_privacy: str = field(default_factory=lambda: _get("YOUTUBE_PRIVACY", "public"))
    youtube_category: str = field(default_factory=lambda: _get("YOUTUBE_CATEGORY", "27"))  # Education
    f_youtube_link: str = field(default_factory=lambda: _get("AIRTABLE_YOUTUBE_LINK_FIELD", "YouTube Link"))
    # Auto-upload the vertical clips to YouTube as Shorts (cut shorter so they
    # stay under the classic 60s Shorts limit; #Shorts added to the title).
    youtube_shorts_upload: bool = field(default_factory=lambda: _bool("YOUTUBE_SHORTS_UPLOAD", True))
    youtube_shorts_seconds: int = field(default_factory=lambda: _int("YOUTUBE_SHORTS_SECONDS", 58))

    # Auto-post to TikTok + Facebook via Zernio (set the key to enable). Account
    # IDs are auto-discovered from GET /api/v1/accounts; env vars override.
    zernio_api_key: str = field(default_factory=lambda: _get("ZERNIO_API_KEY", ""))
    zernio_tiktok_account_id: str = field(default_factory=lambda: _get("ZERNIO_TIKTOK_ACCOUNT_ID", ""))
    zernio_facebook_account_id: str = field(default_factory=lambda: _get("ZERNIO_FACEBOOK_ACCOUNT_ID", ""))
    # Posting Schedule table — optimal times are read from here (no code changes
    # needed to retune); Zernio schedules each post via scheduledFor.
    posting_schedule_table: str = field(default_factory=lambda: _get("AIRTABLE_POSTING_SCHEDULE_TABLE_ID", ""))
    # Public base URL of THIS worker (Railway injects RAILWAY_PUBLIC_DOMAIN).
    # Used to serve rendered media to Zernio from the worker's own /media endpoint
    # instead of a flaky Drive public link.
    public_base_url: str = field(default_factory=lambda: _public_base())
    media_token: str = field(default_factory=lambda: _get("MEDIA_PROXY_TOKEN") or _get("TRIGGER_TOKEN", ""))
    # After uploading a video to Drive, wait until Drive finishes processing it
    # before handing the public URL to Zernio (poll up to N seconds, every M).
    media_ready_timeout: int = field(default_factory=lambda: _int("MEDIA_READY_TIMEOUT", 300))
    media_ready_interval: int = field(default_factory=lambda: _int("MEDIA_READY_INTERVAL", 10))

    # ---- OneDrive (legacy/optional) -----------------------------------------
    onedrive_mode: str = field(default_factory=lambda: _get("ONEDRIVE_MODE", "local"))
    onedrive_local_dir: str = field(default_factory=lambda: _get("ONEDRIVE_LOCAL_DIR", "./output"))
    ms_tenant: str = field(default_factory=lambda: _get("MS_TENANT_ID", "common"))
    ms_client_id: str = field(default_factory=lambda: _get("MS_CLIENT_ID", ""))
    ms_client_secret: str = field(default_factory=lambda: _get("MS_CLIENT_SECRET", ""))
    ms_refresh_token: str = field(default_factory=lambda: _get("MS_REFRESH_TOKEN", ""))
    ms_drive_folder: str = field(default_factory=lambda: _get("MS_DRIVE_FOLDER", "Faceless Kit Audio"))

    # ---- Gmail --------------------------------------------------------------
    notify_email: str = field(default_factory=lambda: _get("NOTIFY_EMAIL", ""))
    gmail_address: str = field(default_factory=lambda: _get("GMAIL_ADDRESS", ""))
    gmail_app_password: str = field(default_factory=lambda: _get("GMAIL_APP_PASSWORD", ""))
    smtp_host: str = field(default_factory=lambda: _get("SMTP_HOST", "smtp.gmail.com"))
    smtp_port: int = field(default_factory=lambda: _int("SMTP_PORT", 465))

    # ---- Claude -------------------------------------------------------------
    anthropic_key: str = field(default_factory=lambda: _get("ANTHROPIC_API_KEY", ""))
    caption_model: str = field(default_factory=lambda: _get("CAPTION_MODEL", "claude-haiku-4-5-20251001"))
    script_model: str = field(default_factory=lambda: _get("SCRIPT_MODEL", "claude-sonnet-4-6"))

    # ---- Weekly auto-scripting ----------------------------------------------
    trending_table: str = field(default_factory=lambda: _first("AIRTABLE_TRENDING_TOPICS_TABLE_ID", "AIRTABLE_TRENDING_TABLE_ID", ""))
    trending_agent_field: str = field(default_factory=lambda: _get("AIRTABLE_TRENDING_AGENT_FIELD", "Top 5 Trending YouTube Topics Agent"))
    weekly_count: int = field(default_factory=lambda: _int("WEEKLY_COUNT", 7))
    # Skip a new topic if a semantically-similar title was already created/covered
    # within this many days (duplicate-topic guard). 0 disables the check.
    dedup_days: int = field(default_factory=lambda: _int("DEDUP_DAYS", 90))
    # Scheduled weekly run (in the worker; no Airtable scheduling needed).
    weekly_enabled: bool = field(default_factory=lambda: _bool("WEEKLY_ENABLED", True))
    weekly_day: int = field(default_factory=lambda: _int("WEEKLY_DAY", 0))   # Mon=0
    weekly_hour: int = field(default_factory=lambda: _int("WEEKLY_HOUR", _schedule_hours()[0]))
    # Timezone for every scheduled hour above. POSTING_TIMEZONE is the buyer-facing
    # name; WEEKLY_TZ is honored as a fallback for older configs.
    weekly_tz: str = field(default_factory=lambda: _first("POSTING_TIMEZONE", "WEEKLY_TZ", "America/New_York"))

    def folder_for(self, content_type: str | None) -> str:
        return {
            "youtube": self.gdrive_folder_youtube,
            "tiktok": self.gdrive_folder_tiktok,
            "shorts": self.gdrive_folder_shorts,
            "reels": self.gdrive_folder_reels,
        }.get((content_type or "").strip().lower(), self.gdrive_folder_youtube)

    def platform_enabled(self, platform: str) -> bool:
        """True if ``platform`` (e.g. 'tiktok') is in the configured PLATFORMS."""
        return (platform or "").strip().lower() in self.platforms

    @property
    def channel_url(self) -> str:
        """A best-effort public channel URL for descriptions/captions."""
        if self.youtube_channel_id:
            return f"https://youtube.com/channel/{self.youtube_channel_id}"
        return ""

    @property
    def posting_timezone(self) -> str:
        return self.weekly_tz


def load_config() -> Config:
    """Build and validate the configuration object."""
    return Config()
