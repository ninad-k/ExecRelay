"""telegram-ingest — turn Telegram channel signal messages into ExecRelay
webhook commands.

Reads messages from allowlisted Telegram chats/channels via the Bot API
(long-polling getUpdates; the bot must be a member/admin of the channel),
parses them with a STRICT signal grammar, and POSTs flat webhook commands to
ingress. Anything that doesn't match the grammar exactly is ignored; anything
that matches but fails price sanity checks is rejected loudly. Ships in
DRY-RUN mode by default: commands are logged, not sent.

This service is an upstream *producer* like TradingView — it sits outside the
hot path and authenticates to ingress with a normal license/secret.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
from _txnlog import get_txn_logger, log_txn  # noqa: E402
from _tradestore import (  # noqa: E402
    append_signal_trace,
    get_dry_run,
    is_channel_enabled,
    recent_symbols_for_channel,
    record_order,
    record_signal,
)

TXN_LOG = get_txn_logger("telegram-signals")

SERVICE = "telegram-ingest"
ENV = os.environ.get("ENV", "development").lower()
IS_PROD = ENV in ("prod", "production")
HTTP_ADDR = os.environ.get("HTTP_ADDR", "0.0.0.0:8080")

BOT_TOKEN = os.environ.get("TELEGRAM_INGEST_BOT_TOKEN", "")
TELEGRAM_API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
POLL_TIMEOUT = int(os.environ.get("TELEGRAM_INGEST_POLL_TIMEOUT", "25"))

# Only messages from these chat ids are ever considered. Empty = nothing.
ALLOWED_CHAT_IDS = {
    int(x)
    for x in os.environ.get("TELEGRAM_INGEST_ALLOWED_CHAT_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

WEBHOOK_URL = os.environ.get(
    "TELEGRAM_INGEST_WEBHOOK_URL", "http://ingress:8080/webhook"
)
LICENSE_ID = os.environ.get("TELEGRAM_INGEST_LICENSE_ID", "")
SECRET = os.environ.get("TELEGRAM_INGEST_SECRET", "")
FIXED_LOT = os.environ.get("TELEGRAM_INGEST_FIXED_LOT", "0.01")
COMMENT = os.environ.get("TELEGRAM_INGEST_COMMENT", "tg-ingest")

# Total $ risk budget per SIGNAL. When set, orders carry `risk=<budget/N>`
# (N = number of orders the signal expands into) instead of a fixed lot, and
# the executor sizes each lot from its SL distance — so one signal can never
# lose more than this in total, no matter how many legs/targets it has.
# Empty = legacy fixed-lot behavior.
_risk_raw = os.environ.get("TELEGRAM_INGEST_RISK_USD", "").strip()
RISK_USD_TOTAL = float(_risk_raw) if _risk_raw else 0.0

# Adapter-level symbol rewrite ("GOLD=XAUUSD;US30=US30.Cash"). The EA has its
# own per-terminal map; this one is for channel jargon -> canonical name.
SYMBOL_MAP = {
    pair.split("=", 1)[0].strip().upper(): pair.split("=", 1)[1].strip()
    for pair in os.environ.get("TELEGRAM_INGEST_SYMBOL_MAP", "").split(";")
    if "=" in pair
}

# How the signal's first entry is placed: "limit" (default) rests a pending
# limit order at the stated entry price; "market" executes immediately at
# market. The second entry is always pending at its own level.
#
# Only applies to signals that don't state their own trigger direction. A
# "Trigger only Above/Below" message names the pending order type itself and
# always wins over this setting.
ENTRY_MODE = os.environ.get("TELEGRAM_INGEST_ENTRY_MODE", "limit").lower()

# Channels that publish a target ladder ("Target 4790 4788 4784 4770+") give
# more take-profit levels than one flat webhook command can carry:
#   first  — nearest target only (default, smallest exposure)
#   last   — furthest target only
#   ladder — one order per target, each at the fixed lot (N x exposure)
TP_MODE = os.environ.get("TELEGRAM_INGEST_TP_MODE", "first").lower()

# Safety default: log what WOULD be sent, send nothing. This is only the
# BOOT-TIME fallback -- the effective switch is _dry_run_cached() below, which
# lets the dashboard flip dry-run at runtime. Never branch on DRY_RUN directly
# on the message path.
DRY_RUN = os.environ.get("TELEGRAM_INGEST_DRY_RUN", "true").lower() in (
    "true",
    "1",
    "yes",
    "on",
)

DEBUG = os.environ.get("DEBUG", "false" if IS_PROD else "true").lower() in (
    "true",
    "1",
    "yes",
    "on",
)


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        from datetime import datetime, timezone

        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": SERVICE,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


logger = logging.getLogger(SERVICE)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_JSONFormatter())
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO, handlers=[_handler], force=True
)

_readiness = {"poll_ok": False, "detail": "not started"}


# ---------------------------------------------------------------------------
# Signal grammar
# ---------------------------------------------------------------------------
#
# Two dialects are understood. Both are strict: the whole message is rejected
# unless every recognised part is consistent.
#
# A. Explicit "@" format — the entry line names symbol and side, an optional
#    second leg names its own pending kind:
#
#      GOLD SELL @ 4099
#      SECOND SELL LIMIT @ 4109
#      SL @ 4119
#      TP @ 4089
#
# B. "Trigger" format — the message states a breakout/pullback trigger and a
#    ladder of targets. The above/below keyword decides the pending order
#    type, so ENTRY_MODE does not apply:
#
#      XAUUSD Sell Trigger only Below 4792 📉
#      🛑 SL 4808 ⚠️
#      🎯 Target 4790 4788 4784 4770+ 🎯
#
# Trailing commentary (risk tables, disclaimers, referral links, emoji) is
# ignored in both.

_ENTRY_RE = re.compile(
    r"^\s*(?P<symbol>[A-Z][A-Z0-9._]{1,14})\s+(?P<side>BUY|SELL)\s*@\s*(?P<entry>\d+(?:\.\d+)?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_SECOND_RE = re.compile(
    r"^\s*(?:SECOND\s+)?(?P<side>BUY|SELL)\s+(?P<kind>LIMIT|STOP)\s*@\s*(?P<entry>\d+(?:\.\d+)?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_SL_RE = re.compile(r"^\s*SL\s*@\s*(\d+(?:\.\d+)?)\s*$", re.IGNORECASE | re.MULTILINE)
_TP_RE = re.compile(r"^\s*TP\s*@\s*(\d+(?:\.\d+)?)\s*$", re.IGNORECASE | re.MULTILINE)

# --- dialect B ---------------------------------------------------------------

# Emoji and other pictographic decoration, stripped before matching so that
# "🛑 SL 4808 ⚠️" reads as a plain SL line.
_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001F9FF\U00002600-\U000027BF\U0000FE00-\U0000FEFF"
    r"\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF\U00002702-\U000027B0"
    r"\U0000200D\U0000FE0F]+",
    flags=re.UNICODE,
)

# Symbols this adapter will accept as the leading word of a trigger message.
# An unknown first word means "not a signal" rather than "guess" — a channel
# that posts a symbol we don't list is a config change, not a trade.
KNOWN_SYMBOLS: frozenset[str] = frozenset(
    {
        "XAUUSD", "XAGUSD", "GOLD", "SILVER",
        "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "NZDUSD", "USDCAD",
        "GBPJPY", "EURJPY", "EURGBP", "AUDJPY", "CADJPY", "CHFJPY",
        "EURCHF", "EURAUD", "EURNZD", "GBPAUD", "GBPNZD", "GBPCAD", "GBPCHF",
        "AUDNZD", "AUDCAD", "AUDCHF", "NZDCAD", "NZDCHF", "CADCHF",
        "US30", "NAS100", "SPX500", "US500", "USTEC", "DJ30",
        "BTCUSD", "ETHUSD", "BTCUSDT", "ETHUSDT",
        "USOIL", "UKOIL", "WTI", "BRENT",
    }
)

# Channel jargon → the name the rest of the platform uses. Applied before the
# operator's TELEGRAM_INGEST_SYMBOL_MAP, which still has the final say.
SYMBOL_ALIASES: dict[str, str] = {
    "GOLD": "XAUUSD",
    "SILVER": "XAGUSD",
    "WTI": "USOIL",
    "BRENT": "UKOIL",
    "DJ30": "US30",
    "SPX500": "US500",
    "USTEC": "NAS100",
}

# Messages relayed by scripts/telegram_user_forwarder.py carry a leading
# "[SRC:<channel title>]" line identifying the original channel (the bot only
# ever sees the relay chat, never the source channel itself). Stripped before
# parsing so it can never be mistaken for the signal's first line.
_SRC_TAG_RE = re.compile(r"^\[SRC:(?P<name>.+?)\]\n", re.DOTALL)

_BUY_RE = re.compile(r"\b(?:buy|long)\b", re.IGNORECASE)
_SELL_RE = re.compile(r"\b(?:sell|short)\b", re.IGNORECASE)
_ABOVE_RE = re.compile(r"\babove\b", re.IGNORECASE)
_BELOW_RE = re.compile(r"\bbelow\b", re.IGNORECASE)
_TRIGGER_RE = re.compile(r"(?:\b(?:above|below|at)\b|@)\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
# "SL 4808", "SL: 4808", "Stop Loss 4808" — with or without the "@" of dialect A.
_SL_LOOSE_RE = re.compile(r"\b(?:sl|stop\s*loss)\s*:?\s*@?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_TARGET_HEADER_RE = re.compile(r"\b(?:target|targets|tp)\s*:?\s*", re.IGNORECASE)
# Deliberately no optional digit after the keyword: "Target 4790" must not have
# its leading 4 eaten as a "TP4" label.
_TP_LABEL_RE = re.compile(r"\btp\s*\d\s*:?\s*", re.IGNORECASE)
_TP_LABELED_RE = re.compile(r"\btp\s*\d\s*:?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


class SignalError(ValueError):
    """Message looked like a signal but is inconsistent — reject loudly."""


def strip_source_tag(text: str) -> tuple[str | None, str]:
    """Split a "[SRC:<name>]" header off the front of a relayed message.

    Returns (channel_name, remaining_text). channel_name is None when the
    message carries no tag (e.g. a message sent to the bot directly)."""
    m = _SRC_TAG_RE.match(text)
    if m is None:
        return None, text
    return m.group("name").strip(), text[m.end():]


def channel_initials(name: str, max_len: int = 6) -> str:
    """"Dr Devendra's Crypto Advisory" -> "DDCA"; "VIP GOLD TRADING ACADEMY"
    -> "VGTA". Used to tag trade comments with their originating channel."""
    letters = [w[0].upper() for w in name.split() if w[:1].isalnum()]
    return "".join(letters)[:max_len] or "TG"


# --- follow-up "amend the trades already running" messages -----------------
# A channel does not only post new signals; it also amends live ones ("Tp set
# @ 4346 for both trade"). These carry a price but no side, entry or symbol,
# so parse_signal() correctly refuses them -- they are not signals. They are
# matched here instead, and only AFTER parse_signal has declined, so a real
# signal can never be swallowed by this path.
#
# Deliberately narrow: the verb (set/change/move/...) must be present next to
# the TP keyword. Plain "TP @ 4089" is a LINE OF A SIGNAL, not an amendment,
# and must not match -- which is why a bare keyword+price is not accepted.
# How far back to look for the instruments an amendment should address. A
# channel amending a trade it opened weeks ago is not a case worth guessing at.
_TP_UPDATE_LOOKBACK_DAYS = 7

_TP_UPDATE_RES = (
    re.compile(
        r"\b(?:tp|take\s*profit|target)\b[\s:@-]{0,6}"
        r"\b(?:set|chang(?:e|ed)|updat(?:e|ed)|revis(?:e|ed)|mov(?:e|ed)|shift(?:ed)?|now|to)\b"
        r"[\s:@to-]{0,8}(\d+(?:\.\d+)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:set|chang(?:e|ed)|updat(?:e|ed)|revis(?:e|ed)|mov(?:e|ed)|shift(?:ed)?)\b"
        r"[\s:@-]{0,6}\b(?:tp|take\s*profit|target)\b"
        r"[\s:@to-]{0,8}(\d+(?:\.\d+)?)",
        re.IGNORECASE,
    ),
)


def parse_tp_update(text: str) -> float | None:
    """Extract the new take-profit from an amendment message, or None if this
    is not one. Never raises -- an unrecognised message is simply not an
    amendment, and the caller falls through to its normal handling."""
    if not text:
        return None
    for pattern in _TP_UPDATE_RES:
        m = pattern.search(text)
        if m:
            try:
                tp = float(m.group(1))
            except (TypeError, ValueError):
                return None
            return tp if tp > 0 else None
    return None


def build_tp_update_commands(tp: float, symbols: list[str], comment: str | None = None) -> list[str]:
    """Render a TP amendment as modify commands -- one per (symbol, side).

    Both sides are emitted for every symbol because the message never says
    which way the running trades face. That is safe: ea_shim matches
    positions by side AND by this comment, so the side that does not exist
    (and any position belonging to a different channel) is simply a no-op.
    The comment is what scopes the change to THIS channel's trades.

    No volume is sent -- these commands do not open exposure, and the parser
    only requires an SL or a TP on a modify."""
    secret = f",secret={SECRET}" if SECRET else ""
    comment = comment or COMMENT
    cmds: list[str] = []
    for raw_symbol in symbols:
        symbol = resolve_symbol(raw_symbol)
        for side in ("newsltplong", "newsltpshort"):
            cmds.append(
                f"{LICENSE_ID},{side},{symbol},tp={_fmt(tp)},comment={comment}{secret}"
            )
    return cmds


def _blank_signal(symbol: str, side: str, entry: float) -> dict:
    return {
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "sl": 0.0,
        "tp": 0.0,
        "tps": [],
        "order_type": None,
        "second": None,
    }


def parse_signal(text: str) -> dict | None:
    """Parse a channel message into a signal dict, or None if the message is
    not a signal at all. Raises SignalError when it matches the grammar but
    fails a consistency check (wrong stop side, mismatched second order, ...).
    """
    clean = _EMOJI_RE.sub(" ", text)
    entry_m = _ENTRY_RE.search(clean)
    sl_m = _SL_RE.search(clean)
    tp_m = _TP_RE.search(clean)
    if entry_m is not None and sl_m is not None and tp_m is not None:
        return _parse_explicit(clean, entry_m, sl_m, tp_m)
    # A dialect-A entry line with looser SL/TP lines still parses as a trigger
    # signal; only if that fails too is the message genuinely malformed.
    sig = _parse_trigger(clean)
    if sig is None and entry_m is not None:
        raise SignalError("signal missing SL or TP line")
    return sig


def _parse_explicit(
    text: str, entry_m: re.Match[str], sl_m: re.Match[str], tp_m: re.Match[str]
) -> dict:
    """Dialect A: `SYMBOL SIDE @ price` with `SL @` / `TP @` lines."""
    side = entry_m.group("side").lower()
    entry = float(entry_m.group("entry"))
    sl = float(sl_m.group(1))
    tp = float(tp_m.group(1))

    sig = _blank_signal(entry_m.group("symbol").upper(), side, entry)
    sig.update(sl=sl, tp=tp, tps=[tp])
    _check_bracket(side, entry, sl, [tp])

    second_m = _SECOND_RE.search(text)
    if second_m is not None:
        s_side = second_m.group("side").lower()
        s_kind = second_m.group("kind").lower()
        s_entry = float(second_m.group("entry"))
        if s_side != side:
            raise SignalError("second order side differs from first")
        # Limit sits on the better-price side of the first entry; stop on the
        # worse-price side. Anything else means we misread the message.
        if s_kind == "limit" and not (s_entry > entry if side == "sell" else s_entry < entry):
            raise SignalError(f"{side} limit at {s_entry} is on the wrong side of entry {entry}")
        if s_kind == "stop" and not (s_entry < entry if side == "sell" else s_entry > entry):
            raise SignalError(f"{side} stop at {s_entry} is on the wrong side of entry {entry}")
        # The shared SL/TP must also make sense for the second entry.
        if side == "sell" and not (sl > s_entry > tp):
            raise SignalError("second entry outside SL/TP bracket")
        if side == "buy" and not (sl < s_entry < tp):
            raise SignalError("second entry outside SL/TP bracket")
        sig["second"] = {"kind": s_kind, "entry": s_entry}

    return sig


def _parse_trigger(text: str) -> dict | None:
    """Dialect B: `SYMBOL Buy Trigger only Above <price>` + SL + target ladder.

    The symbol must be the first word and known; the direction, trigger price
    and SL are taken from their first occurrence, which is the signal block at
    the top of the post — everything below it is promo copy.
    """
    symbol = _extract_symbol(text)
    if symbol is None:
        return None
    side = _extract_side(text)
    if side is None:
        return None
    trigger_m = _TRIGGER_RE.search(text)
    if trigger_m is None:
        return None
    entry = float(trigger_m.group(1))

    sl_m = _SL_LOOSE_RE.search(text)
    if sl_m is None:
        raise SignalError("signal missing SL")
    sl = float(sl_m.group(1))

    tps = _extract_targets(text)
    if not tps:
        raise SignalError("signal missing TP/target line")

    _check_bracket(side, entry, sl, tps)

    sig = _blank_signal(symbol, side, entry)
    sig.update(sl=sl, tp=tps[0], tps=tps, order_type=_derive_order_type(side, text))
    return sig


def _extract_symbol(text: str) -> str | None:
    words = text.split()
    if not words:
        return None
    candidate = words[0].upper().strip(".,;:!?")
    if candidate in KNOWN_SYMBOLS:
        return candidate
    # Some channels split the pair ("XAU USD").
    if len(words) >= 2:
        combined = (words[0] + words[1]).upper().strip(".,;:!?")
        if combined in KNOWN_SYMBOLS:
            return combined
    return None


def _extract_side(text: str) -> str | None:
    buy = _BUY_RE.search(text)
    sell = _SELL_RE.search(text)
    if buy and sell:
        # Both words present (e.g. a disclaimer mentions the other side) —
        # whichever leads the message is the signal.
        return "buy" if buy.start() < sell.start() else "sell"
    if buy:
        return "buy"
    if sell:
        return "sell"
    return None


def _derive_order_type(side: str, text: str) -> str | None:
    """Map the above/below keyword onto an ExecRelay pending order type.

    buy + above  -> buystop   (break upward into the entry)
    buy + below  -> buylimit  (dip down into the entry)
    sell + above -> selllimit (rally up into the entry)
    sell + below -> sellstop  (break downward into the entry)

    No keyword means the message never said which way price reaches the
    entry — fall back to ENTRY_MODE rather than guessing.
    """
    if _ABOVE_RE.search(text):
        return "buystop" if side == "buy" else "selllimit"
    if _BELOW_RE.search(text):
        return "buylimit" if side == "buy" else "sellstop"
    return None


def _extract_targets(text: str) -> list[float]:
    """Pull the take-profit ladder out of `TP1: .. TP2: ..` or a `Target` line."""
    labeled = _TP_LABELED_RE.findall(text)
    if len(labeled) >= 2:
        return [float(v) for v in labeled]

    for line in text.split("\n"):
        if not _TARGET_HEADER_RE.search(line):
            continue
        # Separators and the "+" that marks an open-ended last target are
        # noise; so are the header words themselves.
        cleaned = re.sub(r"[/|,+]", " ", line)
        cleaned = _TP_LABEL_RE.sub(" ", cleaned)
        cleaned = _TARGET_HEADER_RE.sub(" ", cleaned)
        numbers = _NUMBER_RE.findall(cleaned)
        if numbers:
            return [float(n) for n in numbers]
    return []


def _check_bracket(side: str, entry: float, sl: float, tps: list[float]) -> None:
    """SL must sit on the losing side of entry and every target on the winning
    side. A stray number swept up from promo text lands on the wrong side and
    takes the whole message down with it — deliberately."""
    if side == "sell":
        if not sl > entry:
            raise SignalError(f"sell price sanity failed: SL {sl} > entry {entry} required")
        bad = [tp for tp in tps if not tp < entry]
    else:
        if not sl < entry:
            raise SignalError(f"buy price sanity failed: SL {sl} < entry {entry} required")
        bad = [tp for tp in tps if not tp > entry]
    if bad:
        raise SignalError(f"{side} target(s) {bad} on the wrong side of entry {entry}")


def _fmt(x: float) -> str:
    return f"{x:g}"


def resolve_symbol(raw: str) -> str:
    """Channel jargon -> canonical name. The built-in alias table normalises
    the obvious ones (GOLD -> XAUUSD); the operator's SYMBOL_MAP overrides it
    for either spelling."""
    if raw in SYMBOL_MAP:
        return SYMBOL_MAP[raw]
    canonical = SYMBOL_ALIASES.get(raw, raw)
    return SYMBOL_MAP.get(canonical, canonical)


def select_targets(sig: dict) -> list[float]:
    """Reduce the signal's target ladder to the take-profit(s) actually traded."""
    tps = sig.get("tps") or [sig["tp"]]
    if len(tps) == 1 or TP_MODE == "ladder":
        return tps
    return [tps[-1]] if TP_MODE == "last" else [tps[0]]


def build_commands(sig: dict, comment: str | None = None) -> list[str]:
    """Render a parsed signal as flat ExecRelay webhook command bodies.

    A signal that names its own trigger direction ("Trigger only Above") is
    placed as exactly that pending order type. Otherwise the default "limit"
    entry mode rests BOTH legs as pending limit orders — the first at the
    signal's stated entry price, the second at its own level; "market" mode
    executes the first leg immediately instead. In `ladder` TP mode each
    target gets its own order at the fixed lot.

    The fixed lot is deliberate: position sizing is configured here, never
    taken from the channel message.

    comment defaults to TELEGRAM_INGEST_COMMENT; callers pass an override
    (e.g. the originating channel's initials) to identify the source on the
    broker side, where MT5's comment field is the only place it's visible."""
    symbol = resolve_symbol(sig["symbol"])
    secret = f",secret={SECRET}" if SECRET else ""
    targets = select_targets(sig)
    comment = comment or COMMENT

    # Split the per-signal risk budget evenly over every order this signal
    # expands into, so the SIGNAL total — not each leg — is the cap.
    n_orders = len(targets) * (2 if sig["second"] is not None else 1)
    if RISK_USD_TOTAL > 0:
        sizing = f"risk={round(RISK_USD_TOTAL / n_orders, 2):g}"
    else:
        sizing = f"vol_lots={FIXED_LOT}"

    def common(tp: float) -> str:
        return (
            f"{sizing},sl={_fmt(sig['sl'])},tp={_fmt(tp)}"
            f",comment={comment}{secret}"
        )

    cmds: list[str] = []
    for tp in targets:
        if sig.get("order_type"):
            cmds.append(
                f"{LICENSE_ID},{sig['order_type']},{symbol}"
                f",entry_price={_fmt(sig['entry'])},{common(tp)}"
            )
        elif ENTRY_MODE == "market":
            cmds.append(f"{LICENSE_ID},{sig['side']},{symbol},{common(tp)}")
        else:
            cmds.append(
                f"{LICENSE_ID},{sig['side']}limit,{symbol}"
                f",entry_price={_fmt(sig['entry'])},{common(tp)}"
            )
        if sig["second"] is not None:
            cmd = f"{sig['side']}{sig['second']['kind']}"
            cmds.append(
                f"{LICENSE_ID},{cmd},{symbol}"
                f",entry_price={_fmt(sig['second']['entry'])},{common(tp)}"
            )
    return cmds


# ---------------------------------------------------------------------------
# Telegram + webhook I/O
# ---------------------------------------------------------------------------


def telegram_call(method: str, params: dict) -> dict:
    url = f"{TELEGRAM_API_BASE}/bot{BOT_TOKEN}/{method}"
    req = urllib.request.Request(
        url,
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    timeout = POLL_TIMEOUT + 10 if method == "getUpdates" else 15
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode())
        except Exception:
            raise exc from None


def post_webhook(body: str) -> tuple[int, str]:
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=body.encode(),
        headers={"Content-Type": "text/plain"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()[:300]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:300]


def _command_field(body: str, key: str) -> str:
    for part in body.split(","):
        if part.startswith(f"{key}="):
            return part.split("=", 1)[1]
    return ""


def _as_float(v: str) -> float | None:
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


_SECRET_RE = re.compile(r"secret=[^,]*")


def _redact_secret(body: str) -> str:
    """Mask the live webhook secret out of a command body before it is
    logged anywhere (transaction log or logger calls). The un-redacted body
    must still be the one POSTed to the webhook — this is for output only."""
    return _SECRET_RE.sub("secret=***", body)


def send_notification(chat_id: int, text: str) -> None:
    """Best-effort Telegram notification back to the chat a signal came
    from. Never raises — a failed notification must not affect ingest."""
    try:
        resp = telegram_call("sendMessage", {"chat_id": chat_id, "text": text})
        if not resp.get("ok"):
            logger.error("notify sendMessage failed: %s", resp.get("description"))
    except Exception as exc:
        logger.error("notify sendMessage failed: %s", exc)


def notify_order_placed(
    chat_id: int, body: str, channel_name: str | None, webhook_response: str
) -> None:
    """Confirms the signal was accepted and routed to the broker. This is
    ingress acceptance, not a fill confirmation — MT5 fills happen
    asynchronously once the EA executes the order."""
    _license, command, symbol = body.split(",", 3)[:3]
    entry = _command_field(body, "entry_price")
    sl = _command_field(body, "sl")
    tp = _command_field(body, "tp")
    vol = _command_field(body, "vol_lots")
    risk = _command_field(body, "risk")
    trace_id = ""
    try:
        trace_id = json.loads(webhook_response).get("trace_id", "")
    except Exception:
        pass

    lines = [f"✅ Order placed: {command.upper()} {symbol}"]
    if entry:
        lines.append(f"Entry {entry}")
    size = f"Lot {vol}" if vol else (f"Risk ${risk}" if risk else "Lot auto")
    lines.append(f"SL {sl or '-'} | TP {tp or '-'} | {size}")
    if channel_name:
        lines.append(f"Source: {channel_name}")
    if trace_id:
        lines.append(f"trace {trace_id[:12]}")
    send_notification(chat_id, "\n".join(lines))


# Bounded (chat_id, message_id) memory so restarts/redeliveries can't
# double-trade within a process lifetime. Ingress-side duplicate/quota
# checks are the durable backstop.
_seen: set[tuple[int, int]] = set()
_seen_order: list[tuple[int, int]] = []
_SEEN_MAX = 5000

# Channel-registry enforcement (defense-in-depth point (a) -- see
# scripts/_tradestore.py `channels` table / is_channel_enabled). Cached per
# channel name for ~10s so a disabled/enabled toggle takes effect within
# ~15s without hitting SQLite on every poll iteration for repeat channels.
_CHANNEL_CACHE_TTL_SEC = 10.0
_channel_enabled_cache: dict[str | None, tuple[float, bool]] = {}


def _channel_enabled_cached(channel_name: str | None) -> bool:
    now = time.time()
    cached = _channel_enabled_cache.get(channel_name)
    if cached is not None and (now - cached[0]) < _CHANNEL_CACHE_TTL_SEC:
        return cached[1]
    enabled = is_channel_enabled(channel_name)
    _channel_enabled_cache[channel_name] = (now, enabled)
    return enabled


# Dry-run is an operator switch the dashboard can flip at runtime (persisted in
# the trade store's `meta` table), so it is re-read here rather than trusted
# from the boot-time env var. Same ~10s cache as the channel registry above, so
# a flip takes effect within ~15s without restarting the stack. DRY_RUN (env)
# is only the fallback for a store that has never been written to, and
# get_dry_run() fails safe back to it -- see its docstring.
_DRY_RUN_CACHE_TTL_SEC = 10.0
_dry_run_cache: tuple[float, bool] | None = None


def _dry_run_cached() -> bool:
    global _dry_run_cache
    now = time.time()
    if _dry_run_cache is not None and (now - _dry_run_cache[0]) < _DRY_RUN_CACHE_TTL_SEC:
        return _dry_run_cache[1]
    effective = get_dry_run(DRY_RUN)
    # Backstop: LICENSE_ID prefixes every command built for the broker, and the
    # startup check enforcing it could only ever run at boot -- back when
    # dry-run was fixed for the life of the process. A runtime override must
    # not become a way around it, so an override that says "live" without a
    # license id is refused HERE, the point every message passes through. The
    # dashboard rejects the same combination up front; this is what makes it
    # unbypassable.
    if not effective and not LICENSE_ID:
        logger.error(
            "dry-run override says LIVE but TELEGRAM_INGEST_LICENSE_ID is empty -- "
            "staying in DRY-RUN (commands would be malformed)"
        )
        effective = True
    _dry_run_cache = (now, effective)
    return effective


def _mark_seen(key: tuple[int, int]) -> bool:
    """Returns False if already seen."""
    if key in _seen:
        return False
    _seen.add(key)
    _seen_order.append(key)
    if len(_seen_order) > _SEEN_MAX:
        _seen.discard(_seen_order.pop(0))
    return True


def _handle_tp_update(
    chat_id: int, message_id: int, channel_name: str | None, text: str
) -> bool:
    """Amend the take-profit on the trades this channel already has running.

    Returns True if the message WAS an amendment (handled here; the caller
    stops), False if it was not (the caller carries on with its normal
    not-a-signal / rejected handling).

    Scoping is what makes this safe: every command carries this channel's
    comment tag, and ea_shim only touches positions whose comment matches, so
    one channel's amendment can never move another channel's TP -- nor that of
    a trade placed by hand."""
    tp = parse_tp_update(text)
    if tp is None:
        return False

    comment = f"tg-{channel_initials(channel_name)}" if channel_name else COMMENT
    symbols = recent_symbols_for_channel(channel_name, days=_TP_UPDATE_LOOKBACK_DAYS)
    if not symbols:
        # Nothing traded recently -> nothing to amend. Recorded rather than
        # dropped, so the dashboard shows the amendment arrived and why it
        # did nothing.
        logger.info(
            "chat %s msg %s: TP amendment to %s from %r, but this channel has traded "
            "nothing in the last %s day(s) -- nothing to amend",
            chat_id, message_id, _fmt(tp), channel_name or "direct", _TP_UPDATE_LOOKBACK_DAYS,
        )
        record_signal(
            chat_id=chat_id, message_id=message_id, channel=channel_name,
            outcome="tp_update_noop", tp=tp, n_commands=0,
            raw=_redact_secret(text)[:500],
        )
        return True

    commands = build_tp_update_commands(tp, symbols, comment=comment)
    dry_run = _dry_run_cached()
    logger.info(
        "chat %s msg %s: TP amendment -> %s on %s (scope %s)",
        chat_id, message_id, _fmt(tp), ", ".join(symbols), comment,
    )
    # Deliberately NOT the plain "dry_run" outcome: that one feeds the
    # dashboard's "signals not placed" panel, which offers a resubmit -- and
    # resubmitting an amendment would run it back through parse_signal and
    # fail, since it was never a signal. Its own outcome keeps it out.
    record_signal(
        chat_id=chat_id, message_id=message_id, channel=channel_name,
        outcome="tp_update_dry_run" if dry_run else "tp_update",
        symbol=symbols[0], tp=tp, n_commands=len(commands),
        raw=_redact_secret(text)[:500],
    )

    posted = 0
    for body in commands:
        if dry_run:
            logger.info("DRY-RUN would POST: %s", _redact_secret(body))
            log_txn(
                TXN_LOG, chat_id=chat_id, message_id=message_id, channel=channel_name,
                outcome="dry_run", command=_redact_secret(body),
            )
            continue
        try:
            status, resp = post_webhook(body)
        except Exception as exc:
            logger.error("webhook POST failed: %s (body: %s)", exc, _redact_secret(body))
            log_txn(
                TXN_LOG, chat_id=chat_id, message_id=message_id, channel=channel_name,
                outcome="webhook_error", command=_redact_secret(body), error=str(exc),
            )
            continue
        (logger.info if status == 200 else logger.error)(
            "webhook %s -> %d %s", body.split(",", 3)[1], status, resp
        )
        log_txn(
            TXN_LOG, chat_id=chat_id, message_id=message_id, channel=channel_name,
            outcome="posted", command=_redact_secret(body),
            http_status=status, response=resp,
        )
        if status == 200:
            posted += 1
            try:
                trace_id = json.loads(resp).get("trace_id", "")
            except (json.JSONDecodeError, AttributeError):
                trace_id = ""
            if trace_id:
                append_signal_trace(chat_id, message_id, trace_id)

    if posted and not dry_run:
        send_notification(
            chat_id,
            f"✏️ TP amended to {_fmt(tp)}\n"
            f"Applied to open {', '.join(symbols)} trade(s)"
            + (f"\nSource: {channel_name}" if channel_name else ""),
        )
    return True


def handle_message(chat_id: int, message_id: int, text: str) -> None:
    if chat_id not in ALLOWED_CHAT_IDS:
        logger.debug("ignoring message from non-allowlisted chat %s", chat_id)
        return
    if not _mark_seen((chat_id, message_id)):
        return
    channel_name, text = strip_source_tag(text)

    # Enforcement point (a): a channel explicitly disabled in the registry
    # is skipped entirely -- no ingress POST, no Telegram notification.
    # A tagged channel with NO registry row at all fails OPEN (stays
    # enabled) so a config gap can never silently stop trading; see
    # is_channel_enabled()'s docstring. The dashboard's "unregistered
    # channel" warning is what surfaces that gap to the operator.
    if not _channel_enabled_cached(channel_name):
        logger.info(
            "chat %s msg %s: channel %r is disabled in the registry -- skipping",
            chat_id, message_id, channel_name or "direct",
        )
        log_txn(
            TXN_LOG,
            chat_id=chat_id,
            message_id=message_id,
            channel=channel_name,
            outcome="channel_disabled",
            raw_text=text[:500],
        )
        record_signal(
            chat_id=chat_id,
            message_id=message_id,
            channel=channel_name,
            outcome="channel_disabled",
            n_commands=0,
            raw=_redact_secret(text)[:500],
        )
        return

    try:
        sig = parse_signal(text)
    except SignalError as exc:
        # An amendment ("Tp set @ 4346 for both trade") is not a signal, so
        # parse_signal is right to refuse it. Give it its own path before
        # writing the message off as rejected.
        if _handle_tp_update(chat_id, message_id, channel_name, text):
            return
        logger.warning(
            "chat %s msg %s: signal REJECTED (%s): %r", chat_id, message_id, exc, text[:200]
        )
        log_txn(
            TXN_LOG,
            chat_id=chat_id,
            message_id=message_id,
            channel=channel_name,
            outcome="rejected",
            reason=str(exc),
            raw_text=text[:500],
        )
        record_signal(
            chat_id=chat_id,
            message_id=message_id,
            channel=channel_name,
            outcome="rejected",
            n_commands=0,
            raw=_redact_secret(text)[:500],
        )
        return
    if sig is None:
        if _handle_tp_update(chat_id, message_id, channel_name, text):
            return
        # Recorded, not just dropped: the dashboard's "Ignored messages" page
        # is how an operator checks that nothing tradeable was passed over.
        # A channel posts far more chatter than signals, so this is the
        # highest-volume outcome by some margin -- raw stays truncated and
        # secret-redacted like every other row.
        logger.debug("chat %s msg %s: not a signal", chat_id, message_id)
        record_signal(
            chat_id=chat_id,
            message_id=message_id,
            channel=channel_name,
            outcome="ignored",
            n_commands=0,
            raw=_redact_secret(text)[:500],
        )
        return

    comment = f"tg-{channel_initials(channel_name)}" if channel_name else None
    commands = build_commands(sig, comment=comment)
    # Read the switch ONCE for the whole message: the outcome recorded below
    # and the per-command branch further down must agree even if the operator
    # flips dry-run (or the cache expires) midway through handling this one.
    dry_run = _dry_run_cached()
    record_signal(
        chat_id=chat_id,
        message_id=message_id,
        channel=channel_name,
        outcome="dry_run" if dry_run else "posted",
        symbol=sig.get("symbol"),
        side=sig.get("side"),
        entry=sig.get("entry"),
        sl=sig.get("sl"),
        tp=sig.get("tp"),
        n_commands=len(commands),
        raw=_redact_secret(text)[:500],
    )
    for body in commands:
        if dry_run:
            logger.info("DRY-RUN would POST: %s", _redact_secret(body))
            log_txn(
                TXN_LOG,
                chat_id=chat_id,
                message_id=message_id,
                channel=channel_name,
                outcome="dry_run",
                signal=sig,
                command=_redact_secret(body),
            )
            continue
        try:
            status, resp = post_webhook(body)
        except Exception as exc:
            logger.error("webhook POST failed: %s (body: %s)", exc, _redact_secret(body))
            log_txn(
                TXN_LOG,
                chat_id=chat_id,
                message_id=message_id,
                channel=channel_name,
                outcome="webhook_error",
                signal=sig,
                command=_redact_secret(body),
                error=str(exc),
            )
            continue
        log = logger.info if status == 200 else logger.error
        log("webhook %s -> %d %s", body.split(",", 3)[1], status, resp)
        log_txn(
            TXN_LOG,
            chat_id=chat_id,
            message_id=message_id,
            channel=channel_name,
            outcome="posted",
            signal=sig,
            command=_redact_secret(body),
            http_status=status,
            response=resp,
        )
        if status == 200:
            notify_order_placed(chat_id, body, channel_name, resp)
            try:
                trace_id = json.loads(resp).get("trace_id", "")
            except (json.JSONDecodeError, AttributeError):
                trace_id = ""
            if trace_id:
                _command, _symbol = body.split(",", 3)[1:3]
                append_signal_trace(chat_id, message_id, trace_id)
                record_order(
                    trace_id=trace_id,
                    source="telegram",
                    command=_command,
                    symbol=_symbol,
                    requested_risk=_as_float(_command_field(body, "risk")),
                    volume=_as_float(_command_field(body, "vol_lots")),
                    sl=_as_float(_command_field(body, "sl")),
                    tp=_as_float(_command_field(body, "tp")),
                    entry=_as_float(_command_field(body, "entry_price")),
                    status="accepted",
                    comment=_command_field(body, "comment") or None,
                )


def poll_loop() -> None:
    offset: int | None = None
    logger.info(
        "ingest loop started (dry_run=%s, chats=%s, lot=%s, entry_mode=%s, tp_mode=%s)",
        _dry_run_cached(),
        sorted(ALLOWED_CHAT_IDS),
        FIXED_LOT,
        ENTRY_MODE,
        TP_MODE,
    )
    while True:
        params: dict = {
            "timeout": POLL_TIMEOUT,
            "allowed_updates": ["channel_post", "message"],
        }
        if offset is not None:
            params["offset"] = offset
        try:
            resp = telegram_call("getUpdates", params)
        except Exception as exc:
            _readiness.update(poll_ok=False, detail=repr(exc)[:200])
            logger.warning("getUpdates transport error: %s", exc)
            time.sleep(3)
            continue
        if not resp.get("ok"):
            _readiness.update(poll_ok=False, detail=str(resp.get("description"))[:200])
            logger.warning("getUpdates failed: %s", resp.get("description"))
            time.sleep(3)
            continue
        _readiness.update(poll_ok=True, detail="")
        for update in resp.get("result", []):
            offset = update["update_id"] + 1
            msg = update.get("channel_post") or update.get("message")
            if not msg or "text" not in msg:
                continue
            try:
                handle_message(msg["chat"]["id"], msg["message_id"], msg["text"])
            except Exception:
                logger.exception("update %s failed", update.get("update_id"))


# ---------------------------------------------------------------------------
# Health server + entrypoint
# ---------------------------------------------------------------------------


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in ("/health", "/healthz"):
            self._json(200, {"service": SERVICE, "status": "ok"})
        elif self.path == "/readyz":
            snap = dict(_readiness)
            ok = bool(snap.get("poll_ok"))
            self._json(
                200 if ok else 503,
                {"service": SERVICE, "ok": ok, "detail": snap.get("detail", "")},
            )
        else:
            self.send_response(404)
            self.end_headers()

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, _fmt: str, *_args: object) -> None:
        pass


def start_health_server(addr: str) -> None:
    host, port_str = addr.rsplit(":", 1)
    server = ThreadingHTTPServer((host, int(port_str)), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def healthcheck(addr: str) -> None:
    host = "127.0.0.1" if addr.startswith("0.0.0.0:") else addr.rsplit(":", 1)[0]
    port = addr.rsplit(":", 1)[1]
    with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=1.5) as r:
        if r.status != 200:
            raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    if args.healthcheck:
        healthcheck(HTTP_ADDR)
        return

    errors = []
    if not BOT_TOKEN:
        errors.append("TELEGRAM_INGEST_BOT_TOKEN is required")
    if not ALLOWED_CHAT_IDS:
        errors.append("TELEGRAM_INGEST_ALLOWED_CHAT_IDS is required (comma-separated)")
    if not DRY_RUN and not LICENSE_ID:
        errors.append("TELEGRAM_INGEST_LICENSE_ID is required when dry-run is off")
    if ENTRY_MODE not in ("limit", "market"):
        errors.append(f"TELEGRAM_INGEST_ENTRY_MODE must be limit|market, got {ENTRY_MODE!r}")
    if TP_MODE not in ("first", "last", "ladder"):
        errors.append(f"TELEGRAM_INGEST_TP_MODE must be first|last|ladder, got {TP_MODE!r}")
    if errors:
        for e in errors:
            logger.error(e)
        raise SystemExit(2)

    if _dry_run_cached():
        logger.warning(
            "running in DRY-RUN mode: commands are logged, nothing is traded. "
            "Turn the dry-run switch off in the trade dashboard's pipeline bar "
            "(or set TELEGRAM_INGEST_DRY_RUN=false) to go live."
        )

    start_health_server(HTTP_ADDR)
    poll_loop()


if __name__ == "__main__":
    main()
