"""Rey Capital — combined trade dashboard for the ExecRelay dev stack.

Single-file stdlib HTTP server, branded with the Rey Capital design system
(colors/typography/components mirrored from C:\\AccountManagementSystem
frontend/src/index.css). Combines, on every request:

  * transactions/telegram-signals.log*  -- Telegram-sourced signals
  * transactions/mt5-fills.log*         -- every order the EA shim executed,
    classified Telegram vs TradingView by its comment prefix ("tg-…")
  * the running MT5 terminal (optional) -- open positions + last 7 days of
    closed deals for the shim's magic number, with realized P/L
  * .local-stack/journal.json           -- lightweight trading journal keyed
    by MT5 position ticket; fields mirror the ReyLens journal schema
    (setup / emotion / mistakes / rating / notes / reviewed) so entries can
    be migrated into ReyLens later.

Started by scripts/local-stack.ps1 as service "trade-dashboard". Binds to
localhost only: the page shows account numbers and balances and has no auth,
so it must not share the public exposure that ingress gets.

Environment:
    DASHBOARD_ADDR   default 127.0.0.1:8090
    EA_SHIM_MAGIC    default 20240101 (must match ea_shim.py)
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import MetaTrader5 as mt5
except ImportError:  # dashboard still works log-only
    mt5 = None

ADDR = os.environ.get("DASHBOARD_ADDR", "127.0.0.1:8090")
MAGIC = int(os.environ.get("EA_SHIM_MAGIC", "20240101"))
ROOT = Path(__file__).resolve().parent.parent
TXN_DIR = ROOT / ".local-stack" / "logs" / "transactions"
JOURNAL_PATH = ROOT / ".local-stack" / "journal.json"
ASSETS = Path(__file__).resolve().parent / "dashboard-assets"

EMOTIONS = ["calm", "confident", "neutral", "anxious", "fearful", "greedy", "fomo", "revenge", "bored"]

_mt5_ready = False
_journal_lock = threading.Lock()


def _ensure_mt5() -> bool:
    global _mt5_ready
    if mt5 is None:
        return False
    if _mt5_ready:
        return True
    _mt5_ready = bool(mt5.initialize())
    return _mt5_ready


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------


def _read_txn(name: str) -> list[dict]:
    """All retained JSONL records for one txn logger, oldest first."""
    files = sorted(TXN_DIR.glob(f"{name}.log.*")) + [TXN_DIR / f"{name}.log"]
    records: list[dict] = []
    for f in files:
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except OSError:
            continue
    return records


def _load_journal() -> dict:
    try:
        return json.loads(JOURNAL_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_journal(journal: dict) -> None:
    tmp = JOURNAL_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(journal, indent=1), encoding="utf-8")
    tmp.replace(JOURNAL_PATH)


def _side_of(command: str) -> str:
    c = (command or "").lower()
    if c == "closeshortopenlong" or c.startswith("buy"):
        return "buy"
    if c == "closelongopenshort" or c.startswith("sell"):
        return "sell"
    return ""


def _source_of(comment: str) -> str:
    return "telegram" if (comment or "").startswith("tg-") else "tradingview"


def _deal_source(magic: int, comment: str) -> str:
    """Classify account activity: this stack's trades (by magic) split into
    telegram/tradingview by comment; anything else on the account (other
    EAs, manual trades) is "other"."""
    if magic == MAGIC:
        return _source_of(comment)
    return "other"


def _redact(cmd: str) -> str:
    return re.sub(r"secret=[^,]*", "secret=***", cmd or "")


def _signal_stats() -> dict:
    rows = _read_txn("telegram-signals")
    by_outcome: dict[str, int] = {}
    for r in rows:
        outcome = r.get("outcome", "other")
        by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
    recent = [
        {
            "ts": r.get("ts", ""),
            "channel": r.get("channel") or "direct",
            "outcome": r.get("outcome", ""),
            "detail": _redact(r.get("command") or r.get("raw_text") or "")[:130],
        }
        for r in rows[-12:]
    ][::-1]
    return {
        "received": len(rows),
        "posted": by_outcome.get("posted", 0),
        "rejected": by_outcome.get("rejected", 0),
        "dry_run": by_outcome.get("dry_run", 0),
        "errors": by_outcome.get("webhook_error", 0),
        "recent": recent,
    }


def _order_stats() -> dict:
    rows = [r for r in _read_txn("mt5-fills") if r.get("command")]
    out = {"telegram": _empty_bucket(), "tradingview": _empty_bucket()}
    recent = []
    for r in rows:
        b = out[_source_of(r.get("comment", ""))]
        b["total"] += 1
        status = r.get("status")
        if status in ("filled", "placed"):
            b["executed"] += 1
        elif status == "rejected":
            b["rejected"] += 1
        side = _side_of(r.get("command", ""))
        if side == "buy":
            b["buys"] += 1
        elif side == "sell":
            b["sells"] += 1
        if r.get("event") != "position_closed":
            recent.append(
                {
                    "ts": r.get("ts", ""),
                    "source": _source_of(r.get("comment", "")),
                    "command": r.get("command", ""),
                    "symbol": r.get("symbol", ""),
                    "risk": r.get("risk") or "",
                    "volume": r.get("volume") or "",
                    "sl": r.get("sl") or "",
                    "tp": r.get("tp") or "",
                    "status": status or "",
                    "error": (r.get("error") or "")[:80],
                }
            )
    return {"by_source": out, "recent": recent[-12:][::-1]}


def _empty_bucket() -> dict:
    return {"total": 0, "executed": 0, "rejected": 0, "buys": 0, "sells": 0}


def _mt5_stats(journal: dict) -> dict:
    if not _ensure_mt5():
        return {"available": False}
    acct = mt5.account_info()
    if acct is None:
        return {"available": False}

    open_rows = []
    for p in mt5.positions_get() or []:
        open_rows.append(
            {
                "ticket": p.ticket,
                "symbol": p.symbol,
                "side": "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
                "volume": p.volume,
                "entry": p.price_open,
                "sl": p.sl,
                "tp": p.tp,
                "profit": round(p.profit, 2),
                "source": _deal_source(p.magic, p.comment),
            }
        )

    deals = (
        mt5.history_deals_get(datetime.now() - timedelta(days=7), datetime.now() + timedelta(days=1))
        or []
    )
    closed_rows = []
    for d in deals:
        if d.entry == mt5.DEAL_ENTRY_IN or d.type not in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL):
            continue
        ticket = str(d.position_id)
        closed_rows.append(
            {
                "ticket": ticket,
                "time": datetime.fromtimestamp(d.time, tz=timezone.utc).isoformat(),
                "symbol": d.symbol,
                # the closing deal's type is opposite to the position's side
                "side": "sell" if d.type == mt5.DEAL_TYPE_BUY else "buy",
                "volume": d.volume,
                "close": d.price,
                "profit": round(d.profit + d.commission + d.swap, 2),
                "source": _deal_source(d.magic, d.comment),
                "journal": journal.get(ticket) or None,
            }
        )
    wins = [r for r in closed_rows if r["profit"] >= 0]
    return {
        "available": True,
        "account": acct.login,
        "currency": acct.currency,
        "balance": acct.balance,
        "equity": acct.equity,
        "open": open_rows,
        "closed": {
            "count": len(closed_rows),
            "wins": len(wins),
            "losses": len(closed_rows) - len(wins),
            "net": round(sum(r["profit"] for r in closed_rows), 2),
            "buys": sum(1 for r in closed_rows if r["side"] == "buy"),
            "sells": sum(1 for r in closed_rows if r["side"] == "sell"),
            "rows": closed_rows[::-1],
        },
    }


def summary() -> dict:
    journal = _load_journal()
    journaled = [j for j in journal.values() if j.get("setup") or j.get("notes") or j.get("rating")]
    ratings = [j["rating"] for j in journaled if j.get("rating")]
    return {
        "updated": datetime.now(timezone.utc).isoformat(),
        "signals": _signal_stats(),
        "orders": _order_stats(),
        "mt5": _mt5_stats(journal),
        "journal": {
            "entries": len(journaled),
            "reviewed": sum(1 for j in journaled if j.get("reviewed")),
            "avg_rating": round(sum(ratings) / len(ratings), 1) if ratings else None,
            "emotions": EMOTIONS,
        },
    }


def save_journal_entry(payload: dict) -> dict:
    ticket = str(payload.get("ticket", "")).strip()
    if not ticket:
        raise ValueError("ticket required")
    rating = payload.get("rating")
    entry = {
        "setup": str(payload.get("setup", ""))[:120],
        "emotion": payload.get("emotion") if payload.get("emotion") in EMOTIONS else "",
        "mistakes": str(payload.get("mistakes", ""))[:500],
        "rating": int(rating) if rating and str(rating).isdigit() and 1 <= int(rating) <= 5 else None,
        "notes": str(payload.get("notes", ""))[:4000],
        "reviewed": bool(payload.get("reviewed")),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    with _journal_lock:
        journal = _load_journal()
        journal[ticket] = entry
        _save_journal(journal)
    return entry


# ---------------------------------------------------------------------------
# UI — Rey Capital design system (mirrored from AccountManagementSystem)
# ---------------------------------------------------------------------------

LOGO_SVG = """<svg viewBox="0 0 89 89" fill="currentColor" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
<g transform="matrix(28.48369473913729,0,0,28.48369473913729,-26.717708028593407,-27.14496304221794)">
<polygon points="2.499,1.705 4.062,2.605 4.062,1.854 2.498,0.953 0.938,1.855 0.938,2.607"/>
<polygon points="3.812,3.51 4.062,3.363 4.062,2.902 2.498,2 0.938,2.902 0.938,3.365 1.188,3.51 2.498,2.752"/>
<polygon points="2.499,3.818 2.896,4.047 3.548,3.672 2.498,3.066 1.452,3.672 2.104,4.047"/>
</g></svg>"""

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rey Capital | Trade Operations</title>
<link rel="icon" href="/assets/favicon.png">
<style>
:root {
  --color-background: #020c1b;
  --color-surface: #061526;
  --color-surface-2: #0b2040;
  --color-border: #123060;
  --color-border-light: #1d4a8a;
  --color-primary: #00c2e0;
  --color-primary-dim: #00a3be;
  --color-primary-glow: rgb(0 194 224 / 0.18);
  --color-profit: #05e8a4;
  --color-profit-dim: rgb(5 232 164 / 0.14);
  --color-loss: #ff3d5f;
  --color-loss-dim: rgb(255 61 95 / 0.14);
  --color-warning: #ffb52e;
  --color-gold: #f4b942;
  --color-text: #cde4ff;
  --color-text-muted: #4e7aab;
  --color-text-dim: #7aa3cc;
  --radius-sm: 0.375rem;
  --radius: 0.625rem;
  --radius-lg: 0.875rem;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: system-ui, "Segoe UI", Roboto, sans-serif;
  background-color: var(--color-background);
  color: var(--color-text);
}
.number { font-variant-numeric: tabular-nums; font-feature-settings: "tnum"; }
::-webkit-scrollbar { width: 4px; height: 4px; }
::-webkit-scrollbar-thumb { background: var(--color-border-light); border-radius: 2px; }

header {
  position: sticky; top: 0; z-index: 10;
  display: grid; grid-template-columns: 1fr auto 1fr; align-items: center;
  gap: 1rem; padding: 0.875rem 1.5rem;
  background: color-mix(in srgb, var(--color-surface) 85%, transparent 15%);
  backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--color-border);
  box-shadow: 0 1px 0 rgb(0 194 224 / 0.06), 0 4px 16px rgb(0 0 0 / 0.25);
}
.brand { display: flex; align-items: center; gap: 0.65rem; }
.logo-tile {
  width: 2.25rem; height: 2.25rem; border-radius: 0.5rem; background: #004AAC;
  display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  box-shadow: 0 4px 14px rgb(0 0 0 / 0.4);
}
.logo-tile svg { width: 58%; height: 58%; color: #fff; }
.brand-name { font-size: 0.95rem; font-weight: 600; line-height: 1.15; }
.brand-sub { font-size: 0.68rem; color: var(--color-text-muted); }
.kpi { text-align: center; }
.kpi-label { font-size: 10px; letter-spacing: 0.15em; text-transform: uppercase; color: var(--color-text-muted); }
.kpi-value { font-size: 1.125rem; font-weight: 600; }
.acct { text-align: right; font-size: 0.72rem; color: var(--color-text-muted); line-height: 1.5; }
.acct b { color: var(--color-text-dim); font-weight: 500; }

main { max-width: 1600px; margin: 0 auto; padding: 1.5rem; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 1rem; }
.stat-card {
  background: linear-gradient(180deg, var(--color-surface-2), var(--color-surface));
  border: 1px solid var(--color-border); border-radius: var(--radius);
  padding: 1rem 1.25rem; transition: transform .15s, box-shadow .15s;
}
.stat-card:hover { transform: translateY(-1px); box-shadow: 0 6px 20px rgb(0 0 0 / 0.35), 0 0 12px var(--color-primary-glow); }
.stat-card b { display: block; font-size: 1.35rem; font-weight: 600; }
.stat-card span { color: var(--color-text-muted); font-size: 0.72rem; }
.stat-card .sub { font-size: 0.68rem; color: var(--color-text-dim); margin-top: 2px; }

h2 { font-size: 0.82rem; letter-spacing: 0.08em; text-transform: uppercase; color: var(--color-text-dim); margin: 2rem 0 0.75rem; display:flex; align-items:center; gap:.5rem; }
h2 .chip { font-size: 0.65rem; letter-spacing: normal; text-transform: none; border-radius: 999px; padding: 0.1rem 0.6rem; border: 1px solid var(--color-border-light); color: var(--color-text-muted); }
.grid2 { display: grid; grid-template-columns: 1fr; gap: 1.5rem; }
@media (min-width: 1100px) { .grid2 { grid-template-columns: 1fr 1fr; } }

.tablewrap { overflow-x: auto; border: 1px solid var(--color-border); border-radius: var(--radius-lg); background: linear-gradient(180deg, color-mix(in srgb, var(--color-surface-2) 60%, var(--color-surface)), var(--color-surface)); }
table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
thead { background: var(--color-surface-2); }
th { text-align: left; padding: 0.5rem 0.75rem; color: var(--color-text-muted); font-weight: 500; font-size: 0.72rem; white-space: nowrap; }
td { padding: 0.45rem 0.75rem; border-top: 1px solid var(--color-border); white-space: nowrap; }
.pos { color: var(--color-profit); } .neg { color: var(--color-loss); } .warn { color: var(--color-warning); }
.muted { color: var(--color-text-muted); }
.badge { display: inline-block; padding: 0.05rem 0.5rem; border-radius: 999px; font-size: 0.68rem; }
.badge.buy { background: var(--color-profit-dim); color: var(--color-profit); }
.badge.sell { background: var(--color-loss-dim); color: var(--color-loss); }
.badge.tg { background: rgb(0 194 224 / 0.12); color: var(--color-primary); }
.badge.tv { background: rgb(244 185 66 / 0.15); color: var(--color-gold); }
.badge.ok { background: var(--color-profit-dim); color: var(--color-profit); }
.badge.bad { background: var(--color-loss-dim); color: var(--color-loss); }
.badge.neutral { background: var(--color-surface-2); color: var(--color-text-dim); }
.stars { color: var(--color-gold); letter-spacing: 1px; }
button.jbtn {
  background: transparent; border: 1px solid var(--color-border-light); color: var(--color-primary);
  border-radius: var(--radius-sm); padding: 0.15rem 0.6rem; font-size: 0.7rem; cursor: pointer;
}
button.jbtn:hover { background: var(--color-primary-glow); }

#modal-scrim { position: fixed; inset: 0; background: rgb(0 0 0 / 0.55); display: none; align-items: center; justify-content: center; z-index: 50; }
#modal {
  width: min(480px, 92vw); background: linear-gradient(180deg, var(--color-surface-2), var(--color-surface));
  border: 1px solid var(--color-border-light); border-radius: var(--radius-lg); padding: 1.5rem;
  box-shadow: 0 20px 60px rgb(0 0 0 / 0.5);
}
#modal h3 { margin: 0 0 1rem; font-size: 0.95rem; }
#modal label { display: block; font-size: 0.7rem; color: var(--color-text-muted); margin: 0.7rem 0 0.25rem; text-transform: uppercase; letter-spacing: 0.06em; }
#modal input[type=text], #modal textarea, #modal select {
  width: 100%; background: var(--color-background); color: var(--color-text);
  border: 1px solid var(--color-border); border-radius: var(--radius-sm); padding: 0.45rem 0.6rem; font-size: 0.82rem;
  font-family: inherit;
}
#modal textarea { min-height: 70px; resize: vertical; }
.rating-row { display: flex; gap: 0.3rem; font-size: 1.3rem; cursor: pointer; color: var(--color-border-light); }
.rating-row span.on { color: var(--color-gold); }
.modal-actions { display: flex; justify-content: flex-end; gap: 0.6rem; margin-top: 1.2rem; }
.btn-primary {
  background: linear-gradient(135deg, var(--color-primary), var(--color-primary-dim)); color: #04121f;
  border: 0; border-radius: var(--radius-sm); padding: 0.45rem 1.1rem; font-weight: 600; font-size: 0.8rem; cursor: pointer;
}
.btn-ghost { background: transparent; border: 1px solid var(--color-border); color: var(--color-text-dim); border-radius: var(--radius-sm); padding: 0.45rem 1rem; font-size: 0.8rem; cursor: pointer; }
.checkline { display: flex; align-items: center; gap: 0.5rem; margin-top: 0.8rem; font-size: 0.8rem; color: var(--color-text-dim); }
#meta { color: var(--color-text-muted); font-size: 0.72rem; margin: 2.5rem 0 1rem; }
</style></head><body>
<header>
  <div class="brand">
    <div class="logo-tile">__LOGO__</div>
    <div><div class="brand-name">Rey Capital</div><div class="brand-sub">Trade Operations</div></div>
  </div>
  <div class="kpi"><div class="kpi-label">Net P/L · 7 days</div><div class="kpi-value number" id="kpi-net">—</div></div>
  <div class="acct" id="acct">connecting…</div>
</header>
<main>
  <div class="cards" id="cards"></div>

  <div class="grid2">
    <section>
      <h2>Telegram signals <span class="chip" id="chip-tg"></span></h2>
      <div class="tablewrap"><table id="tbl-signals"></table></div>
    </section>
    <section>
      <h2>Orders executed <span class="chip" id="chip-orders"></span></h2>
      <div class="tablewrap"><table id="tbl-orders"></table></div>
    </section>
  </div>

  <h2>Open positions</h2>
  <div class="tablewrap"><table id="tbl-open"></table></div>

  <h2>Closed trades &amp; journal <span class="chip" id="chip-journal"></span></h2>
  <div class="tablewrap"><table id="tbl-closed"></table></div>

  <div id="meta"></div>
</main>

<div id="modal-scrim"><div id="modal">
  <h3 id="modal-title">Journal</h3>
  <input type="hidden" id="j-ticket">
  <label>Setup / pattern</label><input type="text" id="j-setup" placeholder="e.g. breakout retest, supply zone">
  <label>Emotion</label><select id="j-emotion"><option value="">—</option></select>
  <label>Mistakes</label><input type="text" id="j-mistakes" placeholder="comma-separated, e.g. chased entry, moved SL">
  <label>Rating</label><div class="rating-row" id="j-rating"></div>
  <label>Notes / lessons</label><textarea id="j-notes"></textarea>
  <div class="checkline"><input type="checkbox" id="j-reviewed"><label for="j-reviewed" style="margin:0;text-transform:none;letter-spacing:0">Reviewed</label></div>
  <div class="modal-actions">
    <button class="btn-ghost" onclick="closeModal()">Cancel</button>
    <button class="btn-primary" onclick="saveJournal()">Save entry</button>
  </div>
</div></div>

<script>
const $ = id => document.getElementById(id);
const money = (v, c) => (v >= 0 ? "+" : "\u2212") + "$" + Math.abs(v).toFixed(2);
const cls = v => v >= 0 ? "pos" : "neg";
const esc = s => String(s ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const srcBadge = s => s === "telegram" ? '<span class="badge tg">Telegram</span>'
  : s === "tradingview" ? '<span class="badge tv">TradingView</span>'
  : '<span class="badge neutral">Other EA</span>';
const sideBadge = s => s === "close" ? '<span class="badge neutral">CLOSE</span>'
  : `<span class="badge ${s}">${s.toUpperCase()}</span>`;
let lastSummary = null, ratingVal = 0;

function card(label, value, extra, sub) {
  return `<div class="stat-card"><b class="number ${extra||""}">${value}</b><span>${label}</span>${sub?`<div class="sub">${sub}</div>`:""}</div>`;
}
function table(id, header, rows) {
  $(id).innerHTML = "<thead><tr>" + header.map(h => `<th>${h}</th>`).join("") + "</tr></thead><tbody>" +
    (rows.length ? rows.join("") : `<tr><td colspan="${header.length}" class="muted">none yet</td></tr>`) + "</tbody>";
}
async function refresh() {
  const s = await (await fetch("/api/summary")).json();
  lastSummary = s;
  const bs = s.orders.by_source, tg = bs.telegram, tv = bs.tradingview;
  const c = s.mt5.available ? s.mt5.closed : {count:0,wins:0,losses:0,net:0,rows:[]};
  const winRate = c.count ? Math.round(100 * c.wins / c.count) : null;

  $("kpi-net").textContent = money(c.net);
  $("kpi-net").className = "kpi-value number " + cls(c.net);
  $("acct").innerHTML = s.mt5.available
    ? `<b>MT5 demo ${s.mt5.account}</b><br>balance $${s.mt5.balance.toFixed(2)} · equity $${s.mt5.equity.toFixed(2)}`
    : '<span class="neg">MT5 offline</span>';

  $("cards").innerHTML =
    card("Telegram signals", s.signals.received, "", `${s.signals.posted} routed · ${s.signals.rejected} rejected`) +
    card("Telegram orders", tg.executed, "", `${tg.buys} buy · ${tg.sells} sell`) +
    card("TradingView orders", tv.executed, "", `${tv.buys} buy · ${tv.sells} sell`) +
    card("Open positions", s.mt5.available ? s.mt5.open.length : "—") +
    card("Closed · 7d", c.count, "", `${c.wins} win · ${c.losses} loss · ${c.rows.filter(r => r.source !== "other").length} from signals`) +
    card("Win rate", winRate === null ? "—" : winRate + "%", winRate === null ? "" : (winRate >= 50 ? "pos" : "neg")) +
    card("Net P/L · 7d", money(c.net), cls(c.net)) +
    card("Journal", s.journal.entries, "", s.journal.avg_rating ? `avg rating ${s.journal.avg_rating}\u2605 · ${s.journal.reviewed} reviewed` : `${s.journal.reviewed} reviewed`);

  $("chip-tg").textContent = `${s.signals.received} total`;
  $("chip-orders").textContent = `${tg.total + tv.total} total`;
  $("chip-journal").textContent = `${s.journal.entries} entries`;

  table("tbl-signals", ["time (UTC)","channel","outcome","detail"], s.signals.recent.map(r =>
    `<tr><td class="muted number">${esc((r.ts||"").slice(5,19).replace("T"," "))}</td><td>${esc(r.channel)}</td>
     <td><span class="badge ${r.outcome==="posted"?"ok":(r.outcome==="rejected"||r.outcome==="webhook_error"?"bad":"neutral")}">${esc(r.outcome)}</span></td>
     <td class="muted" style="white-space:normal;max-width:340px">${esc(r.detail)}</td></tr>`));

  table("tbl-orders", ["time (UTC)","source","command","symbol","size","SL","TP","status"], s.orders.recent.map(r =>
    `<tr><td class="muted number">${esc((r.ts||"").slice(5,19).replace("T"," "))}</td><td>${srcBadge(r.source)}</td>
     <td>${sideBadge(_sideOf(r.command))} <span class="muted">${esc(r.command)}</span></td><td>${esc(r.symbol)}</td>
     <td class="number">${r.risk ? "risk $"+esc(r.risk) : esc(r.volume)}</td>
     <td class="number">${esc(r.sl)}</td><td class="number">${esc(r.tp)}</td>
     <td><span class="badge ${r.status==="rejected"?"bad":"ok"}">${esc(r.status)}</span>${r.error?` <span class="neg" title="${esc(r.error)}">!</span>`:""}</td></tr>`));

  table("tbl-open", ["ticket","source","symbol","side","lot","entry","SL","TP","floating P/L"], (s.mt5.open||[]).map(p =>
    `<tr><td class="muted number">${p.ticket}</td><td>${srcBadge(p.source)}</td><td>${esc(p.symbol)}</td><td>${sideBadge(p.side)}</td>
     <td class="number">${p.volume}</td><td class="number">${p.entry}</td><td class="number">${p.sl}</td><td class="number">${p.tp}</td>
     <td class="number ${cls(p.profit)}">${money(p.profit)}</td></tr>`));

  table("tbl-closed", ["closed (UTC)","source","symbol","side","lot","close","P/L","setup","emotion","rating","",""], c.rows.map(r => {
    const j = r.journal || {};
    return `<tr><td class="muted number">${esc((r.time||"").slice(5,19).replace("T"," "))}</td><td>${srcBadge(r.source)}</td>
     <td>${esc(r.symbol)}</td><td>${sideBadge(r.side)}</td><td class="number">${r.volume}</td><td class="number">${r.close}</td>
     <td class="number ${cls(r.profit)}">${money(r.profit)}</td>
     <td>${esc(j.setup||"")}</td><td class="muted">${esc(j.emotion||"")}</td>
     <td class="stars">${j.rating ? "\u2605".repeat(j.rating) : ""}</td>
     <td>${j.reviewed ? '<span class="badge ok">reviewed</span>' : ""}</td>
     <td><button class="jbtn" onclick='openModal("${r.ticket}")'>Journal</button></td></tr>`;
  }));

  $("meta").textContent = "updated " + s.updated + " — auto-refreshes every 10s — journal entries are stored locally and mirror the ReyLens schema";
}
function _sideOf(cmd) {
  cmd = (cmd||"").toLowerCase();
  if (cmd === "closeshortopenlong" || cmd.startsWith("buy")) return "buy";
  if (cmd === "closelongopenshort" || cmd.startsWith("sell")) return "sell";
  return "close";
}
function openModal(ticket) {
  const row = (lastSummary?.mt5?.closed?.rows || []).find(r => String(r.ticket) === String(ticket));
  const j = row?.journal || {};
  $("modal-title").textContent = `Journal — ${row ? row.side.toUpperCase()+" "+row.symbol+" ("+money(row.profit)+")" : "#"+ticket}`;
  $("j-ticket").value = ticket;
  $("j-setup").value = j.setup || "";
  $("j-emotion").value = j.emotion || "";
  $("j-mistakes").value = j.mistakes || "";
  $("j-notes").value = j.notes || "";
  $("j-reviewed").checked = !!j.reviewed;
  setRating(j.rating || 0);
  $("modal-scrim").style.display = "flex";
}
function closeModal() { $("modal-scrim").style.display = "none"; }
function setRating(n) {
  ratingVal = n;
  $("j-rating").innerHTML = [1,2,3,4,5].map(i =>
    `<span class="${i<=n?"on":""}" onclick="setRating(${i})">\u2605</span>`).join("");
}
async function saveJournal() {
  const payload = {
    ticket: $("j-ticket").value, setup: $("j-setup").value, emotion: $("j-emotion").value,
    mistakes: $("j-mistakes").value, rating: ratingVal || null, notes: $("j-notes").value,
    reviewed: $("j-reviewed").checked,
  };
  const res = await fetch("/api/journal", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(payload) });
  if (res.ok) { closeModal(); refresh(); } else { alert("save failed: " + await res.text()); }
}
document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });
$("modal-scrim").addEventListener("click", e => { if (e.target.id === "modal-scrim") closeModal(); });
fetch("/api/summary").then(r => r.json()).then(s => {
  const sel = $("j-emotion");
  s.journal.emotions.forEach(e => { const o = document.createElement("option"); o.value = e; o.textContent = e; sel.appendChild(o); });
});
setRating(0); refresh(); setInterval(refresh, 10000);
</script></body></html>""".replace("__LOGO__", LOGO_SVG)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in ("/health", "/healthz"):
            self._send(200, b'{"status":"ok"}', "application/json")
        elif self.path == "/api/summary":
            self._send(200, json.dumps(summary(), default=str).encode(), "application/json")
        elif self.path == "/assets/favicon.png":
            try:
                self._send(200, (ASSETS / "favicon.png").read_bytes(), "image/png")
            except OSError:
                self.send_response(404)
                self.end_headers()
        elif self.path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        if self.path != "/api/journal":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            entry = save_journal_entry(payload)
            self._send(200, json.dumps(entry).encode(), "application/json")
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(400, str(exc).encode(), "text/plain")

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _fmt: str, *_args: object) -> None:
        pass


def main() -> None:
    host, port = ADDR.rsplit(":", 1)
    server = ThreadingHTTPServer((host, int(port)), Handler)
    print(time.strftime("%H:%M:%S"), f"trade dashboard listening on http://{ADDR}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
