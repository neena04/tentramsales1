#!/usr/bin/env python3
"""
Tentram — CS Quality (chat response times)

Run:    python3 kommo_cs_quality.py            (add --no-git to skip commit/push)
Output: ~/tentramsales1/cs.html  (standalone; linked from sales.html via "CS Quality")

Source: Kommo's incoming_chat_message / outgoing_chat_message events. They are the only
place the REST API exposes chat message timestamps (message text is not available).
Definitions are documented on the page itself, in the "Cara menghitung" box.
"""

import json, os, re, sys, time, bisect, subprocess, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────────────────────
SUBDOMAIN = "tentram"
BASE_URL  = f"https://{SUBDOMAIN}.kommo.com/api/v4"
TZ_OFFSET = 7                                   # WIB

# Order matters: index = pipeline code used in the embedded data.
PIPELINES = [(13334859, "[Cleaning] Tentram CS"),
             (13498915, "[PM] Inbound")]
PIPE_CODE = {pid: i for i, (pid, _) in enumerate(PIPELINES)}

# First chat event on the account is March 2026 (verified 2026-09-14).
HISTORY_FROM = datetime(2026, 3, 1, tzinfo=timezone.utc)

WORK_START, WORK_END = 8, 22                    # working hours, WIB
HANG_LIMIT_MIN       = 15                       # "customer left hanging" threshold

# Outgoing messages with created_by = 0 are a mix of automation and CS replying from
# the WhatsApp phone app. Verified on 30 days of data (2026-09-14):
#   - auto-replies land within seconds of the customer message (humans: 10 of 1,881
#     replies were <= 5s)
#   - scheduled broadcasts hit many leads at once — 337 of 339 went out at 08:00
AUTOREPLY_SEC        = 15
BROADCAST_WINDOW_SEC = 90                       # +- around each message
BROADCAST_MIN_LEADS  = 4

TOKEN = os.environ.get("KOMMO_TOKEN", "")
if not TOKEN:
    try:
        TOKEN = re.search(r'export KOMMO_TOKEN="([^"]+)"',
                          open(os.path.expanduser("~/.zshrc")).read()).group(1)
    except Exception:
        raise SystemExit("KOMMO_TOKEN not set and not found in ~/.zshrc")


# ── Kommo API ─────────────────────────────────────────────────────────────────
def api_get(path, retries=5):
    """Raises instead of returning {} on failure — a silently missing week of events
    would read as a week of perfect response times."""
    req = urllib.request.Request(BASE_URL + path,
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 204:
                return {}
            if e.code in (400, 401, 403, 404):
                raise SystemExit(f"Kommo API HTTP {e.code}: {path}")
            print(f"\n  [retry {attempt+1}/{retries}] HTTP {e.code}")
            time.sleep(2 ** attempt)
        except Exception as e:
            print(f"\n  [retry {attempt+1}/{retries}] {e}")
            time.sleep(2 ** attempt)
    raise SystemExit(f"Kommo API failed after {retries} attempts: {path}")


def paged(path, key):
    page = 1
    while True:
        d = api_get(f"{path}&limit=250&page={page}")
        batch = (d.get("_embedded") or {}).get(key, [])
        yield from batch
        if len(batch) < 250:
            return
        page += 1
        time.sleep(0.15)                        # Kommo allows ~7 rps


def fetch_events(types, start, step_days):
    """Events in fixed windows — keeps page counts small. De-duplicated on id because
    window edges are inclusive."""
    q = "".join(f"&filter[type][]={t}" for t in types)
    seen, out = set(), []
    now = int(time.time())
    a = int(start.timestamp())
    while a < now:
        b = a + step_days * 86400
        for ev in paged(f"/events?filter[created_at][from]={a}&filter[created_at][to]={b}{q}",
                        "events"):
            if ev["id"] not in seen:
                seen.add(ev["id"])
                out.append(ev)
        print(".", end="", flush=True)
        a = b
    return out


def fetch_by_ids(entity, ids, extra=""):
    out, ids = [], sorted(ids)
    for i in range(0, len(ids), 100):
        q = "&".join(f"filter[id][]={x}" for x in ids[i:i + 100])
        d = api_get(f"/{entity}?limit=250&{q}{extra}")
        out.extend((d.get("_embedded") or {}).get(entity, []))
        print(".", end="", flush=True)
        time.sleep(0.15)
    return out


# ── Transform ─────────────────────────────────────────────────────────────────
def work_seconds(a, b):
    """Seconds between unix ts a and b that fall inside working hours (WIB)."""
    if b <= a:
        return 0
    off = TZ_OFFSET * 3600
    total = 0
    for day in range((a + off) // 86400, (b + off) // 86400 + 1):
        ws = day * 86400 - off + WORK_START * 3600
        we = day * 86400 - off + WORK_END * 3600
        total += max(0, min(b, we) - max(a, ws))
    return total


def pipeline_timeline(status_events):
    """lead_id -> sorted [(ts, pipeline_before, pipeline_after)]"""
    tl = defaultdict(list)
    for ev in status_events:
        if ev.get("entity_type") != "lead":
            continue
        before = ((ev.get("value_before") or [{}])[0].get("lead_status") or {}).get("pipeline_id")
        after  = ((ev.get("value_after")  or [{}])[0].get("lead_status") or {}).get("pipeline_id")
        tl[ev["entity_id"]].append((ev["created_at"], before, after))
    for v in tl.values():
        v.sort()
    return tl


def pipeline_at(timeline, ts, current):
    """Pipeline the lead sat in at `ts` — so a lead later moved to Review Customer
    still counts toward CS for the chats it had while in CS."""
    if not timeline:
        return current
    i = bisect.bisect_right([t for t, _, _ in timeline], ts)
    if i > 0:
        return timeline[i - 1][2] or current
    return timeline[0][1] or current


def find_broadcasts(chat_events):
    out0 = sorted((e for e in chat_events
                   if e["type"] == "outgoing_chat_message" and not e.get("created_by")),
                  key=lambda e: e["created_at"])
    ts = [e["created_at"] for e in out0]
    ids = set()
    for i, e in enumerate(out0):
        lo = bisect.bisect_left(ts, e["created_at"] - BROADCAST_WINDOW_SEC)
        hi = bisect.bisect_right(ts, e["created_at"] + BROADCAST_WINDOW_SEC)
        if len({(out0[j]["entity_type"], out0[j]["entity_id"]) for j in range(lo, hi)}) \
                >= BROADCAST_MIN_LEADS:
            ids.add(e["id"])
    return ids


def build_turns(chat_events, status_events, leads, contacts, users):
    now = int(time.time())
    meta = defaultdict(int)
    broadcasts = find_broadcasts(chat_events)
    timelines = pipeline_timeline(status_events)
    lead_by_id = {l["id"]: l for l in leads}
    contact_name = {c["id"]: (c.get("name") or "").strip() for c in contacts}
    user_idx = {uid: i for i, uid in enumerate(users)}

    by_lead = defaultdict(list)
    talk_contact = {}
    for e in chat_events:
        if e["entity_type"] != "lead":
            meta["msg_contact_only"] += 1       # chat on a contact with no lead
            continue
        by_lead[e["entity_id"]].append(e)
        cid = ((e.get("_embedded") or {}).get("entity") or {}).get("linked_talk_contact_id")
        if cid:
            talk_contact[e["entity_id"]] = cid
    meta["messages"] = len(chat_events)

    turns, names = [], {}
    for lid, evs in by_lead.items():
        evs.sort(key=lambda e: (e["created_at"], e["type"] == "outgoing_chat_message"))
        lead = lead_by_id.get(lid)
        current = lead["pipeline_id"] if lead else None
        tl = timelines.get(lid)
        idx, start, last_in = 0, None, None

        def emit(reply, by):
            nonlocal idx
            pipe = PIPE_CODE.get(pipeline_at(tl, start, current))
            end = reply or now
            if pipe is not None:
                turns.append([pipe, lid, start, reply or 0, by, 1 if idx == 0 else 0,
                              work_seconds(start, end), end - start])
                names[lid] = 1
            idx += 1

        for e in evs:
            t = e["created_at"]
            if e["type"] == "incoming_chat_message":
                if start is None:
                    start = t
                last_in = t
                continue
            uid = e.get("created_by") or 0
            if not uid:
                if e["id"] in broadcasts:
                    meta["ignored_broadcast"] += 1
                    continue
                if start is not None and t - last_in <= AUTOREPLY_SEC:
                    meta["ignored_autoreply"] += 1
                    continue
            if start is None:
                continue                        # CS talking with no customer message open
            if not uid:
                meta["phone_replies"] += 1
            emit(t, user_idx.get(uid, -2) if uid else -1)
            start = None
        if start is not None:
            emit(None, -3)

    lead_names = {}
    for lid in names:
        lead = lead_by_id.get(lid) or {}
        cid = talk_contact.get(lid)
        if not contact_name.get(cid):
            linked = (lead.get("_embedded") or {}).get("contacts") or []
            main = [c["id"] for c in linked if c.get("is_main")] + [c["id"] for c in linked]
            cid = next((c for c in main if contact_name.get(c)), None)
        lead_names[lid] = contact_name.get(cid) or lead.get("name") or ""
    meta["turns"] = len(turns)
    return turns, lead_names, dict(meta)


# ── HTML ──────────────────────────────────────────────────────────────────────
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tentram — CS Quality</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#f5f6fa;color:#2d3436;padding-bottom:48px}
  header{background:#6c5ce7;color:#fff;padding:20px 32px;display:flex;
         align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
  header h1{font-size:20px;font-weight:700}
  header p{opacity:.75;font-size:12px;margin-top:4px}
  .navbtn{background:#fff;color:#6c5ce7;text-decoration:none;border-radius:6px;
          padding:8px 14px;font-size:12px;font-weight:700;white-space:nowrap}
  .navbtn:hover{background:#f4f3ff}
  .controls{background:#fff;border-bottom:1px solid #eee;padding:14px 32px;
            display:flex;gap:14px;align-items:center;flex-wrap:wrap;
            position:sticky;top:0;z-index:30}
  .controls label,.flagbar label{font-size:12px;color:#636e72;font-weight:600}
  select{border:1px solid #dfe6e9;border-radius:6px;padding:6px 10px;font-size:13px;
         color:#2d3436;background:#fff}
  .seg{display:flex;border:1px solid #dfe6e9;border-radius:6px;overflow:hidden}
  .seg button{border:none;padding:6px 14px;font-size:12px;cursor:pointer;
              background:#fff;color:#636e72}
  .seg button.active{background:#6c5ce7;color:#fff;font-weight:600}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;
        margin:20px 32px 0}
  .kpi{background:#fff;border-radius:10px;box-shadow:0 1px 3px rgba(0,0,0,.07);padding:16px 20px}
  .kpi .k{font-size:11px;color:#636e72;font-weight:600}
  .kpi .n{font-size:24px;font-weight:700;margin-top:4px;font-variant-numeric:tabular-nums}
  .kpi .s{font-size:11px;color:#b2bec3;margin-top:2px}
  .kpi.bad .n{color:#e17055}
  .box{background:#fff;border-radius:10px;margin:20px 32px;
       box-shadow:0 1px 3px rgba(0,0,0,.07);overflow:hidden}
  .box > h2{font-size:13px;font-weight:700;color:#2d3436;padding:18px 24px 6px}
  .box > .sub{font-size:12px;color:#b2bec3;padding:0 24px 14px}
  .box.red{border-left:3px solid #e17055}
  .scroller{overflow-x:auto}
  table{border-collapse:separate;border-spacing:0;font-size:12px;white-space:nowrap}
  th,td{padding:7px 10px;border-bottom:1px solid #f1f2f6;text-align:right}
  thead th{font-size:10px;text-transform:uppercase;letter-spacing:.4px;color:#636e72;
           background:#fafbfc;border-bottom:1px solid #e8e8e8;font-weight:700}
  .c-metric{text-align:left;min-width:190px;position:sticky;left:0;background:#fff;
            z-index:10;border-right:1px solid #e8e8e8;font-weight:600}
  thead .c-metric{background:#fafbfc;z-index:20}
  .c-total{min-width:84px;background:#fbfaff;font-weight:700;border-right:1px solid #e8e8e8}
  th.c-day{min-width:62px}
  td{font-variant-numeric:tabular-nums}
  .grp td{background:#f4f3ff;color:#6c5ce7;font-size:10px;font-weight:700;
          text-transform:uppercase;letter-spacing:.6px;padding:8px 10px;text-align:left}
  .grp td.c-metric{background:#f4f3ff}
  .zero{color:#dfe3e8}
  .warnnum{color:#e17055;font-weight:700}
  a.jump{color:#e17055;font-weight:700;text-decoration:none;border-bottom:1px dashed #e17055}
  .legend{padding:12px 24px 18px;font-size:11px;color:#8a8f98;border-top:1px solid #f1f2f6;
          line-height:1.5}
  .flagbar{display:flex;gap:10px;align-items:center;padding:0 24px 14px;flex-wrap:wrap}
  .flagbar .cnt{font-size:12px;color:#636e72}
  #t-flags{width:100%}
  #t-flags th,#t-flags td{text-align:left}
  #t-flags td.num{text-align:right}
  #t-flags .day td{background:#fff5f2;color:#c0392b;font-size:11px;font-weight:700;
                   padding:9px 12px}
  #t-flags a{color:#6c5ce7;text-decoration:none;font-weight:600}
  #t-flags a:hover{text-decoration:underline}
  .muted{color:#b2bec3;font-size:11px;font-weight:400}
  .badge{background:#ffeaa7;color:#7a6a3a;border-radius:4px;padding:1px 7px;font-size:11px;
         font-weight:700}
  .tag{background:#f4f3ff;color:#6c5ce7;border-radius:4px;padding:1px 7px;font-size:11px}
  .empty{padding:24px;color:#b2bec3;font-size:12px}
  .method{padding:4px 24px 20px;font-size:12px;color:#636e72;line-height:1.65}
  .method li{margin:0 0 6px 18px}
  .method b{color:#2d3436}
  @media(max-width:700px){.box,.kpis{margin-left:16px;margin-right:16px}
    header,.controls{padding-left:16px;padding-right:16px}}
</style>
</head>
<body>

<header>
  <div>
    <h1>Tentram — CS Quality</h1>
    <p>Waktu respons chat CS · di-generate __GENERATED__ WIB · data chat sejak __FROM__</p>
  </div>
  <a class="navbtn" href="sales.html">← Sales Tables</a>
</header>

<div class="controls">
  <div class="seg" id="seg-pipe"></div>
  <label for="month">Bulan</label>
  <select id="month"></select>
  <div class="seg" id="seg-mode">
    <button data-v="daily">Harian</button><button data-v="weekly">Mingguan</button>
  </div>
  <div class="seg" id="seg-basis">
    <button data-v="work">Jam kerja 08–22</button><button data-v="clock">Jam penuh</button>
  </div>
</div>

<div class="kpis" id="kpis"></div>

<div class="box">
  <h2 id="resp-title">Waktu respons</h2>
  <div class="sub" id="resp-sub"></div>
  <div class="scroller"><table id="t-resp"></table></div>
  <div class="legend">
    Format: <b>45s</b> = detik · <b>12m</b> = menit · <b>1j 05m</b> = jam &amp; menit.
    Arahkan kursor ke nilai tercepat / terlama untuk melihat lead-nya.
    Angka merah bisa diklik — langsung ke daftar chat di bawah.
  </div>
</div>

<div class="box red" id="flags">
  <h2>Customer menunggu lebih dari 15 menit</h2>
  <div class="sub">Dihitung dalam jam kerja 08:00–22:00 WIB, semua jenis lead (baru / repeat,
    semua sumber, B2C / B2B). Tanggal = tanggal chat customer.</div>
  <div class="flagbar">
    <label for="flag-day">Tanggal</label><select id="flag-day"></select>
    <span class="cnt" id="flag-count"></span>
  </div>
  <div class="scroller"><table id="t-flags"></table></div>
</div>

<div class="box">
  <h2>Cara menghitung</h2>
  <ul class="method">
    <li><b>Giliran customer.</b> Pesan customer yang berturut-turut dihitung sebagai satu
      giliran. Waktu tunggu dimulai dari pesan pertama yang belum dibalas dan berhenti saat
      CS membalas.</li>
    <li><b>First response</b> = giliran pertama di setiap lead. <b>Consecutive response</b> =
      semua giliran sesudahnya.</li>
    <li><b>Jam kerja 08:00–22:00 WIB.</b> Hanya menit di dalam jam kerja yang dihitung —
      chat jam 21:50 yang dibalas jam 08:10 = 20 menit. Chat yang masuk <i>dan</i> dibalas di luar
      jam kerja tidak masuk rata-rata pada mode ini. Mode <b>Jam penuh</b> memakai selisih jam
      biasa. Daftar &gt; 15 menit selalu memakai jam kerja.</li>
    <li><b>Pesan otomatis bukan balasan.</b> Auto-reply (terkirim ≤ 15 detik setelah pesan
      customer, tanpa nama pengirim) dan broadcast (terkirim ke ≥ 4 lead dalam 3 menit,
      mis. pesan terjadwal jam 08:00) diabaikan. Balasan tanpa nama pengirim yang bukan
      otomatis dihitung sebagai balasan CS dari HP — ditandai <b>HP / WhatsApp</b>.</li>
    <li><b>Pipeline</b> = pipeline tempat lead berada saat chat masuk (dari riwayat
      perpindahan stage), jadi lead yang kemudian pindah pipeline tetap terhitung benar.</li>
    <li><b>Mingguan</b> = Senin–Minggu. <b>Belum dibalas</b> = customer masih menunggu saat
      halaman di-generate (__GENERATED__ WIB).</li>
    <li id="meta-line"></li>
  </ul>
</div>

<script>
const RAW = __TURNS__;
const NAMES = __NAMES__;
const USERS = __USERS__;
const META = __META__;
const PIPES = __PIPES__;
const SUBDOMAIN = '__SUBDOMAIN__';
const TODAY = '__TODAY__';
const WS = __WS__, WE = __WE__, LIMIT = __LIMIT__ * 60, WIB = 7 * 3600;
</script>
<script>
(function(){
const MON = ['Jan','Feb','Mar','Apr','Mei','Jun','Jul','Agu','Sep','Okt','Nov','Des'];
const HARI = ['Min','Sen','Sel','Rab','Kam','Jum','Sab'];
const dayOf = ts => new Date((ts + WIB) * 1000).toISOString().slice(0, 10);
const hm    = ts => new Date((ts + WIB) * 1000).toISOString().slice(11, 16);
const inHours = ts => { const h = new Date((ts + WIB) * 1000).getUTCHours(); return h >= WS && h < WE; };
const addDays = (d, n) => { const x = new Date(d + 'T00:00:00Z'); x.setUTCDate(x.getUTCDate() + n); return x.toISOString().slice(0, 10); };
const monday  = d => addDays(d, -((new Date(d + 'T00:00:00Z').getUTCDay() + 6) % 7));
const dlabel  = d => +d.slice(8) + ' ' + MON[+d.slice(5, 7) - 1];
const dlong   = d => HARI[new Date(d + 'T00:00:00Z').getUTCDay()] + ', ' + dlabel(d) + ' ' + d.slice(0, 4);
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function fmt(sec){
  if (sec == null) return '—';
  sec = Math.round(sec);
  if (sec < 60) return sec + 's';
  const m = Math.round(sec / 60);
  if (m < 60) return m + 'm';
  return Math.floor(m / 60) + 'j ' + String(m % 60).padStart(2, '0') + 'm';
}

const turns = RAW.map(r => ({p:r[0], lead:r[1], s:r[2], r:r[3], by:r[4], first:r[5] === 1,
                             w:r[6], c:r[7], day:dayOf(r[2])}));

let S = {pipe:0, mode:'daily', basis:'work', month:null, flagDay:''};
try { Object.assign(S, JSON.parse(localStorage.getItem('csq') || '{}')); } catch(e) {}
const save = () => { try { localStorage.setItem('csq', JSON.stringify({pipe:S.pipe, mode:S.mode, basis:S.basis})); } catch(e) {} };

const val = t => S.basis === 'work' ? t.w : t.c;
// In working-hours mode a turn handled entirely outside 08–22 has no working-hours wait
// to measure; counting it as 0 would flatter the average.
const counted = t => S.basis === 'clock' || t.w > 0 || inHours(t.s);

function stats(from, to){
  const ts = turns.filter(t => t.p === S.pipe && t.day >= from && t.day <= to);
  const rep = ts.filter(t => t.r > 0 && counted(t));
  const avg = a => a.length ? a.reduce((x, t) => x + val(t), 0) / a.length : null;
  const f = rep.filter(t => t.first), c = rep.filter(t => !t.first);
  let mn = null, mx = null;
  for (const t of rep) { if (!mn || val(t) < val(mn)) mn = t; if (!mx || val(t) > val(mx)) mx = t; }
  return {n: ts.length, f: avg(f), fn: f.length, c: avg(c), cn: c.length, a: avg(rep), mn, mx,
          fast: rep.length ? rep.filter(t => t.w <= LIMIT).length / rep.length : null,
          late: ts.filter(t => t.r > 0 && t.w > LIMIT).length,
          open: ts.filter(t => t.r === 0 && t.w > LIMIT).length};
}

function monthRange(){
  const y = +S.month.slice(0, 4), m = +S.month.slice(5, 7);
  const first = S.month + '-01';
  const last = addDays(new Date(Date.UTC(y, m, 1)).toISOString().slice(0, 10), -1);
  return [first, last < TODAY ? last : TODAY];
}

function columns(){
  const [first, end] = monthRange(), cols = [];
  if (S.mode === 'daily') {
    for (let d = first; d <= end; d = addDays(d, 1)) cols.push({label: dlabel(d), from: d, to: d});
  } else {
    for (let w = monday(first); w <= end; w = addDays(w, 7))
      cols.push({label: dlabel(w) + '–' + dlabel(addDays(w, 6)), from: w, to: addDays(w, 6)});
  }
  return cols;
}

const num  = n => n ? n : '<span class="zero">0</span>';
const pct  = x => x == null ? '—' : Math.round(x * 100) + '%';
const cellT = t => t ? `<span title="Lead ${t.lead} · ${esc(NAMES[t.lead] || '')} · ${dlabel(t.day)} ${hm(t.s)}">${fmt(val(t))}</span>` : '—';
const jump = (n, col) => !n ? '<span class="zero">0</span>'
  : (col.from === col.to ? `<a class="jump" href="#flags" data-day="${col.from}">${n}</a>` : `<span class="warnnum">${n}</span>`);

const ROWS = [
  {grp: 'Volume'},
  {label: 'Chat customer (giliran)', f: s => num(s.n)},
  {grp: 'First response'},
  {label: 'Rata-rata first response', f: s => fmt(s.f)},
  {label: 'Jumlah dibalas', f: s => num(s.fn)},
  {grp: 'Consecutive response'},
  {label: 'Rata-rata consecutive', f: s => fmt(s.c)},
  {label: 'Jumlah dibalas', f: s => num(s.cn)},
  {grp: 'Semua respons'},
  {label: 'Rata-rata semua', f: s => fmt(s.a)},
  {label: 'Tercepat (min)', f: s => cellT(s.mn)},
  {label: 'Terlama (max)', f: s => cellT(s.mx)},
  {label: 'Dibalas ≤ 15 menit (jam kerja)', f: s => pct(s.fast)},
  {grp: 'Customer menunggu > 15 menit (jam kerja)'},
  {label: 'Dibalas terlambat', f: (s, col) => jump(s.late, col)},
  {label: 'Belum dibalas', f: (s, col) => jump(s.open, col)},
];

function renderTable(){
  const cols = columns();
  const [first, end] = monthRange();
  const tot = {label: S.mode === 'daily' ? 'Bulan ini' : 'Total', from: cols[0].from, to: cols[cols.length - 1].to};
  const cs = cols.map(c => stats(c.from, c.to)), ts = stats(tot.from, tot.to);
  let h = '<thead><tr><th class="c-metric">Metrik</th><th class="c-total">' + tot.label + '</th>';
  cols.forEach(c => { h += `<th class="c-day">${c.label}</th>`; });
  h += '</tr></thead><tbody>';
  for (const row of ROWS) {
    if (row.grp) { h += `<tr class="grp"><td class="c-metric">${row.grp}</td><td colspan="${cols.length + 1}"></td></tr>`; continue; }
    h += `<tr><td class="c-metric">${row.label}</td><td class="c-total">${row.f(ts, tot)}</td>`;
    cs.forEach((s, i) => { h += `<td>${row.f(s, cols[i])}</td>`; });
    h += '</tr>';
  }
  document.getElementById('t-resp').innerHTML = h + '</tbody>';
  document.getElementById('resp-title').textContent =
    'Waktu respons — ' + PIPES[S.pipe] + (S.mode === 'daily' ? ' · harian' : ' · mingguan');
  document.getElementById('resp-sub').innerHTML = S.basis === 'work'
    ? 'Hanya menit di dalam jam kerja 08:00–22:00 WIB yang dihitung'
    : 'Selisih jam penuh, termasuk malam hari';

  const mst = stats(first, end), bad = mst.late + mst.open;
  document.getElementById('kpis').innerHTML = [
    ['Rata-rata first response', fmt(mst.f), mst.fn + ' lead dibalas', false],
    ['Rata-rata consecutive response', fmt(mst.c), mst.cn + ' balasan', false],
    ['Dibalas ≤ 15 menit', pct(mst.fast), 'dari semua balasan, jam kerja', false],
    ['Customer menunggu > 15 menit', bad, mst.late + ' terlambat · ' + mst.open + ' belum dibalas', bad > 0],
  ].map(([k, n, s, b]) => `<div class="kpi${b ? ' bad' : ''}"><div class="k">${k}</div><div class="n">${n}</div><div class="s">${s}</div></div>`).join('');
}

function renderFlags(){
  const [first, end] = monthRange();
  const all = turns.filter(t => t.p === S.pipe && t.day >= first && t.day <= end && t.w > LIMIT)
                   .sort((a, b) => b.day.localeCompare(a.day) || a.s - b.s);
  const days = [...new Set(all.map(t => t.day))];
  if (S.flagDay && !days.includes(S.flagDay)) S.flagDay = '';
  const sel = document.getElementById('flag-day');
  sel.innerHTML = '<option value="">Semua tanggal</option>' +
    days.map(d => `<option value="${d}"${d === S.flagDay ? ' selected' : ''}>${dlong(d)} (${all.filter(t => t.day === d).length})</option>`).join('');
  const list = S.flagDay ? all.filter(t => t.day === S.flagDay) : all;
  document.getElementById('flag-count').textContent =
    list.length + ' chat · ' + list.filter(t => !t.r).length + ' belum dibalas';
  const tbl = document.getElementById('t-flags');
  if (!list.length) { tbl.innerHTML = '<tbody><tr><td class="empty">Tidak ada customer yang menunggu lebih dari 15 menit pada periode ini.</td></tr></tbody>'; return; }
  let h = '<thead><tr><th>Chat masuk</th><th>Lead ID</th><th>Customer</th><th class="num">Lama tidak dibalas</th><th>Dibalas</th><th>Oleh</th><th>Jenis</th></tr></thead><tbody>';
  let cur = null;
  for (const t of list) {
    if (t.day !== cur) {
      cur = t.day;
      const n = list.filter(x => x.day === cur).length;
      h += `<tr class="day"><td colspan="7">${dlong(cur)} · ${n} chat</td></tr>`;
    }
    const replied = t.r
      ? (dayOf(t.r) !== t.day ? dlabel(dayOf(t.r)) + ' ' : '') + hm(t.r)
      : '<span class="badge">Belum dibalas</span>';
    const by = t.by >= 0 ? esc(USERS[t.by]) : t.by === -1 ? 'HP / WhatsApp' : t.by === -2 ? 'User lain' : '—';
    const clock = Math.abs(t.c - t.w) >= 60 ? ` <span class="muted">(${fmt(t.c)} jam penuh)</span>` : '';
    h += `<tr><td>${hm(t.s)}</td>` +
         `<td><a href="https://${SUBDOMAIN}.kommo.com/leads/detail/${t.lead}" target="_blank" rel="noopener">${t.lead}</a></td>` +
         `<td>${esc(NAMES[t.lead] || '(tanpa nama)')}</td>` +
         `<td class="num"><span class="warnnum">${t.r ? '' : '≥ '}${fmt(t.w)}</span>${clock}</td>` +
         `<td>${replied}</td><td>${by}</td>` +
         `<td><span class="tag">${t.first ? 'First' : 'Lanjutan'}</span></td></tr>`;
  }
  tbl.innerHTML = h + '</tbody>';
}

function render(){
  document.querySelectorAll('.seg button').forEach(b => {
    const seg = b.parentNode.id, v = b.dataset.v;
    b.classList.toggle('active', seg === 'seg-pipe' ? +v === S.pipe : seg === 'seg-mode' ? v === S.mode : v === S.basis);
  });
  renderTable(); renderFlags(); save();
}

// controls
document.getElementById('seg-pipe').innerHTML = PIPES.map((p, i) => `<button data-v="${i}">${esc(p)}</button>`).join('');
const months = [...new Set(turns.map(t => t.day.slice(0, 7)))].sort().reverse();
if (!months.length) months.push(TODAY.slice(0, 7));
if (!months.includes(S.month)) S.month = months[0];
const msel = document.getElementById('month');
msel.innerHTML = months.map(m => `<option value="${m}">${MON[+m.slice(5) - 1]} ${m.slice(0, 4)}</option>`).join('');
msel.value = S.month;
msel.onchange = () => { S.month = msel.value; S.flagDay = ''; render(); };
document.querySelectorAll('.seg').forEach(seg => seg.addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  if (seg.id === 'seg-pipe') { S.pipe = +b.dataset.v; S.flagDay = ''; }
  else if (seg.id === 'seg-mode') S.mode = b.dataset.v;
  else S.basis = b.dataset.v;
  render();
}));
document.getElementById('flag-day').onchange = e => { S.flagDay = e.target.value; renderFlags(); };
document.getElementById('t-resp').addEventListener('click', e => {
  const a = e.target.closest('a.jump'); if (!a) return;
  S.flagDay = a.dataset.day; renderFlags();
});
document.getElementById('meta-line').innerHTML =
  `<b>Data:</b> ${META.messages.toLocaleString('id')} pesan chat · ${(META.ignored_autoreply||0).toLocaleString('id')} auto-reply dan ` +
  `${(META.ignored_broadcast||0).toLocaleString('id')} pesan broadcast diabaikan · ${(META.phone_replies||0).toLocaleString('id')} balasan dari HP · ` +
  `${(META.msg_contact_only||0).toLocaleString('id')} pesan di kontak tanpa lead tidak bisa dipetakan ke pipeline.`;
render();
})();
</script>
</body>
</html>
"""


def build_html(turns, names, users, meta):
    now_wib = datetime.utcnow() + timedelta(hours=TZ_OFFSET)
    user_names = [u[1] for u in users]
    j = lambda x: json.dumps(x, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return (HTML_TEMPLATE
        .replace("__TURNS__",     j(turns))
        .replace("__NAMES__",     j({str(k): v for k, v in names.items()}))
        .replace("__USERS__",     j(user_names))
        .replace("__META__",      j(meta))
        .replace("__PIPES__",     j([n for _, n in PIPELINES]))
        .replace("__SUBDOMAIN__", SUBDOMAIN)
        .replace("__WS__",        str(WORK_START))
        .replace("__WE__",        str(WORK_END))
        .replace("__LIMIT__",     str(HANG_LIMIT_MIN))
        .replace("__FROM__",      HISTORY_FROM.strftime("%-d %b %Y"))
        .replace("__TODAY__",     now_wib.strftime("%Y-%m-%d"))
        .replace("__GENERATED__", now_wib.strftime("%Y-%m-%d %H:%M")))


def main():
    print("=== Tentram — CS Quality ===\n")
    d = api_get("/users?limit=250")
    users = [(u["id"], u["name"]) for u in (d.get("_embedded") or {}).get("users", [])]

    print("Fetching chat events", end="", flush=True)
    chat = fetch_events(["incoming_chat_message", "outgoing_chat_message"], HISTORY_FROM, 7)
    print(f" {len(chat)}")
    if not chat:
        raise SystemExit("Aborted — 0 chat events fetched. Not overwriting existing output.")

    print("Fetching stage history", end="", flush=True)
    status = fetch_events(["lead_status_changed"], HISTORY_FROM, 30)
    print(f" {len(status)}")

    lead_ids = {e["entity_id"] for e in chat if e["entity_type"] == "lead"}
    print(f"Fetching {len(lead_ids)} leads", end="", flush=True)
    leads = fetch_by_ids("leads", lead_ids, "&with=contacts")
    print(f" {len(leads)}")

    contact_ids = {c["id"] for l in leads for c in ((l.get("_embedded") or {}).get("contacts") or [])}
    contact_ids |= {((e.get("_embedded") or {}).get("entity") or {}).get("linked_talk_contact_id")
                    for e in chat} - {None}
    print(f"Fetching {len(contact_ids)} contacts", end="", flush=True)
    contacts = fetch_by_ids("contacts", contact_ids)
    print(f" {len(contacts)}")

    turns, names, meta = build_turns(chat, status, leads, contacts, [u[0] for u in users])
    print("\nMeta:")
    for k, v in sorted(meta.items()):
        print(f"  {k:<20} {v}")
    for code, (_, pname) in enumerate(PIPELINES):
        ts = [t for t in turns if t[0] == code]
        print(f"  {pname:<22} {len(ts)} turns · "
              f"{sum(1 for t in ts if t[6] > HANG_LIMIT_MIN * 60)} waited > {HANG_LIMIT_MIN} min")

    html = build_html(turns, names, users, meta)
    out = os.path.abspath(os.environ.get("CS_OUT") or os.path.expanduser("~/tentramsales1/cs.html"))
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nSaved -> {out}  ({len(html)/1e6:.1f} MB)")

    repo = os.path.expanduser("~/tentramsales1")
    if "--no-git" in sys.argv:
        print("--no-git: skipping commit/push")
    elif out.startswith(repo):
        try:
            subprocess.run(["git", "-C", repo, "add", "cs.html"], check=True)
            subprocess.run(["git", "-C", repo, "commit", "-m",
                            f"cs quality {datetime.now().strftime('%Y-%m-%d %H:%M')}"], check=True)
            subprocess.run(["git", "-C", repo, "pull", "--rebase", "--autostash"], check=True)
            subprocess.run(["git", "-C", repo, "push"], check=True)
            print("Pushed -> Vercel will deploy /cs.html")
        except subprocess.CalledProcessError as e:
            print(f"Git step failed: {e}")


if __name__ == "__main__":
    main()
