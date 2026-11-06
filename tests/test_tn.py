from conftest import PAY_OK

from src.pipeline.tn import map_tn


def test_hotel_manager_likely(make, criteria):
    p = make(title="Hotel Operations Associate", sectors=["hotel company"],
             lines=["## About the role", "Oversee property operations, budgets and guest satisfaction.", PAY_OK])
    tn = map_tn(p, criteria)
    assert tn.category == "hotel_manager" and tn.fit == "likely"
    assert tn.label == "TN: Hotel Manager (likely)"


def test_hotel_unclear_is_verify(make, criteria):
    p = make(title="Hotel Asset Management Analyst", sectors=["hotel company"], lines=[PAY_OK])
    tn = map_tn(p, criteria)
    assert tn.category == "hotel_manager" and tn.fit == "possible"
    assert any("verify" in f for f in tn.flags)


def test_hotel_needs_hotel_employer(make, criteria):
    p = make(lines=["Oversee property operations, budgets and guest satisfaction."], sectors=["proptech"])
    assert map_tn(p, criteria).category != "hotel_manager"


def test_management_consultant_by_duties(make, criteria):
    p = make(title="Business Analyst", lines=["## The role", "Deliver recommendations on client engagements.", PAY_OK])
    tn = map_tn(p, criteria)
    assert tn.category == "management_consultant" and tn.fit == "possible"


def test_unlikely_and_canada(make, criteria):
    tn = map_tn(make(lines=[PAY_OK]), criteria)
    assert tn.fit == "unlikely" and tn.label == "TN: unlikely"
    ca = map_tn(make(locations=["Toronto, ON"]), criteria)
    assert ca.label == "No visa needed" and not ca.applies


def test_pre_graduation_start_flag(make, criteria):
    p = make(lines=[PAY_OK, "Start date: January 2027"])
    assert any("before degree" in f for f in map_tn(p, criteria).flags)
