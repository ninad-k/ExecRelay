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
     saved so this never repeats)
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

try:
    from telethon import TelegramClient, events
except ImportError:  # pragma: no cover
    print("telethon is required: pip install telethon", file=sys.stderr)
    sys.exit(2)

API_ID = os.environ.get("TG_FORWARDER_API_ID", "")
API_HASH = os.environ.get("TG_FORWARDER_API_HASH", "")
PHONE = os.environ.get("TG_FORWARDER_PHONE", "")
SESSION = os.environ.get("TG_FORWARDER_SESSION", ".local-stack/tg-forwarder")
SOURCE_CHAT = os.environ.get("TG_FORWARDER_SOURCE_CHAT", "")
TARGET_CHAT = os.environ.get("TG_FORWARDER_TARGET_CHAT", "")


def log(*a) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


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


async def cmd_login() -> None:
    c = client()
    # Interactive: phone + login code prompts. A configured phone skips one.
    await c.start(**({"phone": PHONE} if PHONE else {}))
    async with c:
        me = await c.get_me()
        log(f"logged in as {me.first_name} (@{me.username}) — session saved to {SESSION}.session")


async def cmd_chats() -> None:
    async with client() as c:
        async for d in c.iter_dialogs():
            kind = "channel" if d.is_channel else "group" if d.is_group else "user"
            print(f"{d.id:>15}  {kind:<7}  {d.name}")


async def cmd_resolve() -> None:
    """Dry run of `run`'s lookup step — no messages are relayed."""
    c = client()
    await c.start()
    async with c:
        await _resolve_all(c, SOURCE_CHAT, "source")
        await _resolve_all(c, TARGET_CHAT, "target")


async def cmd_run() -> None:
    if not SOURCE_CHAT or not TARGET_CHAT:
        print("TG_FORWARDER_SOURCE_CHAT and TG_FORWARDER_TARGET_CHAT are required", file=sys.stderr)
        sys.exit(2)
    c = client()
    await c.start()  # errors out (rather than prompting) if not logged in

    sources = await _resolve_all(c, SOURCE_CHAT, "source")
    targets = await _resolve_all(c, TARGET_CHAT, "target")
    if not sources or not targets:
        print("nothing to relay: source or target did not resolve", file=sys.stderr)
        await c.disconnect()
        sys.exit(1)
    if len(targets) > 1:
        print("TG_FORWARDER_TARGET_CHAT must name exactly one group", file=sys.stderr)
        await c.disconnect()
        sys.exit(2)
    target = targets[0][0]

    names_by_id = {cid: name for cid, name in sources}

    @c.on(events.NewMessage(chats=[cid for cid, _ in sources]))
    async def _on_message(event) -> None:
        text = event.message.message or ""
        if not text.strip():
            return  # media-only posts carry nothing the parser can use
        # Tag with the source channel's title so telegram-ingest can label
        # the trade comment (and notifications) with where it came from —
        # the bot otherwise only ever sees this relay chat, never the
        # source channel itself.
        name = names_by_id.get(event.chat_id, "")
        tagged = f"[SRC:{name}]\n{text}" if name else text
        await c.send_message(target, tagged)
        log(f"relayed message {event.message.id}: {text[:60].replace(chr(10), ' / ')}")

    names = ", ".join(f"{name} ({cid})" for cid, name in sources)
    log(f"watching {names}, relaying text posts to {targets[0][1]} — Ctrl+C to stop")
    await c.run_until_disconnected()


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    commands = {"login": cmd_login, "chats": cmd_chats, "resolve": cmd_resolve, "run": cmd_run}
    if cmd not in commands:
        print(f"usage: telegram_user_forwarder.py {'|'.join(commands)}", file=sys.stderr)
        sys.exit(2)
    asyncio.run(commands[cmd]())


if __name__ == "__main__":
    main()
