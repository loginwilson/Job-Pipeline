"""Match posting locations to criteria.yaml location tiers.

City lists and tier points come from criteria.yaml (location.tiers). Region
aliases ("New York" for NYC) and state/province/country qualifiers come from
config/reference.yaml so "Richmond, VA" is not read as Richmond, BC.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import Criteria, load_reference
from ..textmatch import normalize, term_regex


@dataclass
class OfficeMatch:
    region: str
    city: str
    tier: int
    country: str
    raw: str


@dataclass
class LocationResult:
    offices: list[OfficeMatch] = field(default_factory=list)
    remote_listed: bool = False
    unmatched: list[str] = field(default_factory=list)
    arrangement: str = "unknown"      # in_office / hybrid / remote / unknown
    fully_remote: bool = False

    @property
    def best(self) -> OfficeMatch | None:
        return min(self.offices, key=lambda o: o.tier) if self.offices else None

    @property
    def market(self) -> str | None:
        return self.best.country if self.best else None


_SEGMENT_SPLIT = re.compile(r"\s*(?:•|;|\||/|\n|\bor\b|\s&\s)\s*", re.IGNORECASE)


class LocationMatcher:
    def __init__(self, criteria: Criteria, reference: dict | None = None):
        ref = (reference or load_reference())["geo"]
        self.remote_words = ref.get("remote_words", [])
        self.countries = {code: [normalize(n) for n in names] for code, names in ref["countries"].items()}
        self.state_tokens = {normalize(s) for s in ref["us_states"] + ref["ca_provinces"]}
        self.region_meta = ref["regions"]
        self.names: list[tuple[str, str, str, int]] = []  # (name, region, display city, tier)
        for tier_key, tier in (criteria.get("location", "tiers") or {}).items():
            for region, cities in tier.items():
                if region == "points":
                    continue
                meta = self.region_meta.get(region)
                if meta is None:
                    raise KeyError(f"config/reference.yaml geo.regions is missing {region!r}")
                for city in cities:
                    self.names.append((city, region, city, int(tier_key)))
                for alias in meta.get("aliases", []):
                    self.names.append((alias, region, region, int(tier_key)))
        self.names.sort(key=lambda n: -len(n[0]))

    # -- helpers -----------------------------------------------------------
    def _is_qualifier(self, comp: str) -> bool:
        n = normalize(comp)
        return n in self.state_tokens or any(n in names for names in self.countries.values())

    def _qualifier_ok(self, comp: str, region: str) -> bool:
        meta = self.region_meta[region]
        n = normalize(comp)
        if n in {normalize(s) for s in meta.get("states", [])}:
            return True
        return n in self.countries.get(meta["country"], [])

    def _country_of(self, comp: str) -> str | None:
        n = normalize(comp)
        for code, names in self.countries.items():
            if n in names:
                return code
        return None

    def is_remote(self, text: str) -> bool:
        return any(term_regex(w).search(normalize(text, lower=False)) for w in self.remote_words)

    def match_component(self, comp: str):
        for name, region, city, tier in self.names:
            if term_regex(name).search(normalize(comp, lower=False)):
                return name, region, city, tier
        return None

    # -- main --------------------------------------------------------------
    def match(self, locations: list[str], country: str | None = None,
              workplace_type: str | None = None, description: str = "") -> LocationResult:
        res = LocationResult()
        single = len(locations) == 1
        for loc in locations:
            if not loc or not loc.strip():
                continue
            if self.is_remote(loc):
                res.remote_listed = True
            found = False
            stripped = re.sub(r"\([^)]*\)", " ", loc)
            for seg in _SEGMENT_SPLIT.split(stripped):
                comps = [c.strip(" .-") for c in seg.split(",")]
                comps = [c for c in comps if c]
                current = None  # (name, region, city, tier, ok)
                pending: list[OfficeMatch] = []
                for i, comp in enumerate(comps):
                    if i > 0 and self._is_qualifier(comp):
                        if current is not None and not self._qualifier_ok(comp, current[1]):
                            current = None
                            if pending:
                                pending.pop()
                        continue
                    hit = self.match_component(comp)
                    if hit and not self.is_remote(comp.replace(hit[0], "")):
                        current = hit
                        pending.append(OfficeMatch(hit[1], hit[2], hit[3],
                                                   self.region_meta[hit[1]]["country"], loc.strip()))
                    else:
                        current = None
                for office in pending:
                    if single and country and country != office.country:
                        continue
                    res.offices.append(office)
                    found = True
            if not found and not self.is_remote(loc):
                res.unmatched.append(loc.strip())

        wt = normalize(workplace_type or "").replace(" ", "")
        if wt in ("onsite", "inoffice", "office"):
            res.arrangement = "in_office"
        elif wt == "hybrid":
            res.arrangement = "hybrid"
        elif wt == "remote":
            res.arrangement = "remote"
        else:
            if res.remote_listed and not res.offices:
                res.arrangement = "remote"
            elif re.search(r"\bhybrid\b", description, re.IGNORECASE):
                res.arrangement = "hybrid"
            elif re.search(r"\b(?:on[- ]?site|in[- ]office|in[- ]person)\b", description, re.IGNORECASE):
                res.arrangement = "in_office"
        # "solely remote" = remote with none of the criteria cities listed; "Remote, New York, NY" counts as NYC
        res.fully_remote = res.arrangement == "remote" and not res.offices
        return res
