"""Thin Airtable REST client for the Video Library table (with retries)."""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger("faceless_kit.airtable")

API_BASE = "https://api.airtable.com/v0"


class AirtableClient:
    def __init__(self, token: str, base_id: str, table_id: str):
        self.base_id = base_id
        self.table_id = table_id
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )

    @property
    def _url(self) -> str:
        return f"{API_BASE}/{self.base_id}/{self.table_id}"

    def _request(self, method: str, url: str, **kw) -> requests.Response:
        """Request with retry/backoff on timeouts and connection errors.

        Airtable occasionally times out; without retries a poll cycle would find
        nothing and silently skip until the next cycle. Up to 4 attempts with
        exponential backoff (2s, 4s, 8s).
        """
        kw.setdefault("timeout", 45)
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                return self.session.request(method, url, **kw)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_exc = exc
                wait = 2 ** (attempt + 1)
                log.warning("Airtable %s timed out (attempt %d/4); retrying in %ds: %s",
                            method, attempt + 1, wait, exc)
                time.sleep(wait)
        raise last_exc  # type: ignore[misc]

    def find_by_status(self, status_field: str, status_value: str) -> list[dict[str, Any]]:
        formula = f"{{{status_field}}} = '{status_value}'"
        resp = self._request("GET", self._url,
                             params={"filterByFormula": formula, "pageSize": 100})
        resp.raise_for_status()
        return resp.json().get("records", [])

    def find_by_field(self, field: str, value: str) -> list[dict[str, Any]]:
        safe = value.replace("'", "\\'")
        formula = f"{{{field}}} = '{safe}'"
        resp = self._request("GET", self._url,
                             params={"filterByFormula": formula, "pageSize": 10})
        resp.raise_for_status()
        return resp.json().get("records", [])

    def get_record(self, record_id: str) -> dict[str, Any] | None:
        resp = self._request("GET", f"{self._url}/{record_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def update_fields(self, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        resp = self._request("PATCH", f"{self._url}/{record_id}",
                             json={"fields": fields, "typecast": True})
        if not resp.ok:
            log.error("Airtable update failed (%s): %s", resp.status_code, resp.text)
        resp.raise_for_status()
        return resp.json()

    def set_status(self, record_id: str, status_field: str, value: str) -> None:
        self.update_fields(record_id, {status_field: value})

    def list_records_from(self, table_id: str, params: dict | None = None) -> list[dict[str, Any]]:
        """List records from any table in the base (e.g. the trending topics table)."""
        url = f"{API_BASE}/{self.base_id}/{table_id}"
        resp = self._request("GET", url, params=params or {"pageSize": 100})
        resp.raise_for_status()
        return resp.json().get("records", [])

    def count_recent(self, hours: float) -> int:
        """Count records created within the last ``hours`` (for weekly dedup)."""
        from datetime import datetime, timedelta, timezone

        resp = self._request("GET", self._url, params={"pageSize": 100})
        resp.raise_for_status()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        n = 0
        for r in resp.json().get("records", []):
            ct = r.get("createdTime")
            if not ct:
                continue
            try:
                if datetime.fromisoformat(ct.replace("Z", "+00:00")) >= cutoff:
                    n += 1
            except ValueError:
                pass
        return n

    def recent_used_clips(self, field: str, limit: int = 100) -> set:
        """Aggregate Pexels clip IDs used in recent videos (for cross-video dedup)."""
        resp = self._request("GET", self._url, params={"pageSize": limit})
        resp.raise_for_status()
        ids: set = set()
        for r in resp.json().get("records", []):
            v = r.get("fields", {}).get(field)
            if isinstance(v, str):
                for tok in v.replace("\n", ",").split(","):
                    tok = tok.strip()
                    if tok.isdigit():
                        ids.add(int(tok))
        return ids

    def posting_schedule(self, table_id: str) -> dict[str, dict[str, str]]:
        """Read the Posting Schedule table -> {platform_lower: {weekday, weekend, tz}}.

        Lets Brian retune optimal posting times in Airtable without code changes.
        Returns {} on any error so posting falls back to safe defaults.
        """
        out: dict[str, dict[str, str]] = {}
        try:
            for r in self.list_records_from(table_id):
                f = r.get("fields", {})
                platform = str(f.get("Platform") or "").strip().lower()
                if not platform:
                    continue
                out[platform] = {
                    "weekday": str(f.get("Optimal Time Weekday") or "").strip(),
                    "weekend": str(f.get("Optimal Time Weekend") or "").strip(),
                    "tz": str(f.get("Timezone") or "America/New_York").strip(),
                }
        except Exception:  # noqa: BLE001 — never block a render on schedule lookup
            log.exception("Could not read Posting Schedule table %s", table_id)
        return out

    def recent_titles(self, title_field: str, days: int = 90) -> list[str]:
        """Titles of records created within the last ``days`` (for duplicate-topic
        dedup). Records with no createdTime are included to be safe."""
        from datetime import datetime, timedelta, timezone

        resp = self._request("GET", self._url, params={"pageSize": 100})
        resp.raise_for_status()
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        out: list[str] = []
        for r in resp.json().get("records", []):
            title = (r.get("fields", {}) or {}).get(title_field)
            if not title:
                continue
            ct = r.get("createdTime")
            if ct:
                try:
                    if datetime.fromisoformat(ct.replace("Z", "+00:00")) < cutoff:
                        continue
                except ValueError:
                    pass
            out.append(str(title))
        return out

    def create_record(self, fields: dict[str, Any]) -> dict[str, Any]:
        """Create a record in the bound (Video Library) table."""
        resp = self._request("POST", self._url, json={"fields": fields, "typecast": True})
        if not resp.ok:
            log.error("Airtable create failed (%s): %s", resp.status_code, resp.text)
        resp.raise_for_status()
        return resp.json()
