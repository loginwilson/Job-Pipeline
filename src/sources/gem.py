"""Gem job boards (jobs.gem.com/{slug}) - one public request returns every posting.

  GET https://api.gem.com/job_board/v0/{slug}/job_posts/
  -> [{id, title, absolute_url, content (HTML), location {name}, offices[] {name, location},
       departments[] {name}, employment_type, first_published_at, updated_at, ...}]
"""
from __future__ import annotations

from typing import Any

from ..pipeline.html_text import html_to_lines
from .base import RawPosting, Source


class GemSource(Source):
    platform = "gem"

    def endpoint(self, slug: str) -> str:
        return f"https://api.gem.com/job_board/v0/{slug}/job_posts/"

    def parse(self, payload: Any, slug: str) -> list[RawPosting]:
        out = []
        for j in payload or []:
            locs = [(j.get("location") or {}).get("name") or ""]
            locs += [(o.get("location") or {}).get("name") or o.get("name") or "" for o in j.get("offices") or []]
            out.append(RawPosting(
                platform=self.platform, slug=slug, native_id=str(j.get("id")),
                title=(j.get("title") or "").strip(),
                url=j.get("absolute_url") or f"https://jobs.gem.com/{slug}/{j.get('id')}",
                locations=list(dict.fromkeys(l for l in locs if l)),
                department=", ".join(d.get("name", "") for d in j.get("departments") or []) or None,
                employment_type=j.get("employment_type"),
                workplace_type="remote" if j.get("location_type") == "remote" else j.get("location_type"),
                description_lines=html_to_lines(j.get("content")),
                posted_at=j.get("first_published_at") or j.get("updated_at")))
        return out
