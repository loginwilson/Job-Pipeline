"""Coresignal job postings (LinkedIn jobs with full text). Key: GitHub secret CORESIGNAL_API_KEY.

  POST https://api.coresignal.com/cdapi/v2/job_base/search/filter
       {"title", "location", "application_active": true, "deleted": false, "created_at_gte"}
       -> [id, ...]                        (about half a credit per search)
  GET  https://api.coresignal.com/cdapi/v2/job_base/collect/{id}
       -> {id, title, company_name, company_url, location, description (HTML), salary,
           employment_type, seniority, created, url, ...}     (one credit per posting)
Free credits never renew, so only postings not seen before are collected, within a daily cap.
Nov 10 sample (203 NYC postings): 3 real jobs the pipeline had never seen (Mogul, VectorShift,
Turbo AI chiefs of staff - small companies that post only on LinkedIn).
"""
from __future__ import annotations

import html
import os
import re
import time
from typing import Any

import requests

from .base import TIMEOUT_S, USER_AGENT, CompRange, RawPosting, Source, SourceError
from .jsearch import _aggregator

BASE = "https://api.coresignal.com/cdapi/v2/job_base"
_SALARY = re.compile(r"(?:USD|\$)\s*([\d,.]+)\s*(?:-|–|to)\s*\$?\s*([\d,.]+)\s*(?:/?\s*)(YEAR|yr|HOUR|hr)?", re.I)


def _headers() -> dict:
    key = os.environ.get("CORESIGNAL_API_KEY")
    if not key:
        raise SourceError("CORESIGNAL_API_KEY is not set")
    return {"apikey": key, "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": USER_AGENT}


def _call(method: str, url: str, session: requests.Session, **kw) -> Any:
    for attempt in range(3):
        try:
            r = session.request(method, url, headers=_headers(), timeout=TIMEOUT_S * 3, **kw)
            if r.status_code in (401, 403):
                raise SourceError(f"Coresignal refused the key ({r.status_code}) - check CORESIGNAL_API_KEY")
            if r.status_code == 402:
                raise SourceError("Coresignal credits are used up (402)")
            if r.status_code == 429:
                time.sleep(1.5)
                continue
            r.raise_for_status()
            return r.json()
        except SourceError:
            raise
        except (requests.RequestException, ValueError) as exc:
            if attempt == 2:
                raise SourceError(f"Coresignal failed: {exc}") from exc
            time.sleep(2 * (attempt + 1))
    raise SourceError("Coresignal rate limit (429)")


def search(title: str, location: str, since: str, session: requests.Session | None = None) -> list[int]:
    """Ids of active postings with this title in this place, created since `since` (YYYY-MM-DD)."""
    body = {"title": title, "location": location, "application_active": True, "deleted": False,
            "created_at_gte": f"{since} 00:00:00"}
    out = _call("POST", f"{BASE}/search/filter", session or requests.Session(), json=body)
    return [int(x) for x in out or [] if str(x).isdigit()]


def collect(job_id: int, session: requests.Session | None = None) -> dict:
    return _call("GET", f"{BASE}/collect/{job_id}", session or requests.Session())


def _lines(description: str) -> list[str]:
    text = html.unescape(re.sub(r"<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", description or "", flags=re.I))
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return [re.sub(r"\s+", " ", l).strip() for l in text.split("\n") if l.strip()]


class CoresignalSource(Source):
    platform = "coresignal"

    def parse(self, payload: Any, slug: str = "") -> list[RawPosting]:
        jobs = payload.get("jobs") if isinstance(payload, dict) and "jobs" in payload else [payload]
        out = []
        for j in jobs or []:
            if not isinstance(j, dict) or not j.get("title"):
                continue
            company = (j.get("company_name") or "").strip()
            if not company or _aggregator(company):
                continue                                   # a recruiter / job site, not the employer
            ranges = []
            m = _SALARY.search(str(j.get("salary") or ""))
            if m:
                lo, hi = (float(x.replace(",", "")) for x in m.groups()[:2])
                period = "hour" if (m.group(3) or "").lower() in ("hour", "hr") or hi < 1000 else "year"
                ranges.append(CompRange(lo, hi, None, period, label="LinkedIn"))
            lines = _lines(j.get("description") or "")
            if j.get("salary") and not m:
                lines.append(f"Salary: {j['salary']}")
            out.append(RawPosting(
                platform=self.platform, slug=re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-"),
                native_id=str(j.get("id")), title=j["title"].strip(), url=j.get("url") or "",
                locations=[j.get("location") or ""],
                country={"united states": "US", "canada": "CA"}.get((j.get("country") or "").strip().lower()),
                employment_type=j.get("employment_type"), description_lines=lines, comp_ranges=ranges,
                posted_at=j.get("created"), company=company, raw={"job": j}))
        return out
