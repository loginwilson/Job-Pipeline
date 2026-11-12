"""Split description lines into sections; label required / preferred headings.

Heading phrases come from criteria.yaml experience.parse_sections. A heading
matches a phrase when the phrase's word stems appear in order in the heading
("What We Require" matches "requirements"; "Nice-to-haves" matches
"nice to have"). Headings that match nothing are kept as "other" and listed
in `unmapped_headings` so the owner can add them to parse_sections.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Criteria
from ..textmatch import contains_sequence, stems


@dataclass
class Section:
    heading: str
    kind: str                      # required / preferred / other / intro
    lines: list[str] = field(default_factory=list)


@dataclass
class SectionSplit:
    sections: list[Section]

    def lines(self, kind: str) -> list[str]:
        """Section lines; when a section has bullets, only the bullets are items."""
        out = []
        for s in self.sections:
            if s.kind != kind:
                continue
            bullets = [ln for ln in s.lines if ln.startswith("- ")]
            out.extend(bullets or s.lines)
        return out

    @property
    def unmapped_headings(self) -> list[str]:
        return [s.heading for s in self.sections if s.kind == "other"]

    def kind_of_line(self) -> list[tuple[str, str]]:
        return [(s.kind, ln) for s in self.sections for ln in s.lines]


def classify_heading(heading: str, criteria: Criteria) -> str:
    ps = criteria.get("experience", "parse_sections", default={}) or {}
    # "Skills & Experience" == "Skills and Experience": ignore "and" on both sides
    h = [w for w in stems(heading) if w != "and"]
    for kind in ("preferred", "required"):
        for phrase in ps.get(kind, []) or []:
            if contains_sequence(h, [w for w in stems(str(phrase)) if w != "and"]):
                return kind
    return "other"


def split_sections(lines: list[str], criteria: Criteria) -> SectionSplit:
    sections = [Section(heading="", kind="intro")]
    for line in lines:
        if line.startswith("## "):
            heading = line[3:].strip()
            sections.append(Section(heading=heading, kind=classify_heading(heading, criteria)))
        elif (line.strip().endswith(":") and len(line.strip()) <= 40 and not line.startswith("- ")
              and classify_heading(line.strip()[:-1], criteria) == "preferred"):
            # Nov 12 (Hebbia): a "Bonus:" / "Nice to have:" line inside a requirements list starts the
            # preferred part, even without heading formatting
            sections.append(Section(heading=line.strip()[:-1], kind="preferred"))
        else:
            sections[-1].lines.append(line)
    return SectionSplit(sections)


def item_text(line: str) -> str:
    text = line[2:] if line.startswith("- ") else line
    return text.strip().lstrip("•*\u2022·").strip()
