"""Adzuna job search (free developer key: ADZUNA_APP_ID + ADZUNA_APP_KEY).

  GET https://api.adzuna.com/v1/api/jobs/{country}/search/{page}
      ?app_id&app_key&what&where&max_days_old&results_per_page&full_time=1&content-type=application/json
  -> {count, results[] {id, title, description (a ~500-character snippet), redirect_url,
      created, company {display_name}, location {display_name, area[]}, salary_min,
      salary_max, salary_is_predicted ("1" = Adzuna's estimate), contract_time, category {label}}}
The description is only a snippet, so a company found here also gets its own job board
looked up (discovery) and read in full from then on.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any

import requests

from .base import TIMEOUT_S, USER_AGENT, CompRange, RawPosting, Source, SourceError
from .jsearch import _aggregator

API = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"


def search(what: str, where: str, country: str = "us", max_days_old: int = 3, per_page: int = 50,
           session: requests.Session | None = None) -> dict:
    app_id, key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and key):
        raise SourceError("ADZUNA_APP_ID / ADZUNA_APP_KEY are not set")
    sess = session or requests.Session()
    params = {"app_id": app_id, "app_key": key, "what_or": what, "where": where, "max_days_old": max_days_old,
              "results_per_page": per_page, "full_time": 1, "sort_by": "date", "content-type": "application/json"}
    for attempt in range(3):
        try:
            r = sess.get(API.format(country=country, page=1), params=params, timeout=TIMEOUT_S * 2,
                         headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            if r.status_code in (401, 403):
                raise SourceError(f"Adzuna refused the key ({r.status_code}) — check ADZUNA_APP_ID / ADZUNA_APP_KEY")
            if r.status_code == 429:
                raise SourceError("Adzuna daily limit reached (429)")
            r.raise_for_status()
            return r.json()
        except SourceError:
            raise
        except (requests.RequestException, ValueError) as exc:
            if attempt == 2:
                raise SourceError(f"Adzuna failed: {exc}") from exc
            time.sleep(2 * (attempt + 1))
    raise SourceError("unreachable")


class AdzunaSource(Source):
    platform = "adzuna"

    def parse(self, payload: Any, slug: str = "") -> list[RawPosting]:
        out = []
        for j in (payload or {}).get("results") or []:
            company = ((j.get("company") or {}).get("display_name") or "").strip()
            if not company or _aggregator(company):
                continue                                   # a job site, not the employer
            ranges = []
            if (j.get("salary_min") or j.get("salary_max")) and str(j.get("salary_is_predicted")) != "1":
                ranges.append(CompRange(j.get("salary_min"), j.get("salary_max"), None, "year", label="Adzuna"))
            text = re.sub(r"\s+", " ", j.get("description") or "").strip()
            out.append(RawPosting(
                platform=self.platform, slug=re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-"),
                native_id=str(j.get("id")), title=(j.get("title") or "").strip(),
                url=j.get("redirect_url") or "",
                locations=[(j.get("location") or {}).get("display_name") or ""],
                country="US" if "/us/" in (j.get("redirect_url") or "") else None,
                employment_type="full_time" if j.get("contract_time") == "full_time" else j.get("contract_time"),
                description_lines=[text] if text else [], comp_ranges=ranges,
                posted_at=j.get("created"), company=company,
                raw={"category": (j.get("category") or {}).get("label"), "job": j}))
        return out
