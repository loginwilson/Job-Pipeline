"""Auto-discovery: a company found by a search source gets its own job board watched.

For each new company name, try likely board slugs on Greenhouse, Lever,
Ashby and SmartRecruiters. A board is accepted only if it actually lists the job that led us
there (title fuzzy-match >= reference.yaml matching.fuzzy_min_score), so a
slug that happens to belong to a different company is never added.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import requests
from rapidfuzz import fuzz

from ..config import load_reference
from ..sources import get_source
from ..sources.base import SourceError

log = logging.getLogger(__name__)
PLATFORMS = ("greenhouse", "lever", "ashby", "smartrecruiters")
_SUFFIX = re.compile(r"\b(?:inc|llc|ltd|corp|corporation|co|company|group|holdings|technologies|technology|labs|hq)\b\.?",
                     re.I)


@dataclass
class Found:
    company: str
    platform: str
    slug: str
    postings: int
    matched_title: str


def candidate_slugs(name: str, hint: str | None = None) -> list[str]:
    base = _SUFFIX.sub(" ", name.lower().replace("&", " and "))
    words = re.findall(r"[a-z0-9]+", base)
    cands = []
    if hint:
        cands += [hint.lower(), hint.lower().replace("-", "")]
    if words:
        cands += ["".join(words), "-".join(words)]
        if len(words) > 1 and len(words[0]) > 3:
            cands.append(words[0])
    return [c for c in dict.fromkeys(cands) if len(c) >= 2]


def _titles(platform: str, payload, slug: str) -> list[str]:
    raws = get_source(platform).parse(payload, slug)
    return [r.title for r in raws]


def find_board(company: str, titles: list[str], hint: str | None = None,
               session: requests.Session | None = None, min_score: int | None = None) -> Found | None:
    min_score = min_score or int(load_reference()["matching"]["fuzzy_min_score"])
    sess = session or requests.Session()
    for slug in candidate_slugs(company, hint):
        for platform in PLATFORMS:
            src = get_source(platform)
            try:
                payload = (src.fetch_payload_list(slug, sess) if platform in ("smartrecruiters", "workday")
                           else src.fetch_payload(slug, sess))
            except Exception:
                continue
            try:
                board_titles = _titles(platform, payload, slug)
            except Exception as exc:      # a malformed board never stops discovery
                log.warning("discovery: %s:%s unreadable (%s)", platform, slug, exc)
                continue
            if not board_titles:
                continue
            for want in titles:
                best = max(board_titles, key=lambda t: fuzz.token_sort_ratio(want, t))
                if fuzz.token_sort_ratio(want, best) >= min_score:
                    return Found(company, platform, slug, len(board_titles), best)
    return None


_LINK_PATTERNS = [
    ("greenhouse", re.compile(r"(?:job-)?boards(?:-api)?\.greenhouse\.io/(?:embed/job_app\?for=)?([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"greenhouse\.io/.*[?&]for=([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_.-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)")),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
]
_WORKDAY = re.compile(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")


def board_from_url(url: str) -> tuple[str, str] | None:
    """('greenhouse', slug) etc. for a link into a known job board; ('workday', 'tenant.wdN/Site')."""
    if not url:
        return None
    for platform, rx in _LINK_PATTERNS:
        m = rx.search(url)
        if m and m.group(1).lower() not in ("embed", "jobs", "v1"):
            return platform, m.group(1)
    m = _WORKDAY.search(url)
    if m:
        return "workday", f"{m.group(1)}.{m.group(2)}/{m.group(3)}"
    return None


_GH_JOB_ID = re.compile(r"[?&](?:gh_jid|token)=(\d{5,})")
GREENHOUSE_JOB = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"


def greenhouse_behind(url: str, company: str, session: requests.Session | None = None) -> str | None:
    """A company career page that embeds Greenhouse ('stripe.com/jobs?gh_jid=123') → the board
    slug, found by trying the likely slugs and keeping the one that has that exact job."""
    m = _GH_JOB_ID.search(url or "")
    if not m:
        return None
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower().removeprefix("www.").removeprefix("careers.").removeprefix("jobs.")
    hint = host.split(".")[0] if host and "greenhouse" not in host else None
    sess = session or requests.Session()
    base = candidate_slugs(company, hint)
    words = re.findall(r"[a-z0-9]+", _SUFFIX.sub(" ", company.lower()))
    extra = ["".join(w[0] for w in words) if len(words) > 1 else ""] + [b + suf for b in base[:2] for suf in ("us", "careers")]
    for slug in [c for c in dict.fromkeys(base + extra) if len(c) >= 2][:10]:
        try:
            r = sess.get(GREENHOUSE_JOB.format(slug=slug, job_id=m.group(1)), timeout=15,
                         headers={"User-Agent": "job-pipeline"})
        except requests.RequestException:
            continue
        if r.status_code == 200:
            return slug
    return None


def verify_board(platform: str, slug: str, session: requests.Session | None = None) -> int:
    """Number of postings on the board (0 if it doesn't exist)."""
    src = get_source(platform)
    try:
        payload = (src.fetch_payload_list(slug, session) if platform in ("smartrecruiters", "workday")
                   else src.fetch_payload(slug, session))
    except Exception:
        return 0
    try:
        return len(_titles(platform, payload, slug))
    except Exception as exc:
        log.warning("discovery: %s:%s unreadable (%s)", platform, slug, exc)
        return 0
