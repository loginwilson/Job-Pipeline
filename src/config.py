"""Load config/criteria.yaml, config/reference.yaml and config/watchlist.yaml.

criteria.yaml is the single source of truth for every threshold, weight and
term list. Nothing in src/ hard-codes those values; modules read them through
the helpers here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


def load_yaml(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class Criteria:
    """Thin wrapper over criteria.yaml with weight resolution."""

    def __init__(self, data: dict):
        self.data = data
        self.weights: dict[str, float] = data.get("weights", {})

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, *path: str, default: Any = None) -> Any:
        node: Any = self.data
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def weight(self, value: Any) -> float:
        """Resolve 'mild', '-small', 'big_bump', 0, 10 ... to a number."""
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip()
        sign = -1.0 if text.startswith("-") else 1.0
        name = text.lstrip("-+").strip()
        if name in self.weights:
            return sign * float(self.weights[name])
        try:
            return float(text)
        except ValueError as exc:
            raise KeyError(f"Unknown weight name {value!r} in criteria.yaml") from exc

    # --- candidate facts derived from criteria text -------------------------
    @property
    def graduation(self) -> tuple[int, int]:
        """(year, month) the candidate graduates, read from timing.drop."""
        rule = str(self.get("timing", "drop", default=""))
        m = re.search(r"(\d{4})-(\d{2})", rule)
        if not m:
            raise ValueError("timing.drop must contain the graduation date as YYYY-MM")
        return int(m.group(1)), int(m.group(2))

    @property
    def gpa(self) -> float | None:
        blob = " ".join(
            [str(self.get("candidate_profile", "summary", default=""))]
            + [str(s) for s in self.get("candidate_profile", "skills", "have", default=[]) or []]
        )
        m = re.search(r"(\d\.\d{1,2})\s*GPA|GPA\s*(?:of\s*)?(\d\.\d{1,2})", blob)
        return float(m.group(1) or m.group(2)) if m else None


@lru_cache(maxsize=4)
def load_criteria(path: str | None = None) -> Criteria:
    return Criteria(load_yaml(Path(path) if path else CONFIG_DIR / "criteria.yaml"))


@lru_cache(maxsize=4)
def load_reference(path: str | None = None) -> dict:
    return load_yaml(Path(path) if path else CONFIG_DIR / "reference.yaml")


@dataclass
class Company:
    name: str
    platform: str
    slug: str
    anchor: bool = False
    category: str | None = None
    sectors: list[str] = field(default_factory=list)
    defense_primary: bool = False
    headcount: int | None = None
    stage: str | None = None
    careers_url: str | None = None


def load_watchlist(path: str | None = None) -> list[Company]:
    rows = load_yaml(Path(path) if path else CONFIG_DIR / "watchlist.yaml") or []
    companies = []
    for row in rows:
        companies.append(
            Company(
                name=row["name"],
                platform=row["platform"],
                slug=row["slug"],
                anchor=bool(row.get("anchor", False)),
                category=row.get("category"),
                sectors=list(row.get("sectors") or []),
                defense_primary=bool(row.get("defense_primary", False)),
                headcount=row.get("headcount"),
                stage=row.get("stage"),
                careers_url=row.get("careers_url"),
            )
        )
    return companies
