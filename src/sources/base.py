"""RawPosting + the Source interface every job-board adapter implements."""
from __future__ import annotations

import gzip
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

USER_AGENT = "job-pipeline/1.0 (personal job-search tool; reads public job-board APIs only)"
TIMEOUT_S = 20
RETRIES = 3
BACKOFF_S = 2.0


@dataclass
class CompRange:
    """One structured pay range as the board API provides it."""
    min: float | None
    max: float | None
    currency: str | None
    period: str | None          # year / hour / month / week / None
    label: str | None = None    # e.g. a city or tier title, used for multi-range picking
    kind: str = "base"          # base / ote / unknown


@dataclass
class RawPosting:
    platform: str
    slug: str
    native_id: str
    title: str
    url: str
    team: str | None = None
    department: str | None = None
    locations: list[str] = field(default_factory=list)
    country: str | None = None              # ISO-2 if the API gives one
    workplace_type: str | None = None       # raw value from the API
    employment_type: str | None = None      # raw value from the API
    description_lines: list[str] = field(default_factory=list)  # structured text, "## " headings
    comp_ranges: list[CompRange] = field(default_factory=list)
    comp_extra: list[str] = field(default_factory=list)          # structured extra-comp signals
    posted_at: str | None = None            # ISO 8601
    company: str | None = None              # set by search sources (boards take it from the watchlist)
    raw: dict[str, Any] = field(default_factory=dict)


class SourceError(Exception):
    pass


class Source:
    platform = "base"

    def endpoint(self, slug: str) -> str:
        raise NotImplementedError

    def parse(self, payload: Any, slug: str) -> list[RawPosting]:
        raise NotImplementedError

    def fetch_payload(self, slug: str, session: requests.Session | None = None) -> Any:
        """One request per company per run, with retry + backoff."""
        url = self.endpoint(slug)
        sess = session or requests.Session()
        last_exc: Exception | None = None
        for attempt in range(RETRIES):
            try:
                resp = sess.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                                timeout=TIMEOUT_S)
                if resp.status_code == 404:
                    raise SourceError(f"{self.platform}:{slug} not found (404)")
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.json()
            except SourceError:
                raise
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                if attempt < RETRIES - 1:
                    time.sleep(BACKOFF_S * (2 ** attempt))
        raise SourceError(f"{self.platform}:{slug} failed after {RETRIES} attempts: {last_exc}")

    def fetch(self, slug: str, session: requests.Session | None = None) -> list[RawPosting]:
        return self.parse(self.fetch_payload(slug, session), slug)


def load_fixture(path: str | Path) -> Any:
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)
