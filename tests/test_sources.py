from conftest import FIXTURES

from src.sources import get_source, load_fixture


def test_lever_palantir_fixture():
    raws = get_source("lever").parse(load_fixture(FIXTURES / "lever_palantir.json.gz"), "palantir")
    assert len(raws) == 309
    ds = next(r for r in raws if r.title == "Deployment Strategist" and r.locations == ["New York, NY"])
    assert ds.url.startswith("https://jobs.lever.co/palantir/")
    assert ds.team == "Echo" and ds.workplace_type == "hybrid" and ds.employment_type == "Full-time"
    assert ds.country == "US" and ds.posted_at
    assert "## What We Require" in ds.description_lines
    assert any("$110,000 - $170,000/year" in ln for ln in ds.description_lines)


def test_greenhouse_fixtures():
    gh = get_source("greenhouse")
    wc = gh.parse(load_fixture(FIXTURES / "greenhouse_withcoverage.json.gz"), "withcoverage")
    assert len(wc) == 19
    pm = next(r for r in wc if r.title == "Product Manager")
    assert pm.url.startswith("https://job-boards.greenhouse.io/withcoverage/jobs/")
    assert "New York, NY" in pm.locations
    assert pm.description_lines and not any("&lt;" in ln for ln in pm.description_lines)
    figma = gh.parse(load_fixture(FIXTURES / "greenhouse_figma.json.gz"), "figma")
    with_pay = [r for r in figma if r.comp_ranges]
    assert with_pay and with_pay[0].comp_ranges[0].min > 1000


def test_ashby_fixtures():
    ab = get_source("ashby")
    ramp = ab.parse(load_fixture(FIXTURES / "ashby_ramp.json.gz"), "ramp")
    assert len(ramp) == 170
    r = next(r for r in ramp if r.comp_ranges)
    assert r.comp_ranges[0].currency in ("USD", "CAD") and r.comp_ranges[0].period == "year"
    assert any(x.startswith("equity") for p in ramp for x in p.comp_extra)
    assert any(c.kind == "ote" for p in ramp for c in p.comp_ranges)
    elise = ab.parse(load_fixture(FIXTURES / "ashby_eliseai.json.gz"), "eliseai")
    assert any("New York City" in p.locations for p in elise)


def test_smartrecruiters_fixture():
    sr = get_source("smartrecruiters")
    payload = load_fixture(FIXTURES / "smartrecruiters_servicenow.json.gz")
    raws = sr.parse(payload, "ServiceNow")
    assert len(raws) == 100
    detailed = [r for r in raws if r.raw["has_detail"]]
    assert len(detailed) == len(payload["details"]) and detailed[0].url.startswith("https://jobs.smartrecruiters.com/")
    assert any(ln.startswith("## ") for ln in detailed[0].description_lines)
    assert all(r.company == "ServiceNow" and r.employment_type for r in raws)


def test_smartrecruiters_skips_inactive():
    item = {"id": "1", "name": "Analyst", "location": {"fullLocation": "New York, New York, United States"},
            "typeOfEmployment": {"label": "Full-time"}, "company": {"name": "Acme"}}
    raws = get_source("smartrecruiters").parse({"pages": [{"content": [item]}],
                                                "details": {"1": {"active": False}}}, "acme")
    assert raws == []
