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


def test_oracle_parse_and_link():
    from src.resolve.discover import board_from_url
    from src.sources.oracle import OracleSource
    assert board_from_url("https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210677412") \
        == ("oracle", "jpmc.fa.oraclecloud.com/CX_1001")
    payload = {"pages": [{"requisitionList": [
        {"Id": "1", "Title": "Analyst", "PostedDate": "2026-10-30", "PrimaryLocation": "New York, NY, United States",
         "PrimaryLocationCountry": "US", "secondaryLocations": [{"Name": "Jersey City, NJ, United States"}]}]}],
        "details": {"1": {"ExternalDescriptionStr": "<p>Do things</p>",
                          "ExternalQualificationsStr": "<ul><li>Bachelor's degree</li></ul>",
                          "JobSchedule": "Full time", "WorkplaceType": "Hybrid"}}}
    r = OracleSource().parse(payload, "jpmc.fa.oraclecloud.com/CX_1001")[0]
    assert r.url.endswith("/sites/CX_1001/job/1") and r.workplace_type == "hybrid"
    assert r.locations == ["New York, NY, United States", "Jersey City, NJ, United States"]
    assert "- Bachelor's degree" in r.description_lines and r.employment_type == "Full time"


def test_workable_parse_and_link():
    from src.resolve.discover import board_from_url
    from src.sources.workable import WorkableSource
    assert board_from_url("https://apply.workable.com/tickpick/j/5840ECEB50/apply") == ("workable", "tickpick")
    assert board_from_url("https://apply.workable.com/j/5840ECEB50") is None
    payload = {"name": "TickPick", "jobs": [{"title": "Operations Associate", "shortcode": "5840ECEB50",
               "employment_type": "Full-time", "telecommuting": False, "url": "https://apply.workable.com/tickpick/j/5840ECEB50/",
               "published_on": "2026-11-01", "country": "United States", "city": "New York", "state": "New York",
               "locations": [{"country": "United States", "city": "New York", "region": "New York"}],
               "description": "<p>About</p><ul><li>Bachelor's degree</li></ul>"}]}
    r = WorkableSource().parse(payload, "tickpick")[0]
    assert r.locations == ["New York, New York, United States"] and r.country == "US"
    assert r.native_id == "5840ECEB50" and "- Bachelor's degree" in r.description_lines


def test_jsearch_skips_reposting_sites():
    """Nov 9: trabajo.org / Jobrapido are reposting sites - never the link, never the employer."""
    from src.sources.jsearch import JSearchSource
    job = lambda emp, links: {"job_id": emp, "job_title": "Operations Associate", "employer_name": emp,
                              "job_apply_link": links[0], "job_city": "New York", "job_state": "NY",
                              "apply_options": [{"apply_link": u, "is_direct": False} for u in links]}
    out = JSearchSource.__new__(JSearchSource).parse({"data": [
        job("Jobrapido", ["https://us.jobrapido.com/1"]),
        job("Skylight", ["https://us.trabajo.org/job-1", "https://jobs.ashbyhq.com/skylight/1"])]})
    assert [(r.company, r.url) for r in out] == [("Skylight", "https://jobs.ashbyhq.com/skylight/1")]


def test_gem_rippling_comeet_boards():
    """Nov 9: Bilt / Spotnana (Gem), Bowery / TheGuarantors (Rippling), Guesty / Buildots (Comeet)."""
    from src.resolve.discover import board_from_url
    from src.sources.comeet import ComeetSource
    from src.sources.gem import GemSource
    from src.sources.rippling import RipplingSource
    assert board_from_url("https://jobs.gem.com/bilt") == ("gem", "bilt")
    assert board_from_url("https://ats.rippling.com/bowery-valuation/jobs") == ("rippling", "bowery-valuation")
    assert board_from_url("https://www.comeet.com/jobs/guesty/10.000") == ("comeet", "guesty/10.000")
    g = GemSource().parse([{"id": "1", "title": "Strategy Associate", "absolute_url": "https://jobs.gem.com/bilt/1",
                            "content": "<p>Own projects.</p>", "location": {"name": "New York, United States"},
                            "offices": [{"name": "NYC", "location": {"name": "New York, United States"}}],
                            "location_type": "in_office"}], "bilt")[0]
    assert (g.title, g.locations, g.description_lines) == ("Strategy Associate", ["New York, United States"],
                                                           ["Own projects."])
    r = RipplingSource().parse({"items": [{"id": "a", "name": "Associate, Valuation", "url": "https://x/a",
                                           "locations": [{"name": "New York, NY", "workplaceType": "ON_SITE"}],
                                           "detail": {"description": {"company": "<p>Who we are</p>",
                                                                      "role": "<p>You will value buildings.</p>"}}}]},
                               "bowery-valuation")[0]
    assert r.locations == ["New York, NY"] and "You will value buildings." in r.description_lines
    c = ComeetSource().parse([{"uid": "2F.276", "name": "Market Manager", "url_active_page": "https://x/2F.276",
                               "location": {"city": "New York", "state": "NY", "country": "US"},
                               "custom_fields": {"details": [{"name": "Requirements", "order": 2,
                                                              "value": "<ul><li>2+ years</li></ul>"},
                                                             {"name": "Description", "order": 1,
                                                              "value": "<p>Grow our market.</p>"}]}}], "guesty/10.000")[0]
    assert c.locations == ["New York, NY, US"]
    assert c.description_lines[0] == "## Description" and "## Requirements" in c.description_lines


def test_yc_startups_in_your_cities_become_board_guesses(criteria):
    from src.pipeline.locations import LocationMatcher
    from src.run import yc_candidates
    yc = [{"name": "Canary Technologies", "website": "https://www.canarytechnologies.com",
           "all_locations": "New York, NY, USA"},
          {"name": "RADAR", "website": "https://goradar.com", "all_locations": "New York, NY, USA"},
          {"name": "Faraway Co", "website": "https://faraway.io", "all_locations": "Berlin, Germany"}]
    c = yc_candidates(yc, LocationMatcher(criteria))
    assert ("ashby", "canarytechnologies") in c and ("lever", "canarytechnologies") in c
    assert ("ashby", "goradar") in c and ("ashby", "radar") not in c      # generic name: website name only
    assert not any(v == "Faraway Co" for v in c.values())                 # not in your cities
