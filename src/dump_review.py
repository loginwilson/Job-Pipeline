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
    # job-site results (LinkedIn / Indeed links can't be opened): print their stored text to the log
    for r in rows:
        if r["posting_id"].split(":")[0] in ("jsearch", "adzuna", "jobspipe", "serpapi", "muse"):
            job = (r["snapshot"] or {}).get("job", r["snapshot"]) or {}
            text = " ".join(str(job.get(k) or "") for k in ("description", "job_description", "contents",
                                                              "salary", "salary_min", "salary_max", "job_highlights"))
            print(f"=== REVIEW {r['posting_id']} | {r['company']} | {r['title']} | {r['url']}\n{text[:6000]}\n")
    print(f"review dump: {len(rows)} postings")


if __name__ == "__main__":
    main(*sys.argv[1:])
