"""JobsPipe (jobspipe.dev): one API over 30+ sources (LinkedIn, Indeed, Workday, Greenhouse,
Ashby, Lever, The Muse, YC ...). Free tier: 1,000 credits ONCE (1 credit = 1 job, 25 per page),
so it runs as a one-time sweep (Nov 9) whose main payoff is discovery: each result's source link
names the employer's own job board, which is then watched for free.

  POST https://api.jobspipe.dev/v1/jobs/search   Authorization: Bearer $JOBSPIPE_API_KEY
       {job_title_or, job_title_not, metro_code_or, employer_type_not, posted_at_max_age_days,
        max_ghost_score, status, limit, cursor}
  -> {metadata {next_cursor}, data[] {id, job_title, company, location, country_code, remote,
      date_posted, url, source_url, final_url, description, min/max_annual_salary_usd,
      salary_source, employment_statuses, ...}}
"""
from __future__ import annotations

import os
import time
from typing import Any

import requests

from .base import TIMEOUT_S, USER_AGENT, CompRange, RawPosting, Source, SourceError
from .jsearch import _aggregator

API = "https://api.jobspipe.dev/v1/jobs/search"


def search(body: dict, session: requests.Session | None = None) -> dict:
    key = os.environ.get("JOBSPIPE_API_KEY")
    if not key:
        raise SourceError("JOBSPIPE_API_KEY is not set")
    sess = session or requests.Session()
    for attempt in range(3):
        try:
            r = sess.post(API, json=body, timeout=TIMEOUT_S * 2,
                          headers={"Authorization": f"Bearer {key}", "User-Agent": USER_AGENT})
            if r.status_code == 402:
                raise SourceError("JobsPipe free credits are used up (402)")
            if r.status_code in (400, 401, 403):
                raise SourceError(f"JobsPipe refused the request ({r.status_code}): {r.text[:200]}")
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except SourceError:
            raise
        except (requests.RequestException, ValueError) as exc:
            if attempt == 2:
                raise SourceError(f"JobsPipe failed: {exc}") from exc
            time.sleep(3 * (attempt + 1))
    raise SourceError("unreachable")


class JobsPipeSource(Source):
    platform = "jobspipe"

    def parse(self, payload: Any, slug: str = "") -> list[RawPosting]:
        out = []
        for j in (payload or {}).get("data") or []:
            company = (j.get("company") or "").strip()
            if not company or _aggregator(company):
                continue
            ranges = []
            lo, hi = j.get("min_annual_salary_usd"), j.get("max_annual_salary_usd")
            if (lo or hi) and (j.get("salary_source") or "posted") != "estimated":
                ranges.append(CompRange(lo, hi, "USD", "year", label="JobsPipe"))
            text = j.get("description") or ""
            out.append(RawPosting(
                platform=self.platform, slug="", native_id=str(j.get("id")),
                title=(j.get("job_title") or j.get("title") or "").strip(),
                url=j.get("final_url") or j.get("source_url") or j.get("url") or j.get("apply_url") or "",
                locations=[j.get("location") or j.get("short_location") or ""],
                country=j.get("country_code"), workplace_type="remote" if j.get("remote") else None,
                employment_type=", ".join(j.get("employment_statuses") or []) or j.get("employment_type"),
                description_lines=[ln.strip() for ln in text.splitlines() if ln.strip()],
                comp_ranges=ranges, posted_at=j.get("date_posted"), company=company,
                raw={"source_url": j.get("source_url"), "final_url": j.get("final_url"), "job": j}))
        return out
