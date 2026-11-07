"""Hacker News "Ask HN: Who is hiring?" — used for DISCOVERY only.

Verified 2026-11-07 against the public Algolia HN API (fixture:
tests/fixtures/hn_whoishiring.json.gz):
  GET https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring
      → hits[] {objectID, title "Ask HN: Who is hiring? (November 2026)", created_at}
  GET https://hn.algolia.com/api/v1/items/{id}
      → {id, title, children[] {id, author, created_at, text (HTML)}}
Each top-level comment is one company's post, usually "Company | Role | Location
| ...". Posts that mention one of your cities and link straight into a
Greenhouse / Lever / Ashby / SmartRecruiters / Workday board give us that board.
"""
from __future__ import annotations

import html
import re

import requests

from .base import TIMEOUT_S, USER_AGENT, SourceError

API = "https://hn.algolia.com/api/v1"
_TAG = re.compile(r"<[^>]+>")
_LINK = re.compile(r"https?://[^\s\"<>]+")


def _get(url: str, session: requests.Session) -> dict:
    try:
        r = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S * 2)
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as exc:
        raise SourceError(f"HN: {exc}") from exc


def recent_threads(n: int, session: requests.Session) -> list[str]:
    hits = _get(f"{API}/search_by_date?tags=story,author_whoishiring&hitsPerPage=12", session).get("hits", [])
    return [h["objectID"] for h in hits if "who is hiring" in (h.get("title") or "").lower()][:n]


def thread(item_id: str, session: requests.Session) -> dict:
    return _get(f"{API}/items/{item_id}", session)


def posts(item: dict) -> list[dict]:
    """[{company, text, links}] for each top-level post."""
    out = []
    for c in item.get("children") or []:
        raw = html.unescape(c.get("text") or "")
        text = re.sub(r"\s+", " ", _TAG.sub(" ", raw)).strip()
        if not text:
            continue
        company = re.split(r"\s*[|•–—(]\s*|\s+-\s+", text, maxsplit=1)[0].strip()[:60]
        links = [u.rstrip(").,;") for u in _LINK.findall(raw)]
        out.append({"company": company, "text": text, "links": links})
    return out
