# Brand assets

Drop your own two brand images here (exact filenames) and commit them — the
worker picks them up automatically:

- `logo.png` — square channel logo on a **black** background. Used as a top-left
  watermark (black is keyed out so only the artwork/glow shows, ~70% opacity).
- `intro.png` — 16:9 intro/outro card. Shown 3s at the start (fade in) and 3s at
  the end (fade in, then fade to black).

If a file is absent, that feature is skipped gracefully (a text watermark from
`CHANNEL_NAME`, no intro card) and renders still succeed. Override paths with the
`LOGO_PATH` / `INTRO_PATH` env vars if you store them elsewhere.

`music.mp3` (if present) is used as the background music bed; set `MUSIC_URL` or
`MUSIC_FILE` to override, or leave it and a built-in ambient pad is generated.
