-- Telegram notification linking (PineConnector-style).
-- One row per user. The portal issues a short-lived link_token; the user opens
-- the bot deep link (t.me/<bot>?start=<token>) and the tasks service resolves
-- the /start <token> update into chat_id, completing the link.

CREATE TABLE IF NOT EXISTS telegram_links (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    chat_id BIGINT,
    link_token TEXT NOT NULL UNIQUE,
    token_expires_at TIMESTAMPTZ NOT NULL,
    linked_at TIMESTAMPTZ,
    notify_fills BOOLEAN NOT NULL DEFAULT true,
    notify_timeouts BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A Telegram chat may be linked to at most one user.
CREATE UNIQUE INDEX IF NOT EXISTS idx_telegram_links_chat_id
    ON telegram_links (chat_id)
    WHERE chat_id IS NOT NULL;

-- The fill notifier de-dups against notifications_log by fill id; give that
-- lookup an index so the scan stays cheap as the log grows.
CREATE INDEX IF NOT EXISTS idx_notifications_log_telegram_fill
    ON notifications_log ((payload->>'fill_id'))
    WHERE channel = 'telegram';
