"""Graduation-window and start-date parsing (criteria.yaml timing).

The candidate's graduation month comes from criteria timing.drop
("..._before_2027-05"). Seasons map to months: Spring = Jan-May,
Summer = Jun-Aug, Fall/Autumn = Sep-Dec, Winter = Dec-Feb.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})
_MONTHS["sept"] = 9
_SEASONS = {"spring": (1, 5), "summer": (6, 8), "fall": (9, 12), "autumn": (9, 12), "winter": (12, 2)}
_WHEN = r"(?:(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|spring|summer|fall|autumn|winter)\s*(?:of\s*)?)?(?P<year>20\d\d)"
_WHEN_RE = re.compile(_WHEN, re.IGNORECASE)
_GRAD_RE = re.compile(r"\b(?:graduat\w*|class of|degree (?:completion|conferral|by)|completing (?:your|a) degree)", re.I)
_START_RE = re.compile(r"\b(?:start(?:ing)?(?: date)?|begin(?:ning)?|commenc\w+|join(?:ing)? us|cohort|onboard\w*)\b", re.I)
_DEGREE_RE = re.compile(r"\b(?:bachelor'?s?|ba/bs|bs/ba|b\.?s\.?|b\.?a\.?|undergraduate|degree)\b", re.I)


@dataclass
class Window:
    start: tuple[int, int]
    end: tuple[int, int]
    text: str


@dataclass
class TimingResult:
    grad_windows: list[Window] = field(default_factory=list)
    starts: list[Window] = field(default_factory=list)
    degree_required: bool = False

    def earliest_start(self) -> Window | None:
        return min(self.starts, key=lambda w: w.start) if self.starts else None


def _span(mon: str | None, year: int) -> tuple[tuple[int, int], tuple[int, int]]:
    if not mon:
        return (year, 1), (year, 12)
    m = mon.lower()
    if m in _SEASONS:
        a, b = _SEASONS[m]
        if m == "winter":
            return (year, 12), (year + 1, 2)
        return (year, a), (year, b)
    num = _MONTHS.get(m) or _MONTHS.get(m[:3])
    return (year, num), (year, num)


def _windows(line: str) -> list[Window]:
    out = []
    for m in _WHEN_RE.finditer(line):
        a, b = _span(m.group("mon"), int(m.group("year")))
        out.append(Window(a, b, line.strip()))
    return out


def parse_timing(lines: list[str]) -> TimingResult:
    res = TimingResult()
    for raw in lines:
        line = raw.lstrip("-# ").strip()
        if not line:
            continue
        has_grad = bool(_GRAD_RE.search(line))
        has_start = bool(_START_RE.search(line))
        wins = _windows(line)
        if has_grad and wins:
            res.grad_windows.extend(wins)
        elif has_start and wins:
            res.starts.extend(wins)
        if _DEGREE_RE.search(line) and re.search(r"\b(?:require|must|need|minimum|completed|hold)\w*", line, re.I):
            res.degree_required = True
    return res


def grad_ok(windows: list[Window], grad: tuple[int, int]) -> bool | None:
    """True if the candidate's graduation falls in any listed window; None if none listed."""
    if not windows:
        return None
    return any(w.start <= grad <= w.end for w in windows)
