# Job Alerts Setup — for Login

The pipeline can't scrape LinkedIn, Indeed, ZipRecruiter, Glassdoor or Handshake (their terms forbid it). Instead, each site emails you new matches, and the pipeline reads those emails, finds each role on the company's own site, and scores it. This takes about an hour, once.

---

## 1. Make a dedicated inbox (10 min)

Use a separate Gmail account just for alerts, so the pipeline never touches your personal inbox.

1. Create a new Gmail account (e.g. "firstname.lastname.jobs" — any name).
2. Turn on **2-Step Verification** for it (Google Account → Security).
3. Create an **App password** (Google Account → Security → App passwords). Name it "job pipeline". Save the 16-character password in your password manager.
4. In Gmail settings → **Forwarding and POP/IMAP**, make sure **IMAP is enabled**.

These become two GitHub secrets: `ALERTS_IMAP_USER` (the address) and `ALERTS_IMAP_APP_PASSWORD`.

Use this new address when you sign up for alerts below. Where a site only sends to an existing account email, forward those emails to the new inbox with a Gmail filter.

---

## 2. Alert searches

Set every alert to: **daily**, **full-time**, **entry level / associate**, **on-site + hybrid** (not remote).

Keyword groups (paste into the keyword box; LinkedIn and Indeed accept OR):

| Group | Keywords |
|---|---|
| A · Deployment | `"deployment strategist" OR "implementation consultant" OR "solutions consultant" OR "implementation specialist"` |
| B · Product | `"associate product manager" OR "product analyst" OR "product designer" OR "product operations"` |
| C · Strategy & Ops | `"strategy and operations" OR "business operations" OR bizops OR "founder's associate"` |
| D · AI & Automation | `"AI operations" OR "AI implementation" OR "workflow automation" OR "process automation"` |
| E · Real estate finance | `"real estate analyst" OR "acquisitions analyst" OR "asset management analyst" OR "underwriting analyst"` |
| F · Proptech & RE data | `proptech OR "real estate technology" OR "real estate data"` |
| G · Hospitality | `"hotel analyst" OR "hospitality analyst" OR "revenue strategy" OR "hotel asset management"` |
| H · Consulting & analyst | `"business analyst" OR "consulting analyst" OR "associate consultant" OR "strategy analyst"` |

### Where to set them

| Site | What to set up |
|---|---|
| **LinkedIn** | Groups A–H for **New York**. Groups A, B, C, E for **Chicago, Miami, San Francisco, Toronto**. (LinkedIn allows plenty of alerts.) |
| **Indeed** | Groups A, B, E, H for New York. One broad alert per secondary city: `analyst OR associate OR strategist` + entry level. |
| **ZipRecruiter** | Groups A, B, E, H for New York. |
| **Glassdoor** | Groups A, B, E for New York. |
| **Handshake** | Job alerts for A–H, location New York + Chicago + Toronto. Handshake emails your NYU address — add a filter in your NYU Gmail that forwards Handshake job-alert emails to the new inbox. If NYU blocks auto-forwarding, tell me and we'll handle Handshake another way. |
| **Built In NYC** and **Built In Chicago** | Saved searches for product, operations, data and analyst roles, entry level. |
| **Wellfound** | Job alerts: product, operations, strategy; New York; startups. |
| **YC Work at a Startup** | Profile + job alerts: product, operations, business; New York / SF. |
| **Hcareers** | Hospitality: analyst, revenue, asset management, corporate roles; New York. |
| **eFinancialCareers** | Real estate analyst / associate roles; New York. |

You can tune these anytime — the pipeline reads whatever arrives.

---

## 3. After the first day

Once alerts start arriving, leave 2–3 emails from each site in the inbox untouched. Claude Code saves them as test samples so it can build a parser for each site's email format.

---

## What happens to each alert

1. The pipeline reads new emails in the inbox (read-only — it never deletes or sends).
2. It pulls out the company, title, location and link.
3. It looks for the same role on the **company's own careers site**. If found, it scores the full posting like any other — and adds that company to the hourly watchlist, so its future roles are caught directly.
4. If it can't find it, the role still appears in the sheet, flagged **"From alert — unverified"**, with the original link. You open it yourself; it's scored only on what the email shows, so it's capped at "Worth a look."
