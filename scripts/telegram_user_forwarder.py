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

Environment:
  TG_FORWARDER_API_ID / TG_FORWARDER_API_HASH   my.telegram.org credentials
  TG_FORWARDER_SESSION       session file path (default .local-stack/tg-forwarder)
  TG_FORWARDER_SOURCE_CHAT   numeric id of the channel to watch
  TG_FORWARDER_TARGET_CHAT   numeric id of the relay group the bot reads
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


async def cmd_login() -> None:
    async with client() as c:  # interactive: phone + login code prompts
        me = await c.get_me()
        log(f"logged in as {me.first_name} (@{me.username}) — session saved to {SESSION}.session")


async def cmd_chats() -> None:
    async with client() as c:
        async for d in c.iter_dialogs():
            kind = "channel" if d.is_channel else "group" if d.is_group else "user"
            print(f"{d.id:>15}  {kind:<7}  {d.name}")


async def cmd_run() -> None:
    if not SOURCE_CHAT or not TARGET_CHAT:
        print("TG_FORWARDER_SOURCE_CHAT and TG_FORWARDER_TARGET_CHAT are required", file=sys.stderr)
        sys.exit(2)
    source = int(SOURCE_CHAT)
    target = int(TARGET_CHAT)
    c = client()
    await c.start()  # errors out (rather than prompting) if not logged in

    @c.on(events.NewMessage(chats=source))
    async def _on_message(event) -> None:
        text = event.message.message or ""
        if not text.strip():
            return  # media-only posts carry nothing the parser can use
        await c.send_message(target, text)
        log(f"relayed message {event.message.id}: {text[:60].replace(chr(10), ' / ')}")

    log(f"watching chat {source}, relaying text posts to {target} — Ctrl+C to stop")
    await c.run_until_disconnected()


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd not in ("login", "chats", "run"):
        print("usage: telegram_user_forwarder.py login|chats|run", file=sys.stderr)
        sys.exit(2)
    asyncio.run({"login": cmd_login, "chats": cmd_chats, "run": cmd_run}[cmd]())


if __name__ == "__main__":
    main()
