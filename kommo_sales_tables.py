#!/usr/bin/env python3
"""
Tentram CS — Sales Tables (B2C / B2B)
Implements tentram-360-sales-spec.md sections 2-8.

Run:    python3 kommo_sales_tables.py
Output: ~/tentramsales1/sales.html  (standalone; all leads embedded)

Separate page from index.html (the funnel dashboard) — that file is not touched.
"""

import json, os, re, sys, time, subprocess, urllib.request, urllib.error
from datetime import datetime, timedelta
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────────────────────
SUBDOMAIN   = "tentram"
BASE_URL    = f"https://{SUBDOMAIN}.kommo.com/api/v4"
PIPELINE_ID = 13334859          # Tentram CS
TZ_OFFSET   = 7                 # WIB

# Pipeline stages (verified against the live account 2026-09-08)
ST_INCOMING       = 102842147   # "Incoming leads"  — not in the spec, see README note
ST_INCOMING_ASGN  = 102842151   # "Incoming Leads (ASSIGNED)"
ST_CUSTOMER_NEEDS = 102842155
ST_QUOTE          = 102842159   # "Service Suggestions & Quote"
ST_WORK_SCHEDULED = 103507939
ST_REPEAT         = 103437559   # deprecated, should always be empty (spec §8)
ST_WON            = 142
ST_LOST           = 143

# Reaching any of these counts the lead as qualified (spec §5, "or beyond").
# Changed 2026-09-09 at Nina's request: the bar moved from Service Suggestions & Quote
# down to Customer Needs, so a lead counts as qualified once CS starts qualifying it
# rather than only once a quote has gone out. Still a cohort, keyed on created date.
QUALIFYING_STAGES = {ST_CUSTOMER_NEEDS, ST_QUOTE, ST_WORK_SCHEDULED, ST_REPEAT, ST_WON}

# Custom field ids (verified against the live account 2026-09-08)
CF_TANGGAL_PENGERJAAN = 3322834   # lead, date_time
CF_NOMINAL_DISKON     = 3444300   # lead, numeric  (not used in these tables, spec §6)
CF_DEAL_TYPE          = 3390854   # lead, select
CF_SERVICE            = 3340252   # lead, multiselect
CF_SUMBER_LEADS       = 3322396   # lead, select
CF_BILLING_PERIOD     = 3444516   # lead, text
CF_CUSTOMER_TYPE      = 3444080   # CONTACT, select: B2C / B2B
CF_PHONE              = 3194444   # CONTACT, multitext "Telepon"

# Monthly targets, per table and row. Fill these in as the team sets them.
# Keys: "B2C" / "B2B"  ->  row key -> {"YYYY-MM": value}. Missing = shown as "—".
TARGETS = {
    "B2C": {"inbound": {}, "qualified": {}, "won": {}, "sales": {}},
    "B2B": {"inbound": {}, "qualified": {}, "won": {}, "sales": {}},
    "Unknown": {"inbound": {}, "qualified": {}, "won": {}, "sales": {}},
}

TOKEN = os.environ.get("KOMMO_TOKEN", "")
if not TOKEN:
    # launchd passes it in the env; interactive shells have it in .zshrc
    try:
        TOKEN = re.search(r'export KOMMO_TOKEN="([^"]+)"',
                          open(os.path.expanduser("~/.zshrc")).read()).group(1)
    except Exception:
        raise SystemExit("KOMMO_TOKEN not set and not found in ~/.zshrc")


# ── Kommo API ─────────────────────────────────────────────────────────────────
def api_get(path, retries=5):
    req = urllib.request.Request(BASE_URL + path,
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 204:
                return {}
            print(f"\n  [retry {attempt+1}/{retries}] HTTP {e.code}")
            time.sleep(2 ** attempt)
        except Exception as e:
            print(f"\n  [retry {attempt+1}/{retries}] {e}")
            time.sleep(2 ** attempt)
    return {}


def fetch_stage_names():
    d = api_get(f"/leads/pipelines/{PIPELINE_ID}")
    return {s["id"]: s["name"] for s in (d.get("_embedded") or {}).get("statuses", [])}


def fetch_loss_reasons():
    out, page = {}, 1
    while True:
        d = api_get(f"/leads/loss_reasons?limit=250&page={page}")
        batch = (d.get("_embedded") or {}).get("loss_reasons", [])
        if not batch:
            break
        for lr in batch:
            out[lr["id"]] = lr["name"]
        if len(batch) < 250:
            break
        page += 1
        time.sleep(0.15)
    return out


def fetch_leads():
    """with=contacts is required — Customer type lives on the contact (spec §3)."""
    leads, page = [], 1
    print("Fetching leads", end="", flush=True)
    while True:
        d = api_get(f"/leads?filter[pipeline_id]={PIPELINE_ID}"
                    f"&limit=250&page={page}&with=contacts")
        batch = (d.get("_embedded") or {}).get("leads", [])
        if not batch:
            break
        leads.extend(batch)
        print(".", end="", flush=True)
        if len(batch) < 250:
            break
        page += 1
        time.sleep(0.15)          # Kommo allows ~7 rps (spec §3)
    print(f" {len(leads)} leads")
    return leads


def fetch_contacts(contact_ids):
    contacts = []
    ids = sorted(contact_ids)
    print(f"Fetching {len(ids)} contacts", end="", flush=True)
    for i in range(0, len(ids), 100):
        q = "&".join(f"filter[id][]={x}" for x in ids[i:i + 100])
        d = api_get(f"/contacts?limit=250&{q}")
        contacts.extend((d.get("_embedded") or {}).get("contacts", []))
        print(".", end="", flush=True)
        time.sleep(0.15)
    print(f" {len(contacts)} contacts")
    return contacts


def fetch_events():
    """lead_status_changed history — used for the qualified test (spec §5)."""
    by_lead = defaultdict(list)
    page, total = 1, 0
    print("Fetching events", end="", flush=True)
    while True:
        d = api_get("/events?filter[entity]=leads"
                    f"&filter[type][]=lead_status_changed&limit=250&page={page}")
        batch = (d.get("_embedded") or {}).get("events", [])
        if not batch:
            break
        for ev in batch:
            lid = ev.get("entity_id")
            if lid:
                by_lead[lid].append(ev)
                total += 1
        print(".", end="", flush=True)
        if len(batch) < 250:
            break
        page += 1
        time.sleep(0.2)
    for lid in by_lead:
        by_lead[lid].sort(key=lambda e: e["created_at"])
    print(f" {total} events across {len(by_lead)} leads")
    return by_lead


# ── Transform ─────────────────────────────────────────────────────────────────
def ts_to_date(ts):
    """Unix ts -> 'YYYY-MM-DD' in WIB."""
    if not ts:
        return None
    return datetime.utcfromtimestamp(ts + TZ_OFFSET * 3600).strftime("%Y-%m-%d")


def cf_value(entity, field_id):
    for f in (entity.get("custom_fields_values") or []):
        if f.get("field_id") == field_id:
            vals = f.get("values") or []
            if vals:
                return vals[0].get("value")
    return None


def entered_status(events, status_id):
    """Timestamp of the LAST transition into `status_id`, or None.

    Kommo's native `closed_at` is unusable on this account: all 2,877 Closed - Lost
    and all 192 Closed - Won leads report the same closed_at (the day of the last
    bulk edit), which would dump every rupiah of revenue onto a single column.
    The lead_status_changed events carry the real dates, so they are used instead
    of the `closed_at` that spec §6 names.
    """
    last = None
    for ev in events:
        after = (ev.get("value_after") or [{}])[0].get("lead_status", {}).get("id")
        if after == status_id:
            last = ev["created_at"]
    return last


def linked_contact_ids(lead):
    """Linked contacts, main one first (spec §3)."""
    linked = (lead.get("_embedded") or {}).get("contacts") or []
    return ([c["id"] for c in linked if c.get("is_main")] +
            [c["id"] for c in linked if not c.get("is_main")])


def norm_phones(contact):
    """Normalise to digits with a +62 country code (spec §8)."""
    out = []
    for f in (contact.get("custom_fields_values") or []):
        if f.get("field_id") != CF_PHONE:
            continue
        for v in (f.get("values") or []):
            d = re.sub(r"\D", "", str(v.get("value") or "").lstrip("'"))
            if d.startswith("0"):
                d = "62" + d[1:]
            elif d.startswith("8"):
                d = "62" + d
            if len(d) >= 9:
                out.append(d)
    return out


def build_dataset(leads, contacts, events_by_lead, loss_reasons):
    # Customer type lives on the contact, but this account carries ~880 duplicate
    # contact pairs (spec §8) and the value has been backfilled onto the NEWEST
    # duplicate while `is_main` points at the OLDEST. Joining strictly to the main
    # contact as spec §3 describes resolves only 21 of 3,866 leads, so resolution
    # cascades: main contact -> any linked duplicate -> phone-normalised cluster.
    ctype_by_contact, ctype_by_phone = {}, {}
    for c in contacts:
        v = cf_value(c, CF_CUSTOMER_TYPE)
        v = v if v in ("B2C", "B2B") else None
        ctype_by_contact[c["id"]] = v
        if v:
            for p in norm_phones(c):
                ctype_by_phone[p] = v
    contact_by_id = {c["id"]: c for c in contacts}

    dataset = []
    dq = {
        "unknown_ctype": 0,          # spec §7.1
        "won_zero_value": 0,         # spec §7.2
        "sched_no_date": 0,          # spec §7.3
        "repeat_stage": 0,           # spec §7.5 / §8 — must be 0
        "won_no_date": 0,            # won but no usable date at all
        "won_date_from_fallback": 0, # won with no status event -> dated by Tanggal pengerjaan
        "qualified_via_price": 0,    # lost leads that qualify only on the price proxy
        "no_contact": 0,
        "ctype_via_main": 0,
        "ctype_via_dup": 0,
        "ctype_via_phone": 0,
        "ctype_conflict": 0,
    }

    for lead in leads:
        lid        = lead["id"]
        status_id  = lead.get("status_id")
        created_at = lead.get("created_at") or 0
        closed_at  = lead.get("closed_at") or None
        price      = lead.get("price") or 0

        # ── Customer type, via the cascade described above ──
        cids  = linked_contact_ids(lead)
        ctype = None
        if not cids:
            dq["no_contact"] += 1
        else:
            ctype = ctype_by_contact.get(cids[0])            # 1. main contact
            if ctype:
                dq["ctype_via_main"] += 1
            else:
                for c2 in cids[1:]:                          # 2. linked duplicate
                    if ctype_by_contact.get(c2):
                        ctype = ctype_by_contact[c2]
                        dq["ctype_via_dup"] += 1
                        break
            if not ctype:                                    # 3. phone cluster
                for c2 in cids:
                    for p in norm_phones(contact_by_id.get(c2, {})):
                        if p in ctype_by_phone:
                            ctype = ctype_by_phone[p]
                            dq["ctype_via_phone"] += 1
                            break
                    if ctype:
                        break
            # disagreement between duplicates of the same customer
            seen = {ctype_by_contact[c2] for c2 in cids if ctype_by_contact.get(c2)}
            if len(seen) > 1:
                dq["ctype_conflict"] += 1
        if ctype is None:
            dq["unknown_ctype"] += 1
        bucket = ctype or "Unknown"

        # ── Qualified: stage timeline first, price proxy as fallback (spec §5) ──
        visited = set()
        evs = events_by_lead.get(lid) or []
        for ev in evs:
            before = (ev.get("value_before") or [{}])[0].get("lead_status", {}).get("id")
            after  = (ev.get("value_after")  or [{}])[0].get("lead_status", {}).get("id")
            if before:
                visited.add(before)
            if after:
                visited.add(after)
        visited.add(status_id)

        qual_timeline = bool(visited & QUALIFYING_STAGES)
        qual_price    = status_id == ST_LOST and price > 0
        qualified     = qual_timeline or qual_price
        if qual_price and not qual_timeline:
            dq["qualified_via_price"] += 1

        # ── Sales attribution — one lead, one date (spec §6) ──
        tanggal_raw = cf_value(lead, CF_TANGGAL_PENGERJAAN)
        tanggal_ts  = int(tanggal_raw) if isinstance(tanggal_raw, (int, float)) else None
        attr_date   = None
        attr_src    = None
        if status_id == ST_WON:
            # real won date -> scheduled work date -> closed_at (see entered_status)
            won_ts = entered_status(evs, ST_WON)
            if won_ts is None:
                won_ts = tanggal_ts or closed_at
                dq["won_date_from_fallback"] += 1
            attr_date = ts_to_date(won_ts)
            attr_src  = "won"
            if attr_date is None:
                dq["won_no_date"] += 1
            if price == 0:
                dq["won_zero_value"] += 1
        elif status_id == ST_WORK_SCHEDULED:
            attr_date = ts_to_date(tanggal_ts)
            attr_src  = "sched"
            if attr_date is None:
                dq["sched_no_date"] += 1
        # every other stage: not counted — the elif is load-bearing (spec §6)

        if status_id == ST_REPEAT:
            dq["repeat_stage"] += 1

        lost_date = None
        if status_id == ST_LOST:
            lost_date = ts_to_date(entered_status(evs, ST_LOST) or closed_at)
        lost_reason = loss_reasons.get(lead.get("loss_reason_id")) if status_id == ST_LOST else None

        dataset.append({
            "id":       lid,
            # deliberately no customer name: the tables are aggregate-only, so there
            # is no reason to ship 3,866 customer names into a static HTML file
            "t":        bucket,                    # B2C / B2B / Unknown
            "cd":       ts_to_date(created_at),    # created date
            "q":        1 if qualified else 0,
            "ad":       attr_date,                 # sales attribution date
            "as":       attr_src,                  # "won" | "sched" — which §6 branch
            "sl":       cf_value(lead, CF_SUMBER_LEADS) or "(kosong)",
            "p":        price,
            "ld":       lost_date,
            "lr":       lost_reason or "(tanpa alasan)",
            "s":        status_id,
        })

    return dataset, dq


# ── HTML ──────────────────────────────────────────────────────────────────────
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tentram CS — Sales Tables</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#f5f6fa;color:#2d3436;padding-bottom:48px}
  header{background:#6c5ce7;color:#fff;padding:20px 32px}
  header h1{font-size:20px;font-weight:700}
  header p{opacity:.75;font-size:12px;margin-top:4px}
  .controls{background:#fff;border-bottom:1px solid #eee;padding:14px 32px;
            display:flex;gap:14px;align-items:center;flex-wrap:wrap;
            position:sticky;top:0;z-index:30}
  .controls label{font-size:12px;color:#636e72;font-weight:600}
  select{border:1px solid #dfe6e9;border-radius:6px;padding:6px 10px;font-size:13px;
         color:#2d3436;background:#fff}
  .seg{display:flex;border:1px solid #dfe6e9;border-radius:6px;overflow:hidden}
  .seg button{border:none;padding:6px 14px;font-size:12px;cursor:pointer;
              background:#fff;color:#636e72}
  .seg button.active{background:#6c5ce7;color:#fff;font-weight:600}
  .box{background:#fff;border-radius:10px;margin:20px 32px;
       box-shadow:0 1px 3px rgba(0,0,0,.07);overflow:hidden}
  .box > h2{font-size:13px;font-weight:700;color:#2d3436;padding:18px 24px 6px}
  .box > .sub{font-size:12px;color:#b2bec3;padding:0 24px 14px}
  .scroller{overflow-x:auto}
  table{border-collapse:separate;border-spacing:0;font-size:12px;white-space:nowrap}
  th,td{padding:7px 10px;border-bottom:1px solid #f1f2f6;text-align:right}
  thead th{font-size:10px;text-transform:uppercase;letter-spacing:.4px;color:#636e72;
           background:#fafbfc;border-bottom:1px solid #e8e8e8;font-weight:700}
  .c-metric{text-align:left;min-width:170px;position:sticky;left:0;background:#fff;
            z-index:10;border-right:1px solid #e8e8e8;font-weight:600}
  thead .c-metric{background:#fafbfc;z-index:20}
  .c-target{min-width:78px;color:#636e72;background:#fcfcfd;
            border-right:1px solid #e8e8e8}
  .c-total{min-width:96px;background:#fbfaff;font-weight:700;
           border-left:1px solid #e8e8e8}
  th.c-day{min-width:66px}
  .grp td{background:#f4f3ff;color:#6c5ce7;font-size:10px;font-weight:700;
          text-transform:uppercase;letter-spacing:.6px;padding:8px 10px;text-align:left}
  .grp td.c-metric{background:#f4f3ff}
  .immature{color:#c8c9d4}
  .r-pct td{color:#636e72}
  .r-sales td{font-variant-numeric:tabular-nums}
  .r-reason td{white-space:normal;font-size:10px;color:#8a8f98;line-height:1.35;
               text-align:left;min-width:120px;vertical-align:top}
  .r-reason td.c-metric{white-space:nowrap;font-size:12px;color:#2d3436}
  .zero{color:#dfe3e8}
  .legend{padding:12px 24px 18px;font-size:11px;color:#8a8f98;border-top:1px solid #f1f2f6}
  .btn{border:none;border-radius:6px;padding:7px 14px;font-size:12px;font-weight:600;
       cursor:pointer;background:#6c5ce7;color:#fff}
  .btn:hover{background:#5b4bd6}
  .btn:disabled{background:#c8c9d4;cursor:default}
  .rstat{font-size:11px;color:#636e72}
  .rstat.err{color:#c0392b}
  .rstat.ok{color:#00b894}
  .stale{background:#fff8e6;border:1px solid #ffeaa7;color:#7a6a3a;
         padding:2px 8px;border-radius:5px;font-size:11px;font-weight:600}
  .box.amber{border-left:3px solid #fdcb6e}
  .box.amber > h2{color:#9a7b1f}
  .r-total td{background:#fbfaff;font-weight:700;border-top:1px solid #e8e8e8}
  #t-source .c-metric{min-width:200px}
  .r-total td.c-metric{background:#fbfaff}
  .legend .swatch{display:inline-block;width:10px;height:10px;background:#c8c9d4;
                  border-radius:2px;vertical-align:-1px;margin-right:5px}
  .dq{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:1px;
      background:#f1f2f6}
  .dq div{background:#fff;padding:16px 20px}
  .dq .n{font-size:22px;font-weight:700;margin-bottom:3px}
  .dq .k{font-size:11px;color:#636e72;line-height:1.4}
  .ok{color:#00b894}.warn{color:#e17055}
  .note{margin:20px 32px;padding:14px 18px;background:#fff8e6;border:1px solid #ffeaa7;
        border-radius:8px;font-size:12px;color:#7a6a3a;line-height:1.5}
  .note b{color:#5f5228}
  @media(max-width:700px){.box,.note{margin:16px 12px}
    header,.controls{padding-left:16px;padding-right:16px}}
</style>
</head>
<body>

<header>
  <h1>Tentram CS — Sales Tables</h1>
  <p>Pipeline Tentram CS · di-generate __GENERATED__ WIB · semua lead tertanam di halaman ini</p>
</header>

<div class="controls">
  <label>Bulan</label>
  <select id="month" onchange="render()"></select>
  <div class="seg">
    <button id="m-daily" class="active" onclick="setMode('daily')">Harian</button>
    <button id="m-compact" onclick="setMode('compact')">Ringkas</button>
  </div>
  <span style="font-size:11px;color:#b2bec3" id="lead-count"></span>
  <span id="stale-badge" style="display:none"></span>
  <span style="margin-left:auto;display:flex;align-items:center;gap:10px">
    <span class="rstat" id="refresh-status"></span>
    <button class="btn" id="refresh-btn" onclick="doRefresh()">Refresh data</button>
  </span>
</div>

<div class="box">
  <h2>Tabel 1 — B2C</h2>
  <div class="sub">Lead yang kontak utamanya bertipe <b>B2C</b></div>
  <div class="scroller"><table id="t-B2C"></table></div>
  <div class="legend">
    <span class="swatch"></span>Kolom abu-abu belum matang — lead baru masih diproses,
    angka Qualified akan naik beberapa hari ke depan.
  </div>
</div>

<div class="box">
  <h2>Tabel 2 — B2B</h2>
  <div class="sub">Lead yang kontak utamanya bertipe <b>B2B</b></div>
  <div class="scroller"><table id="t-B2B"></table></div>
  <div class="legend">
    <span class="swatch"></span>Kolom abu-abu belum matang — lead baru masih diproses,
    angka Qualified akan naik beberapa hari ke depan.
  </div>
</div>

<div class="box amber">
  <h2>Tabel 3 — Tanpa Customer Type</h2>
  <div class="sub">Lead yang kontak utamanya <b>belum punya Customer Type</b> di Kommo —
    belum bisa dimasukkan ke B2C maupun B2B</div>
  <div class="scroller"><table id="t-Unknown"></table></div>
  <div class="legend">
    <span class="swatch"></span>Kolom abu-abu belum matang. Tabel ini seharusnya menyusut
    sampai kosong seiring Customer Type diisi — anggap saja progress bar backfill.
  </div>
</div>

<div class="box">
  <h2>Rekonsiliasi — cocokkan dengan Kommo</h2>
  <div class="sub">Jumlah lead masuk per hari, semua tipe. Baris <b>Total</b> inilah yang
    harus sama dengan angka di Kommo; ketiga tabel di atas adalah pecahannya</div>
  <div class="scroller"><table id="t-recon"></table></div>
</div>

<div class="box">
  <h2>Sumber Leads — per hari</h2>
  <div class="sub">Deal yang menang (Closed - Won + Work Scheduled), semua tipe customer
    digabung. Dasar tanggal sama dengan baris Sales di atas, jadi baris TOTAL di sini =
    Tabel 1 + Tabel 2 + Tabel 3</div>
  <div class="scroller"><table id="t-source"></table></div>
</div>

<div class="note">
  <b>Ketiga tabel memang tidak bisa dijumlahkan ke bawah.</b>
  Baris Funnel dihitung dari tanggal lead masuk; baris Sales dihitung dari tanggal closing
  atau tanggal pengerjaan. Keduanya menggambarkan kumpulan lead yang berbeda, jadi
  "Leads Won ÷ Inbound leads" pada tanggal yang sama bukan conversion rate dan sengaja
  tidak ditampilkan.
</div>

<div class="box">
  <h2>Data quality</h2>
  <div class="sub">Bukan bagian dari tabel di atas — ini penanda kualitas data di Kommo</div>
  <div class="dq" id="dq"></div>
</div>

<script>
const LEADS = __LEADS__;
const DQ    = __DQ__;
const TARGETS = __TARGETS__;
const TODAY = "__TODAY__";
const GENERATED_AT = "__GENERATED_DATE__";
const MONTHS_ID = ['Jan','Feb','Mar','Apr','Mei','Jun','Jul','Agu','Sep','Okt','Nov','Des'];

let mode = 'daily';

function setMode(m){
  mode = m;
  document.getElementById('m-daily').classList.toggle('active', m==='daily');
  document.getElementById('m-compact').classList.toggle('active', m==='compact');
  render();
}

function fmtInt(n){ return n ? n.toLocaleString('id-ID') : '<span class="zero">0</span>'; }

function fmtRp(n){
  if(!n) return '<span class="zero">0</span>';
  if(n >= 1e6) return (n/1e6).toFixed(1).replace('.',',') + 'jt';
  if(n >= 1e3) return Math.round(n/1e3) + 'rb';
  return String(n);
}
function fmtRpFull(n){ return 'Rp' + (n||0).toLocaleString('id-ID'); }

function dayLabel(ds){
  const d = new Date(ds + 'T00:00:00');
  return d.getDate() + ' ' + MONTHS_ID[d.getMonth()];
}

// Every day of the selected month, capped at today for the current month
function daysOfMonth(ym){
  const [y,m] = ym.split('-').map(Number);
  const last = new Date(y, m, 0).getDate();
  const out = [];
  for(let d=1; d<=last; d++){
    const ds = `${y}-${String(m).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
    if(ds > TODAY) break;
    out.push(ds);
  }
  return out;
}

// Column defs: {label, dates:[...]}
function buildColumns(ym){
  const days = daysOfMonth(ym);
  if(mode === 'daily') return days.map(d => ({label: dayLabel(d), dates:[d]}));

  // Ringkas: the last 7 days stay daily, everything earlier is bucketed into
  // 7-day ranges labelled like "1 - 7 Agu".
  const cut = days.length - 7;
  const cols = [];
  for(let i=0; i<Math.max(0,cut); i+=7){
    const chunk = days.slice(i, Math.min(i+7, cut));
    if(!chunk.length) continue;
    const a = new Date(chunk[0]+'T00:00:00'), b = new Date(chunk[chunk.length-1]+'T00:00:00');
    cols.push({label: `${a.getDate()} - ${b.getDate()} ${MONTHS_ID[b.getMonth()]}`, dates: chunk});
  }
  days.slice(Math.max(0,cut)).forEach(d => cols.push({label: dayLabel(d), dates:[d]}));
  return cols;
}

// Spec §5: the most recent 7 date columns are immature and are greyed out.
function immatureSet(cols){
  const cutoff = new Date(TODAY + 'T00:00:00');
  cutoff.setDate(cutoff.getDate() - 6);
  const cutStr = cutoff.toISOString().slice(0,10);
  const flags = cols.map(c => c.dates[c.dates.length-1] >= cutStr);
  // never grey more than the 7 rightmost columns
  let seen = 0;
  for(let i=flags.length-1; i>=0; i--){
    if(flags[i]){ seen++; if(seen > 7) flags[i] = false; }
  }
  return flags;
}
</script>
</body>
</html>
"""

# Appended into HTML_TEMPLATE just before </script>
JS_RENDER = r"""
// ── Aggregation ───────────────────────────────────────────────────────────────
function metricsFor(bucket){
  const m = {inbound:{}, qualified:{}, won:{}, sales:{}, lost:{}, reasons:{},
             wonC:{}, wonV:{}, schedC:{}, schedV:{}};
  LEADS.forEach(l => {
    if(l.t !== bucket) return;
    if(l.cd){                                   // Group A — created date
      m.inbound[l.cd] = (m.inbound[l.cd]||0) + 1;
      if(l.q) m.qualified[l.cd] = (m.qualified[l.cd]||0) + 1;
    }
    if(l.ad){                                   // Group B — attribution date (spec §6)
      m.won[l.ad]   = (m.won[l.ad]||0) + 1;
      m.sales[l.ad] = (m.sales[l.ad]||0) + l.p;
      const c = l.as === 'won' ? 'wonC' : 'schedC';
      const v = l.as === 'won' ? 'wonV' : 'schedV';
      m[c][l.ad] = (m[c][l.ad]||0) + 1;
      m[v][l.ad] = (m[v][l.ad]||0) + l.p;
    }
    if(l.ld){                                   // Closed - Lost, by closed_at
      m.lost[l.ld] = (m.lost[l.ld]||0) + 1;
      const r = m.reasons[l.ld] || (m.reasons[l.ld] = {});
      r[l.lr] = (r[l.lr]||0) + 1;
    }
  });
  return m;
}

const sumOver = (map, dates) => dates.reduce((s,d) => s + (map[d]||0), 0);

function reasonsOver(map, dates){
  const agg = {};
  dates.forEach(d => {
    const r = map[d]; if(!r) return;
    for(const k in r) agg[k] = (agg[k]||0) + r[k];
  });
  const parts = Object.entries(agg).sort((a,b) => b[1]-a[1]).map(([k,v]) => `${k} (${v})`);
  return parts.length ? parts.join(', ') : '<span class="zero">—</span>';
}

function target(bucket, row, ym){
  const v = (TARGETS[bucket] && TARGETS[bucket][row]) ? TARGETS[bucket][row][ym] : undefined;
  if(v === undefined || v === null) return '<span class="zero">—</span>';
  return row === 'sales' ? fmtRp(v) : fmtInt(v);
}

// ── Table rendering ───────────────────────────────────────────────────────────
function renderTable(bucket, ym, cols, imm){
  const m = metricsFor(bucket);
  const allDates = cols.flatMap(c => c.dates);

  let head = '<thead><tr><th class="c-metric">Metrik</th><th class="c-target">Target</th>';
  cols.forEach((c,i) => {
    head += `<th class="c-day${imm[i] ? ' immature' : ''}">${c.label}</th>`;
  });
  head += '<th class="c-total">Total</th></tr></thead>';

  const cells = (fn, cls, greyable) => cols.map((c,i) =>
    `<td class="${cls||''}${greyable && imm[i] ? ' immature' : ''}">${fn(c.dates)}</td>`).join('');

  let body = '<tbody>';

  // ── Group A — funnel, keyed on the lead's created date ──
  body += `<tr class="grp"><td class="c-metric">Funnel — dasar: tanggal lead masuk</td>` +
          `<td colspan="${cols.length + 2}"></td></tr>`;

  body += '<tr><td class="c-metric">Inbound leads</td>' +
          `<td class="c-target">${target(bucket,'inbound',ym)}</td>` +
          cells(ds => fmtInt(sumOver(m.inbound, ds)), '', false) +
          `<td class="c-total">${fmtInt(sumOver(m.inbound, allDates))}</td></tr>`;

  body += '<tr><td class="c-metric">Qualified leads</td>' +
          `<td class="c-target">${target(bucket,'qualified',ym)}</td>` +
          cells(ds => fmtInt(sumOver(m.qualified, ds)), '', true) +
          `<td class="c-total">${fmtInt(sumOver(m.qualified, allDates))}</td></tr>`;

  const pct = ds => {
    const i = sumOver(m.inbound, ds), q = sumOver(m.qualified, ds);
    return i ? (q/i*100).toFixed(0) + '%' : '<span class="zero">—</span>';
  };
  body += '<tr class="r-pct"><td class="c-metric">Qualified %</td>' +
          '<td class="c-target"><span class="zero">—</span></td>' +
          cells(pct, '', true) +
          `<td class="c-total">${pct(allDates)}</td></tr>`;

  // ── Group B — sales, keyed on the closing / work date ──
  body += `<tr class="grp"><td class="c-metric">Sales — dasar: tanggal closing / pengerjaan</td>` +
          `<td colspan="${cols.length + 2}"></td></tr>`;

  // Split by which branch of the §6 rule attributed the lead, so each half can be
  // checked against its own Kommo stage. A lead still appears in exactly one of them.
  const countRow = (label, key, cls, tgt) =>
    `<tr class="${cls||''}"><td class="c-metric">${label}</td>` +
    `<td class="c-target">${tgt ? target(bucket,tgt,ym) : '<span class="zero">—</span>'}</td>` +
    cols.map(c => `<td>${fmtInt(sumOver(m[key], c.dates))}</td>`).join('') +
    `<td class="c-total">${fmtInt(sumOver(m[key], allDates))}</td></tr>`;

  const valueRow = (label, key, cls, tgt) =>
    `<tr class="r-sales ${cls||''}"><td class="c-metric">${label}</td>` +
    `<td class="c-target">${tgt ? target(bucket,tgt,ym) : '<span class="zero">—</span>'}</td>` +
    cols.map(c => {
      const v = sumOver(m[key], c.dates);
      return `<td title="${fmtRpFull(v)}">${fmtRp(v)}</td>`;
    }).join('') +
    `<td class="c-total" title="${fmtRpFull(sumOver(m[key], allDates))}">` +
    `${fmtRp(sumOver(m[key], allDates))}</td></tr>`;

  body += countRow('Closed - Won', 'wonC');
  body += valueRow('Closed - Won — Rp',       'wonV');
  body += countRow('Work Scheduled', 'schedC');
  body += valueRow('Work Scheduled — Rp',       'schedV');
  body += countRow('TOTAL Leads Won', 'won',   'r-total', 'won');
  body += valueRow('TOTAL Sales',     'sales', 'r-total', 'sales');

  body += '<tr><td class="c-metric">Deals Lost</td>' +
          '<td class="c-target"><span class="zero">—</span></td>' +
          cells(ds => fmtInt(sumOver(m.lost, ds)), '', false) +
          `<td class="c-total">${fmtInt(sumOver(m.lost, allDates))}</td></tr>`;

  body += '<tr class="r-reason"><td class="c-metric">Lost Reason</td>' +
          '<td class="c-target"><span class="zero">—</span></td>' +
          cols.map(c => `<td>${reasonsOver(m.reasons, c.dates)}</td>`).join('') +
          `<td class="c-total">${reasonsOver(m.reasons, allDates)}</td></tr>`;

  body += '</tbody>';
  document.getElementById('t-' + bucket).innerHTML = head + body;
}

// ── Reconciliation: every lead accounted for, so the page can be checked ──────
function renderRecon(ym, cols, imm){
  const m = {B2C: metricsFor('B2C'), B2B: metricsFor('B2B'), Unknown: metricsFor('Unknown')};
  const allDates = cols.flatMap(c => c.dates);

  let head = '<thead><tr><th class="c-metric">Inbound leads</th>';
  cols.forEach(c => { head += `<th class="c-day">${c.label}</th>`; });
  head += '<th class="c-total">Total</th></tr></thead><tbody>';

  const row = (label, key, cls) =>
    `<tr class="${cls||''}"><td class="c-metric">${label}</td>` +
    cols.map(c => `<td>${fmtInt(sumOver(m[key].inbound, c.dates))}</td>`).join('') +
    `<td class="c-total">${fmtInt(sumOver(m[key].inbound, allDates))}</td></tr>`;

  let body = row('Tabel 1 — B2C', 'B2C')
           + row('Tabel 2 — B2B', 'B2B')
           + row('Tabel 3 — Tanpa Customer Type', 'Unknown');

  const totalOf = ds => ['B2C','B2B','Unknown']
    .reduce((s,k) => s + sumOver(m[k].inbound, ds), 0);
  body += '<tr class="r-total"><td class="c-metric">Total — cocokkan dengan Kommo</td>' +
          cols.map(c => `<td>${fmtInt(totalOf(c.dates))}</td>`).join('') +
          `<td class="c-total">${fmtInt(totalOf(allDates))}</td></tr>`;

  document.getElementById('t-recon').innerHTML = head + body + '</tbody>';
}

// ── Sumber Leads per day, all customer types ──────────────────────────────────
// Same date basis and same lead set as the Sales rows of tables 1-3, so the TOTAL
// rows here equal Tabel1 + Tabel2 + Tabel3 for every column.
function renderSource(ym, cols){
  const allDates = cols.flatMap(c => c.dates);
  const cnt = {}, val = {};          // source -> {date -> n}
  LEADS.forEach(l => {
    if(!l.ad) return;
    (cnt[l.sl] || (cnt[l.sl] = {}));
    (val[l.sl] || (val[l.sl] = {}));
    cnt[l.sl][l.ad] = (cnt[l.sl][l.ad] || 0) + 1;
    val[l.sl][l.ad] = (val[l.sl][l.ad] || 0) + l.p;
  });

  // only sources that actually appear in this month, busiest first
  const sources = Object.keys(cnt)
    .map(s => [s, sumOver(cnt[s], allDates), sumOver(val[s], allDates)])
    .filter(([, n]) => n > 0)
    .sort((a, b) => (b[1] - a[1]) || (b[2] - a[2]))
    .map(([s]) => s);

  let head = '<thead><tr><th class="c-metric">Sumber Leads</th>';
  cols.forEach(c => { head += `<th class="c-day">${c.label}</th>`; });
  head += '<th class="c-total">Total</th></tr></thead>';

  if(!sources.length){
    document.getElementById('t-source').innerHTML = head +
      `<tbody><tr><td class="c-metric">—</td><td colspan="${cols.length+1}" ` +
      `style="text-align:left;color:#b2bec3">Belum ada deal yang closing di bulan ini</td>` +
      `</tr></tbody>`;
    return;
  }

  const line = (label, map, money, cls) =>
    `<tr class="${cls||''}${money ? ' r-sales' : ''}"><td class="c-metric">${label}</td>` +
    cols.map(c => {
      const v = sumOver(map, c.dates);
      return money ? `<td title="${fmtRpFull(v)}">${fmtRp(v)}</td>` : `<td>${fmtInt(v)}</td>`;
    }).join('') +
    (money
      ? `<td class="c-total" title="${fmtRpFull(sumOver(map, allDates))}">${fmtRp(sumOver(map, allDates))}</td></tr>`
      : `<td class="c-total">${fmtInt(sumOver(map, allDates))}</td></tr>`);

  // a per-date total across every source
  const totalMap = m => {
    const t = {};
    Object.values(m).forEach(byDate => {
      for(const d in byDate) t[d] = (t[d] || 0) + byDate[d];
    });
    return t;
  };

  let body = '<tbody>';
  body += `<tr class="grp"><td class="c-metric">Jumlah deal</td>` +
          `<td colspan="${cols.length + 1}"></td></tr>`;
  sources.forEach(s => { body += line(s, cnt[s], false); });
  body += line('TOTAL deal', totalMap(cnt), false, 'r-total');

  body += `<tr class="grp"><td class="c-metric">Nilai (Rp)</td>` +
          `<td colspan="${cols.length + 1}"></td></tr>`;
  sources.forEach(s => { body += line(s, val[s], true); });
  body += line('TOTAL nilai', totalMap(val), true, 'r-total');

  document.getElementById('t-source').innerHTML = head + body + '</tbody>';
}

// ── Data quality panel (spec §7) ──────────────────────────────────────────────
function renderDQ(){
  const items = [
    [DQ.unknown_ctype,      'Lead dengan <b>Customer type kosong</b> — tidak masuk tabel B2C maupun B2B', true],
    [DQ.won_zero_value,     'Lead <b>Closed - Won</b> dengan Sales value = 0', true],
    [DQ.sched_no_date,      'Lead <b>Work Scheduled</b> tanpa Tanggal pengerjaan — hilang dari baris Sales', true],
    [DQ.repeat_stage,       'Lead di stage <b>Repeat customer</b> — stage sudah tidak dipakai, harus selalu 0', true],
    [DQ.won_no_date,        'Lead <b>Closed - Won</b> tanpa tanggal apa pun — tidak masuk baris Sales', true],
    [DQ.won_date_from_fallback,'Lead <b>Closed - Won</b> tanpa riwayat perpindahan stage — tanggalnya diambil dari Tanggal pengerjaan (deal migrasi saat pipeline dibuat)', false],
    [DQ.ctype_conflict,     'Lead yang kontak duplikatnya <b>saling bertentangan</b> soal Customer type', true],
    [DQ.qualified_via_price,'Lead <b>Closed - Lost</b> yang dihitung Qualified hanya lewat proxy Sales value &gt; 0', false],
    [DQ.ctype_via_dup,      'Customer type terbaca dari <b>kontak duplikat</b>, bukan kontak utama (is_main kosong)', false],
    [DQ.ctype_via_phone,    'Customer type terbaca lewat <b>pencocokan nomor telepon</b> antar duplikat', false],
  ];
  document.getElementById('dq').innerHTML = items.map(([n,k,bad]) =>
    `<div><div class="n ${n ? (bad ? 'warn' : '') : 'ok'}">${n.toLocaleString('id-ID')}</div>
     <div class="k">${k}</div></div>`).join('');
}

// ── Manual refresh ────────────────────────────────────────────────────────────
// The page holds no GitHub token — it posts a shared password to /api/refresh, which
// dispatches the workflow server-side. Run status comes from GitHub's public API.
const GH_RUNS = "https://api.github.com/repos/neena04/tentramsales1/actions/workflows/refresh.yml/runs?per_page=1";

function setStatus(msg, cls){
  const el = document.getElementById('refresh-status');
  el.textContent = msg || '';
  el.className = 'rstat' + (cls ? ' ' + cls : '');
}

async function doRefresh(){
  const pw = prompt('Password untuk refresh data:');
  if(pw === null) return;
  const btn = document.getElementById('refresh-btn');
  btn.disabled = true;
  setStatus('Mengirim permintaan…');
  try {
    const r = await fetch('/api/refresh', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password: pw})
    });
    const d = await r.json().catch(() => ({}));
    if(!r.ok){
      setStatus(d.error || `Gagal (HTTP ${r.status})`, 'err');
      btn.disabled = false;
      return;
    }
    setStatus('Refresh dimulai — kira-kira 2 menit…');
    pollRun();
  } catch(e){
    setStatus('Tidak bisa menghubungi server: ' + e.message, 'err');
    btn.disabled = false;
  }
}

async function pollRun(){
  const started = Date.now();
  const btn = document.getElementById('refresh-btn');
  const tick = async () => {
    if(Date.now() - started > 10*60*1000){
      setStatus('Timeout — cek tab Actions di GitHub', 'err');
      btn.disabled = false;
      return;
    }
    try {
      const d = await (await fetch(GH_RUNS)).json();
      const run = d.workflow_runs && d.workflow_runs[0];
      if(run && run.status === 'completed'){
        if(run.conclusion === 'success'){
          setStatus('Selesai — memuat ulang halaman…', 'ok');
          setTimeout(() => location.reload(true), 20000);
        } else {
          setStatus('Gagal: ' + run.conclusion, 'err');
          btn.disabled = false;
        }
        return;
      }
      const secs = Math.round((Date.now() - started)/1000);
      setStatus(`Sedang berjalan… ${secs}s`);
    } catch(e){ /* keep polling */ }
    setTimeout(tick, 5000);
  };
  setTimeout(tick, 5000);
}

// ── Boot ──────────────────────────────────────────────────────────────────────
function render(){
  const ym = document.getElementById('month').value;
  const cols = buildColumns(ym);
  const imm  = immatureSet(cols);
  renderTable('B2C', ym, cols, imm);
  renderTable('B2B', ym, cols, imm);
  renderTable('Unknown', ym, cols, imm);
  renderRecon(ym, cols, imm);
  renderSource(ym, cols);
  const ageDays = Math.floor((Date.now() - new Date(GENERATED_AT + 'T00:00:00').getTime())/864e5);
  const badge = document.getElementById('stale-badge');
  if(ageDays >= 1){
    badge.style.display = '';
    badge.className = 'stale';
    badge.textContent = ageDays === 1 ? 'Data kemarin — belum di-refresh hari ini'
                                      : `Data sudah ${ageDays} hari — belum di-refresh`;
  } else {
    badge.style.display = 'none';
  }
  const n = c => LEADS.filter(l => l.t === c).length;
  document.getElementById('lead-count').textContent =
    `${LEADS.length.toLocaleString('id-ID')} lead · B2C ${n('B2C')} · B2B ${n('B2B')} · Unknown ${n('Unknown')}`;
}

(function init(){
  const months = new Set();
  LEADS.forEach(l => { if(l.cd) months.add(l.cd.slice(0,7));
                       if(l.ad) months.add(l.ad.slice(0,7));
                       if(l.ld) months.add(l.ld.slice(0,7)); });
  const sorted = [...months].sort().reverse();
  const sel = document.getElementById('month');
  sel.innerHTML = sorted.map(ym => {
    const [y,m] = ym.split('-');
    return `<option value="${ym}">${MONTHS_ID[+m-1]} ${y}</option>`;
  }).join('');
  const cur = TODAY.slice(0,7);
  sel.value = sorted.includes(cur) ? cur : sorted[0];
  renderDQ();
  render();
})();
"""


def build_html(dataset, dq):
    tpl = HTML_TEMPLATE.replace("</script>", JS_RENDER + "\n</script>")
    return (tpl
        .replace("__LEADS__",     json.dumps(dataset, ensure_ascii=False, separators=(",", ":")))
        .replace("__DQ__",        json.dumps(dq))
        .replace("__TARGETS__",   json.dumps(TARGETS))
        .replace("__TODAY__",     (datetime.utcnow() + timedelta(hours=TZ_OFFSET)).strftime("%Y-%m-%d"))
        .replace("__GENERATED_DATE__", (datetime.utcnow() + timedelta(hours=TZ_OFFSET)).strftime("%Y-%m-%d"))
        .replace("__GENERATED__", (datetime.utcnow() + timedelta(hours=TZ_OFFSET)).strftime("%Y-%m-%d %H:%M")))


def main():
    print("=== Tentram CS — Sales Tables ===\n")
    loss_reasons   = fetch_loss_reasons()
    leads          = fetch_leads()
    if not leads:
        raise SystemExit("Aborted — 0 leads fetched. Not overwriting existing output.")

    contact_ids    = {c["id"] for l in leads
                      for c in ((l.get("_embedded") or {}).get("contacts") or [])}
    contacts       = fetch_contacts(contact_ids)
    events_by_lead = fetch_events()

    dataset, dq = build_dataset(leads, contacts, events_by_lead, loss_reasons)

    print("\nData quality:")
    for k, v in dq.items():
        print(f"  {k:<22} {v}")
    buckets = defaultdict(int)
    for r in dataset:
        buckets[r["t"]] += 1
    print(f"\nBuckets: B2C {buckets['B2C']} · B2B {buckets['B2B']} · Unknown {buckets['Unknown']}")

    html = build_html(dataset, dq)
    # SALES_OUT lets the GitHub Actions runner write into the checked-out repo,
    # where ~/tentramsales1 does not exist.
    out = os.environ.get("SALES_OUT") or os.path.expanduser("~/tentramsales1/sales.html")
    out = os.path.abspath(out)
    if not os.path.isdir(os.path.dirname(out)):
        out = os.path.expanduser("~/sales.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nSaved -> {out}  ({len(html)/1e6:.1f} MB)")

    # index.html (the funnel dashboard) is deliberately left alone.
    repo = os.path.expanduser("~/tentramsales1")
    if "--no-git" in sys.argv:
        print("--no-git: skipping commit/push")
    elif out.startswith(repo):
        try:
            subprocess.run(["git", "-C", repo, "add", "sales.html"], check=True)
            subprocess.run(["git", "-C", repo, "commit", "-m",
                            f"sales tables {datetime.now().strftime('%Y-%m-%d %H:%M')}"], check=True)
            # The GitHub Actions job pushes to this branch too, so a local run is
            # usually behind. Rebase first, keeping the freshly generated file on any
            # conflict — sales.html is generated output, never hand-edited.
            subprocess.run(["git", "-C", repo, "fetch", "origin"], check=True)
            r = subprocess.run(["git", "-C", repo, "-c", "core.editor=true",
                                "rebase", "origin/main"])
            if r.returncode != 0:
                subprocess.run(["git", "-C", repo, "checkout", "--theirs", "sales.html"])
                subprocess.run(["git", "-C", repo, "add", "sales.html"], check=True)
                subprocess.run(["git", "-C", repo, "-c", "core.editor=true",
                                "rebase", "--continue"],
                               env={**os.environ, "GIT_EDITOR": "true"}, check=True)
            subprocess.run(["git", "-C", repo, "push"], check=True)
            print("Pushed -> Vercel will deploy /sales.html")
        except subprocess.CalledProcessError as e:
            print(f"Git step failed: {e}  (commit is local; push credentials not configured)")


if __name__ == "__main__":
    main()
