# Job Pipeline — System Design

**Owner:** Login Wilson · **Version:** 1.0 · 2026-11-03 (v1.1 note added 2026-11-05)
**Config it runs on:** `config/criteria.yaml`

> **v1.1 note:** Scoring now ranks by requirements match (criteria.yaml §2b–2c), titles are labels except engineering (hard drop) and sales (passes, ranked lower), TN fit is mapped on US roles, and the alert-email reader moved into v1 (BUILD_BRIEF Step 1b). Where this document differs from `criteria.yaml` or `BUILD_BRIEF.md`, those win.

## Purpose

A constant inflow of job postings that fit the criteria, scored and ranked, feeding an application workspace where cover letters and applications get produced. The pipeline finds, filters and prepares. **You decide where effort goes** — Palantir gets a deep effort, a long-shot gets a quick one, most get none.

## Design principles

1. **Two layers of inflow.** Hourly polling of known company boards catches roles *when they come*. Criteria-driven search ("reverse SEO") finds roles that *have been in the market* at companies not yet watched. Each feeds the other.
2. **Cheap filters first, AI last.** Deterministic gates drop most postings for free. The AI reviewer only reads survivors.
3. **Official access only.** Company boards via their public feeds, aggregators via your own alert emails or licensed job-data APIs. No scraping LinkedIn or Indeed.
4. **Snapshot everything.** Postings vanish when roles close. Every posting is stored in full the first time it's seen, so the description is still there when you're writing the cover letter.
5. **Measure the funnel.** Every run logs what came in, what passed, and why — so a quiet week is diagnosable.

---

## Architecture

```
              ┌─────────────── SOURCES ────────────────┐
              │                                         │
  [A] Board pollers        [B] Reverse-SEO search    [C] Alert emails     [D] Company discovery
  Greenhouse · Lever       query matrix from          Gmail label:         funding news · investor
  Ashby · Workday          criteria → search APIs     LinkedIn · Indeed    portfolios · YC
  SmartRecruiters          + job-data APIs            Built In · Handshake
  hourly                   rotating batches           hourly               daily
              │                    │                       │                    │
              │                    └──────────┬────────────┴────────────────────┘
              │                               ▼
              │                     RESOLVER → find company's own board,
              │                     add to WATCHLIST (flywheel) ──────► back to [A]
              ▼                               │
           NORMALIZE (one Posting schema) ◄───┘
              ▼
           DEDUPE (same role from several sources → one record)
              ▼
           GATES (criteria.yaml, deterministic) ──── drop + log reason
              ▼
           SIGNALS (rule points: lane, description, city, timing, company upside)
              ▼
           AI REVIEW (survivors only) → fit_for_employer · fit_for_me · reason
              ▼
           TIERS: strong · worth a look · stored
              ▼
     ┌────────┼───────────────┐
  PUSH (strong)   DAILY DIGEST   DASHBOARD
                       ▼
             APPLICATION WORKSPACE (you pick effort level)
             posting snapshot · company brief · fit notes · cover letter draft · tracker
                       ▼
             FEEDBACK → calibration, keyword learning, query yield
```

---

## 1. Sources

### [A] Board pollers — the live feed
- Poll every company on the watchlist **hourly** through its public job feed (Greenhouse, Lever, Ashby; Workday and SmartRecruiters with dedicated adapters).
- One adapter per platform, all returning the same raw shape.
- A posting is "new" the first time its ID appears; "closed" when it disappears from the feed.

### [B] Reverse-SEO search — discovery and backfill
- **Keyword universe** (`config/keywords.yaml`), generated from criteria.yaml:
  - Title variants per lane (Deployment Strategist, Forward Deployed Strategist, Solutions Strategist, Implementation Consultant, APM, Product Designer New Grad, AI Operations, Strategy & Operations, Founder's Associate…)
  - × seniority words (new grad, entry level, associate, 2027)
  - × cities (NYC, Chicago, Miami, SF, LA, Toronto…)
  - \+ description phrases (workflow automation, embed with customers, partner with engineering)
- **Where queries run:**
  - Search API (Brave Search or SerpAPI) restricted to job-board domains (Lever, Greenhouse, Ashby, Workday pages).
  - Job-data APIs (e.g. Adzuna; Google job listings via SerpAPI) for postings hosted outside common job boards.
- Queries run in **rotating batches** so every query is retried regularly without blowing the quota.
- **Query yield tracking:** each query records postings found → passed gates → strong tier. Zero-yield queries are retired after a few weeks; budget shifts to winners.
- **Self-learning:** a strong hit whose title isn't in the keyword universe gets added automatically.

Search engines index new postings with a delay of hours to days, so [B] is for coverage, not speed. Speed comes from [A].

### [C] Alert emails — the wide net
- You set up job alerts on LinkedIn, Indeed, Built In NYC/Chicago, Work at a Startup and Handshake, routed to a Gmail label (e.g. `job-alerts`).
- The pipeline reads that label hourly, extracts company + title + link, and resolves each to the company's own posting.

### [D] Company discovery — growing the watchlist
- Daily scan of AI / data / automation / proptech funding news and top-investor portfolios.
- New companies are checked for a job board and added as watchlist candidates.

### Resolver and the flywheel
- Any hit from [B], [C] or [D] → identify the company → detect its job-board platform from URL patterns or its careers page → add to the watchlist.
- From then on, that company is polled hourly by [A]. Search finds companies; polling catches their future roles.

---

## 2. Processing

### Normalize — one Posting schema
| Field | Notes |
|---|---|
| `posting_id` | source + native ID |
| `company_id` | links to companies table |
| `title`, `team` | team kept separately (gov/defense exclusion reads it) |
| `locations[]`, `work_arrangement` | in-office / hybrid / remote |
| `comp` | min, max, currency, period, ranges by city, extra-comp flags |
| `employment_type` | full-time, part-time, contract, internship… |
| `description_text` | full text, plus parsed `required` / `preferred` sections |
| `posted_at`, `first_seen`, `last_seen`, `closed_at` | freshness tracking |
| `url`, `source` | where it came from |
| `snapshot` | full raw posting, stored once |

### Dedupe
- The same role often arrives from several sources. Records are matched on company + normalized title + location, and merged, keeping the company's own board as the primary record.

### Gates (deterministic)
- Every pass/fail rule in criteria.yaml: employment type, salary midpoint, seniority, experience, location tier and arrangement, work authorization, government/defense exclusion, company size.
- Each drop logs **which gate** removed it — this is how you'll spot a rule that's too strict.

### Signals (rule-based scoring)
- Lane match, description core signals, sales flags, technical tolerance, product-design signals, sector bonus, location tier, timing, company upside, salary bump.

### AI review (survivors only)
- Claude reads each surviving posting against the north star and your candidate profile.
- Returns `fit_for_employer`, `fit_for_me`, a one-line reason, `sales_in_disguise`, and a TN category guess when the no-sponsorship rule applies.
- Cached by posting content hash, so an unchanged posting is never re-reviewed.
- **Calibration:** before trusting it, you label ~30 real postings; prompt and weights are tuned until it agrees with you.

### Tiers
- **Strong:** push notification immediately.
- **Worth a look:** daily digest.
- **Stored:** searchable on the dashboard, never pushed.
- **Anchors** (WithCoverage, Palantir) always shown if they pass the gates.

---

## 3. Delivery

| Channel | What | When |
|---|---|---|
| Push | Strong-tier hits, with the one-line reason | Within the hour |
| Daily digest | New worth-a-look roles, closing roles you shortlisted, funnel stats | Each morning |
| Dashboard | Every scored posting, filters, statuses, funnel, source health | Live |

---

## 4. Application workspace

The point of the inflow. When you shortlist a posting, the pipeline builds an **application packet**:

- **Posting snapshot** — full description, saved even if the role later closes.
- **Company brief** — what they do, stage/upside signals, recent news, NYC presence.
- **Fit notes** — which requirements you meet, where you stretch, which portfolio piece to lead with (C.R.E.D., Territory Intelligence), likely interview angles.
- **Cover letter draft** — written from your master profile and story bank, tuned to the posting's language. Generated on request, not for every posting.
- **Effort level, set by you:**
  - *Deep* (e.g. Palantir): full packet, multiple draft passes, portfolio tailoring, outreach plan.
  - *Standard*: packet + one cover letter draft.
  - *Quick*: fit notes + short note only.
- **Tracker states:** shortlisted → drafting → applied → interviewing → offer / rejected / withdrawn, with dates.

**Outreach targets:** companies that score as a great fit but have no matching role open are listed separately — at entry level, a well-placed message often beats waiting for a posting.

### Your source material (`profile/`)
- `master_profile.md` — background, education, licenses, roles, systems built.
- `story_bank.md` — reusable stories (building C.R.E.D., Territory Intelligence, BKREA summers, startup community work) with the skills each proves.
- `portfolio.md` — what each project shows and when to lead with it.
- `voice.md` — how you write (opens with "Hi", signs "Login Wilson").

---

## 5. Feedback loops

- **Shortlist / dismiss with a reason** → feeds calibration of weights and the AI reviewer.
- **Strong hits with new titles** → added to the keyword universe.
- **Query yield** → prunes dead queries, funds productive ones.
- **Gate drop log** → reveals rules that are too strict.

---

## 6. Data store

| Table | Holds |
|---|---|
| `companies` | name, domain, board platform + slug, headcount, funding, upside signals, anchor flag |
| `boards` | per-company feed status, last poll, last error (health checks) |
| `postings` | normalized postings + snapshot |
| `scores` | gate results, signal points, AI review output, tier |
| `queries` / `query_runs` | keyword universe and yield per run |
| `applications` | packet, effort level, status, dates |
| `events` | run logs and funnel counts |

Start on SQLite; move to hosted Postgres (e.g. Supabase) once the dashboard needs it.

---

## 7. Schedule

| Cadence | Jobs |
|---|---|
| Hourly | Poll watchlist boards · read alert emails · score new postings · push strong hits |
| Every few hours | Run next reverse-SEO query batch |
| Daily | Company discovery · digest · board health check · closed/evergreen sweep |
| Weekly | Query yield pruning · calibration report · cost report |

---

## 8. Observability

- **Funnel per run:** seen → passed gates (by gate) → strong / worth a look.
- **Source health:** boards that error, or suddenly return zero postings (often a company switching job-board platforms).
- **Cost tracker:** search queries, job-data API calls and AI reviews per day against budget.

---

## 9. Cost control

- Start on free tiers for search and job-data APIs; scale query volume only once yield proves out.
- Gates before AI; AI results cached by content hash.
- Query budget goes to the highest-yield queries first.

---

## 10. Repo layout

```
job-pipeline/
├── config/
│   ├── criteria.yaml        # the rules (done)
│   ├── keywords.yaml        # reverse-SEO keyword universe
│   └── sources.yaml         # API keys refs, rate limits, batch sizes
├── profile/                 # master_profile, story_bank, portfolio, voice
├── prompts/
│   ├── ai_review.md
│   └── cover_letter.md
├── src/
│   ├── sources/             # greenhouse, lever, ashby, workday, smartrecruiters,
│   │                        # search, jobapis, gmail_alerts, discovery
│   ├── resolve/             # board detection, watchlist flywheel
│   ├── pipeline/            # normalize, dedupe, gates, signals, ai_review, tiers
│   ├── apply/               # packets, cover letter drafts, tracker
│   ├── deliver/             # push, digest, dashboard
│   └── store/               # schema + db access
├── tests/fixtures/          # real sample postings (incl. Palantir) with expected outcomes
└── .github/workflows/       # hourly / daily / weekly schedules
```

Keep the repo **private** — `profile/` holds personal material.

---

## 11. Build phases

| Phase | Delivers | Proves |
|---|---|---|
| **0 · Proof** | Palantir + WithCoverage boards → normalize → gates → signals → printed results | Rules behave as expected on real postings (e.g. Palantir's page: Product Designer New Grad and Deployment Strategist roles kept; engineering, gov/defense and internships dropped) |
| **1 · Live feed** | Seeded watchlist (~200 companies), hourly polling, SQLite, daily digest | Constant inflow from known companies |
| **2 · Judgment** | AI review + calibration on ~30 labeled postings | Scores match your instincts |
| **3 · Reach** | Alert emails, reverse-SEO search, resolver flywheel, query yield | Hits from companies you didn't know |
| **4 · Output** | Dashboard, application packets, cover letter drafting, tracker | Inflow turns into applications |

The Palantir application doesn't need to wait for any of this — its packet can be built by hand now and moved into the workspace in Phase 4.

---

## Open decisions

1. **Hosting:** GitHub Actions schedules (simplest; free minutes may get tight at hourly runs) vs. a small always-on server.
2. **Push channel:** phone notification app, text, or email.
3. **Dashboard:** hosted web page vs. a simple sheet view to start.
4. **Budget:** free tiers first, then a monthly cap for search, job-data APIs and AI review.
