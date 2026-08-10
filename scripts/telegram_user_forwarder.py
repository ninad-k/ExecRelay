"""Auto-forward signal messages from a channel you follow into the ingest
relay group — using YOUR OWN Telegram account via the official MTProto API
(Telethon), because bots cannot read channels they aren't members of.

It re-sends the message TEXT (not a Telegram "forward"), so it also works
for channels that restrict forwarding. Read-only on the source; writes only
to your own relay group. For personal use on your own account: automating
mass actions/spam violates Telegram's terms — this does neither, but the
account and its standing are yours.

Setup (one time, run by the account owner — the login is interactive):
  1. Get an api_id + api_hash at https://my.telegram.org (API development
     tools). These identify your app, not your password.
  2. pip install telethon
  3. TG_FORWARDER_API_ID=... TG_FORWARDER_API_HASH=... \
       python scripts/telegram_user_forwarder.py login
     (asks for your phone + the code Telegram sends you; a session file is
     saved so this never repeats). If code delivery is flood-limited
     (FloodWaitError on SendCodeRequest), use `qrlogin` instead: it links via
     a QR scan from the phone app and sends no code at all.
  4. python scripts/telegram_user_forwarder.py chats
     -> lists your dialogs with numeric ids; note the source channel id and
        the relay group id
  5. TG_FORWARDER_SOURCE_CHAT=-100... TG_FORWARDER_TARGET_CHAT=-... \
       python scripts/telegram_user_forwarder.py run

Sources may be given as numeric ids, @usernames, or a fragment of the channel's
display name ("Dr Devendra"), and more than one may be watched at once —
`resolve` prints what each one matches without starting the relay.

Environment:
  TG_FORWARDER_API_ID / TG_FORWARDER_API_HASH   my.telegram.org credentials
  TG_FORWARDER_PHONE         phone for the login prompt (optional)
  TG_FORWARDER_SESSION       session file path (default .local-stack/tg-forwarder)
  TG_FORWARDER_SOURCE_CHAT   comma-separated channels to watch (id/@name/title)
  TG_FORWARDER_TARGET_CHAT   the relay group the bot reads (id/@name/title)
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from telethon import TelegramClient, events
except ImportError:  # pragma: no cover
    print("telethon is required: pip install telethon", file=sys.stderr)
    sys.exit(2)

# Same directory as _tradestore.py, so a plain import resolves it without
# any sys.path surgery (unlike apps/telegram-ingest/app.py, which lives
# elsewhere and inserts scripts/ explicitly).
import _tradestore as ts


def _load_dotenv() -> None:
    """Fill os.environ from the repo's .env so the script works when run by
    hand, not only under local-stack.ps1 (which exports these itself).
    Already-set variables win, so an explicit override still works."""
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

API_ID = os.environ.get("TG_FORWARDER_API_ID", "")
API_HASH = os.environ.get("TG_FORWARDER_API_HASH", "")
PHONE = os.environ.get("TG_FORWARDER_PHONE", "")
SESSION = os.environ.get("TG_FORWARDER_SESSION", ".local-stack/tg-forwarder")
SOURCE_CHAT = os.environ.get("TG_FORWARDER_SOURCE_CHAT", "")
TARGET_CHAT = os.environ.get("TG_FORWARDER_TARGET_CHAT", "")


def log(*a) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def short_exc(exc: BaseException, limit: int = 200) -> str:
    """One-line, bounded description of an exception.

    Never log Telethon errors with repr(): TypeNotFoundError embeds the
    entire remaining MTProto buffer in its message, so a single logged
    exception dumps megabytes of binary into the log (and the console that
    tails it). Telegram adds constructors Telethon doesn't know yet, so this
    fires routinely on iter_dialogs.
    """
    text = " ".join(str(exc).split())
    if len(text) > limit:
        text = text[:limit] + f"… (+{len(text) - limit} chars)"
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def client() -> TelegramClient:
    if not API_ID or not API_HASH:
        print(
            "TG_FORWARDER_API_ID and TG_FORWARDER_API_HASH are required "
            "(create them at https://my.telegram.org)",
            file=sys.stderr,
        )
        sys.exit(2)
    return TelegramClient(SESSION, int(API_ID), API_HASH)


def _split(spec: str) -> list[str]:
    return [part.strip() for part in spec.split(",") if part.strip()]


async def _resolve(c: TelegramClient, spec: str) -> tuple[int, str] | None:
    """Turn one channel specifier into (id, title).

    Numeric ids and @usernames go straight to Telegram; anything else is
    matched case-insensitively against the titles of the dialogs this account
    is actually in, which is the only way to name a channel you follow but
    that has no public username.
    """
    spec = spec.lstrip("@") if not spec.lstrip("-").isdigit() else spec
    try:
        entity = await c.get_entity(int(spec) if spec.lstrip("-").isdigit() else spec)
        return entity.id, getattr(entity, "title", None) or getattr(entity, "username", spec)
    except Exception:
        pass  # fall through to a title search over the account's dialogs

    needle = spec.lower()
    async for d in c.iter_dialogs():
        if needle in (d.name or "").lower():
            return d.id, d.name
    return None


async def _resolve_all(c: TelegramClient, spec: str, label: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for part in _split(spec):
        hit = await _resolve(c, part)
        if hit is None:
            log(f"{label}: could not resolve {part!r} — are you subscribed to it?")
            continue
        log(f"{label}: {part!r} -> {hit[1]} ({hit[0]})")
        out.append(hit)
    return out


# ---------------------------------------------------------------------------
# Channel registry -- source of truth for WHICH channels are watched, so the
# dashboard's enable/disable toggle and "add channel" form take effect
# without restarting this process. See scripts/_tradestore.py `channels`
# table + docs/... The registry is polled every _ENABLED_REFRESH_SEC by
# cmd_run's background task; this module is the only writer of tg_dialogs
# and the only resolver of pending (non-numeric) channel specs, since it's
# the only process holding a Telethon session.
# ---------------------------------------------------------------------------

_ENABLED_REFRESH_SEC = 30
_DIALOG_REFRESH_SEC = 600
_HEARTBEAT_SEC = 30


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_channel_rows() -> list[dict] | None:
    """Read every row of the `channels` table. Returns None specifically
    when the store itself is unreachable (get_conn() failed) -- distinct
    from a reachable store legitimately returning zero rows -- so callers
    can tell "SQLite hiccup" apart from "operator disabled everything"."""
    conn = ts.get_conn()
    if conn is None:
        return None
    try:
        rows = conn.execute(
            "SELECT chat_id, title, spec, enabled, note FROM channels"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        log(f"channel registry read failed: {short_exc(exc)}")
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


async def refresh_enabled_channels(
    c: TelegramClient | None, last_good: list[dict]
) -> list[dict]:
    """Compute the current watch set: [{"chat_id": int, "title": str}, ...]
    for every ENABLED, Telethon-watchable channel row (the 'direct'
    pseudo-channel is never watchable here -- it means "no [SRC:] tag", not
    a chat this account listens to).

    Resolution: a row whose chat_id isn't purely numeric is "pending" (added
    via free-text spec, e.g. from the dashboard, or seeded from a title-
    fragment TG_FORWARDER_SOURCE_CHAT entry); a row missing its title only
    needs a title backfill. Both are resolved here via Telethon (`c`) and
    written back to the store (resolve_channel / mark_channel_resolution_error)
    so the dashboard can show real titles and clear the "resolving..." state.
    When `c` is None (used by the offline unit test, or if called before the
    client is ready) pending/title-less rows are skipped rather than guessed.

    Fallback contract: returns `last_good` unchanged if the store is
    unreachable -- a SQLite hiccup must mean "keep relaying whatever we were
    already relaying", never "relay nothing" and never "relay everything".
    """
    rows = load_channel_rows()
    if rows is None:
        log("channel registry unreachable; keeping last known-good channel set")
        return last_good

    resolved: list[dict] = []
    for row in rows:
        if not row.get("enabled"):
            continue
        chat_id = str(row.get("chat_id") or "")
        if chat_id == "direct" or not chat_id:
            continue  # not a Telethon chat -- represents "no [SRC:] tag"
        title = (row.get("title") or "").strip()
        is_numeric = chat_id.lstrip("-").isdigit()

        if is_numeric and title:
            resolved.append({"chat_id": int(chat_id), "title": title})
            continue

        if c is None:
            continue  # can't resolve right now; drop until a real refresh can

        spec = row.get("spec") or chat_id
        hit = await _resolve(c, spec)
        if hit is None:
            log(f"channel registry: could not resolve {spec!r} — are you subscribed to it?")
            ts.mark_channel_resolution_error(
                chat_id, f"could not resolve {spec!r} — not subscribed, or the channel/spec is wrong"
            )
            continue
        new_id, new_title = hit
        ts.resolve_channel(pending_chat_id=chat_id, resolved_chat_id=str(new_id), resolved_title=new_title)
        log(f"channel registry: resolved {spec!r} -> {new_title} ({new_id})")
        resolved.append({"chat_id": int(new_id), "title": new_title})

    return resolved


async def _refresh_tg_dialogs(c: TelegramClient) -> None:
    """Repopulate tg_dialogs from the account's current dialog list, so the
    dashboard's "add channel" picker offers real titles/ids. Best-effort:
    Telethon errors are logged, not raised (this must never take the relay
    loop down)."""
    try:
        dialogs: list[tuple[str, str, str]] = []
        async for d in c.iter_dialogs():
            kind = "channel" if d.is_channel else "group" if d.is_group else "user"
            dialogs.append((str(d.id), d.name or "", kind))
        ts.replace_tg_dialogs(dialogs)
        log(f"tg_dialogs refreshed: {len(dialogs)} dialog(s)")
    except Exception as exc:  # noqa: BLE001
        log(f"tg_dialogs refresh failed: {short_exc(exc)}")


# Held for the process lifetime; the OS releases it when we exit (even on a
# hard kill), so a stale lock file can never wedge the next start.
_instance_lock_handle = None


def acquire_single_instance_lock() -> None:
    """Refuse to start a second relay against the same Telethon session.

    Two processes sharing one .session file corrupt each other: SQLite
    reports "database is locked", and the loser can read the session as
    unauthorized and drop into the interactive phone prompt — where, with no
    console attached, it hangs forever, relaying nothing while the
    supervisor still reports it "running". That failure silently swallowed a
    live channel signal on 2026-08-10, so it is now impossible by
    construction.
    """
    global _instance_lock_handle
    lock_path = Path(f"{SESSION}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")  # noqa: SIM115 - must outlive this function
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        print(
            f"another forwarder already holds {lock_path} — refusing to start a "
            "second relay on the same Telegram session (they would corrupt it "
            "and drop this one into a login prompt).\n"
            "Stop the other instance first: .\\stop.ps1, or check for a stray "
            "python scripts/telegram_user_forwarder.py process.",
            file=sys.stderr,
        )
        sys.exit(4)
    _instance_lock_handle = handle


async def connect_authorized(c: TelegramClient) -> None:
    """Connect with an EXISTING session, or exit non-zero with an actionable
    message.

    Never falls through to Telethon's interactive phone prompt: under the
    stack supervisor stdin is not a console, so `client.start()` blocks
    forever on that prompt — the process stays alive, relays nothing, and
    `local-stack status` still calls it "running". A revoked session must
    fail loudly instead (the supervisor then reports it DOWN)."""
    await c.connect()
    if not await c.is_user_authorized():
        print(
            f"session {SESSION}.session is not authorized (revoked, expired, or "
            "never created) — no messages can be relayed.\n"
            "Re-authorize interactively, then restart the stack:\n"
            "    python scripts/telegram_user_forwarder.py qrlogin",
            file=sys.stderr,
        )
        await c.disconnect()
        sys.exit(3)


async def cmd_login() -> None:
    c = client()
    # Interactive: phone + login code prompts. A configured phone skips one.
    await c.start(**({"phone": PHONE} if PHONE else {}))
    async with c:
        me = await c.get_me()
        log(f"logged in as {me.first_name} (@{me.username}) — session saved to {SESSION}.session")


async def cmd_chats() -> None:
    c = client()
    await connect_authorized(c)
    async with c:
        async for d in c.iter_dialogs():
            kind = "channel" if d.is_channel else "group" if d.is_group else "user"
            print(f"{d.id:>15}  {kind:<7}  {d.name}")


async def cmd_resolve() -> None:
    """Dry run of `run`'s lookup step — no messages are relayed."""
    c = client()
    await connect_authorized(c)
    async with c:
        await _resolve_all(c, SOURCE_CHAT, "source")
        await _resolve_all(c, TARGET_CHAT, "target")


async def cmd_qrlogin() -> None:
    """Log in by scanning a QR code with the Telegram app on your phone
    (Settings -> Devices -> Link Desktop Device). No SMS/login code is sent,
    so this also works while SendCodeRequest is flood-limited."""
    try:
        import qrcode
    except ImportError:  # pragma: no cover
        print("qrcode is required: pip install qrcode", file=sys.stderr)
        sys.exit(2)
    from telethon.errors import SessionPasswordNeededError

    c = client()
    await c.connect()
    if await c.is_user_authorized():
        me = await c.get_me()
        log(f"already logged in as {me.first_name} (@{me.username}) — nothing to do")
        await c.disconnect()
        return

    qr = await c.qr_login()
    log("on your phone: Telegram -> Settings -> Devices -> Link Desktop Device, then scan:")
    while True:
        code = qrcode.QRCode()
        code.add_data(qr.url)
        code.print_ascii(invert=True)
        print(f"(or open manually: {qr.url})")
        try:
            await qr.wait(30)
            break
        except asyncio.TimeoutError:
            await qr.recreate()  # tokens expire every ~30s; show a fresh one
            log("QR expired — here is a new one:")
        except SessionPasswordNeededError:
            import getpass

            await c.sign_in(password=getpass.getpass("two-step verification password: "))
            break
    me = await c.get_me()
    log(f"logged in as {me.first_name} (@{me.username}) — session saved to {SESSION}.session")
    await c.disconnect()


async def cmd_run() -> None:
    if not SOURCE_CHAT or not TARGET_CHAT:
        print("TG_FORWARDER_SOURCE_CHAT and TG_FORWARDER_TARGET_CHAT are required", file=sys.stderr)
        sys.exit(2)
    acquire_single_instance_lock()
    c = client()
    await connect_authorized(c)

    # Resolved once at startup from the env list -- this is the fallback
    # baseline used whenever the channel-registry store is unreachable, so a
    # SQLite hiccup can never mean "relay nothing" (nor "relay everything").
    # It is NOT what decides what's watched during normal operation once the
    # store is reachable; see refresh_enabled_channels().
    env_sources = await _resolve_all(c, SOURCE_CHAT, "source")
    targets = await _resolve_all(c, TARGET_CHAT, "target")
    if not env_sources or not targets:
        print("nothing to relay: source or target did not resolve", file=sys.stderr)
        await c.disconnect()
        sys.exit(1)
    if len(targets) > 1:
        print("TG_FORWARDER_TARGET_CHAT must name exactly one group", file=sys.stderr)
        await c.disconnect()
        sys.exit(2)
    target = targets[0][0]

    last_good: list[dict] = [{"chat_id": cid, "title": name} for cid, name in env_sources]
    # Resolve pending/title-less registry rows synchronously before we start
    # "watching" so the very first relayed message already carries the right
    # [SRC:<title>] tag -- no behavior change vs. the old fixed-list startup.
    watch_list = await refresh_enabled_channels(c, last_good)
    watched: dict[int, str] = {row["chat_id"]: row["title"] for row in watch_list}

    @c.on(events.NewMessage())
    async def _on_message(event) -> None:
        if event.chat_id not in watched:
            return  # not in the current enabled set -- registry-filtered
        text = event.message.message or ""
        if not text.strip():
            return  # media-only posts carry nothing the parser can use
        # Tag with the source channel's title so telegram-ingest can label
        # the trade comment (and notifications) with where it came from —
        # the bot otherwise only ever sees this relay chat, never the
        # source channel itself.
        name = watched.get(event.chat_id, "")
        tagged = f"[SRC:{name}]\n{text}" if name else text
        await c.send_message(target, tagged)
        log(f"relayed message {event.message.id}: {text[:60].replace(chr(10), ' / ')}")

    async def _enabled_refresh_loop() -> None:
        nonlocal watched, watch_list
        while True:
            await asyncio.sleep(_ENABLED_REFRESH_SEC)
            try:
                watch_list = await refresh_enabled_channels(c, watch_list)
                watched = {row["chat_id"]: row["title"] for row in watch_list}
            except Exception as exc:  # noqa: BLE001 - background task must never die
                log(f"enabled-channel refresh failed: {short_exc(exc)}")

    async def _dialog_refresh_loop() -> None:
        while True:
            await _refresh_tg_dialogs(c)
            await asyncio.sleep(_DIALOG_REFRESH_SEC)

    async def _heartbeat_loop() -> None:
        while True:
            try:
                ts.meta_set("hb_forwarder", _utcnow_iso())
                ts.meta_set("hb_forwarder_channels", str(len(watched)))
            except Exception as exc:  # noqa: BLE001
                log(f"heartbeat write failed: {short_exc(exc)}")
            await asyncio.sleep(_HEARTBEAT_SEC)

    names = ", ".join(f"{name} ({cid})" for cid, name in watched.items())
    log(f"watching {len(watched)} channel(s) [{names}], relaying text posts to {targets[0][1]} — Ctrl+C to stop")
    log("channel registry now controls the watch set: enable/disable/add/remove from the dashboard takes effect within ~60s, no restart needed")

    asyncio.create_task(_enabled_refresh_loop())
    asyncio.create_task(_dialog_refresh_loop())
    asyncio.create_task(_heartbeat_loop())
    await c.run_until_disconnected()


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    commands = {"login": cmd_login, "qrlogin": cmd_qrlogin, "chats": cmd_chats, "resolve": cmd_resolve, "run": cmd_run}
    if cmd not in commands:
        print(f"usage: telegram_user_forwarder.py {'|'.join(commands)}", file=sys.stderr)
        sys.exit(2)
    asyncio.run(commands[cmd]())


if __name__ == "__main__":
    main()
