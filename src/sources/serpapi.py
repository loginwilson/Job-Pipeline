"""SerpApi Google Jobs (free plan: a monthly search allowance; key: SERPAPI_API_KEY).

  GET https://serpapi.com/search.json?engine=google_jobs&q&location&hl=en&chips=date_posted:3days&api_key
  -> {jobs_results[] {job_id, title, company_name, location, via, description, job_highlights[]
      {title, items[]}, detected_extensions {posted_at, schedule_type, salary}, apply_options[]
      {title, link}, share_link}}
Same Google Jobs data as JSearch, through a second free quota.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any

import requests

from .base import TIMEOUT_S, USER_AGENT, RawPosting, Source, SourceError
from .jsearch import _aggregator

API = "https://serpapi.com/search.json"


def search(q: str, location: str, chips: str = "date_posted:3days", session: requests.Session | None = None) -> dict:
    key = os.environ.get("SERPAPI_API_KEY")
    if not key:
        raise SourceError("SERPAPI_API_KEY is not set")
    sess = session or requests.Session()
    params = {"engine": "google_jobs", "q": q, "location": location, "hl": "en", "gl": "us", "api_key": key}
    if chips:
        params["chips"] = chips
    for attempt in range(3):
        try:
            r = sess.get(API, params=params, timeout=TIMEOUT_S * 3, headers={"User-Agent": USER_AGENT})
            if r.status_code in (401, 403):
                raise SourceError(f"SerpApi refused the key ({r.status_code})")
            if r.status_code == 429:
                raise SourceError("SerpApi monthly searches used up (429)")
            r.raise_for_status()
            data = r.json()
            if data.get("error") and "hasn't returned any results" not in data["error"]:
                raise SourceError(f"SerpApi: {data['error']}")
            return data
        except SourceError:
            raise
        except (requests.RequestException, ValueError) as exc:
            if attempt == 2:
                raise SourceError(f"SerpApi failed: {exc}") from exc
            time.sleep(2 * (attempt + 1))
    raise SourceError("unreachable")


class SerpApiSource(Source):
    platform = "serpapi"

    def parse(self, payload: Any, slug: str = "") -> list[RawPosting]:
        out = []
        for j in (payload or {}).get("jobs_results") or []:
            company = (j.get("company_name") or "").strip()
            if not company or _aggregator(company):
                continue
            links = [o.get("link") or "" for o in j.get("apply_options") or []]
            url = next((u for u in links if u and not _aggregator(u)), None) or j.get("share_link") or ""
            lines = [ln.strip() for ln in re.split(r"\n+", j.get("description") or "") if ln.strip()]
            for h in j.get("job_highlights") or []:
                lines.append("## " + (h.get("title") or ""))
                lines += [f"- {it}" for it in h.get("items") or []]
            ext = j.get("detected_extensions") or {}
            out.append(RawPosting(
                platform=self.platform, slug=re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-"),
                native_id=str(j.get("job_id") or "")[:200], title=(j.get("title") or "").strip(), url=url,
                locations=[j.get("location") or ""], employment_type=ext.get("schedule_type"),
                description_lines=lines, company=company,
                raw={"apply_links": links, "publisher": j.get("via"), "posted": ext.get("posted_at"), "job": j}))
        return out
