"""Review aid: the stored text of every open posting on To review that you haven't scored.

Writes review_dump.json (company, title, link, source, stored copy) so a person - or a
careful reader - can check what the rules judged, including job-site results whose link
(LinkedIn / Indeed) can't be opened.
"""
import json
import sys

from .store.db import Store


def main(path: str = "data/pipeline.db", out: str = "review_dump.json") -> None:
    store = Store(path)
    states = store.owner_states()
    rows = []
    for r in store.pipeline_rows():
        st = states.get(r["posting_id"], {})
        if r["closed_at"] or st.get("interest") or (st.get("decision") or "Pending") != "Pending":
            continue
        rows.append({"posting_id": r["posting_id"], "company": r["company"], "title": r["title"], "url": r["url"],
                     "passed": r["passed"], "snapshot": json.loads(r["snapshot"] or "null")})
    json.dump(rows, open(out, "w"), default=str)
    # every company board the pipeline reads (watchlist + discovered), for offline audits (Nov 12)
    from .config import load_watchlist
    boards = [{"company": c.name, "platform": c.platform, "slug": c.slug} for c in load_watchlist()]
    boards += [{"company": r["company"], "platform": r["platform"], "slug": r["slug"]} for r in store.discovered_boards()]
    json.dump(boards, open("boards.json", "w"))
    for b in boards:                       # also in the log (artifacts can't always be fetched)
        print(f"BOARD | {b['platform']} | {b['slug']} | {b['company']}")
    # near misses: open jobs in your cities and role types that a later rule dropped - read these
    # to catch good jobs the rules cut by mistake (Nov 9)
    early = ("field", "not_related_to_degree", "not_a_real_opening", "location", "employment_type",
             "title_language", "no_description", "specialist", "edge", "hotel_corporate_only")
    near = store.conn.execute(
        "SELECT p.posting_id, p.company, p.title, p.url, s.gate, s.matched FROM postings p JOIN scores s "
        "USING(posting_id) WHERE p.closed_at IS NULL AND p.merged_into IS NULL AND s.passed = 0 "
        f"AND COALESCE(s.gate, '') NOT IN ({','.join('?' * len(early))}) ORDER BY s.gate, p.company",
        early).fetchall()
    print(f"near misses: {len(near)}")
    for r in near:
        print(f"NEAR | {r['gate']} | {r['company']} | {r['title']} | {r['url']} | {(r['matched'] or '')[:200]}")
    # job-site results (LinkedIn / Indeed links can't be opened): print their stored text to the log
    for r in rows:
        if r["posting_id"].split(":")[0] in ("jsearch", "adzuna", "jobspipe", "serpapi", "muse", "coresignal"):
            job = (r["snapshot"] or {}).get("job", r["snapshot"]) or {}
            text = " ".join(str(job.get(k) or "") for k in ("description", "job_description", "contents",
                                                              "salary", "salary_min", "salary_max", "job_highlights"))
            print(f"=== REVIEW {r['posting_id']} | {r['company']} | {r['title']} | {r['url']}\n{text[:6000]}\n")
    print(f"review dump: {len(rows)} postings")


if __name__ == "__main__":
    main(*sys.argv[1:])
