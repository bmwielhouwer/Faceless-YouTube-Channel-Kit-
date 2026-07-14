#!/usr/bin/env python3
"""Print the element names inside your Creatomate template.

Use the output to set CREATOMATE_AUDIO_ELEMENT, CREATOMATE_SUBTITLE_ELEMENT and
CREATOMATE_TITLE_ELEMENT in .env so the pipeline targets the right layers.

  python -m tools.inspect_creatomate_template
"""
from __future__ import annotations

import sys

import requests

from config import load_config


def _walk(elements, depth=0):
    for el in elements or []:
        name = el.get("name") or "(unnamed)"
        etype = el.get("type")
        print(f"{'  ' * depth}- {name}  [{etype}]")
        _walk(el.get("elements"), depth + 1)


def main() -> int:
    cfg = load_config()
    if not cfg.creatomate_template:
        print("CREATOMATE_TEMPLATE_ID is not set.", file=sys.stderr)
        return 2

    resp = requests.get(
        f"https://api.creatomate.com/v1/templates/{cfg.creatomate_template}",
        headers={"Authorization": f"Bearer {cfg.creatomate_key}"},
        timeout=30,
    )
    resp.raise_for_status()
    source = resp.json().get("source", {})
    print(f"Template: {cfg.creatomate_template}")
    print(f"Output: {source.get('width')}x{source.get('height')}\n")
    print("Elements (use these names in your .env):")
    _walk(source.get("elements"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
