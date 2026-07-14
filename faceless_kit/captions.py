"""Generate upload metadata: a fixed-layout YouTube description, tags, and a
Facebook caption. Script-specific parts (hook, "what we cover" bullets, hashtags)
come from Claude when ANTHROPIC_API_KEY is set, otherwise a clean template.

Everything brand/niche-specific is read from config (channel name, niche, an
optional affiliate link) — nothing about any one channel is hard-coded here.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("faceless_kit.captions")


# --------------------------------------------------------------------------- #
# Brand-neutral helpers (all derived from config)
# --------------------------------------------------------------------------- #
def _channel_tag(cfg) -> str:
    """A single hashtag built from the channel name, e.g. 'My Channel' -> '#MyChannel'."""
    slug = re.sub(r"[^A-Za-z0-9]", "", cfg.channel_name or "")
    return f"#{slug}" if slug else ""


def _subscribe_url(cfg) -> str:
    url = cfg.channel_url
    return f"{url}?sub_confirmation=1" if url else ""


def _niche_hashtags(cfg, n: int = 15) -> list[str]:
    """Fallback hashtag set derived from the channel name + niche keywords, padded
    with a few evergreen tags. Used only when Claude isn't drafting the hashtags."""
    tags: list[str] = []
    seen: set[str] = set()

    def add(tag: str):
        tag = re.sub(r"[^#A-Za-z0-9]", "", tag)
        if len(tag) > 1 and tag.lower() not in seen:
            seen.add(tag.lower())
            tags.append(tag)

    ct = _channel_tag(cfg)
    if ct:
        add(ct)
    for word in re.findall(r"[A-Za-z0-9]+", cfg.niche_topic or ""):
        if len(word) > 2:
            add("#" + word.capitalize())
    for ever in ("#YouTube", "#HowTo", "#Tutorial", "#Tips", "#Explained",
                 "#Guide", "#Trending", "#Review"):
        add(ever)
    return tags[:n]


def _assemble_youtube(cfg, title: str, hook: str, bullets: list[str], hashtags: list[str]) -> str:
    cover = "\n".join(f"✅ {b}" for b in bullets[:6])
    tags = " ".join(hashtags[:15])
    parts = [hook, "", "✅ WHAT WE COVER", cover, ""]
    if cfg.affiliate_link:
        parts += [f"🔗 Tools & resources: {cfg.affiliate_link}", ""]
    sub = _subscribe_url(cfg)
    if sub:
        parts += [f"🔔 Subscribe to {cfg.channel_name}: {sub}", ""]
    if cfg.channel_url:
        parts += [f"▶️ Channel: {cfg.channel_url}", ""]
    parts += [tags]
    return "\n".join(parts)


def draft(cfg, title: str, script: str) -> dict[str, str]:
    """Return {'youtube': fixed-layout description, 'tags': ..., 'facebook': ...}."""
    if cfg.anthropic_key:
        try:
            return _draft_with_claude(cfg, title, script)
        except Exception as exc:  # noqa: BLE001 — never block the video
            log.error("Claude metadata drafting failed, using template: %s", exc)
    return _template(cfg, title)


def _draft_with_claude(cfg, title: str, script: str) -> dict[str, str]:
    import json

    from anthropic import Anthropic

    client = Anthropic(api_key=cfg.anthropic_key)
    system = (
        f"You write YouTube metadata for '{cfg.channel_name}', a faceless channel about "
        f"{cfg.niche_topic}. Punchy, confident, premium. Return STRICT JSON only."
    )
    user = (
        f"Video title: {title}\n\nScript:\n{script[:6000]}\n\n"
        'Return JSON with keys: "hook" (1-2 sentence opening hook from THIS script), '
        '"bullets" (4-6 short "what we cover" points specific to the script), '
        '"hashtags" (15 relevant hashtags WITH the # sign), '
        '"facebook" (2-4 line caption + a question + 4-6 hashtags). JSON only.'
    )
    msg = client.messages.create(
        model=cfg.caption_model, max_tokens=900, system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
    data = json.loads(text)
    hook = data.get("hook") or _default_hook(cfg)
    bullets = data.get("bullets") or DEFAULT_BULLETS
    hashtags = [h if h.startswith("#") else f"#{h}" for h in (data.get("hashtags") or [])] or _niche_hashtags(cfg)
    youtube = _assemble_youtube(cfg, title, hook, bullets, hashtags)
    log.info("Metadata drafted by Claude (%s)", cfg.caption_model)
    return {"youtube": youtube, "tags": ", ".join(h.lstrip("#") for h in hashtags[:15]),
            "facebook": data.get("facebook") or _template(cfg, title)["facebook"]}


DEFAULT_BULLETS = [
    "What it actually does",
    "How it works step by step",
    "Exactly what it changes",
    "The honest catch",
    "How to put it to work today",
]


def _default_hook(cfg) -> str:
    return f"Here's what you need to know about {cfg.niche_topic} — the useful version, no fluff."


# --------------------------------------------------------------------------- #
# Social post caption (TikTok + Facebook)
# --------------------------------------------------------------------------- #
DEFAULT_SOCIAL_BULLETS = [
    "The key idea and why it matters",
    "How it works, simply",
    "How to start using it today",
]


def social_bullets(cfg, title: str, script: str) -> list[str]:
    """Three short 'what you'll learn' bullets from the script (Claude, with fallback)."""
    if cfg.anthropic_key:
        try:
            from anthropic import Anthropic

            client = Anthropic(api_key=cfg.anthropic_key)
            msg = client.messages.create(
                model=cfg.caption_model, max_tokens=300,
                system=(f"You write punchy social captions for '{cfg.channel_name}' "
                        f"(a faceless channel about {cfg.niche_topic})."),
                messages=[{"role": "user", "content": (
                    f"Video title: {title}\n\nScript:\n{script[:6000]}\n\n"
                    "Give exactly 3 short 'what you'll learn' bullet points specific to "
                    "this script. 4-8 words each, no emojis, no numbering, no hashtags. "
                    "Return ONLY the 3 lines, one per line.")}],
            )
            text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
            lines = [l.strip(" -•*\t") for l in text.splitlines() if l.strip()]
            bullets = [l for l in lines if l][:3]
            if len(bullets) == 3:
                return bullets
        except Exception as exc:  # noqa: BLE001 — never block a post
            log.error("Social bullet generation failed, using defaults: %s", exc)
    return DEFAULT_SOCIAL_BULLETS


def _social_hashtags(cfg) -> str:
    return " ".join(_niche_hashtags(cfg, 10))


def build_social_caption(cfg, title: str, youtube_link: str, bullets: list[str]) -> str:
    """The shared caption used for every TikTok + Facebook post."""
    bullet_block = "\n".join(f"• {b}" for b in (bullets or DEFAULT_SOCIAL_BULLETS)[:3])
    lines = [
        f"{title} — Watch the full video on YouTube 🔗 {youtube_link}",
        "🔑 What you'll learn:",
        "",
        bullet_block,
    ]
    if cfg.affiliate_link:
        lines.append(f"🔗 Tools & resources: {cfg.affiliate_link}")
    lines.append(f"Follow {cfg.channel_name} for more 🔔")
    lines.append(_social_hashtags(cfg))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# TikTok caption (short, curiosity-driven, NON-promotional)
#
# TikTok gets its own caption — deliberately NOT the shared promotional one.
# Rules (enforced below, not just requested of the model):
#   * under 150 characters total
#   * hook statement + engagement question + exactly 5 hashtags
#   * NO affiliate links / URLs, NO dollar amounts or income claims
#   * conversational, curiosity-driven — not a sales pitch
#
# NOTE: the sanitizer + length logic below is a solved safeguard — its behavior
# is unchanged. Only the brand-neutral hashtag defaults and prompt text are now
# config-driven.
# --------------------------------------------------------------------------- #
TIKTOK_MAX_CHARS = 150
DEFAULT_TIKTOK_HOOK = "The thing quietly changing how this works."
DEFAULT_TIKTOK_QUESTION = "Would you try it?"

# Any URL, and any dollar/income claim ("$50,000", "$16/mo", "$5k a month").
_URL_RE = re.compile(r"\b(?:https?://|www\.)\S+", re.I)
_DOLLAR_RE = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?\s*(?:k|m|b|/mo|/month|/yr|/year|"
    r"a\s+month|a\s+week|a\s+year|per\s+\w+)?\+?",
    re.I,
)
# Hashtags that read as income/earnings hype — kept out of the non-promotional set.
_BANNED_TAG_RE = re.compile(r"(money|cash|income|profit|salary|payroll|financial|rich|\$)", re.I)


def _tiktok_fallback_tags(cfg) -> list[str]:
    """Neutral, non-income hashtags for TikTok — channel tag + evergreen topical tags."""
    tags = []
    ct = _channel_tag(cfg)
    if ct:
        tags.append(ct)
    tags += ["#FYP", "#Tips", "#HowTo", "#Trending", "#LearnOnTikTok"]
    return tags


def _sanitize_tiktok_text(text: str) -> str:
    """Strip links and dollar/income claims; collapse the leftover whitespace."""
    text = _URL_RE.sub("", text or "")
    text = _DOLLAR_RE.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _clean_hashtags(raw: list[str] | None, fallback: list[str]) -> list[str]:
    """Return exactly 5 clean, non-income hashtags (model picks, backfilled)."""
    out: list[str] = []
    seen: set[str] = set()
    for h in list(raw or []) + fallback:
        if len(out) >= 5:
            break
        tag = re.sub(r"[^#A-Za-z0-9]", "", h.strip())
        if not tag.startswith("#"):
            tag = "#" + tag.lstrip("#")
        if len(tag) <= 1 or _BANNED_TAG_RE.search(tag):
            continue
        if tag.lower() in seen:
            continue
        seen.add(tag.lower())
        out.append(tag)
    return out[:5]


def _assemble_tiktok(hook: str, question: str, hashtags: list[str], fallback: list[str]) -> str:
    """hook + question + 5 hashtags, guaranteed <= 150 chars. The question and all
    five hashtags are always preserved; the hook is trimmed if space is tight."""
    tags = " ".join(_clean_hashtags(hashtags, fallback))
    hook = _sanitize_tiktok_text(hook).rstrip(" .,;:—-")
    question = _sanitize_tiktok_text(question)
    if question and not question.endswith("?"):
        question += "?"
    budget = TIKTOK_MAX_CHARS - len(tags) - 1  # newline before the hashtags
    if len(question) > budget:                 # question alone too long (rare)
        question = question[:budget].rsplit(" ", 1)[0]
        text = question
    else:
        hook_budget = budget - len(question) - 1  # space between hook and question
        if len(hook) > hook_budget:
            hook = hook[:max(0, hook_budget)].rsplit(" ", 1)[0].rstrip(" ,.;:—-")
        text = f"{hook} {question}".strip()
    return f"{text}\n{tags}"


def tiktok_caption(cfg, title: str, script: str) -> str:
    """Short, curiosity-driven TikTok caption — hook + question + 5 hashtags, under
    150 chars, with no affiliate links and no dollar/income claims. Claude writes
    the hook/question/tags when available; a clean template is the fallback. The
    output is always sanitized and length-capped regardless of source."""
    hook, question, hashtags = DEFAULT_TIKTOK_HOOK, DEFAULT_TIKTOK_QUESTION, []
    if cfg.anthropic_key:
        try:
            import json

            from anthropic import Anthropic

            client = Anthropic(api_key=cfg.anthropic_key)
            system = (
                f"You write short, curiosity-driven TikTok captions for "
                f"'{cfg.channel_name}', a faceless channel about {cfg.niche_topic}. "
                "Conversational and intriguing, NOT promotional. Never mention prices, "
                "dollar amounts, earnings/income claims, or links."
            )
            user = (
                f"Video title: {title}\n\nScript:\n{script[:4000]}\n\n"
                "Return STRICT JSON with keys: "
                '"hook" (one short curiosity-driven statement, max ~10 words, no dollar '
                "amounts, no prices, no income claims, no links), "
                '"question" (one short question to drive engagement, max ~8 words), '
                '"hashtags" (exactly 5 relevant topical hashtags WITH the # sign, no '
                "money/income hashtags). JSON only."
            )
            msg = client.messages.create(
                model=cfg.caption_model, max_tokens=300, system=system,
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(b.text for b in msg.content
                            if getattr(b, "type", None) == "text").strip()
            if text.startswith("```"):
                text = text.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
            data = json.loads(text)
            hook = data.get("hook") or hook
            question = data.get("question") or question
            hashtags = data.get("hashtags") or []
        except Exception as exc:  # noqa: BLE001 — never block a post
            log.error("TikTok caption generation failed, using template: %s", exc)
    caption = _assemble_tiktok(hook, question, hashtags, _tiktok_fallback_tags(cfg))
    log.info("TikTok caption (%d chars): %s", len(caption), caption.replace("\n", " | "))
    return caption


def _template(cfg, title: str) -> dict[str, str]:
    hashtags = _niche_hashtags(cfg)
    youtube = _assemble_youtube(cfg, title, _default_hook(cfg), DEFAULT_BULLETS, hashtags)
    fb_tags = " ".join(hashtags[:5])
    facebook = (
        f"{title}\n\n"
        "Here's the full breakdown — the useful version, no fluff.\n\n"
        f"Watch it on {cfg.channel_name}. What would you want covered next?\n\n"
        f"{fb_tags}"
    )
    log.info("Metadata generated from template (set ANTHROPIC_API_KEY for script-tailored copy)")
    return {"youtube": youtube, "tags": ", ".join(h.lstrip("#") for h in hashtags[:15]),
            "facebook": facebook}
