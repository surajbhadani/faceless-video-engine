"""Local management dashboard: channel health, queue control, schedule editing.

Usage:
  python portal.py            # serves http://localhost:8765
  python portal.py --port N   # custom port

No new dependencies — built on Python's stdlib http.server plus the existing
src/analytics.py, src/schedule.py, src/queue_manager.py and run_daily.py queue
helpers. Pulls cached performance data (refresh it first with a normal
`python run_daily.py` run, or hit /refresh).
"""
import argparse
import html
import json
import re
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent
QUEUE_DIR = ROOT / "queue"
OUTPUT_DIR = ROOT / "output"
LOG_DIR = ROOT / "logs"

import run_daily  # noqa: E402
from src import analytics, queue_manager, schedule, script_gen  # noqa: E402

try:
    from src import upload as upload_mod
except Exception:
    upload_mod = None

_STEP_RE = re.compile(r"^\[(?P<ts>[\d\-: ]+)\]\s+(?P<msg>\d/6 .+|Queue build \d+/\d+.+|.*queued as .+|.*LIVE:.*|.*FAILED.*)$")


def _tail_lines(path: Path, n: int = 80) -> list[str]:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        return lines[-n:]
    except Exception:
        return []


def read_build_progress() -> dict:
    """Best-effort parse of the most recent step/queue line from today's log
    so the UI can show what the background build is doing right now."""
    log_path = LOG_DIR / f"{datetime.now():%Y-%m}.log"
    lines = _tail_lines(log_path, 120)
    last_step, last_queued = None, None
    for line in lines:
        m = _STEP_RE.match(line.strip())
        if not m:
            continue
        msg = m.group("msg")
        if "queued as" in msg or "LIVE:" in msg:
            last_queued = {"ts": m.group("ts"), "msg": msg.strip()}
        else:
            last_step = {"ts": m.group("ts"), "msg": msg.strip()}
    return {"step": last_step, "last_queued": last_queued}


def list_queue_videos() -> list[dict]:
    """All videos currently buffered in queue/, newest first."""
    if not QUEUE_DIR.exists():
        return []
    items = []
    for qdir in QUEUE_DIR.iterdir():
        video = qdir / "video.mp4"
        plan_path = qdir / "plan.json"
        if not (video.exists() and plan_path.exists()):
            continue
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except Exception:
            plan = {}
        try:
            size_mb = round(video.stat().st_size / (1024 * 1024), 1)
            mtime = datetime.fromtimestamp(video.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        except Exception:
            size_mb, mtime = 0, ""
        items.append({
            "stamp": qdir.name,
            "topic": plan.get("topic", ""),
            "title": plan.get("title", ""),
            "created": mtime,
            "size_mb": size_mb,
        })
    items.sort(key=lambda v: v["stamp"], reverse=True)
    return items


def list_longform_videos() -> list[dict]:
    """All long-form videos built locally into output/ (long_*.mp4), newest first."""
    if not OUTPUT_DIR.exists():
        return []
    items = []
    for video in OUTPUT_DIR.glob("long_*.mp4"):
        try:
            size_mb = round(video.stat().st_size / (1024 * 1024), 1)
            mtime = datetime.fromtimestamp(video.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        except Exception:
            size_mb, mtime = 0, ""
        items.append({
            "stamp": video.stem,  # e.g. long_20261021_125728
            "title": video.stem,
            "created": mtime,
            "size_mb": size_mb,
        })
    items.sort(key=lambda v: v["stamp"], reverse=True)
    return items


_STAMP_RE = re.compile(r"^[A-Za-z0-9_]+$")


def build_recommendations(perf: dict | None, qdepth: int) -> list[str]:
    recs = []
    if qdepth < 3:
        recs.append(f"Queue buffer is low ({qdepth} video(s)) — run "
                     "`python run_daily.py --build-queue {n}` soon so a bad network "
                     "day doesn't skip a post.")
    videos = (perf or {}).get("videos", [])
    if not videos:
        recs.append("No performance data yet — needs at least one live video and "
                     "either the yt-analytics.readonly OAuth scope or a "
                     "YOUTUBE_API_KEY in .env.")
        return recs
    today = date.today()
    scored = []
    for v in videos:
        try:
            age = max((today - date.fromisoformat(v["date"])).days, 1)
        except Exception:
            age = 1
        scored.append((v.get("views", 0) / age, age, v))
    scored.sort(key=lambda t: t[0], reverse=True)
    top, bottom = scored[:2], scored[-2:]
    if top:
        names = ", ".join(f"\"{t[2]['topic']}\"" for t in top)
        recs.append(f"Your best-performing topics are {names} — make more videos "
                     "in that emotional angle/sub-theme (the pipeline already "
                     "biases toward this automatically via performance_block()).")
    if bottom and len(scored) >= 5:
        names = ", ".join(f"\"{t[2]['topic']}\"" for t in bottom)
        recs.append(f"Weakest performers: {names} — avoid that flavor going forward.")
    low_retention = [v for v in videos if v.get("avg_pct") is not None and v["avg_pct"] < 40]
    if low_retention:
        names = ", ".join(f"\"{v['topic']}\"" for v in low_retention[:3])
        recs.append(f"Low average view % on {names} — tighten the hook (first 2s) "
                     "and cut dead air; viewers are dropping before the payoff.")
    weak_hooks = [v for v in videos
                  if (v.get("retention") or {}).get("hook_hold_pct") is not None
                  and v["retention"]["hook_hold_pct"] < 70]
    if weak_hooks:
        names = ", ".join(f"\"{v['topic']}\"" for v in weak_hooks[:3])
        recs.append(f"Hook is losing viewers fast on {names} (<70% held by the 5% "
                     "mark) — the opening line needs a sharper shock stat or question.")
    low_subs = [v for v in videos if v.get("subs", 0) == 0 and v.get("views", 0) > 200]
    if low_subs:
        recs.append("Several videos with real views but zero new subscribers — "
                     "try an identity-based CTA ('Follow if you keep losing money "
                     "to tricks like this') instead of a generic one.")
    if not recs:
        recs.append("No red flags in current data — keep the daily cadence and "
                     "the series/playlist structure going.")
    return recs


def _channel_url() -> str | None:
    if upload_mod is None:
        return None
    try:
        return upload_mod.get_channel_url()
    except Exception:
        return None


CSS = """
:root{--bg:#0b0d12;--panel:#12151c;--panel2:#171b24;--border:#242934;--text:#e7e9ee;
--muted:#8b93a3;--accent:#5b8cff;--good:#3ecf8e;--warn:#ffb020;--bad:#ff5c5c}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);
color:var(--text);max-width:1080px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:22px;font-weight:600;margin:0}
h2{font-size:15px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;
margin:36px 0 14px}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.btn{background:var(--accent);color:#fff;border:none;padding:9px 16px;border-radius:8px;font-size:13px;
font-weight:600;cursor:pointer}
.btn:hover{filter:brightness(1.1)}
.btn.secondary{background:var(--panel2);color:var(--text);border:1px solid var(--border)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-top:18px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px}
.card .label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}
.card .value{font-size:24px;font-weight:700;margin-top:6px}
.card .value.good{color:var(--good)} .card .value.warn{color:var(--warn)} .card .value.bad{color:var(--bad)}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:20px}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end;margin-bottom:12px}
.field{display:flex;flex-direction:column;gap:5px}
.field label{font-size:12px;color:var(--muted)}
input[type=number],input[type=text]{background:var(--panel2);border:1px solid var(--border);color:var(--text);
padding:8px 10px;border-radius:7px;width:90px;font-size:13px}
ul.recs{margin:0;padding-left:20px} ul.recs li{margin:9px 0;font-size:14px;line-height:1.5}
table{border-collapse:collapse;width:100%;font-size:13px}
td,th{border-bottom:1px solid var(--border);padding:8px 10px;text-align:left}
th{color:var(--muted);font-weight:600;text-transform:uppercase;font-size:11px;letter-spacing:.04em}
tr:hover td{background:var(--panel2)}
.status-line{font-size:13px;color:var(--muted);margin-top:10px}
.toggle{display:flex;align-items:center;gap:8px;font-size:13px}
.msg{font-size:13px;margin-top:10px;min-height:16px}
.msg.ok{color:var(--good)} .msg.err{color:var(--bad)}
.banner{display:none;align-items:center;gap:10px;background:#1b2436;border:1px solid var(--accent);
border-radius:10px;padding:12px 16px;margin-bottom:18px;font-size:13px}
.banner.show{display:flex}
.spinner{width:14px;height:14px;border-radius:50%;border:2px solid #3a4258;border-top-color:var(--accent);
animation:spin 0.8s linear infinite;flex-shrink:0}
@keyframes spin{to{transform:rotate(360deg)}}
.vgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:16px;margin-top:4px}
.vcard{background:var(--panel2);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.vcard video{width:100%;aspect-ratio:9/16;background:#000;display:block}
.vcard.wide video{aspect-ratio:16/9}
.vgrid.wide-grid{grid-template-columns:repeat(auto-fill,minmax(280px,1fr))}
.vcard .vinfo{padding:10px}
.vcard .vtitle{font-size:12px;font-weight:600;line-height:1.3;margin-bottom:4px;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.vcard .vmeta{font-size:11px;color:var(--muted)}
.empty{color:var(--muted);font-size:13px}
.toast{position:fixed;bottom:20px;right:20px;background:var(--good);color:#06291a;padding:12px 18px;
border-radius:8px;font-size:13px;font-weight:600;box-shadow:0 6px 20px rgba(0,0,0,.4);
transform:translateY(20px);opacity:0;transition:all .25s;z-index:50}
.toast.show{transform:translateY(0);opacity:1}
"""

JS = """
async function postJSON(url, data){
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data||{})});
  return r.json();
}
async function buildQueue(){
  const n = document.getElementById('buildCount').value;
  const msg = document.getElementById('buildMsg');
  const res = await postJSON('/api/build-queue', {count: parseInt(n,10)});
  msg.textContent = res.ok ? ('Build started for ' + n + ' video(s).') : (res.error || 'A build is already running.');
  msg.className = 'msg ' + (res.ok ? 'ok' : 'err');
  refreshStatus();
}
async function saveMaintenance(){
  const target = document.getElementById('targetBuffer').value;
  const enabled = document.getElementById('maintEnabled').checked;
  const res = await postJSON('/api/maintenance', {target_buffer: parseInt(target,10), maintenance_enabled: enabled});
  const msg = document.getElementById('maintMsg');
  msg.textContent = res.ok ? 'Saved.' : (res.error || 'Save failed.');
  msg.className = 'msg ' + (res.ok ? 'ok' : 'err');
}
async function saveSchedule(){
  const data = {
    daily_hour: parseInt(document.getElementById('dailyHour').value,10),
    daily_minute: parseInt(document.getElementById('dailyMinute').value,10),
    refill_hour: parseInt(document.getElementById('refillHour').value,10),
    refill_minute: parseInt(document.getElementById('refillMinute').value,10),
    refill_count: parseInt(document.getElementById('refillCount').value,10),
  };
  const res = await postJSON('/api/schedule', data);
  const msg = document.getElementById('schedMsg');
  msg.textContent = res.ok ? 'Schedule updated and reloaded.' : (res.error || 'Save failed.');
  msg.className = 'msg ' + (res.ok ? 'ok' : 'err');
}
async function refreshStatus(){
  try{
    const r = await fetch('/api/status');
    const s = await r.json();
    const el = document.getElementById('buildStatus');
    if(el) el.textContent = s.build.running ? ('Building ' + s.build.target + ' video(s) in the background...') : 'Idle.';
  }catch(e){}
}
function showToast(msg){
  const t = document.getElementById('toast');
  if(!t) return;
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._hideTimer);
  t._hideTimer = setTimeout(() => t.classList.remove('show'), 6000);
}
async function refreshProgress(){
  try{
    const r = await fetch('/api/progress');
    const p = await r.json();
    const banner = document.getElementById('progressBanner');
    const text = document.getElementById('progressText');
    const statusR = await fetch('/api/status');
    const status = await statusR.json();
    if(status.build.running && p.step){
      banner.classList.add('show');
      text.textContent = 'Generating video: ' + p.step.msg;
    } else {
      banner.classList.remove('show');
    }
    if(p.last_queued && p.last_queued.ts !== window._lastQueuedTs){
      if(window._lastQueuedTs !== undefined){
        showToast('New video added to queue: ' + p.last_queued.msg.replace(/.*queued as /,'').replace(/.*LIVE: /,''));
      }
      window._lastQueuedTs = p.last_queued.ts;
    }
  }catch(e){}
}
function videoCardHTML(v){
  return '<div class="vcard" data-stamp="' + v.stamp + '">' +
    '<video controls preload="metadata" src="/media/' + v.stamp + '.mp4"></video>' +
    '<div class="vinfo"><div class="vtitle">' + (v.title || v.topic || v.stamp) + '</div>' +
    '<div class="vmeta">' + v.created + ' &middot; ' + v.size_mb + ' MB</div></div></div>';
}
function longformCardHTML(v){
  return '<div class="vcard wide" data-stamp="' + v.stamp + '">' +
    '<video controls preload="metadata" src="/media/' + v.stamp + '.mp4"></video>' +
    '<div class="vinfo"><div class="vtitle">' + v.title + '</div>' +
    '<div class="vmeta">' + v.created + ' &middot; ' + v.size_mb + ' MB</div></div></div>';
}
async function refreshLongformVideos(){
  try{
    const r = await fetch('/api/longform-videos');
    const videos = await r.json();
    const grid = document.getElementById('longformGrid');
    if(!grid) return;
    const known = window._knownLongformStamps || new Set();
    const current = new Set(videos.map(v => v.stamp));
    const changed = current.size !== known.size || [...current].some(s => !known.has(s));
    if(known.size > 0){
      for(const v of videos){
        if(!known.has(v.stamp)) showToast('New long-form video built: ' + v.title);
      }
    }
    window._knownLongformStamps = current;
    if(changed || grid.dataset.empty === '1'){
      grid.innerHTML = videos.length ? videos.map(longformCardHTML).join('') : '<div class="empty">No long-form videos built yet.</div>';
      grid.dataset.empty = videos.length ? '0' : '1';
    }
  }catch(e){}
}
async function refreshQueueVideos(){
  try{
    const r = await fetch('/api/queue-videos');
    const videos = await r.json();
    const grid = document.getElementById('videoGrid');
    if(!grid) return;
    const known = window._knownStamps || new Set();
    const current = new Set(videos.map(v => v.stamp));
    const changed = current.size !== known.size || [...current].some(s => !known.has(s));
    if(known.size > 0){
      for(const v of videos){
        if(!known.has(v.stamp)) showToast('New video added to queue: ' + (v.title || v.topic || v.stamp));
      }
    }
    window._knownStamps = current;
    if(changed || grid.dataset.empty === '1'){
      grid.innerHTML = videos.length ? videos.map(videoCardHTML).join('') : '<div class="empty">No videos buffered yet.</div>';
      grid.dataset.empty = videos.length ? '0' : '1';
    }
  }catch(e){}
}
setInterval(refreshStatus, 8000);
setInterval(refreshProgress, 5000);
setInterval(refreshQueueVideos, 10000);
setInterval(refreshLongformVideos, 15000);
window.addEventListener('DOMContentLoaded', () => { refreshStatus(); refreshProgress(); refreshQueueVideos(); refreshLongformVideos(); });
"""


def render(perf: dict | None, qdepth: int, history: list) -> str:
    recs = build_recommendations(perf, qdepth)
    rec_html = "".join(f"<li>{html.escape(r)}</li>" for r in recs)
    rows = ""
    for v in sorted((perf or {}).get("videos", []), key=lambda v: v.get("date", ""), reverse=True)[:20]:
        rows += (f"<tr><td>{html.escape(v.get('date',''))}</td>"
                 f"<td>{html.escape(v.get('topic',''))}</td>"
                 f"<td>{v.get('views','-')}</td>"
                 f"<td>{v.get('avg_pct','-')}</td>"
                 f"<td>{v.get('likes','-')}</td>"
                 f"<td>{v.get('comments','-')}</td>"
                 f"<td>{v.get('subs','-')}</td></tr>")
    fetched_at = (perf or {}).get("fetched_at", "never")
    qclass = "bad" if qdepth < 2 else ("warn" if qdepth < 4 else "good")
    chan_url = _channel_url()
    chan_link = (f'<a class="btn secondary" href="{html.escape(chan_url)}" target="_blank">View channel ↗</a>'
                 if chan_url else '<span class="status-line">Channel link unavailable — run python authorize.py</span>')

    sched = schedule.get_schedule()
    state = queue_manager.load_state()
    queue_videos = list_queue_videos()
    video_cards = "".join(
        f'<div class="vcard" data-stamp="{html.escape(v["stamp"])}">'
        f'<video controls preload="metadata" src="/media/{html.escape(v["stamp"])}.mp4"></video>'
        f'<div class="vinfo"><div class="vtitle">{html.escape(v["title"] or v["topic"] or v["stamp"])}</div>'
        f'<div class="vmeta">{html.escape(v["created"])} &middot; {v["size_mb"]} MB</div></div></div>'
        for v in queue_videos
    ) or '<div class="empty">No videos buffered yet.</div>'
    known_stamps_js = json.dumps([v["stamp"] for v in queue_videos])

    longform_videos = list_longform_videos()
    longform_cards = "".join(
        f'<div class="vcard wide" data-stamp="{html.escape(v["stamp"])}">'
        f'<video controls preload="metadata" src="/media/{html.escape(v["stamp"])}.mp4"></video>'
        f'<div class="vinfo"><div class="vtitle">{html.escape(v["title"])}</div>'
        f'<div class="vmeta">{html.escape(v["created"])} &middot; {v["size_mb"]} MB</div></div></div>'
        for v in longform_videos
    ) or '<div class="empty">No long-form videos built yet.</div>'

    return f"""<html><head><title>Channel Portal</title>
<style>{CSS}</style></head>
<body>
<div class="topbar">
  <h1>Channel Portal</h1>
  <div style="display:flex;gap:10px">{chan_link}<a class="btn secondary" href="/refresh">Refresh analytics</a></div>
</div>

<div id="progressBanner" class="banner"><div class="spinner"></div><span id="progressText"></span></div>

<div class="cards">
  <div class="card"><div class="label">Queue buffer</div><div class="value {qclass}">{qdepth}</div></div>
  <div class="card"><div class="label">Total posted</div><div class="value">{len(history)}</div></div>
  <div class="card"><div class="label">Analytics last fetched</div><div class="value" style="font-size:15px">{html.escape(fetched_at)}</div></div>
  <div class="card"><div class="label">Buffer maintenance</div><div class="value" style="font-size:15px">{'On' if state.get('maintenance_enabled', True) else 'Off'}</div></div>
</div>

<h2>What to do next</h2>
<div class="panel"><ul class="recs">{rec_html}</ul></div>

<h2>Queue buffer control</h2>
<div class="panel">
  <div class="row">
    <div class="field"><label>Videos to build</label><input type="number" id="buildCount" value="{max(1, state.get('target_buffer', 7) - qdepth)}" min="1" max="30"></div>
    <button class="btn" onclick="buildQueue()">Build now (background)</button>
  </div>
  <div id="buildStatus" class="status-line">Checking...</div>
  <div id="buildMsg" class="msg"></div>
  <div class="row" style="margin-top:18px">
    <div class="field"><label>Target buffer size</label><input type="number" id="targetBuffer" value="{state.get('target_buffer', 7)}" min="1" max="60"></div>
    <div class="toggle"><input type="checkbox" id="maintEnabled" {"checked" if state.get('maintenance_enabled', True) else ""}><label for="maintEnabled">Auto-maintain buffer</label></div>
    <button class="btn secondary" onclick="saveMaintenance()">Save</button>
  </div>
  <div id="maintMsg" class="msg"></div>
</div>

<h2>Schedule management</h2>
<div class="panel">
  <div class="row">
    <div class="field"><label>Daily post hour (0-23)</label><input type="number" id="dailyHour" value="{sched['daily']['hour'] if sched['daily']['hour'] is not None else 6}" min="0" max="23"></div>
    <div class="field"><label>Minute</label><input type="number" id="dailyMinute" value="{sched['daily']['minute'] if sched['daily']['minute'] is not None else 0}" min="0" max="59"></div>
    <span class="status-line">{'loaded' if sched['daily']['loaded'] else 'NOT loaded in launchd'}</span>
  </div>
  <div class="row">
    <div class="field"><label>Refill hour (0-23)</label><input type="number" id="refillHour" value="{sched['refill']['hour'] if sched['refill']['hour'] is not None else 14}" min="0" max="23"></div>
    <div class="field"><label>Minute</label><input type="number" id="refillMinute" value="{sched['refill']['minute'] if sched['refill']['minute'] is not None else 0}" min="0" max="59"></div>
    <div class="field"><label>Videos per refill</label><input type="number" id="refillCount" value="{sched['refill'].get('count', 1)}" min="1" max="10"></div>
    <span class="status-line">{'loaded' if sched['refill']['loaded'] else 'NOT loaded in launchd'}</span>
  </div>
  <button class="btn" onclick="saveSchedule()">Save &amp; reload schedule</button>
  <div id="schedMsg" class="msg"></div>
</div>

<h2>Buffered videos ({len(queue_videos)})</h2>
<div class="panel">
<div id="videoGrid" class="vgrid" data-empty="{'1' if not queue_videos else '0'}">{video_cards}</div>
</div>

<h2>Long-form videos ({len(longform_videos)})</h2>
<div class="panel">
<div id="longformGrid" class="vgrid wide-grid" data-empty="{'1' if not longform_videos else '0'}">{longform_cards}</div>
</div>

<h2>Recent videos</h2>
<div class="panel">
<table><tr><th>Date</th><th>Topic</th><th>Views</th><th>Avg % watched</th><th>Likes</th><th>Comments</th><th>Subs gained</th></tr>
{rows}</table>
</div>
<div id="toast" class="toast"></div>
<script>window._knownStamps = new Set({known_stamps_js});</script>
<script>window._knownLongformStamps = new Set({json.dumps([v["stamp"] for v in longform_videos])});</script>
<script>{JS}</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _serve_html(self, force: bool = False):
        perf = analytics.get_performance(force=force)
        qdepth = run_daily.queue_depth()
        history = script_gen.load_history()
        body = render(perf, qdepth, history).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _serve_media(self, stamp: str):
        if not _STAMP_RE.fullmatch(stamp):
            self.send_error(400, "Invalid video id")
            return
        if stamp.startswith("long_"):
            video_path = OUTPUT_DIR / f"{stamp}.mp4"
        else:
            video_path = QUEUE_DIR / stamp / "video.mp4"
        if not video_path.exists():
            self.send_error(404, "Video not found")
            return
        file_size = video_path.stat().st_size
        range_header = self.headers.get("Range")
        if range_header:
            m = re.match(r"bytes=(\d+)-(\d*)", range_header)
            start = int(m.group(1)) if m else 0
            end = int(m.group(2)) if m and m.group(2) else file_size - 1
            end = min(end, file_size - 1)
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            with open(video_path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(file_size))
            self.end_headers()
            with open(video_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

    def do_GET(self):
        if self.path.startswith("/refresh"):
            self._serve_html(force=True)
        elif self.path.startswith("/api/status"):
            self._send_json({"build": queue_manager.build_status(),
                              "queue_depth": run_daily.queue_depth(),
                              "state": queue_manager.load_state()})
        elif self.path.startswith("/api/progress"):
            self._send_json(read_build_progress())
        elif self.path.startswith("/api/queue-videos"):
            self._send_json(list_queue_videos())
        elif self.path.startswith("/api/longform-videos"):
            self._send_json(list_longform_videos())
        elif self.path.startswith("/media/"):
            stamp = self.path[len("/media/"):].split("?")[0]
            if stamp.endswith(".mp4"):
                stamp = stamp[:-4]
            self._serve_media(stamp)
        else:
            self._serve_html(force=False)

    def do_POST(self):
        data = self._read_json()
        try:
            if self.path.startswith("/api/build-queue"):
                count = int(data.get("count", 1))
                ok = queue_manager.trigger_build(count)
                self._send_json({"ok": ok, "error": None if ok else "A build is already running."})
            elif self.path.startswith("/api/maintenance"):
                queue_manager.save_state(
                    target_buffer=int(data.get("target_buffer", 7)),
                    maintenance_enabled=bool(data.get("maintenance_enabled", True)),
                )
                self._send_json({"ok": True})
            elif self.path.startswith("/api/schedule"):
                schedule.set_daily_schedule(int(data["daily_hour"]), int(data["daily_minute"]))
                schedule.set_refill_schedule(int(data["refill_hour"]), int(data["refill_minute"]),
                                              int(data["refill_count"]))
                self._send_json({"ok": True})
            else:
                self._send_json({"ok": False, "error": "unknown endpoint"}, status=404)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def log_message(self, fmt, *args):
        pass  # keep the console quiet


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    queue_manager.start_background_maintenance(run_daily.queue_depth)
    print(f"Portal running at http://localhost:{args.port}  (Ctrl+C to stop)")
    HTTPServer(("localhost", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
