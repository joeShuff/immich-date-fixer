#!/usr/bin/env python3
"""
immich_dates.py - fix Immich photos that have no capture date by reading the
date from the filename, with a review UI so you approve each change.

  pip install flask requests
  python immich_dates.py            # then open http://127.0.0.1:5055

Everything is configured in the web page: server URL, API key, timezone.
Settings and progress are stored in local SQLite files under DATA_DIR.
Each browser gets its own private profile (settings, API key, progress), keyed
by a random id in a cookie. The API keys are stored in plain text in DATA_DIR,
so keep that folder private. Anyone holding the cookie can use that profile.
"""
import argparse
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from flask import Flask, Response, abort, g, jsonify, request

# One SQLite file per browser, named after the random id in that browser's cookie.
DATA_DIR = os.environ.get("DATA_DIR", ".")
LEGACY_DB = "immich_dates.db"  # single-user file from earlier versions
os.makedirs(DATA_DIR, exist_ok=True)
DEFAULT_TIME = (12, 0, 0)  # used when the filename has a date but no time
PAGE_SIZE = 1000

# 20190314_153022 / 2019-03-14 15.30.22 / 2012-04-25 07.40 / IMG-20190314-WA0001
# The separator between year, month and day must be the same both times (or
# absent), so "1975-1104" inside a random ID is not read as a date.
FILENAME_RE = re.compile(
    r"(?<!\d)(?P<y>(?:19|20)\d\d)(?P<s>[-_.]?)(?P<mo>0[1-9]|1[0-2])(?P=s)(?P<d>0[1-9]|[12]\d|3[01])"
    r"(?:[-_ T.]?(?P<h>[01]\d|2[0-3])(?:(?P<ts>[-_.:]?)(?P<mi>[0-5]\d)(?P=ts)(?P<se>[0-5]\d)"
    r"|[-_.:](?P<mi2>[0-5]\d)))?(?!\d)"
)
# Filenames that are just a UUID / hash carry no date at all.
ID_RE = re.compile(
    r"^(?:[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}|[0-9a-f]{24,})$", re.I
)


def parse_filename(name):
    """Return 'YYYY-MM-DD HH:MM:SS' parsed from a filename, or None."""
    stem = name.rsplit(".", 1)[0]
    if ID_RE.match(stem):
        return None
    m = FILENAME_RE.search(name)
    if not m:
        return None
    g = m.groupdict()
    if g["h"] is not None:
        h, mi, s = int(g["h"]), int(g["mi"] or g["mi2"]), int(g["se"] or 0)
    else:
        h, mi, s = DEFAULT_TIME
    try:
        dt = datetime(int(g["y"]), int(g["mo"]), int(g["d"]), h, mi, s)
    except ValueError:
        return None
    if dt > datetime.now():
        return None
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------- storage
def db(uid=None):
    uid = uid or g.uid
    path = os.path.join(DATA_DIR, f"{uid}.db")
    if not os.path.exists(path):
        legacy = os.path.join(DATA_DIR, LEGACY_DB)
        if os.path.exists(legacy):  # first visitor adopts the pre-multi-user data
            try:
                os.rename(legacy, path)
            except OSError:
                pass
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS assets (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            perceived TEXT,
            proposed TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            approved_at TEXT,
            seen INTEGER
        );
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    return con


def meta_get(con):
    return {r["key"]: r["value"] for r in con.execute("SELECT * FROM meta")}


def meta_set(con, **kv):
    for k, v in kv.items():
        con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, str(v)))
    con.commit()


def normalise_url(url):
    url = (url or "").strip().rstrip("/")
    if url.endswith("/api"):
        url = url[:-4]
    if url and not re.match(r"^https?://", url):
        url = "http://" + url
    return url


def load_cfg(override=None):
    """Saved settings, optionally overlaid with values from the form."""
    m = meta_get(db())
    cfg = {
        "url": m.get("cfg_url", ""),
        "api_key": m.get("cfg_api_key", ""),
        "tz": m.get("cfg_tz", "Europe/London"),
        "include_mismatch": m.get("cfg_include_mismatch", "0") == "1",
    }
    for k, v in (override or {}).items():
        if k == "api_key" and not v:
            continue  # blank means "keep the saved key"
        if k in cfg and v is not None:
            cfg[k] = v
    cfg["url"] = normalise_url(cfg["url"])
    return cfg


def api(cfg, method, path, **kw):
    kw.setdefault("timeout", 60)
    return requests.request(
        method, f"{cfg['url']}/api{path}",
        headers={"x-api-key": cfg["api_key"], "Accept": "application/json"}, **kw,
    )


# ------------------------------------------------------------------------ scan
scans = {}  # uid -> progress of that user's scan
scan_lock = threading.Lock()


def new_state():
    return {"running": False, "scanned": 0, "found": 0, "error": None, "finished": None}


def run_scan(cfg, uid, scan_state):
    con = db(uid)
    run = int(time.time())
    scanned = no_exif = unparseable = found = others = 0
    try:
        me = api(cfg, "GET", "/users/me", timeout=30)
        me.raise_for_status()
        my_id = me.json()["id"]
        page = 1
        while page:
            r = api(cfg, "POST", "/search/metadata",
                    json={"page": page, "size": PAGE_SIZE, "withExif": True}, timeout=120)
            r.raise_for_status()
            assets = r.json()["assets"]
            for a in assets["items"]:
                if a.get("ownerId") and a["ownerId"] != my_id:
                    others += 1  # partner-shared: read-only for this key, can't be fixed
                    continue
                scanned += 1
                exif_dt = (a.get("exifInfo") or {}).get("dateTimeOriginal")
                if not exif_dt:
                    no_exif += 1
                proposed = parse_filename(a.get("originalFileName", ""))
                if not proposed:
                    if not exif_dt:
                        unparseable += 1
                    continue
                shown = (a.get("localDateTime") or a.get("fileCreatedAt") or "")[:19]
                perceived = shown.replace("T", " ")
                if not exif_dt:
                    reason = "No date stored in the photo's metadata"
                elif cfg["include_mismatch"] and perceived[:10] != proposed[:10]:
                    reason = "Metadata date differs from the filename"
                else:
                    continue
                found += 1
                con.execute(
                    """
                    INSERT INTO assets (id, filename, perceived, proposed, reason, seen)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        filename=excluded.filename, perceived=excluded.perceived,
                        proposed=excluded.proposed, reason=excluded.reason,
                        seen=excluded.seen
                    WHERE assets.status != 'approved'
                    """,
                    (a["id"], a["originalFileName"], perceived, proposed, reason, run),
                )
            con.commit()
            scan_state.update(scanned=scanned, found=found)
            nxt = assets.get("nextPage")
            page = int(nxt) if nxt else 0

        # pending items no longer discrepant (fixed elsewhere or deleted)
        con.execute("DELETE FROM assets WHERE status='pending' AND seen != ?", (run,))
        meta_set(con, scanned=scanned, no_exif=no_exif, unparseable=unparseable, others=others,
                 scanned_at=datetime.now().strftime("%Y-%m-%d %H:%M"))
        scan_state["finished"] = time.time()
    except Exception as e:  # shown in the UI
        scan_state["error"] = str(e)
    finally:
        scan_state["running"] = False
        con.close()


# ------------------------------------------------------------------------- web
app = Flask(__name__)
UID_RE = re.compile(r"^[0-9a-f]{32}$")


@app.before_request
def identify_user():
    uid = request.cookies.get("uid", "")
    g.new_uid = not UID_RE.match(uid)  # also rejects anything path-like
    g.uid = uuid.uuid4().hex if g.new_uid else uid


@app.after_request
def remember_user(resp):
    if g.get("new_uid"):
        resp.set_cookie("uid", g.uid, max_age=10 * 365 * 24 * 3600, httponly=True,
                        samesite="Lax", secure=request.is_secure)
    return resp


@app.get("/")
def index():
    return PAGE


@app.get("/api/settings")
def get_settings():
    cfg = load_cfg()
    key = cfg["api_key"]
    return jsonify(url=cfg["url"], tz=cfg["tz"], include_mismatch=cfg["include_mismatch"],
                   has_key=bool(key), key_hint=("…" + key[-4:]) if key else "")


def validate_tz(name):
    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False


@app.post("/api/settings")
def save_settings():
    d = request.get_json(force=True)
    if not validate_tz(d.get("tz", "")):
        return jsonify(error=f"Unknown timezone “{d.get('tz')}”. Use a name like Europe/London."), 400
    con = db()
    meta_set(con, cfg_url=normalise_url(d.get("url")), cfg_tz=d["tz"],
             cfg_include_mismatch="1" if d.get("include_mismatch") else "0")
    if d.get("api_key"):
        meta_set(con, cfg_api_key=d["api_key"].strip())
    return jsonify(ok=True)


@app.post("/api/test")
def test_connection():
    cfg = load_cfg(request.get_json(force=True))
    steps = []

    def step(name, ok, detail):
        steps.append({"name": name, "ok": ok, "detail": detail})
        return ok

    if not cfg["url"]:
        return jsonify(ok=False, steps=[{"name": "Server URL", "ok": False, "detail": "Enter your Immich server URL."}])
    try:
        r = requests.get(f"{cfg['url']}/api/server/ping", timeout=10)
        if not step("Reach server", r.ok, "Immich responded." if r.ok else f"HTTP {r.status_code} from {cfg['url']}"):
            return jsonify(ok=False, steps=steps)
    except requests.RequestException as e:
        step("Reach server", False, f"Could not connect to {cfg['url']}: {e.__class__.__name__}")
        return jsonify(ok=False, steps=steps)
    if not cfg["api_key"]:
        step("API key", False, "Enter an API key.")
        return jsonify(ok=False, steps=steps)
    try:
        r = api(cfg, "GET", "/users/me", timeout=15)
        if r.ok:
            u = r.json()
            step("API key", True, f"Signed in as {u.get('name') or u.get('email')}.")
        else:
            step("API key", False, f"Immich rejected the key (HTTP {r.status_code}).")
            return jsonify(ok=False, steps=steps)
        r = api(cfg, "POST", "/search/metadata", json={"page": 1, "size": 1, "withExif": True}, timeout=30)
        step("Read photos", r.ok,
             "Can search your library." if r.ok else
             f"HTTP {r.status_code}. The key needs asset.read permission.")
    except requests.RequestException as e:
        step("API key", False, f"Request failed: {e.__class__.__name__}")
    if not validate_tz(cfg["tz"]):
        step("Timezone", False, f"Unknown timezone “{cfg['tz']}”.")
    return jsonify(ok=all(s["ok"] for s in steps), steps=steps)


@app.post("/api/scan")
def start_scan():
    d = request.get_json(force=True)
    with scan_lock:
        scan_state = scans.setdefault(g.uid, new_state())
        if scan_state["running"]:
            return jsonify(error="A scan is already running."), 409
        if not validate_tz(d.get("tz", "")):
            return jsonify(error=f"Unknown timezone “{d.get('tz')}”."), 400
        con = db()
        meta_set(con, cfg_url=normalise_url(d.get("url")), cfg_tz=d["tz"],
                 cfg_include_mismatch="1" if d.get("include_mismatch") else "0")
        if d.get("api_key"):
            meta_set(con, cfg_api_key=d["api_key"].strip())
        cfg = load_cfg()
        if not cfg["url"] or not cfg["api_key"]:
            return jsonify(error="Enter the server URL and API key first."), 400
        scan_state.update(running=True, scanned=0, found=0, error=None, finished=None)
    threading.Thread(target=run_scan, args=(cfg, g.uid, scan_state), daemon=True).start()
    return jsonify(ok=True)


@app.get("/api/scan/status")
def scan_status():
    return jsonify(scans.get(g.uid) or new_state())


@app.get("/api/stats")
def stats():
    con = db()
    c = lambda w: con.execute(f"SELECT COUNT(*) FROM assets WHERE {w}").fetchone()[0]
    m = meta_get(con)
    return jsonify(
        total=c("1=1"), approved=c("status='approved'"), pending=c("status='pending'"),
        scanned=m.get("scanned"), no_exif=m.get("no_exif"),
        unparseable=m.get("unparseable"), others=m.get("others"),
        scanned_at=m.get("scanned_at"),
    )


@app.get("/api/next")
def next_item():
    skipped = {s for s in request.args.get("skipped", "").split(",") if s}
    rows = db().execute(
        "SELECT * FROM assets WHERE status='pending' ORDER BY proposed, id LIMIT ?",
        (len(skipped) + 1,),
    ).fetchall()
    for r in rows:
        if r["id"] not in skipped:
            return jsonify(dict(r))
    return jsonify(None)


@app.post("/api/approve/<asset_id>")
def approve(asset_id):
    con = db()
    row = con.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    if not row:
        abort(404)
    cfg = load_cfg()
    chosen = (request.get_json(silent=True) or {}).get("date") or row["proposed"]
    try:
        naive = datetime.strptime(chosen, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return jsonify(error="That date isn't valid. Use year, month, day and time."), 400
    local = naive.replace(tzinfo=ZoneInfo(cfg["tz"]))
    try:
        r = api(cfg, "PUT", f"/assets/{asset_id}", json={"dateTimeOriginal": local.isoformat()})
    except requests.RequestException as e:
        return jsonify(error=f"Could not reach Immich: {e.__class__.__name__}"), 502
    if not r.ok:
        return jsonify(error=f"Immich returned {r.status_code}: {r.text[:200]}"), 502
    con.execute("UPDATE assets SET status='approved', approved_at=?, proposed=? WHERE id=?",
                (datetime.now().isoformat(timespec="seconds"), chosen, asset_id))
    con.commit()
    return jsonify(ok=True)


@app.get("/thumb/<asset_id>")
def thumb(asset_id):
    try:
        r = api(load_cfg(), "GET", f"/assets/{asset_id}/thumbnail", params={"size": "preview"})
    except requests.RequestException:
        abort(502)
    if not r.ok:
        abort(r.status_code)
    return Response(r.content, content_type=r.headers.get("Content-Type", "image/jpeg"))


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="color-scheme" content="light dark">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Immich date fixer</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #e9ecef; --panel: #fbfbfa; --ink: #1c2430; --muted: #66707c;
    --line: #cfd5db; --go: #1e6b5c; --go-ink: #fff; --warn: #a3342b;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #14181d; --panel: #1d232b; --ink: #e8ebee; --muted: #8d98a5;
            --line: #323b46; --go: #3aa58f; --go-ink: #0c1512; --warn: #e07a6e; }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink);
         font: 16px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
  main { max-width: 760px; margin: 0 auto; padding: 20px 16px 40px; }
  .panel { background: var(--panel); border: 1px solid var(--line); margin-bottom: 18px; }
  summary { padding: 12px 16px; cursor: pointer; font-weight: 600; }
  .form { padding: 4px 16px 16px; display: grid; gap: 12px; }
  label { display: grid; gap: 4px; font-size: .9rem; color: var(--muted); }
  label.check { display: flex; gap: 8px; align-items: flex-start; color: var(--ink); }
  input[type=text], input[type=password] { font: inherit; padding: 9px 10px; border: 1px solid var(--line);
         background: var(--bg); color: var(--ink); width: 100%; }
  input:focus-visible, button:focus-visible, summary:focus-visible { outline: 3px solid var(--go); outline-offset: 2px; }
  .hint { font-size: .8rem; color: var(--muted); }
  .row { display: flex; gap: 10px; flex-wrap: wrap; }
  button { font: inherit; padding: 11px 16px; border: 1px solid var(--line);
           background: transparent; color: var(--ink); cursor: pointer; }
  button.go { background: var(--go); color: var(--go-ink); border-color: var(--go); font-weight: 600; }
  button:disabled { opacity: .5; cursor: wait; }
  .steps { margin: 0; padding: 0; list-style: none; font-size: .9rem; }
  .steps li { padding: 2px 0; }
  .ok { color: var(--go); } .bad { color: var(--warn); }
  .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1px;
           background: var(--line); border: 1px solid var(--line); }
  .stats div { background: var(--panel); padding: 10px 12px; }
  .stats b { display: block; font-size: 1.5rem; font-variant-numeric: tabular-nums; }
  .stats span { color: var(--muted); font-size: .85rem; }
  .bar { height: 6px; background: var(--line); margin: 10px 0 4px; }
  .bar i { display: block; height: 100%; background: var(--go); width: 0; transition: width .3s; }
  .note { color: var(--muted); font-size: .85rem; margin: 0 0 18px; }
  .card { background: var(--panel); border: 1px solid var(--line); }
  .photo { background: #0003; height: clamp(200px, 38vh, 420px); height: clamp(200px, 38dvh, 420px); }
  .photo img { width: 100%; height: 100%; display: block; object-fit: contain; }
  .info { padding: 16px; }
  .file { font-weight: 600; word-break: break-all; margin: 0 0 2px; min-height: 1.45em; }
  .reason { color: var(--muted); font-size: .85rem; margin: 0 0 14px; }
  .dates { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .dates h3 { margin: 0; font-size: .85rem; font-weight: 500; color: var(--muted); }
  .dates p { margin: 2px 0 0; font-size: 1.25rem; font-variant-numeric: tabular-nums; }
  .dates .new input { font: inherit; font-size: 1.1rem; font-weight: 700; color: var(--go); background: var(--bg);
         border: 1px solid var(--line); padding: 5px 8px; margin-top: 2px; max-width: 100%; font-variant-numeric: tabular-nums; }
  .dates .new input:focus-visible { outline: 3px solid var(--go); outline-offset: 2px; }
  .actions { display: flex; gap: 10px; margin-top: 16px; }
  button { touch-action: manipulation; }
  .actions button { flex: 1; padding: 12px 18px; }
  .msg { padding: 36px 16px; text-align: center; }
  .err { color: var(--warn); margin: 10px 0 0; min-height: 1.2em; font-size: .9rem; }
  .keys { color: var(--muted); font-size: .8rem; margin-top: 10px; text-align: center; }
  @media (hover: none) { .keys { display: none; } }
  @media (max-width: 600px) {
    main { padding: 10px 10px calc(16px + env(safe-area-inset-bottom)); }
    .stats { grid-template-columns: repeat(2, 1fr); }
    .stats div { padding: 8px 10px; }
    .stats b { font-size: 1.25rem; }
    .photo { height: clamp(160px, 32dvh, 320px); }
    .info { padding: 12px; }
    .dates { grid-template-columns: 1fr; gap: 10px; }
    .dates p { font-size: 1.15rem; }
    .dates .new input { width: 100%; font-size: 1.15rem; padding: 10px; }
    .form { padding: 4px 12px 14px; }
    .row button { flex: 1 1 100%; min-height: 48px; }
    input[type=text], input[type=password] { padding: 12px 10px; }
    /* thumb-reachable action bar that stays on screen while the card scrolls */
    .actions { position: sticky; bottom: 0; margin: 12px -12px -12px; padding: 10px 12px calc(10px + env(safe-area-inset-bottom));
               background: var(--panel); border-top: 1px solid var(--line); }
    .actions button { min-height: 52px; font-size: 1.05rem; }
  }
</style></head>
<body><main>
  <details class="panel" id="settings">
    <summary>Immich connection</summary>
    <div class="form">
      <label>Server URL
        <input type="text" id="url" placeholder="http://192.168.1.10:2283" autocomplete="off">
      </label>
      <label>API key
        <input type="password" id="key" autocomplete="off">
        <span class="hint" id="keyhint">Create one in Immich under Account settings, API keys. It needs asset.read, asset.update and asset.view.</span>
      </label>
      <label>Timezone of the times in your filenames
        <input type="text" id="tz" placeholder="Europe/London" autocomplete="off">
      </label>
      <label class="check"><input type="checkbox" id="mismatch">
        <span>Also include photos that do have a metadata date, if its day differs from the filename</span></label>
      <div class="row">
        <button id="test">Test connectivity</button>
        <button id="start" class="go">Start processing dates</button>
      </div>
      <ul class="steps" id="steps" aria-live="polite"></ul>
      <p class="hint">These settings and your progress are private to this browser, kept by a cookie. Clearing cookies starts a fresh profile.</p>
      <p class="hint" id="progress" aria-live="polite"></p>
    </div>
  </details>

  <div class="stats">
    <div><b id="s-total">–</b><span>with date problems</span></div>
    <div><b id="s-done">–</b><span>approved</span></div>
    <div><b id="s-left">–</b><span>still to review</span></div>
    <div><b id="s-skip">0</b><span>skipped this session</span></div>
  </div>
  <div class="bar"><i id="bar"></i></div>
  <p class="note" id="note"></p>

  <div class="card" id="card" hidden>
    <div class="photo"><img id="img" alt="Preview of the photo under review"></div>
    <div class="info">
      <p class="file" id="file"></p>
      <p class="reason" id="reason"></p>
      <div class="dates">
        <div><h3>Date Immich shows now</h3><p id="old"></p></div>
        <div class="new"><h3><label for="new">Date to apply (from the filename, editable)</label></h3><input type="datetime-local" id="new" step="1"></div>
      </div>
      <p class="err" id="err" role="alert"></p>
      <div class="actions">
        <button id="skip">Skip</button>
        <button id="ok" class="go">Approve</button>
      </div>
      <p class="keys">Keys: A or Enter to approve, S to skip. Edit the date first if it's wrong.</p>
    </div>
  </div>
  <div class="card msg" id="empty" hidden></div>
</main>
<script>
const $ = id => document.getElementById(id);
const skipped = new Set(JSON.parse(sessionStorage.getItem("skipped") || "[]"));
let current = null, busy = false, hasKey = false;

const form = () => ({ url: $("url").value, api_key: $("key").value,
                      tz: $("tz").value.trim(), include_mismatch: $("mismatch").checked });
const post = (u, body) => fetch(u, { method: "POST", headers: { "Content-Type": "application/json" },
                                      body: JSON.stringify(body || {}) });

async function loadSettings() {
  const s = await (await fetch("/api/settings")).json();
  $("url").value = s.url; $("tz").value = s.tz; $("mismatch").checked = s.include_mismatch;
  hasKey = s.has_key;
  $("key").placeholder = s.has_key ? "Saved (" + s.key_hint + "). Leave blank to keep it." : "";
  if (!s.url || !s.has_key) $("settings").open = true;
}

function showSteps(steps) {
  $("steps").innerHTML = "";
  for (const s of steps) {
    const li = document.createElement("li");
    li.className = s.ok ? "ok" : "bad";
    li.textContent = (s.ok ? "✓ " : "✗ ") + s.name + ": " + s.detail;
    $("steps").appendChild(li);
  }
}

$("test").onclick = async () => {
  $("test").disabled = true; $("steps").innerHTML = "<li>Testing…</li>";
  try { showSteps((await (await post("/api/test", form())).json()).steps); }
  catch (e) { showSteps([{ name: "App", ok: false, detail: String(e) }]); }
  $("test").disabled = false;
};

$("start").onclick = async () => {
  $("steps").innerHTML = "";
  const r = await post("/api/scan", form());
  if (!r.ok) { showSteps([{ name: "Start", ok: false, detail: (await r.json()).error }]); return; }
  $("key").value = ""; pollScan();
};

async function pollScan() {
  $("start").disabled = $("test").disabled = true;
  while (true) {
    const s = await (await fetch("/api/scan/status")).json();
    if (s.error) { $("progress").textContent = ""; showSteps([{ name: "Processing", ok: false, detail: s.error }]); break; }
    if (!s.running) { $("progress").textContent = `Finished. ${s.scanned} photos checked, ${s.found} proposals saved.`; break; }
    $("progress").textContent = `Processing… ${s.scanned} photos checked, ${s.found} proposals so far.`;
    await new Promise(r => setTimeout(r, 1000));
  }
  $("start").disabled = $("test").disabled = false;
  loadSettings(); loadNext();
}

async function loadStats() {
  const s = await (await fetch("/api/stats")).json();
  $("s-total").textContent = s.total;
  $("s-done").textContent = s.approved;
  $("s-left").textContent = s.pending;
  $("s-skip").textContent = skipped.size;
  $("bar").style.width = (s.total ? 100 * s.approved / s.total : 0) + "%";
  $("note").textContent = s.scanned
    ? `Last processed ${s.scanned_at}: ${s.scanned} photos, ${s.no_exif} without a metadata date, `
      + `${s.unparseable} of those have no date in the filename.`
      + (+s.others ? ` Ignored ${s.others} photos shared by other users (read-only).` : "")
    : "";
  return s;
}

async function loadNext() {
  const s = await loadStats();
  const item = await (await fetch("/api/next?skipped=" + [...skipped].join(","))).json();
  current = item;
  $("err").textContent = "";
  if (!item) {
    $("card").hidden = true; $("empty").hidden = false;
    $("empty").textContent = !s.total
      ? (s.scanned ? "No photos need fixing." : "Nothing to review yet. Enter your connection details and choose Start processing dates.")
      : skipped.size
        ? "No more items this session. Reload to see the skipped ones again."
        : "All done. Every photo with a date in its filename has been resolved.";
    return;
  }
  $("empty").hidden = true; $("card").hidden = false;
  $("img").src = "/thumb/" + item.id;
  $("file").textContent = item.filename;
  $("reason").textContent = item.reason;
  $("old").textContent = item.perceived || "unknown";
  $("new").value = item.proposed.replace(" ", "T");
}

async function approve() {
  if (!current || busy) return;
  busy = true; $("ok").disabled = $("skip").disabled = true;
  try {
    let v = $("new").value;
    if (!v) { $("err").textContent = "Enter a date and time first."; return; }
    if (v.length === 16) v += ":00";
    const r = await post("/api/approve/" + current.id, { date: v.replace("T", " ") });
    if (!r.ok) { $("err").textContent = (await r.json()).error || "Approval failed."; return; }
    await loadNext();
  } catch (e) { $("err").textContent = "Could not reach the app: " + e; }
  finally { busy = false; $("ok").disabled = $("skip").disabled = false; }
}

function skip() {
  if (!current || busy) return;
  skipped.add(current.id);
  sessionStorage.setItem("skipped", JSON.stringify([...skipped]));
  loadNext();
}

$("ok").onclick = approve; $("skip").onclick = skip;
addEventListener("keydown", e => {
  if (document.activeElement.id === "new" && e.key === "Enter") { approve(); return; }
  if (/INPUT|TEXTAREA/.test(document.activeElement.tagName)) return;
  if (e.key === "a" || e.key === "Enter") approve();
  if (e.key === "s") skip();
});

(async () => {
  await loadSettings();
  const st = await (await fetch("/api/scan/status")).json();
  if (st.running) pollScan(); else loadNext();
})();
</script></body></html>
"""

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Immich filename-date fixer")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5055)))
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args()
    print(f"Open http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)