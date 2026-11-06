"""Compensation: structured ranges first, description regex as fallback.

Rules from criteria.yaml salary:
  * midpoint of the posted range; ranges by city -> the target city's range
  * hourly x hourly_to_annual_hours; part-time tested at full-time equivalent
  * never_count_as_base terms (OTE, commission ...) never count as base pay
  * extra_comp_signals found anywhere in the posting; unquantified equity flagged
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import Criteria
from ..sources.base import CompRange
from ..textmatch import find_terms, normalize, term_regex

_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_CUR = r"US\$|USD\s?\$?|CA\$|C\$|CAD\s?\$?|\$"
_MONEY = rf"(?P<c@>{_CUR})?\s?(?P<n@>{_NUM})\s?(?P<k@>[kKmM](?![a-zA-Z]))?\s?(?P<t@>USD|CAD)?"
_RANGE_RE = re.compile(
    _MONEY.replace("@", "1") + r"\s*(?:-|–|—|to|and)\s*" + _MONEY.replace("@", "2")
)
_SINGLE_RE = re.compile(_MONEY.replace("@", "1"))
_PERIOD_RE = [
    ("hour", re.compile(r"^\s*(?:/\s*(?:hour|hr)|per\s+hour|an\s+hour|hourly|/h\b)", re.I)),
    ("year", re.compile(r"^\s*(?:/\s*(?:year|yr|annum)|per\s+(?:year|annum)|a\s+year|annually|annual)", re.I)),
    ("month", re.compile(r"^\s*(?:/\s*(?:month|mo)|per\s+month|a\s+month|monthly)", re.I)),
    ("week", re.compile(r"^\s*(?:/\s*(?:week|wk)|per\s+week|weekly)", re.I)),
]
_PAY_WORDS = re.compile(r"\b(?:salary|salaries|base|pay|compensation|wage|rate)\b", re.I)
_HOURS_PW = re.compile(r"(\d{1,2})\s*(?:-\s*\d{1,2}\s*)?hours?\s*(?:per|a|/)\s*week", re.I)
_QUANT = re.compile(r"\d+(?:\.\d+)?\s*%|\$\s?\d")


@dataclass
class CompResult:
    min: float | None = None
    max: float | None = None
    currency: str | None = None
    period: str | None = None
    midpoint_annual: float | None = None
    source: str | None = None          # structured / text
    label: str | None = None
    matched_text: str | None = None
    ote_only: bool = False
    multi_range: bool = False
    multi_range_matched: bool = False
    extra_terms: list[str] = field(default_factory=list)
    has_equity: bool = False
    has_bonus: bool = False
    equity_unquantified: bool = False

    @property
    def known(self) -> bool:
        return self.midpoint_annual is not None

    @property
    def extra_comp_display(self) -> str:
        parts = []
        if self.has_equity:
            parts.append("Equity (unquantified)" if self.equity_unquantified else "Equity")
        if self.has_bonus:
            parts.append("Bonus")
        return " / ".join(parts) if parts else "—"

    def display(self) -> str:
        if not self.known:
            return "Unknown"
        sym = "$" if self.currency in (None, "USD") else "C$"
        lo, hi = self.min, self.max
        if self.period in ("hour", "day", "week", "month"):
            unit = {"hour": "/hr", "day": "/day", "week": "/wk", "month": "/mo"}[self.period]
            core = f"{sym}{lo:,.0f}–{sym}{hi:,.0f}{unit}" if lo != hi else f"{sym}{lo:,.0f}{unit}"
        else:
            core = f"{sym}{lo/1000:.0f}K–{sym}{hi/1000:.0f}K" if lo != hi else f"{sym}{lo/1000:.0f}K"
        return f"{core} (mid {sym}{self.midpoint_annual/1000:.0f}K)"


def _to_float(num: str, k: str | None) -> float:
    v = float(num.replace(",", ""))
    if k and k.lower() == "k":
        v *= 1000
    return v


def _period_after(text: str) -> str | None:
    for name, rx in _PERIOD_RE:
        if rx.search(text):
            return name
    return None


def _currency(sym: str | None, tail: str | None, market_currency: str | None) -> str | None:
    s = (sym or "").upper().replace(" ", "") + (tail or "").upper()
    if "CAD" in s or "C$" in s or "CA$" in s:
        return "CAD"
    if "USD" in s or "US$" in s:
        return "USD"
    if "$" in s:
        return market_currency or "USD"
    return None


@dataclass
class _Found:
    lo: float
    hi: float
    currency: str | None
    period: str | None
    label: str
    kind: str
    line: str


def _is_ote(text: str, criteria: Criteria) -> bool:
    terms = criteria.get("salary", "never_count_as_base", default=[]) or []
    return bool(find_terms(text, terms, first_only=True))


def find_text_ranges(lines: list[str], criteria: Criteria, market_currency: str | None) -> list[_Found]:
    found: list[_Found] = []
    for line in lines:
        text = line[3:] if line.startswith("## ") else line
        text = text.lstrip("- ")
        spans = []
        for m in _RANGE_RE.finditer(text):
            c1, c2 = m.group("c1"), m.group("c2")
            if not (c1 or c2 or m.group("t1") or m.group("t2")):
                continue  # need a currency marker on at least one side
            k1, k2 = m.group("k1"), m.group("k2")
            if (k1 and k1.lower() == "m") or (k2 and k2.lower() == "m"):
                continue  # funding amounts, not pay
            lo = _to_float(m.group("n1"), k1 or k2)
            hi = _to_float(m.group("n2"), k2 or k1)
            if hi < lo or lo <= 0:
                continue
            period = _period_after(text[m.end():m.end() + 25])
            if period is None and lo < 1000 and hi < 1000:
                period = "hour"
            if period is None and hi < 1000:
                continue
            spans.append(m.span())
            prefix = re.split(r"[.!?](?:\s|$)", text[:m.start()])[-1]
            suffix = text[m.end():m.end() + 20]
            kind = "ote" if _is_ote(prefix, criteria) or _is_ote(re.split(r"[,;]|\.(?:\s|$)", suffix)[0], criteria) else "base"
            found.append(_Found(lo, hi, _currency(c1 or c2, m.group("t1") or m.group("t2"), market_currency),
                                period or "year", prefix.strip()[-80:], kind, text))
        if spans or not _PAY_WORDS.search(text):
            continue
        for m in _SINGLE_RE.finditer(text):
            if not (m.group("c1") or m.group("t1")) or (m.group("k1") or "").lower() == "m":
                continue
            v = _to_float(m.group("n1"), m.group("k1"))
            period = _period_after(text[m.end():m.end() + 25])
            if v < 1000 and period != "hour":
                continue
            prefix = re.split(r"[.!?](?:\s|$)", text[:m.start()])[-1]
            if not _PAY_WORDS.search(prefix):
                continue
            kind = "ote" if _is_ote(prefix, criteria) else "base"
            found.append(_Found(v, v, _currency(m.group("c1"), m.group("t1"), market_currency),
                                period or "year", prefix.strip()[-80:], kind, text))
    return found


def _annual(value: float, period: str | None, criteria: Criteria) -> float:
    hours = float(criteria.get("salary", "hourly_to_annual_hours"))
    if period == "hour":
        return value * hours
    if period == "day":
        return value * hours / 8
    if period == "week":
        return value * 52
    if period == "month":
        return value * 12
    return value


def _label_matches_region(label: str | None, region: str | None, matcher) -> bool:
    if not label or not region or matcher is None:
        return False
    for seg in re.split(r"[/,;|&]|\bor\b", label):
        seg = seg.strip()
        if not seg:
            continue
        hit = matcher.match_component(seg)
        if hit and hit[1] == region:
            return True
        meta = matcher.region_meta.get(region, {})
        n = normalize(seg)
        if n in {normalize(s) for s in meta.get("states", [])}:
            return True
        if n in matcher.countries.get(meta.get("country"), []) and meta.get("country") != "US":
            return True
    return False


def parse_comp(lines: list[str], structured: list[CompRange], structured_extra: list[str],
               criteria: Criteria, market: str | None, region: str | None,
               employment_type: str | None, matcher=None) -> CompResult:
    markets = criteria.get("salary", "markets", default={}) or {}
    market_currency = (markets.get(market) or {}).get("currency") if market else None
    res = CompResult()

    candidates: list[_Found] = []
    for r in structured:
        if r.min is None and r.max is None:
            continue
        lo = r.min if r.min is not None else r.max
        hi = r.max if r.max is not None else r.min
        kind = r.kind
        if kind == "unknown":
            kind = "ote" if r.label and _is_ote(r.label, criteria) else "base"
        candidates.append(_Found(float(lo), float(hi), r.currency or market_currency,
                                 r.period or ("hour" if hi < 1000 else "year"),
                                 r.label or "", kind, f"structured: {r.label or ''} {lo:g}-{hi:g}"))
    if not any(c.kind == "base" for c in candidates):
        candidates += find_text_ranges(lines, criteria, market_currency)

    base = [c for c in candidates if c.kind == "base"]
    if not base and any(c.kind == "ote" for c in candidates):
        res.ote_only = True
        res.matched_text = next(c.line for c in candidates if c.kind == "ote")
    if base:
        res.multi_range = len({(c.lo, c.hi) for c in base}) > 1
        chosen = base[0]
        if res.multi_range:
            for c in base:
                if _label_matches_region(c.label, region, matcher) or _label_matches_region(c.line, region, matcher):
                    chosen = c
                    res.multi_range_matched = True
                    break
        res.min, res.max = chosen.lo, chosen.hi
        res.currency, res.period = chosen.currency, chosen.period
        res.label = chosen.label
        res.matched_text = chosen.line
        res.source = "structured" if chosen.line.startswith("structured") else "text"
        mid = _annual((chosen.lo + chosen.hi) / 2, chosen.period, criteria)
        if employment_type == "part_time" and chosen.period == "year":
            hpw = _HOURS_PW.search("\n".join(lines))
            fte_hours = float(criteria.get("salary", "hourly_to_annual_hours")) / 52
            if hpw and 0 < int(hpw.group(1)) < fte_hours:
                mid = mid * fte_hours / int(hpw.group(1))
        res.midpoint_annual = mid

    # extra comp
    signals = criteria.get("salary", "extra_comp_signals", default=[]) or []
    hits = find_terms(lines, signals)
    res.extra_terms = sorted({h.term for h in hits})
    equity_hits = [h for h in hits if not term_regex("bonus").search(normalize(h.term))]
    bonus_hits = [h for h in hits if term_regex("bonus").search(normalize(h.term))]
    res.has_bonus = bool(bonus_hits) or "bonus" in structured_extra
    res.has_equity = bool(equity_hits) or any(e.startswith("equity") for e in structured_extra)
    if res.has_equity:
        quantified = "equity" in structured_extra or any(_QUANT.search(h.line) and not
                                                         _RANGE_RE.search(h.line) for h in equity_hits)
        res.equity_unquantified = not quantified
        if "equity" in structured_extra or "equity_unquantified" in structured_extra:
            res.extra_terms = sorted(set(res.extra_terms) | {"equity (structured)"})
    return res
