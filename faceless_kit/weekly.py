"""Weekly auto-scripting — research trending topics on the web, then script them.

Uses Claude's built-in web search tool to find what's trending in the channel's
niche (NICHE_TOPIC) over the last 7 days, writes a full script for each in the
channel's own voice (SCRIPT_STYLE_PROMPT), and creates a *Queued* Video Library
record per topic. The records are queued (not Scripted) so the whole batch does
not render at once: the caller renders ONE immediately and the render queue
(Pipeline.render_next_queued) promotes the rest one per day across the week.

Everything niche-specific here is read from config — nothing about any one
channel or niche is hard-coded, so the same generator works for any Kit.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("faceless_kit.weekly")


def _system_prompt(cfg) -> str:
    """Build the script-writing system prompt from the channel's own niche and
    style, so the generator writes in the buyer's voice — not a fixed one."""
    return (
        f"You are writing a script for a faceless YouTube channel called "
        f"{cfg.channel_name}. The channel covers {cfg.niche_topic}. Write a "
        f"complete, in-depth video script: open with a strong hook, welcome "
        f"viewers to {cfg.channel_name}, deliver the core value, and close with a "
        f"subscribe call to action. Expand every part with real depth — concrete "
        f"examples, specifics, step-by-step detail, and short real-world scenarios "
        f"— so the script runs 8 to 10 minutes when spoken at a natural pace "
        f"(approximately 1200 to 1500 words). Do not pad with filler or repetition; "
        f"every sentence should add genuine value or detail. No section headers. "
        f"Just the spoken words.\n\nStyle guidance for this channel: "
        f"{cfg.script_style_prompt}"
    )

_BULLET = re.compile(r"^\s*(?:[-•*–]\s*|\d+[.)]\s+)")


def _parse_titles(text: str, count: int) -> list[str]:
    titles: list[str] = []
    seen: list[str] = []
    for line in text.splitlines():
        t = _BULLET.sub("", line.strip()).strip().strip('"').strip("*").strip()
        if len(t) < 12 or ":" == t[-1:] or t.lower().startswith(("here", "based on", "these")):
            continue
        norm = t.lower()
        if any(norm in s or s in norm for s in seen):
            continue
        seen.append(norm)
        titles.append(t)
        if len(titles) >= count:
            break
    return titles


def get_topics_via_web(cfg, count: int, avoid: list[str] | None = None) -> list[str]:
    """Use Claude's web search tool to find trending topics; fall back to knowledge.

    ``avoid`` lists titles already covered or already chosen this run so the model
    returns fresh replacements instead of repeating them.
    """
    from anthropic import Anthropic

    client = Anthropic(api_key=cfg.anthropic_key)
    avoid_clause = ""
    if avoid:
        avoid_clause = (" Do NOT suggest any topic that overlaps with these already-"
                        "covered or already-chosen titles: " + "; ".join(avoid[:40]) + ".")
    # How many of every N titles should be head-to-head comparisons (COMPARISON_BIAS_RATIO).
    num, den = cfg.comparison_ratio
    wildcard = max(0, den - num)
    prompt = (
        f"Search the web for what is trending RIGHT NOW (the last 7 days) in "
        f"{cfg.niche_topic}. Based on real recent trends, give exactly {count} punchy, "
        f"specific YouTube video titles for the faceless channel '{cfg.channel_name}'.\n\n"
        f"Bias the mix toward head-to-head COMPARISONS — on this channel, 'vs' matchups "
        f"tend to outperform generic topics. Of every {den} titles, about {num} should be "
        f"head-to-head comparisons between current, trending things in this niche (e.g. "
        f"'X vs Y for <task>', or 'I tested 5 <things> for <goal> — here's the winner'). "
        f"The remaining ~{wildcard} of every {den} should be wildcard exploration topics in "
        f"the same space, NOT forced into the vs format, so we keep discovering new winning "
        f"angles. Ground every title — comparisons and wildcards alike — in real, currently "
        f"trending subjects surfaced by your web search.\n\n"
        f"Output ONLY the {count} titles, one per line — no numbering, no quotes, no extra "
        f"commentary." + avoid_clause
    )
    try:
        msg = client.messages.create(
            model=cfg.script_model,
            max_tokens=1200,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}],
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        topics = _parse_titles(text, count)
        if topics:
            log.info("Web search produced %d trending topics", len(topics))
            return topics
        log.warning("Web search returned no parseable titles; falling back")
    except Exception as exc:  # noqa: BLE001 — tool may be unavailable on the SDK/plan
        log.warning("Web search failed (%s); falling back to model knowledge", exc)

    msg = client.messages.create(
        model=cfg.script_model,
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt + " Use your knowledge of current trends."}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    return _parse_titles(text, count)


_STOP = {"the", "a", "an", "to", "of", "for", "in", "on", "with", "and", "or",
         "your", "you", "how", "that", "this", "is", "are", "2024", "2025", "2026"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).strip()


def _tokens(s: str) -> set:
    return {w for w in _norm(s).split() if w and w not in _STOP}


def _text_similar(topic: str, existing: str) -> bool:
    """Cheap pre-check: strong word overlap (Jaccard) catches near-identical titles
    before spending a model call."""
    a, b = _tokens(topic), _tokens(existing)
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= 0.6


def is_duplicate_topic(cfg, topic: str, existing: list[str]) -> bool:
    """True if ``topic`` covers essentially the same subject as any recent existing
    title. Exact/near text match first (free), then a semantic check via Claude."""
    if not existing:
        return False
    for e in existing:
        if _norm(topic) == _norm(e) or _text_similar(topic, e):
            log.info("Duplicate topic (text match) — skipping %r (~ %r)", topic, e)
            return True
    if not cfg.anthropic_key:
        return False
    try:
        from anthropic import Anthropic

        client = Anthropic(api_key=cfg.anthropic_key)
        listing = "\n".join(f"- {e}" for e in existing)
        msg = client.messages.create(
            model=cfg.caption_model or cfg.script_model,
            max_tokens=5,
            messages=[{"role": "user", "content": (
                "Existing recent video titles for a channel:\n" + listing +
                f"\n\nProposed new title:\n- {topic}\n\nWould the proposed video cover "
                "essentially the SAME core topic (same tool, idea, or angle) as any "
                "existing one — i.e. a duplicate? Reply with exactly one word: YES or NO."
            )}],
        )
        ans = "".join(b.text for b in msg.content
                      if getattr(b, "type", None) == "text").strip().upper()
        if ans.startswith("YES"):
            log.info("Duplicate topic (semantic) — skipping: %s", topic)
            return True
    except Exception:  # noqa: BLE001 — never block scripting on the dedup check
        log.exception("Semantic dedup check failed for %s; keeping topic", topic)
    return False


def _recent_titles(cfg, airtable) -> list[str]:
    if cfg.dedup_days <= 0:
        return []
    try:
        return airtable.recent_titles(cfg.f_title, cfg.dedup_days)
    except Exception:  # noqa: BLE001
        log.exception("Could not load recent titles for dedup")
        return []


def _write_script(cfg, topic: str) -> str:
    from anthropic import Anthropic

    client = Anthropic(api_key=cfg.anthropic_key)
    msg = client.messages.create(
        model=cfg.script_model,
        max_tokens=4000,  # room for a full 1200-1500 word (8-10 min) script
        system=_system_prompt(cfg),
        messages=[{"role": "user", "content": f"Topic: {topic}\n\nWrite the complete script now."}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()


def _collect_fresh_topics(cfg, airtable, count: int) -> list[str]:
    """Select ``count`` trending topics that are NOT semantically similar to any
    title covered within the dedup window. Rejected topics are replaced by
    re-fetching (telling the model to avoid the covered/chosen titles), so the run
    still yields a full set of fresh videos instead of coming up short."""
    existing = _recent_titles(cfg, airtable)
    log.info("Dedup: comparing against %d title(s) from the last %dd",
             len(existing), cfg.dedup_days)
    chosen: list[str] = []
    rejected: list[str] = []
    for _ in range(3):  # bounded re-fetch rounds to backfill rejected topics
        if len(chosen) >= count:
            break
        need = count - len(chosen)
        batch = get_topics_via_web(cfg, need + 3, avoid=existing + chosen + rejected)
        if not batch:
            break
        progressed = False
        for topic in batch:
            if len(chosen) >= count:
                break
            if topic in chosen:
                continue
            if is_duplicate_topic(cfg, topic, existing + chosen):
                if topic not in rejected:
                    rejected.append(topic)
                continue
            chosen.append(topic)
            progressed = True
        if not progressed:
            break  # model keeps returning duplicates — stop rather than loop forever
    if rejected:
        log.info("Dedup: rejected %d already-covered topic(s): %s", len(rejected), rejected)
    return chosen


def generate_weekly(cfg, airtable, count: int | None = None) -> list[tuple[str, str]]:
    """Research trending topics, script each, and create Queued records.

    Records are created Queued (not Scripted) so they don't all render at once;
    the caller renders one immediately and the queue drains the rest one per day.

    Duplicate-topic guard runs BEFORE any script is written: a topic semantically
    similar to one covered in the last ``DEDUP_DAYS`` days is rejected and replaced
    with a different trending topic.
    """
    if not cfg.anthropic_key:
        log.error("ANTHROPIC_API_KEY not set — cannot research/script")
        return []
    count = count or cfg.weekly_count
    topics = _collect_fresh_topics(cfg, airtable, count)
    if not topics:
        log.warning("No fresh (non-duplicate) trending topics found to script")
        return []
    log.info("Weekly: scripting %d fresh topic(s): %s", len(topics), topics)
    created: list[tuple[str, str]] = []
    for topic in topics:
        try:
            script = _write_script(cfg, topic)
            if not script:
                log.warning("Empty script for topic: %s", topic)
                continue
            rec = airtable.create_record({
                cfg.f_title: topic,
                cfg.f_script: script,
                # Created Queued (not Scripted) so the whole week's batch does NOT
                # render at once. The caller renders ONE immediately via
                # Pipeline.render_next_queued(); the render queue then drains the
                # rest one per day.
                cfg.f_status: cfg.status_queued,
            })
            created.append((rec["id"], topic))
            log.info("Created Scripted record %s: %s", rec["id"], topic)
        except Exception:  # noqa: BLE001 — one bad topic shouldn't stop the rest
            log.exception("Failed to script/create topic: %s", topic)
    log.info("Weekly: created %d scripted records", len(created))
    return created
