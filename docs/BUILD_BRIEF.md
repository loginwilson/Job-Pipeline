# Build Brief — Job Pipeline v1 (Live Google Sheet)

> **For Claude Code.** Read this file, `criteria.yaml`, and `ARCHITECTURE.md` before writing any code. This brief covers Phases 0–1 of ARCHITECTURE.md plus the Google Sheet output. Build exactly this scope; leave later phases as stubs.

> **v1.1 (2026-11-05) — ranking changed.** The pipeline now ranks postings by **how well the owner meets their requirements**, not by role/title. Hard constraints (salary, location, work authorization, gov/defense, seniority, years of experience, employment type) are unchanged gates. Role type is a label the owner filters on; he decides what's worth applying to. Where this brief and `criteria.yaml` disagree, `criteria.yaml` wins.

## Goal

A pipeline that runs every hour, pulls job postings from company job boards, applies `criteria.yaml`, and keeps a **Google Sheet** continuously updated — one row per posting, with a **direct clickable link to the posting**, ranked best-first. The owner works applications straight from the sheet.

## Stack

- Python 3.11+
- `requests`, `pyyaml`, `gspread`, `google-auth`, `python-dateutil`, `rapidfuzz` (dedupe)
- SQLite (`data/pipeline.db`) as the source of truth; the Sheet is a view of it
- GitHub Actions for the hourly schedule
- `pytest` for tests

## Repo layout

```
job-pipeline/
├── config/
│   ├── criteria.yaml          # provided — do not change semantics
│   └── watchlist.yaml         # companies to poll (you create + seed)
├── src/
│   ├── sources/
│   │   ├── base.py            # RawPosting dataclass + Source interface
│   │   ├── greenhouse.py
│   │   ├── lever.py
│   │   └── ashby.py
│   ├── pipeline/
│   │   ├── normalize.py       # RawPosting → Posting (schema below)
│   │   ├── comp.py            # salary parsing + midpoint + currency
│   │   ├── dedupe.py
│   │   ├── gates.py           # every pass/fail rule in criteria.yaml
│   │   ├── signals.py         # rule-based points
│   │   └── tiers.py
│   ├── store/db.py            # SQLite schema + upserts
│   ├── output/sheet.py        # Google Sheet sync
│   └── run.py                 # entry point: python -m src.run
├── tests/
│   ├── fixtures/              # saved real API responses
│   └── test_*.py
├── data/                      # gitignored except .gitkeep
├── .github/workflows/hourly.yml
├── requirements.txt
└── README.md                  # setup steps for the owner
```

## Step 1 — Sources (company job boards)

Implement one adapter per platform, each returning a list of `RawPosting`. **Before coding each adapter, call the live endpoint once and confirm its current response shape;** save that response to `tests/fixtures/` and code against what's actually returned.

Expected public endpoints (verify):
- **Greenhouse:** `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true`
- **Lever:** `https://api.lever.co/v0/postings/{slug}?mode=json`
- **Ashby:** `https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true`

For each, capture: native ID, title, team/department, location(s), workplace type if given, employment type, full description (HTML → text, keep section headings), structured compensation if the API provides it, posted/updated date, and **the public posting URL** (the page a human applies on).

Rules:
- Use a descriptive User-Agent, 1 request per company per run, retry with backoff, timeout 20s.
- Only these public job-board APIs. No scraping LinkedIn, Indeed, or any site requiring login.
- A board that errors or returns zero postings when it previously had some gets logged to board health (see Sheet tabs).

## Step 1b — Alert-email reader (v1.1: in scope for v1)

Job sites that forbid scraping (LinkedIn, Indeed, ZipRecruiter, Glassdoor, Handshake, plus Built In, Wellfound, YC Work at a Startup, Hcareers, eFinancialCareers) email alerts to a **dedicated Gmail inbox**. See `ALERTS_SETUP.md` for what the owner sets up.

- `src/sources/alerts.py`: connect over **IMAP (read-only, `imaplib`, `EXAMINE` not `SELECT`)** with secrets `ALERTS_IMAP_USER` and `ALERTS_IMAP_APP_PASSWORD`. Never delete, move, flag or send mail. Track processed emails by Message-ID in the DB.
- One parser per sender domain in `src/sources/alert_parsers/`, each returning `(title, company, location, link)` items. Build parsers from real saved emails in `tests/fixtures/alerts/` — **wait until the owner has samples; don't guess formats**. Unknown sender → log it to a new Sheet tab `Unparsed alerts`.
- **Resolve each item to the company's own posting:**
  1. Unwrap tracking/redirect links. If the final URL is a Greenhouse/Lever/Ashby posting, fetch it with that adapter.
  2. Otherwise, find the company on the watchlist, or detect its board from its careers page; fuzzy-match the title (rapidfuzz ≥ 85) and location against that board's postings.
  3. Resolved → normal pipeline, and **add the company to the watchlist** (flywheel).
  4. Unresolved → store an alert-only record: original link, flag `From alert — unverified`, scored on title/company/location only, tier capped at Worth a look.
- **Never fetch LinkedIn, Indeed, ZipRecruiter, Glassdoor or Handshake job pages** — only parse the emails.
- Dedupe alert items against board postings (same company + normalized title + city).
- Runs hourly with the board pollers.

## Step 2 — Watchlist

`config/watchlist.yaml`:
```yaml
- name: Palantir
  platform: lever
  slug: palantir
  anchor: true
- name: WithCoverage
  platform: TBD     # detect: find their careers page and identify the platform
  slug: TBD
  anchor: true
```

Then **seed ~150 more companies** following `discovery.watchlist_seed_mix` in criteria.yaml: AI / data / automation, proptech and CRE data, real estate finance, hospitality and hospitality tech, and consulting/advisory — all with offices in the criteria's tier-1 or tier-2 cities. Larger firms in real estate finance, consulting and hospitality often use Workday or custom career sites; if a company has no Greenhouse/Lever/Ashby board, list it in `watchlist_pending.yaml` (name + careers URL) for a later Workday adapter rather than dropping it. For every company you add to the main watchlist, **verify the slug resolves** against the live endpoint before keeping it. Never guess slugs.

## Step 3 — Normalize to one Posting schema

| Field | Notes |
|---|---|
| `posting_id` | `{platform}:{slug}:{native_id}` |
| `company`, `anchor` | from watchlist |
| `title`, `team` | |
| `locations` | list of strings, plus matched city + tier |
| `work_arrangement` | in_office / hybrid / remote / unknown |
| `employment_type` | full_time / part_time / contract / internship / unknown |
| `comp_min`, `comp_max`, `comp_currency`, `comp_period`, `comp_midpoint_annual` | structured field first; regex on description as fallback; ranges by city → pick target-city range |
| `extra_comp` | equity / bonus terms found; `equity_unquantified` flag |
| `description_text`, `required_text`, `preferred_text` | sections split per criteria `parse_sections` |
| `url` | direct public posting link |
| `posted_at`, `first_seen`, `last_seen`, `closed_at` | |

## Step 4 — Gates, signals, tiers

Implement **every** rule in `criteria.yaml` exactly as written:
- `gates.py`: employment type, salary (midpoint test, extra-comp exception, OTE never base, hourly × 2080, CAD thresholds for Canadian roles), seniority, experience (required vs preferred), location tier + work arrangement + multi-location best tier, work authorization (US roles only), government/defense exclusion, `roles.drop_titles.engineering` (any title containing "engineer", plus the `also` list), and `requirements_match.hard_gap_drop`. Sales titles are **not** gated — they pass with role type `Sales` and take the `final_score.role_preference` penalty. **Each failure records the gate name and the matched text.**
- `requirements.py` (new, v1.1 — the main ranking driver): split `required_text` and `preferred_text` into individual requirement lines; classify each as met / stretch / gap using `candidate_profile.skills` and `requirement_synonyms` (keyword + fuzzy matching in v1; the AI reviewer refines this in Phase 2); unmatched requirements default to **"unclear"** and count as half-credit stretch, never as gaps. Produce the 0–100 match score and the summary string (`Met 6/8 · Stretch: SQL · Gap: none`).
- `signals.py`: now tie-breakers only — lane match, core signals, sales flags (label at ≥2, never drop), technical tolerance, product-design signals, sector bonus, location tier, timing, experience penalties and field-match offset, company upside, salary bump. Assign `role_type` from `roles.role_type_labels` by title/keyword rules.
- `tiers.py`: compute `final_score` with the weights in `final_score.weights` (requirements 70 / experience 15 / signals 15) and assign tiers per `tiers`: **Strong ≥ 70 with no required gaps, Worth a look ≥ 50, Stored** otherwise. Anchors always appear in Worth a look or higher if they pass gates. Put the AI review step behind a stub function `ai_review(posting) -> None` for Phase 2.

Keep all thresholds and term lists read from `criteria.yaml` — nothing hard-coded.

## Step 5 — SQLite store

Tables: `companies`, `postings`, `scores`, `runs` (funnel counts per run), `board_health`.
- Upsert postings by `posting_id`. First time seen → set `first_seen`, mark `is_new`.
- Posting missing from its board this run → set `closed_at`. Reappears → clear it.
- Open longer than `evergreen_after_days` → `evergreen` flag.

## Step 6 — Google Sheet output (the main deliverable)

One spreadsheet, these tabs:

### Tab `Pipeline` (main view, all postings that passed gates)
Columns, in this order:

| Col | Header | Content |
|---|---|---|
| A | Tier | Strong / Worth a look / Stored |
| B | Score | 0–100 (final_score) |
| C | Company | ★ prefix if anchor |
| D | Role | `=HYPERLINK(url, title)` — **clicking opens the posting** |
| E | Role type | label from `roles.role_type_labels` — owner filters on this |
| F | Requirements | match summary, e.g. `Met 6/8 · Stretch: SQL, Figma · Gap: none`; then TN fit for US roles, e.g. `TN: Hotel Manager (likely)` / `TN: Mgmt Consultant (possible)` / `TN: unlikely` / `No visa needed` (Canada) |
| G | Location | matched city (tier) · arrangement |
| H | Salary | e.g. `$130K–$160K (mid $145K)` or `Unknown` |
| I | Extra comp | Equity / Bonus / Equity (unquantified) / — |
| J | Experience ask | e.g. `1–2 yrs required` / `New grad` |
| K | Flags | comma list: New, Apply soon, Salary unknown, Sponsorship unknown, TN possible — verify, January start, Sales-adjacent, Evergreen, Closed |
| L | Why | top 3 factors behind the score, short text |
| M | First seen | date |
| N | Posted | date |
| O | Status | **owner-edited**: data validation dropdown — Shortlisted, Drafting, Applied, Interviewing, Offer, Rejected, Pass |
| P | Effort | **owner-edited**: dropdown — Deep, Standard, Quick |
| Q | Notes | **owner-edited**: free text |
| R | Posting ID | hidden key column |

Sync rules (critical):
- **Upsert by Posting ID.** Never delete a row the owner has touched; never overwrite columns O–Q.
- New postings are inserted; existing ones update A–N in place.
- Add a filter view on row 1 so the owner can filter by Role type and Tier.
- Closed postings stay, with `Closed` in Flags, and move to the bottom.
- Sort: open before closed, then Tier (Strong first), then Score desc, then First seen desc.
- Freeze row 1, bold headers, conditional formatting: Strong row = light green, Closed = gray text.
- Batch writes (one `batch_update` per run), not cell-by-cell — stay well under Sheets API quotas.

### Tab `Dropped`
Postings that failed a gate this week: Company, Role (hyperlink), Gate, Matched text, Date. Capped at last 7 days. Used to spot rules that are too strict.

### Tab `Unparsed alerts`
Alert emails from senders with no parser yet: sender, subject, date. Tells the owner which parser to add next.

### Tab `Funnel`
One row per run: timestamp, boards polled, postings seen, new, passed gates, Strong, Worth a look, errors.

### Tab `Board health`
Company, platform, last success, last error, postings count last run vs previous, status (OK / Error / Went to zero).

## Step 7 — Schedule

`.github/workflows/hourly.yml`:
- `cron: "17 * * * *"` (off the top of the hour) + `workflow_dispatch` for manual runs.
- Restore and save `data/pipeline.db` between runs (Actions cache or artifact), so state persists.
- Secrets: `GOOGLE_SERVICE_ACCOUNT_JSON`, `SHEET_ID`, `ALERTS_IMAP_USER`, `ALERTS_IMAP_APP_PASSWORD`.
- Note in README: hourly runs on a private repo use Actions minutes; if they run short, the fallback is a small always-on server running the same `python -m src.run`.

## Step 8 — Tests (acceptance)

Using saved Palantir fixtures:
- **Kept:** Product Designer, New Grad (commercial); Deployment Strategist, New Grad – Warp Speed; Deployment Strategist and Deployment Strategist – Warp Speed (subject to experience asks).
- **Dropped with the right gate:** every title containing "Engineer", including all Forward Deployed and Software Engineer roles (`drop_titles.engineering`); anything with US Government or Defense in title/team (gov/defense exclusion); all internships (employment type).
- Sales titles (if any appear) are kept with role type `Sales` and ranked below equal matches.
- Each kept US posting shows a TN category per `work_authorization.tn_alignment`.
- Department "Business Development" must **not** trigger sales flags.
- For every kept Palantir posting, print the requirements summary so the owner can check the matcher's met / stretch / gap calls against his own read.

Unit tests for `comp.py` (ranges, hourly, OTE, CAD, multi-city ranges), `gates.py` (each gate), `requirements.py` (synonym matching, unclear → half credit, hard-gap drop, Python-required counts as a gap), `tn.py` (category mapping by duties, pre-graduation start flag), `alerts.py` (each parser against saved fixtures; read-only IMAP; unresolved items flagged), and Sheet sync (owner columns preserved across two runs).

## Step 9 — README (owner setup steps)

Write plain steps for the owner:
1. Create a Google Cloud project, enable the Google Sheets API, create a service account, download its JSON key.
2. Create a blank Google Sheet; share it with the service account email as Editor; copy the sheet ID from its URL.
3. Add `GOOGLE_SERVICE_ACCOUNT_JSON`, `SHEET_ID`, `ALERTS_IMAP_USER` and `ALERTS_IMAP_APP_PASSWORD` as GitHub repo secrets (alert inbox setup is in `ALERTS_SETUP.md`).
4. Run the workflow manually once; confirm the sheet fills.
5. How to add a company to `watchlist.yaml`.

## Order of work

1. Sources + fixtures for Palantir → normalize → gates → requirements match → print results to console. **Stop and show the Palantir results** before continuing.
2. Store + Sheet sync with Palantir only. Confirm hyperlinks work and owner columns survive a second run.
3. Seed and verify the watchlist.
3b. Alert-email reader + parsers, once the owner has saved sample alert emails.
4. Signals + tiers + remaining tabs.
5. Workflow + README.

## Out of scope for v1 (stub only)

AI review, reverse-SEO search, company discovery, push notifications, dashboard, application packets. Leave clean interfaces so Phases 2–4 plug in.
