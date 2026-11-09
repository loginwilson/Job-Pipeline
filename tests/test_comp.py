from conftest import CompRange


def test_range_midpoint_and_extra_comp(make):
    p = make(lines=["The salary range is $110,000 - $170,000/year. Total comp may include a sign-on bonus."])
    assert (p.comp.min, p.comp.max, p.comp.midpoint_annual) == (110000, 170000, 140000)
    assert p.comp.has_bonus and p.comp.extra_comp_display == "Bonus"


def test_k_notation_and_equity_unquantified(make):
    p = make(lines=["Base pay: $130K–$160K plus competitive equity"])
    assert p.comp.midpoint_annual == 145000
    assert p.comp.has_equity and p.comp.equity_unquantified


def test_hourly_annualized(make, criteria):
    p = make(lines=["The estimated salary range is $32 - $35/hour."])
    assert p.comp.period == "hour"
    assert p.comp.midpoint_annual == 33.5 * criteria.get("salary", "hourly_to_annual_hours")


def test_ote_never_base(make, evaluate):
    p = make(lines=["OTE: $150,000 - $200,000 with uncapped commission."])
    assert p.comp.ote_only and not p.comp.known
    ev = evaluate(lines=["OTE: $150,000 - $200,000 with uncapped commission."])
    assert ev.passed and "Salary unknown (OTE only)" in ev.flags     # kept; commission is an incentive
    p2 = make(lines=["Base salary $90,000 - $100,000. OTE $180,000 - $200,000."])
    assert p2.comp.midpoint_annual == 95000


def test_cad_market(make):
    p = make(locations=["Toronto, ON"], lines=["Salary: $150,000 - $170,000 per year"])
    assert p.comp.currency == "CAD" and p.location.market == "CA"


def test_multi_city_structured_ranges(make):
    comp = [CompRange(200000, 240000, "USD", "year", label="SF/NY"),
            CompRange(150000, 170000, "USD", "year", label="Chicago")]
    p = make(locations=["Chicago, IL"], comp=comp)
    assert p.comp.midpoint_annual == 160000 and p.comp.multi_range_matched


def test_multi_city_text_ranges(make):
    lines = ["San Francisco: $180,000 - $200,000", "New York City: $170,000 - $190,000"]
    assert make(locations=["New York, NY"], lines=lines).comp.midpoint_annual == 180000


def test_funding_amounts_ignored(make):
    p = make(lines=["We raised $50M - $60M from top investors."])
    assert not p.comp.known


def test_part_time_full_time_equivalent(make):
    p = make(employment="Part-time", lines=["Salary $60,000 per year for 20 hours per week"])
    assert p.comp.midpoint_annual == 120000


def test_pay_written_the_ways_postings_actually_write_it(criteria):
    """Nov 9: Meow, EliseAI SDR and Fora showed 'Unknown' though each posting states its pay."""
    from src.pipeline.comp import parse_comp
    pay = lambda t: parse_comp([t], [], [], criteria, "US", None, "full_time", None)
    assert pay("$90K base with $150K OTE, uncapped and revenue-based.").midpoint_annual == 90000
    r = pay("The salary for this role is $60,000 - 70,000 (OTE $108,600 - 118,600).")
    assert (r.min, r.max, r.ote_only) == (60000, 70000, False)
    assert pay("Annual salary is $90k + equity.").midpoint_annual == 90000
    assert not pay("$120K OTE").known
