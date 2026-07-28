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
ENTRY_MODE = os.environ.get("TELEGRAM_INGEST_ENTRY_MODE", "limit").lower()

# Safety default: log what WOULD be sent, send nothing.
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
# Strict by design: the whole message is rejected unless every recognised
# part is consistent. Target format (whitespace/case tolerant):
#
#   GOLD SELL @ 4099
#   SECOND SELL LIMIT @ 4109
#   SL @ 4119
#   TP @ 4089
#
# Trailing commentary ("Risk Management Example", disclaimers, lot tables)
# is ignored.

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


class SignalError(ValueError):
    """Message looked like a signal but is inconsistent — reject loudly."""


def parse_signal(text: str) -> dict | None:
    """Parse a channel message into a signal dict, or None if the message is
    not a signal at all. Raises SignalError when it matches the grammar but
    fails a consistency check (wrong stop side, mismatched second order, ...).
    """
    entry_m = _ENTRY_RE.search(text)
    if entry_m is None:
        return None

    sl_m = _SL_RE.search(text)
    tp_m = _TP_RE.search(text)
    if sl_m is None or tp_m is None:
        raise SignalError("signal missing SL or TP line")

    side = entry_m.group("side").lower()
    entry = float(entry_m.group("entry"))
    sl = float(sl_m.group(1))
    tp = float(tp_m.group(1))

    # SL must be on the losing side, TP on the winning side.
    if side == "sell" and not (sl > entry > tp):
        raise SignalError(f"sell price sanity failed: SL {sl} > entry {entry} > TP {tp} required")
    if side == "buy" and not (sl < entry < tp):
        raise SignalError(f"buy price sanity failed: SL {sl} < entry {entry} < TP {tp} required")

    sig: dict = {
        "symbol": entry_m.group("symbol").upper(),
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "second": None,
    }

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


def _fmt(x: float) -> str:
    return f"{x:g}"


def build_commands(sig: dict) -> list[str]:
    """Render a parsed signal as flat ExecRelay webhook command bodies. In
    the default "limit" entry mode BOTH legs rest as pending limit orders —
    the first at the signal's stated entry price, the second at its own
    level; "market" mode executes the first leg immediately instead. The
    fixed lot is deliberate: position sizing is configured here, never taken
    from the channel message."""
    symbol = SYMBOL_MAP.get(sig["symbol"], sig["symbol"])
    secret = f",secret={SECRET}" if SECRET else ""
    common = f"vol_lots={FIXED_LOT},sl={_fmt(sig['sl'])},tp={_fmt(sig['tp'])},comment={COMMENT}{secret}"

    if ENTRY_MODE == "market":
        cmds = [f"{LICENSE_ID},{sig['side']},{symbol},{common}"]
    else:
        cmds = [
            f"{LICENSE_ID},{sig['side']}limit,{symbol},entry_price={_fmt(sig['entry'])},{common}"
        ]
    if sig["second"] is not None:
        cmd = f"{sig['side']}{sig['second']['kind']}"
        cmds.append(
            f"{LICENSE_ID},{cmd},{symbol},entry_price={_fmt(sig['second']['entry'])},{common}"
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


# Bounded (chat_id, message_id) memory so restarts/redeliveries can't
# double-trade within a process lifetime. Ingress-side duplicate/quota
# checks are the durable backstop.
_seen: set[tuple[int, int]] = set()
_seen_order: list[tuple[int, int]] = []
_SEEN_MAX = 5000


def _mark_seen(key: tuple[int, int]) -> bool:
    """Returns False if already seen."""
    if key in _seen:
        return False
    _seen.add(key)
    _seen_order.append(key)
    if len(_seen_order) > _SEEN_MAX:
        _seen.discard(_seen_order.pop(0))
    return True


def handle_message(chat_id: int, message_id: int, text: str) -> None:
    if chat_id not in ALLOWED_CHAT_IDS:
        logger.debug("ignoring message from non-allowlisted chat %s", chat_id)
        return
    if not _mark_seen((chat_id, message_id)):
        return
    try:
        sig = parse_signal(text)
    except SignalError as exc:
        logger.warning(
            "chat %s msg %s: signal REJECTED (%s): %r", chat_id, message_id, exc, text[:200]
        )
        return
    if sig is None:
        logger.debug("chat %s msg %s: not a signal", chat_id, message_id)
        return

    commands = build_commands(sig)
    for body in commands:
        if DRY_RUN:
            logger.info("DRY-RUN would POST: %s", body)
            continue
        try:
            status, resp = post_webhook(body)
        except Exception as exc:
            logger.error("webhook POST failed: %s (body: %s)", exc, body)
            continue
        log = logger.info if status == 200 else logger.error
        log("webhook %s -> %d %s", body.split(",", 3)[1], status, resp)


def poll_loop() -> None:
    offset: int | None = None
    logger.info(
        "ingest loop started (dry_run=%s, chats=%s, lot=%s)",
        DRY_RUN,
        sorted(ALLOWED_CHAT_IDS),
        FIXED_LOT,
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
    if errors:
        for e in errors:
            logger.error(e)
        raise SystemExit(2)

    if DRY_RUN:
        logger.warning(
            "running in DRY-RUN mode: commands are logged, nothing is traded. "
            "Set TELEGRAM_INGEST_DRY_RUN=false to go live."
        )

    start_health_server(HTTP_ADDR)
    poll_loop()


if __name__ == "__main__":
    main()
