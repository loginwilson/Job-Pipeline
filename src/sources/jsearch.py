"""JSearch (Google for Jobs) via OpenWeb Ninja.

Verified 2026-11-07 against https://api.openwebninja.com/jsearch/search
(fixture: tests/fixtures/jsearch_search.json.gz). Key in the `x-api-key`
header, read from JSEARCH_API_KEY. Response: {status, request_id,
parameters, data[]}; each job has job_id, job_title, employer_name,
employer_website, job_publisher, job_employment_type(s), job_apply_link,
apply_options[] {apply_link, is_direct, publisher}, job_description (plain
text), job_is_remote, job_posted_at_datetime_utc, job_city / job_state /
job_country / job_location, job_min_salary / job_max_salary /
job_salary_period (YEAR / HOUR / MONTH), job_highlights.
The pipeline never opens the LinkedIn / Indeed / Glassdoor links — it only
links to them from the Sheet.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any

import requests

from .base import USER_AGENT, CompRange, RawPosting, Source, SourceError, TIMEOUT_S

API = "https://api.openwebninja.com/jsearch/search"
_PERIOD = {"YEAR": "year", "HOUR": "hour", "MONTH": "month", "WEEK": "week", "DAY": "day"}
_COUNTRY = {"US": "US", "CA": "CA"}


def search(query: str, country: str, date_posted: str, session: requests.Session | None = None,
           key: str | None = None) -> dict:
    key = key or os.environ.get("JSEARCH_API_KEY")
    if not key:
        raise SourceError("JSEARCH_API_KEY is not set")
    sess = session or requests.Session()
    params = {"query": query, "page": 1, "num_pages": 1, "country": country, "date_posted": date_posted,
              "employment_types": "FULLTIME"}
    for attempt in range(3):
        try:
            r = sess.get(API, params=params, headers={"x-api-key": key, "User-Agent": USER_AGENT},
                         timeout=TIMEOUT_S * 2)
            if r.status_code in (401, 403):
                raise SourceError(f"JSearch refused the key ({r.status_code}) — check the plan / key")
            if r.status_code == 429:
                raise SourceError("JSearch monthly or hourly limit reached (429)")
            r.raise_for_status()
            return r.json()
        except SourceError:
            raise
        except (requests.RequestException, ValueError) as exc:
            if attempt == 2:
                raise SourceError(f"JSearch failed: {exc}") from exc
            time.sleep(2 * (attempt + 1))
    raise SourceError("unreachable")


DETAILS_API = "https://api.openwebninja.com/jsearch/job-details"


def job_details(job_ids: list[str], country: str = "us", session: requests.Session | None = None,
                key: str | None = None) -> dict:
    """Full postings for up to 20 job ids in one request (same shape as a search response)."""
    key = key or os.environ.get("JSEARCH_API_KEY")
    if not key:
        raise SourceError("JSEARCH_API_KEY is not set")
    try:
        r = (session or requests.Session()).get(
            DETAILS_API, params={"job_id": ",".join(job_ids[:20]), "country": country},
            headers={"x-api-key": key, "User-Agent": USER_AGENT}, timeout=TIMEOUT_S * 2)
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as exc:
        raise SourceError(f"JSearch job-details failed: {exc}") from exc


class JSearchSource(Source):
    platform = "jsearch"

    def parse(self, payload: Any, slug: str = "") -> list[RawPosting]:
        pages = payload if isinstance(payload, list) else [payload]
        out = []
        for page in pages:
            for j in page.get("data") or []:
                loc = j.get("job_location") or ", ".join(x for x in (j.get("job_city"), j.get("job_state")) if x)
                ranges = []
                if j.get("job_min_salary") or j.get("job_max_salary"):
                    ranges.append(CompRange(j.get("job_min_salary"), j.get("job_max_salary"), None,
                                            _PERIOD.get((j.get("job_salary_period") or "").upper()),
                                            label="JSearch"))
                text = j.get("job_description") or ""
                lines = [ln.strip() for ln in re.split(r"\n+", text) if ln.strip()]
                for name, items in (j.get("job_highlights") or {}).items():
                    lines.append("## " + name)
                    lines += [f"- {it}" for it in items or []]
                options = j.get("apply_options") or []
                url = next((o["apply_link"] for o in options if o.get("is_direct")), None) or j.get("job_apply_link") or ""
                emp = j.get("job_employment_type") or ", ".join(j.get("job_employment_types") or [])
                out.append(RawPosting(
                    platform=self.platform, slug=re.sub(r"[^a-z0-9]+", "-", (j.get("employer_name") or "").lower()).strip("-"),
                    native_id=str(j.get("job_id")), title=(j.get("job_title") or "").strip(), url=url,
                    locations=[loc] if loc else [], country=_COUNTRY.get((j.get("job_country") or "").upper()),
                    workplace_type="remote" if j.get("job_is_remote") else None,
                    employment_type=emp, description_lines=[ln if ln.startswith("## ") or ln.startswith("- ")
                                                            else ln for ln in lines],
                    comp_ranges=ranges, posted_at=j.get("job_posted_at_datetime_utc"),
                    company=j.get("employer_name"),
                    raw={"apply_links": [o.get("apply_link") for o in options] + [j.get("job_apply_link")],
                         "publisher": j.get("job_publisher"), "website": j.get("employer_website"),
                         "pay_basis": (j.get("job_salary_period") or "").lower() or None,
                         "job": j}))
        return out
