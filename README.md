# Faceless YouTube Kit

**Your faceless YouTube channel, on autopilot.** This Kit researches trending
topics in *your* niche, writes the scripts, generates the voiceover, edits a
finished video with captions and music, makes a thumbnail, and publishes it to
YouTube, TikTok, and Facebook — one new video a day, hands-free.

You bring the niche and the accounts. The Kit does the production line.

---

## ⚡ One-click deploy

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/new)

Clicking **Deploy on Railway** spins up your own private copy of this pipeline in
the cloud as an always-on worker. Railway asks you to paste in your settings
(your niche, your channel name, your API keys — see the setup guide), then it
builds and runs everything for you. There are no servers to manage; it costs a
couple of dollars a month for this light workload, and it keeps running 24/7 so a
new video goes out every day without you touching it.

> You provide your own accounts and keys (Airtable, ElevenLabs, YouTube, etc.).
> The Kit never ships with anyone's credentials — you stay in full control.

---

## What you get

- **Weekly topic research** — finds what's trending in your niche and drafts a
  week of video ideas, biased toward the high-performing formats you choose.
- **Full script writing** — 8–10 minute scripts in your channel's voice.
- **Voiceover** — natural narration via ElevenLabs, in the voice you pick.
- **Automatic video editing** — captions, music, motion, and topic-matched
  B-roll, rendered to a finished MP4. No editing software, no render credits.
- **Thumbnails** — a clean thumbnail per video in your brand's accent color.
- **Publishing** — uploads to YouTube (plus Shorts) and schedules TikTok +
  Facebook, at your chosen times, one video per day.

Everything is **configurable** — your niche, channel name, script style,
thumbnail color, posting schedule, timezone, and which platforms to post to are
all just settings. Nothing about any one channel is baked into the code.

---

## Setup

Full, step-by-step setup instructions live in the **separate written setup
guide** (`Faceless YouTube Setup Guide`) that comes with your purchase — it walks
you through duplicating the Airtable base, getting each API key, and filling in
your settings on Railway. That guide is the place to start; this repository is
just the engine it deploys.

The complete list of settings is documented in
[`.env.example`](.env.example) if you'd like to preview what you'll be filling in.

---

## 🛟 Troubleshooting

Something not working? The interactive troubleshooting guide covers the common
snags (videos stuck "Processing", YouTube uploads going private, posts not
appearing, and more), with a fix for each:

**➡️ [Open the Troubleshooting Guide](https://bmwielhouwer.github.io/Faceless-YouTube-Channel-Kit-/troubleshooting.html)**

---

## For the technically curious

Under the hood this is a small Python worker that polls an Airtable base and runs
each script through voiceover → render → publish. It's config-driven end to end
(see [`config.py`](config.py)), self-hosts rendering with `ffmpeg` (no paid render
API), and has built-in safeguards against double-posting, double-uploads, and
same-day duplicate videos. You do **not** need to read any of this to use the Kit
— it's here only if you want to look.

## License / use

This Kit is sold for you to deploy and run your own channel. Keep your API keys
private (they live only in your host's settings, never in this repo).
