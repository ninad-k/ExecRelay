"""Shared daily-rotating transaction log for local demo/dev testing.

Used by ea_shim.py and telegram-ingest to keep a JSON-lines audit trail of
every signal and order that passed through them, independent of the
per-service debug logs in .local-stack/logs/*.log. One physical file per
logger name under .local-stack/logs/transactions/, rotated at UTC midnight;
files older than RETENTION_DAYS are deleted, both via the stdlib handler's
own rollover pruning and an explicit purge on startup (so a file that's
stale because the process was simply off for a while is still cleaned up
the next time it starts, not just on the next live rollover).

Not used by the production bridge/EA path — this is dev-harness tooling
(ea_shim.py, telegram-ingest) only.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / ".local-stack" / "logs" / "transactions"
RETENTION_DAYS = 7


def _purge_old(name: str) -> None:
    """Delete rotated files for this logger older than RETENTION_DAYS.

    Belt-and-suspenders alongside the handler's backupCount: that pruning
    only runs when a rollover actually fires, which never happens if the
    process has simply been off across one or more midnights.
    """
    cutoff = time.time() - RETENTION_DAYS * 86400
    for f in LOG_DIR.glob(f"{name}.log.*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass  # another process may be mid-rotation; skip, not fatal


class _SafeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """TimedRotatingFileHandler that tolerates a locked log file at rollover.

    On Windows, os.rename() during rollover fails with PermissionError if
    another process (e.g. a second, un-shut-down instance of the same
    service) has the file open. The stdlib handler lets that exception
    propagate out of emit() -- which drops the very record that triggered
    the rollover -- and only advances rolloverAt on a successful rename, so
    once a rollover fails it fails again on every subsequent call, silently
    dropping every record from then on instead of just missing one day's
    rotation.
    """

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except OSError:
            if self.stream is None:
                self.stream = self._open()
            self.rolloverAt = self.computeRollover(time.time())


def get_txn_logger(name: str) -> logging.Logger:
    """Return a JSON-lines logger writing to transactions/<name>.log,
    rotated daily at UTC midnight, keeping RETENTION_DAYS days of history."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _purge_old(name)

    logger = logging.getLogger(f"txn.{name}")
    if logger.handlers:
        return logger  # already configured (e.g. re-imported)

    handler = _SafeTimedRotatingFileHandler(
        LOG_DIR / f"{name}.log",
        when="midnight",
        interval=1,
        backupCount=RETENTION_DAYS,
        encoding="utf-8",
        utc=True,
    )
    handler.suffix = "%Y-%m-%d"
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def log_txn(logger: logging.Logger, **fields) -> None:
    record = {"ts": datetime.now(timezone.utc).isoformat(), **fields}
    logger.info(json.dumps(record, default=str))
