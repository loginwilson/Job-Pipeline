"""Rippling job boards (ats.rippling.com/{slug}/jobs).

  GET https://ats.rippling.com/api/v2/board/{slug}/jobs         -> {items[] {id, name, url, locations[]}}
  GET https://ats.rippling.com/api/v2/board/{slug}/jobs/{id}    -> {description {company, role, ...} (HTML),
                                                                     employmentType, createdOn, ...}
Boards are small, so each posting's description is fetched with it.
"""
from __future__ import annotations

from typing import Any

import requests

from ..pipeline.html_text import html_to_lines
from .base import USER_AGENT, RawPosting, Source

API = "https://ats.rippling.com/api/v2/board/{slug}/jobs"


class RipplingSource(Source):
    platform = "rippling"

    def endpoint(self, slug: str) -> str:
        return API.format(slug=slug)

    def fetch_payload(self, slug: str, session: requests.Session | None = None) -> Any:
        sess = session or requests.Session()
        listing = super().fetch_payload(slug, sess)
        items = listing.get("items") or []
        for it in items:
            try:
                r = sess.get(f"{API.format(slug=slug)}/{it['id']}", timeout=20,
                             headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
                if r.ok:
                    it["detail"] = r.json()
            except (requests.RequestException, ValueError):
                pass                                   # a posting without its text is still listed
        return {"items": items}

    def parse(self, payload: Any, slug: str) -> list[RawPosting]:
        out = []
        for j in (payload or {}).get("items") or []:
            d = j.get("detail") or {}
            desc = d.get("description") or {}
            html = "".join(v for v in desc.values() if isinstance(v, str)) if isinstance(desc, dict) else str(desc)
            locs = [l.get("name") or "" for l in j.get("locations") or []]
            remote = any((l.get("workplaceType") or "").upper() == "REMOTE" for l in j.get("locations") or [])
            out.append(RawPosting(
                platform=self.platform, slug=slug, native_id=str(j.get("id")),
                title=(j.get("name") or "").strip(),
                url=j.get("url") or f"https://ats.rippling.com/{slug}/jobs/{j.get('id')}",
                locations=[l for l in locs if l], workplace_type="remote" if remote else None,
                employment_type=(d.get("employmentType") or {}).get("label") if isinstance(d.get("employmentType"), dict)
                else d.get("employmentType"),
                department=(j.get("department") or {}).get("name"),
                description_lines=html_to_lines(html),
                posted_at=d.get("createdOn")))
        return out
