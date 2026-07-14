"""Content-aware B-roll: turn each spoken section into a relevant stock query.

Instead of a fixed list of generic dark-tech searches, we segment the narration
by time and derive a Pexels search query from what is actually being said in
each segment, so the footage tracks the topic.
"""
from __future__ import annotations

import random
import re

# Concept map: the concept with the most keyword hits in a segment wins, then a
# RANDOM query variant from that concept is used — so different videos (and even
# different clips in the same video) search different terms for the same topic.
_CONCEPTS: list[tuple[tuple[str, ...], list[str]]] = [
    (("money", "income", "cash", "dollar", "profit", "revenue", "paid", "pay", "earn", "$"),
     ["money cash counting", "hundred dollar bills", "stock market chart", "wealth luxury",
      "coins stacking finance", "online payment phone", "investing trading screen"]),
    (("client", "customer", "business owner", "founder", "company", "agency", "freelance"),
     ["business meeting office", "entrepreneur working cafe", "handshake deal", "modern office team",
      "startup workspace", "video call client", "small business owner shop"]),
    (("youtube", "video", "channel", "edit", "editing", "capcut", "footage", "filming"),
     ["video editing timeline", "camera filming studio", "youtube creator setup", "editing software screen",
      "content creator recording", "film production dark"]),
    (("write", "writing", "content", "blog", "article", "script", "copywriting", "newsletter", "email"),
     ["typing keyboard closeup", "writing notebook desk", "email inbox screen", "blogging laptop",
      "hands typing dark", "document editing screen"]),
    (("design", "canva", "graphic", "thumbnail", "logo", "branding", "visual"),
     ["graphic designer working", "design software screen", "creative studio desk", "color palette design",
      "digital illustration tablet", "branding mockup"]),
    (("social", "facebook", "instagram", "tiktok", "post", "posting", "media", "audience", "followers"),
     ["social media smartphone", "instagram scrolling phone", "notifications phone closeup",
      "influencer phone content", "social feed app", "phone engagement likes"]),
    (("automate", "automation", "workflow", "system", "pipeline", "zapier", "integrate", "agent"),
     ["automation technology network", "robotic process", "workflow diagram screen", "futuristic interface",
      "ai agent automation", "connected systems data"]),
    (("ai", "artificial intelligence", "claude", "chatgpt", "gpt", "model", "machine learning", "neural"),
     ["artificial intelligence interface", "neural network visualization", "ai chatbot screen",
      "machine learning data", "glowing ai brain", "futuristic ai technology"]),
    (("data", "dashboard", "analytics", "metrics", "report", "numbers", "track"),
     ["data dashboard analytics", "charts graphs screen", "business metrics monitor", "big data visualization",
      "analytics report laptop", "growth chart rising"]),
    (("network", "digital", "online", "internet", "cloud", "server", "connection"),
     ["digital network connection", "server room dark", "cloud data center", "fiber optic data",
      "global network map", "cybersecurity dark"]),
    (("phone", "mobile", "smartphone", "app", "device"),
     ["smartphone app technology", "mobile phone closeup", "using phone dark", "app interface screen"]),
    (("laptop", "computer", "code", "coding", "programming", "developer", "software", "tech"),
     ["laptop coding dark", "programmer typing code", "code screen scrolling", "software developer desk",
      "terminal code dark", "macbook working night"]),
    (("team", "remote", "zoom", "call", "meeting", "collaborate", "hire", "employee"),
     ["remote team video call", "online meeting screen", "coworking space", "team collaboration office"]),
    (("growth", "scale", "scaling", "success", "wealth", "passive", "freedom", "future"),
     ["financial growth chart", "upward arrow success", "city skyline ambition", "luxury lifestyle",
      "rising graph money", "sunrise opportunity"]),
    (("time", "family", "lifestyle", "home", "relax", "balance"),
     ["lifestyle laptop home", "relaxing beach laptop", "working from home cozy", "morning coffee desk"]),
    (("city", "night", "skyline", "urban", "world", "global"),
     ["city at night aerial", "city lights timelapse", "skyscraper night", "urban traffic night"]),
]

_FALLBACKS = [
    "dark technology background", "digital network connection", "abstract technology blue",
    "data dashboard analytics", "city at night aerial", "code screen programming",
    "futuristic interface dark", "server room data", "glowing circuit board",
]

_STOP = re.compile(r"[^a-z0-9$ ]+")


def segment_script(words: list[tuple[str, float, float]], target_seconds: float = 38.0):
    """Group timed words into ~target_seconds segments: [(text, start, end), ...]."""
    segments = []
    cur, start, end = [], None, None
    for w, s, e in words:
        if start is None:
            start = s
        cur.append(w)
        end = e
        if end - start >= target_seconds:
            segments.append((" ".join(cur), start, end))
            cur, start = [], None
    if cur:
        segments.append((" ".join(cur), start if start is not None else 0.0, end))
    return segments


def random_fallback() -> str:
    """A random generic dark-tech query (used when a topic query finds nothing)."""
    return random.choice(_FALLBACKS)


def query_for_text(text: str, used_queries: set[str] | None = None) -> str:
    """Pick a relevant Pexels query for a segment, with a random variant for variety."""
    low = " " + _STOP.sub(" ", text.lower()) + " "
    best_variants, best_score = None, 0
    for keywords, variants in _CONCEPTS:
        score = sum(low.count(" " + k + " ") + (low.count(k) if " " in k else 0)
                    for k in keywords)
        if score > best_score:
            best_variants, best_score = variants, score
    pool = best_variants or _FALLBACKS
    return random.choice(pool)
