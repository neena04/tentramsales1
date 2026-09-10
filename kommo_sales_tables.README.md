# Sales Tables — build notes

`kommo_sales_tables.py` implements `tentram-360-sales-spec.md` §2–§8.
Output: `~/tentramsales1/sales.html` (standalone). `index.html` — the existing funnel
dashboard — is not touched.

Run: `python3 ~/kommo_sales_tables.py` (add `--no-git` to skip the commit/push step).

Verified against the live account on 2026-09-08: 3,866 leads, 8,391 contacts, 9,211
lead_status_changed events.

## Where the implementation departs from the spec, and why

### 1. The contact join is a cascade, not `is_main` (spec §3)

Spec: "Join lead to its **main** contact, then read Customer type."

That resolves **21 of 3,866 leads**. This account carries the duplicate contacts described
in §8: most leads link 2–3 contacts with the same customer name. `is_main` points at the
*oldest* duplicate; Customer Type was backfilled onto the *newest* one (all 968 values were
written between 5 and 8 Sept 2026 — the backfill appears to still be in progress).

Implemented resolution order:

1. main contact
2. any other linked contact that has a value  ← resolves 945
3. phone-normalised cluster per §8 (strip non-digits, strip leading `'`, force +62) ← 9

Result: 975 resolved (973 B2C, 2 B2B), 2,891 Unknown. Leads whose duplicates disagree are
counted in the data-quality panel (2 today).

**Coverage is still only 25%.** Both tables will stay mostly empty until Customer Type is
filled in on the remaining ~2,900 leads. This is a Kommo data-entry gap, not a code gap.

### 2. Closing dates come from the event history, not `closed_at` (spec §6)

Spec: "if stage == Closed - Won: attribute to `closed_at` date".

`closed_at` is unusable on this account. **All 3,069 closed leads carry just 29 distinct
`closed_at` values, all on 2026-09-08 between 16:29 and 16:57 WIB.** Following the spec
literally dumps every rupiah of revenue (Rp 152.9M) and every lost deal onto one column.

Cause, confirmed with Nina 2026-09-08: an Excel bulk import run as part of a backfill
exercise. Kommo re-stamps `closed_at` whenever a lead already in a closed status is
written, so the import flattened every historical closing date onto the import time.
The original values are overwritten and not recoverable.

The import was otherwise clean — it created no leads (only 11 leads were created that
day, all organic and outside the import window) and changed no stages (25 status events
that day, none in the import window). So `created_at` and the stage history are intact,
and the Inbound leads row is unaffected.

**This will recur on every future backfill.** Reading dates from the append-only event log
is therefore the durable fix, not a workaround.

The `lead_status_changed` events carry the real transition dates, and the pipeline already
fetches them. Attribution is now:

- `Closed - Won`  → last transition into Won → *fallback* Tanggal pengerjaan → *fallback* `closed_at`
- `Work Scheduled` → Tanggal pengerjaan (unchanged)
- everything else → not counted (the `elif` is preserved: one lead, one date)

Won revenue by month after the change: Mar 36.0M · Apr 5.9M · May 12.6M · Jun 37.7M ·
Jul 28.5M · Aug 26.9M · Sep 5.2M.

The 41 won leads with no status history at all have zero events — they were created
directly in Closed - Won and never moved stage (33 of them in March 2026, when the
pipeline launched). They date off Tanggal pengerjaan and account for most of the March
figure. They are counted separately in the data-quality panel so the
March lump stays explainable.

### 3. Lost Reason reads the native field (spec §4)

There is no `Alasan Lost` custom field on this account. Loss reasons are Kommo's native
`loss_reason_id` (11 configured, populated on 2,588 of 2,877 lost leads). Unset shows as
"(tanpa alasan)".

### 4. Qualified uses the stage timeline first (spec §5)

Spec proposes `Sales value > 0` as the proxy for lost leads. The events give a direct
answer, so: qualified if the lead ever visited Service Suggestions & Quote, Work Scheduled,
Repeat customer or Closed - Won; lost leads additionally qualify on `price > 0`. Only 1 lead
currently qualifies via the price proxy alone — it is counted in the panel.

### 5. Notes on the spec's own assumptions

- The pipeline has an **eighth stage** the spec omits: `Incoming leads` (102842147), which
  sits before `Incoming Leads (ASSIGNED)`. It currently holds 0 leads. Inbound leads counts
  every lead regardless of stage, so nothing is lost either way.
- `Deal Type` has three options (One-off, Prepaid, B2B Monthly). There is no **Retainer**,
  which §9's future revenue-recognition table assumes.
- The refresh is **not** a GitHub Actions cron. It is a local launchd job on this Mac
  (`~/Library/LaunchAgents/com.tentram.kommo-refresh.plist`, 08:00 daily) that runs
  `kommo_funnel.py`. `kommo_sales_tables.py` is not scheduled yet.
- **The existing funnel dashboard's "By Closed Date" filter is now broken** by the same
  `closed_at` damage (`kommo_funnel.py:420`), and its won-date fallback at line 581 will
  mis-date any won lead lacking a status event. Out of scope for this pass, but it needs
  the same event-log treatment.
- `~/tentramsales1` has not pushed since 2026-07-21 (`could not read Username for
  'https://github.com'`), so nothing generated locally has reached Vercel for seven weeks.

## Things the spec asked for that have no data source

- **Target column** — rendered, but no source exists. Fill in `TARGETS` at the top of the
  script (`{"B2C": {"inbound": {"2026-09": 500}, ...}}`); unset shows "—".
- **§7 check 4** (Sales value vs Produk total − Nominal Diskon) — needs Produk line items,
  which are out of scope per §9.

## The "Refresh data" button

`api/refresh.js` dispatches the GitHub Actions workflow so people without GitHub
access can rebuild the dashboard themselves. The page is public, so it never carries
a token: it posts a shared password, the function checks it in constant time and
holds the GitHub token server-side. Run progress is polled from GitHub's public API,
which needs no secret.

Two Vercel environment variables are required (project `tentramsales1`):

| Variable | What |
|---|---|
| `GH_TOKEN` | fine-grained PAT for `neena04/tentramsales1`, Actions: read+write |
| `REFRESH_PASSWORD` | shared password the team types into the dashboard |

Vercel does not redeploy when an environment variable changes — push a commit or
redeploy by hand, or the function keeps running with the old (or missing) values.

The PAT expires (90 days by default). When it does, the button starts failing with
HTTP 401 and nothing else announces it; regenerate the token and update `GH_TOKEN`.

To check the wiring without knowing the password:

    curl -s -X POST https://tentramsales1.vercel.app/api/refresh \
      -H 'Content-Type: application/json' -d '{"password":"wrong"}'

`401 Password salah` means it is configured. `500` means the variables are missing.
