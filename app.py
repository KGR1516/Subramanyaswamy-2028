"""
app.py
------
Local one-click multi-agent stock-analysis dashboard.

  python app.py  ->  http://127.0.0.1:<PORT>

A background thread runs the analysis cycle and drives agent status.
The page polls /status (~500 ms) and re-renders. No cloud backend; the
app itself POSTs BUY signals to the Telegram Bot API.
"""

import os
import re
import json
import sqlite3
import threading
import time
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory, Response

import data_sources
import llm
import tracker

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "audit.db")


# ---------------------------------------------------------------------------
# tiny .env loader (no hard dependency on python-dotenv)
# ---------------------------------------------------------------------------
def load_env():
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_env()

BRAND = os.environ.get("BRAND", "Bourse")
CONFIDENCE_THRESHOLD = int(os.environ.get("CONFIDENCE_THRESHOLD", "7"))
AGENT_DELAY = float(os.environ.get("AGENT_DELAY", "0.6"))
SHORTLIST_PER_BUCKET = int(os.environ.get("SHORTLIST_PER_BUCKET", "4"))
PORT = int(os.environ.get("PORT", "5000"))


# ---------------------------------------------------------------------------
# agent roster (drives the UI cards)
# ---------------------------------------------------------------------------
AGENTS = [
    {"id": "scout", "name": "Scout", "icon": "🔭",
     "role": "screens the stock universe for movers",
     "stat1": "Scanned", "stat2": "Shortlisted"},
    {"id": "technician", "name": "Technician", "icon": "📈",
     "role": "reads price action, RVOL & trend",
     "stat1": "Analyzed", "stat2": "Avg RVOL"},
    {"id": "fundamentalist", "name": "Fundamentalist", "icon": "📊",
     "role": "weighs valuation & analyst targets",
     "stat1": "Covered", "stat2": "Avg upside"},
    {"id": "newsdesk", "name": "Newsdesk", "icon": "📰",
     "role": "pulls live news & scores sentiment",
     "stat1": "Headlines", "stat2": "Net tone"},
    {"id": "bull", "name": "Bull", "icon": "🐂",
     "role": "argues the case to buy",
     "stat1": "Cases", "stat2": "Avg score"},
    {"id": "bear", "name": "Bear", "icon": "🐻",
     "role": "argues the case against",
     "stat1": "Cases", "stat2": "Avg score"},
    {"id": "judge", "name": "Judge", "icon": "⚖️",
     "role": "weighs the debate, issues verdict + confidence",
     "stat1": "Verdicts", "stat2": "Buy"},
    {"id": "messenger", "name": "Messenger", "icon": "📡",
     "role": "sends signals to Telegram",
     "stat1": "Sent", "stat2": "Engine"},
]
PIPELINE = [a["id"] for a in AGENTS]


# ---------------------------------------------------------------------------
# shared state
# ---------------------------------------------------------------------------
STATE_LOCK = threading.Lock()


def _fresh_state():
    return {
        "running": False,
        "mode": "demo",
        "engine": "-",
        "started_at": None,
        "finished_at": None,
        "timestamp": "-",
        "kpis": {"universe": 0, "in_debate": 0, "buy_signals": 0,
                 "top_pick": None, "top_conf": None},
        "agents": {a["id"]: {"status": "offline", "s1": 0, "s2": 0}
                   for a in AGENTS},
        "verdicts": [],
        "log": [],
    }


STATE = _fresh_state()


def _log(msg):
    """Append to the in-memory log; scrub any Telegram token."""
    msg = re.sub(r"bot\d+:[\w-]+", "bot***:***", msg)
    STATE["log"].append(f"{datetime.now().strftime('%H:%M:%S')}  {msg}")
    STATE["log"][:] = STATE["log"][-60:]


def _set_agent(aid, status=None, s1=None, s2=None):
    with STATE_LOCK:
        a = STATE["agents"][aid]
        if status is not None:
            a["status"] = status
        if s1 is not None:
            a["s1"] = s1
        if s2 is not None:
            a["s2"] = s2


# ---------------------------------------------------------------------------
# SQLite audit
# ---------------------------------------------------------------------------
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT, finished_at TEXT,
            mode TEXT, engine TEXT,
            universe INT, in_debate INT, buy_signals INT
        );
        CREATE TABLE IF NOT EXISTS verdicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INT, ts TEXT, symbol TEXT, cap TEXT,
            verdict TEXT, confidence INT, winner TEXT,
            rationale TEXT, key_catalyst TEXT,
            price REAL, day_change_pct REAL, fired INT
        );
    """)
    con.commit()
    con.close()


def _save_run(state):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO runs (started_at, finished_at, mode, engine, universe, in_debate, buy_signals) "
        "VALUES (?,?,?,?,?,?,?)",
        (state["started_at"], state["finished_at"], state["mode"], state["engine"],
         state["kpis"]["universe"], state["kpis"]["in_debate"], state["kpis"]["buy_signals"]),
    )
    run_id = cur.lastrowid
    for v in state["verdicts"]:
        cur.execute(
            "INSERT INTO verdicts (run_id, ts, symbol, cap, verdict, confidence, winner, "
            "rationale, key_catalyst, price, day_change_pct, fired) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, v["ts"], v["symbol"], v["cap"], v["verdict"], v["confidence"],
             v["winner"], v["rationale"], v["key_catalyst"], v["price"],
             v["day_change_pct"], 1 if v["fired"] else 0),
        )
    con.commit()
    con.close()
    return run_id


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def _tg_send(html):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        _log("Telegram not configured — skipping send (analysis still logged)")
        return False
    try:
        import requests
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": html, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=20,
        )
        ok = r.ok and r.json().get("ok", False)
        _log("Telegram message sent" if ok else "Telegram send failed")
        return ok
    except Exception as e:
        _log(f"Telegram error: {type(e).__name__}")
        return False


def _buy_message(v):
    return (
        f"🟢 <b>BUY SIGNAL — {v['symbol']}</b> ({v['cap']} cap)\n\n"
        f"Verdict: BUY | Confidence: {v['confidence']}/10\n"
        f"Winner: {v['winner']}\n"
        f"Why: {v['rationale']}\n"
        f"Key catalyst: {v['key_catalyst']}\n"
        f"Live price: ₹{v['price']} | Day change: {v['day_change_pct']}%\n\n"
        f"— Analysis only. No trade was placed. Not investment advice."
    )


def _summary_message(fired, ts):
    if not fired:
        body = "No BUY signals fired."
    else:
        body = "\n".join(
            f"• {v['symbol']} ({v['cap']}) — {v['confidence']}/10" for v in fired
        )
    return (f"📋 <b>{BRAND} daily summary</b>\n{ts}\n\n{body}\n\n"
            f"— Analysis only. Not investment advice.")


# ---------------------------------------------------------------------------
# the cycle
# ---------------------------------------------------------------------------
def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else 0


def run_cycle(mode):
    with STATE_LOCK:
        STATE.update(_fresh_state())
        STATE["running"] = True
        STATE["mode"] = mode
        STATE["started_at"] = datetime.now().isoformat(timespec="seconds")

    engine = llm.detect_engine()
    with STATE_LOCK:
        STATE["engine"] = engine
    _log(f"Run started — mode={mode}, engine={engine}")

    # ---- Scout: gather evidence ------------------------------------------
    _set_agent("scout", "working")
    try:
        if mode == "live":
            def prog(scanned, shortlisted):
                _set_agent("scout", "working", s1=scanned, s2=shortlisted)
                with STATE_LOCK:
                    STATE["kpis"]["universe"] = scanned
            bundles, scanned, shortlisted = data_sources.fetch_live_bundles(
                SHORTLIST_PER_BUCKET, progress=prog)
        else:
            bundles = data_sources.load_demo_bundles()
            scanned = shortlisted = len(bundles)
    except Exception as e:
        _log(f"Data error: {e} — falling back to demo bundles")
        bundles = data_sources.load_demo_bundles()
        scanned = shortlisted = len(bundles)

    ts = data_sources.timestamp_ist()
    with STATE_LOCK:
        STATE["timestamp"] = ts
        STATE["kpis"]["universe"] = scanned
        STATE["kpis"]["in_debate"] = len(bundles)
    _set_agent("scout", "done", s1=scanned, s2=shortlisted)
    time.sleep(AGENT_DELAY)

    if not bundles:
        _log("No evidence bundles available — nothing to analyze")
        _finish([], mode, engine)
        return

    # ---- analysis-stage agents animate in order --------------------------
    for aid in ("technician", "fundamentalist", "newsdesk", "bull", "bear", "judge"):
        _set_agent(aid, "working")
        time.sleep(AGENT_DELAY * 0.5)

    verdicts, fired = [], []
    rvols, upsides, headlines, net_tone = [], [], 0, 0
    bull_scores, bear_scores = [], []

    for i, ev in enumerate(bundles, 1):
        result = llm.evaluate(ev, engine=engine)
        vd = result["verdict"]
        sc = result["scores"]

        rvols.append(ev["technicals"].get("rvol"))
        upsides.append(ev["analyst"].get("upside_pct"))
        headlines += ev["news"].get("total", 0) or 0
        net_tone += (ev["news"].get("positive", 0) or 0) - (ev["news"].get("negative", 0) or 0)
        bull_scores.append(sc["bull"]["score"])
        bear_scores.append(sc["bear"]["score"])

        row = {
            "ts": datetime.now().strftime("%H:%M:%S"),
            "symbol": ev["symbol"],
            "name": ev.get("name", ev["symbol"]),
            "cap": ev["cap_segment"],
            "verdict": vd["verdict"],
            "confidence": vd["confidence"],
            "winner": vd["winner"],
            "rationale": vd["rationale"],
            "key_catalyst": vd["key_catalyst"],
            "price": ev["price"].get("live"),
            "day_change_pct": ev["price"].get("day_change_pct"),
            "engine": result.get("engine", engine),
            "fired": False,
        }
        if vd["verdict"] == "BUY" and vd["confidence"] >= CONFIDENCE_THRESHOLD:
            row["fired"] = True
            fired.append(row)

        verdicts.append(row)

        # live-update agent stats + verdict feed + KPIs
        with STATE_LOCK:
            STATE["verdicts"] = list(reversed(verdicts))
            STATE["kpis"]["buy_signals"] = len(fired)
            if fired:
                top = max(fired, key=lambda r: r["confidence"])
                STATE["kpis"]["top_pick"] = top["symbol"]
                STATE["kpis"]["top_conf"] = top["confidence"]
        _set_agent("technician", "working", s1=i, s2=_avg(rvols))
        _set_agent("fundamentalist", "working", s1=i, s2=_avg(upsides))
        _set_agent("newsdesk", "working", s1=headlines, s2=net_tone)
        _set_agent("bull", "working", s1=i, s2=_avg(bull_scores))
        _set_agent("bear", "working", s1=i, s2=_avg(bear_scores))
        _set_agent("judge", "working", s1=i, s2=len(fired))
        time.sleep(AGENT_DELAY * 0.4)

    for aid in ("technician", "fundamentalist", "newsdesk", "bull", "bear", "judge"):
        _set_agent(aid, "done")
    _log(f"Debate complete — {len(fired)} BUY signal(s) at confidence ≥ {CONFIDENCE_THRESHOLD}")

    # ---- Messenger: Telegram --------------------------------------------
    _set_agent("messenger", "working", s2=engine)
    sent = 0
    for v in fired:
        if _tg_send(_buy_message(v)):
            sent += 1
        time.sleep(0.2)
    _tg_send(_summary_message(fired, ts))
    _set_agent("messenger", "done", s1=sent, s2=engine)

    _finish(verdicts, mode, engine, fired)


def _finish(verdicts, mode, engine, fired=None):
    with STATE_LOCK:
        STATE["running"] = False
        STATE["finished_at"] = datetime.now().isoformat(timespec="seconds")
        STATE["verdicts"] = list(reversed(verdicts))
        run_state = json.loads(json.dumps(STATE))  # snapshot
    try:
        rid = _save_run(run_state)
        _log(f"Run #{rid} saved to SQLite ({len(verdicts)} verdicts)")
    except Exception as e:
        _log(f"SQLite save failed: {e}")


# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.get("/")
def index():
    return send_from_directory(HERE, "dashboard.html")


@app.get("/config")
def config():
    return jsonify({
        "brand": BRAND,
        "agents": AGENTS,
        "confidence_threshold": CONFIDENCE_THRESHOLD,
        "agent_delay": AGENT_DELAY,
    })


@app.post("/start")
def start():
    if STATE["running"]:
        return jsonify({"ok": False, "error": "A run is already in progress"}), 409
    mode = (request.get_json(silent=True) or {}).get("mode", "demo")
    if mode not in ("demo", "live"):
        mode = "demo"
    threading.Thread(target=run_cycle, args=(mode,), daemon=True).start()
    return jsonify({"ok": True, "mode": mode})


@app.get("/status")
def status():
    with STATE_LOCK:
        return Response(json.dumps(STATE), mimetype="application/json")


@app.get("/scorecard")
def scorecard():
    """Track record: checks past live-mode BUY calls against their actual
    later price move. Demo-run signals can't be checked (fictional tickers)
    and are reported separately, not silently dropped."""
    try:
        min_age_days = int(request.args.get("min_age_days", 3))
    except ValueError:
        min_age_days = 3
    try:
        data = tracker.compute_scorecard(DB_PATH, min_age_days=min_age_days)
        return jsonify({"ok": True, **data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    init_db()
    print(f"\n  {BRAND} · Indian stock analysis · {len(AGENTS)} agents")
    print(f"  http://127.0.0.1:{PORT}\n")
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
