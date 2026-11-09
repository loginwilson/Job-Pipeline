"""Comeet career pages (www.comeet.com/jobs/{company}/{uid}).

The page itself carries every position as data (COMPANY_POSITIONS_DATA), descriptions
included (custom_fields.details[]), so one request per board is enough.
Board "slug" = "{company}/{uid}", e.g. "guesty/10.000".
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import requests

from ..pipeline.html_text import html_to_lines
from .base import BACKOFF_S, RETRIES, TIMEOUT_S, USER_AGENT, RawPosting, Source, SourceError

_DATA = re.compile(r"COMPANY_POSITIONS_DATA\s*=\s*(\[.*?\]);", re.S)


class ComeetSource(Source):
    platform = "comeet"

    def endpoint(self, slug: str) -> str:
        return f"https://www.comeet.com/jobs/{slug}"

    def fetch_payload(self, slug: str, session: requests.Session | None = None) -> Any:
        sess = session or requests.Session()
        last: Exception | None = None
        for attempt in range(RETRIES):
            try:
                r = sess.get(self.endpoint(slug), timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT})
                if r.status_code == 404:
                    raise SourceError(f"comeet:{slug} not found (404)")
                r.raise_for_status()
                m = _DATA.search(r.text)
                if not m:
                    raise SourceError(f"comeet:{slug}: no positions data on the page")
                return json.loads(m.group(1))
            except SourceError:
                raise
            except (requests.RequestException, ValueError) as exc:
                last = exc
                time.sleep(BACKOFF_S * (2 ** attempt))
        raise SourceError(f"comeet:{slug} failed after {RETRIES} attempts: {last}")

    def parse(self, payload: Any, slug: str) -> list[RawPosting]:
        out = []
        for j in payload or []:
            loc = j.get("location") or {}
            where = ", ".join(p for p in (loc.get("city"), loc.get("state"), loc.get("country")) if p)
            details = ((j.get("custom_fields") or {}).get("details")) or []
            lines: list[str] = []
            for d in sorted(details, key=lambda d: d.get("order") or 0):
                if d.get("name"):
                    lines.append("## " + d["name"])
                lines += html_to_lines(d.get("value"))
            out.append(RawPosting(
                platform=self.platform, slug=slug, native_id=str(j.get("uid")),
                title=(j.get("name") or "").strip(),
                url=j.get("url_active_page") or j.get("url_comeet_hosted_page") or "",
                locations=[where] if where else [],
                country=loc.get("country") or None,
                workplace_type="remote" if loc.get("is_remote") else j.get("workplace_type"),
                employment_type=j.get("employment_type"),
                department=j.get("department"),
                description_lines=lines,
                posted_at=j.get("time_updated")))
        return out
