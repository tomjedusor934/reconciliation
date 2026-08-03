"""
Mutual exclusion across processes via Postgres advisory locks.

Use these to make a batch single-writer, so that two concurrent runs of the same
job cannot interleave their writes.

Usage:
    from app.db.locks import LockKey, try_advisory_lock

    with try_advisory_lock(LockKey.RECONCILIATION_ENTRY_WRITER) as acquired:
        if not acquired:
            raise SomethingAlreadyRunning(...)
        ...
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from enum import IntEnum
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from app.core.config import settings

logger = logging.getLogger(__name__)

# classkey shared by every advisory lock of this application. Advisory locks are
# global to the *database* (not to a schema), so this only has to be unique with
# respect to other applications sharing the same database — today, none.
# Must fit in an int32.
ADVISORY_LOCK_CLASS = 42_001


class LockKey(IntEnum):
    """objkey of each named lock.

    Never renumber a value: during a rolling deploy the old and the new backend
    would take two different locks and stop excluding each other — precisely the
    window these locks exist to close.
    """

    # Serialises every bulk writer of reco.reconciliation_entry (run_auto, the
    # émargement sweep). They scan the table through different indexes, hence in
    # different orders, and deadlock against each other when they overlap.
    RECONCILIATION_ENTRY_WRITER = 1


_lock_engine = None


def _get_lock_engine():
    """Return a lazy singleton engine dedicated to advisory locks.

    NullPool and AUTOCOMMIT are both load-bearing:

    * NullPool → ``conn.close()`` closes the libpq connection *physically*, which
      ends the Postgres session and drops the lock no matter what. A pooled
      connection would go back to the pool still holding the lock if the unlock
      ever failed (a session-level advisory lock survives the ROLLBACK the pool
      emits on return), blocking every subsequent run for good. It is the end of
      the Postgres session — not the unlock — that makes a stuck lock impossible.
    * AUTOCOMMIT → under SQLAlchemy 1.4 legacy a SELECT opens a transaction that
      is never closed; the connection would sit "idle in transaction" for the
      whole block (run_auto runs for minutes), pinning a snapshot and blocking
      VACUUM on reconciliation_entry, the most heavily written table we have.
    """
    global _lock_engine
    if _lock_engine is None:
        _lock_engine = create_engine(
            settings.DATABASE_URL,
            poolclass=NullPool,
            isolation_level="AUTOCOMMIT",
        )
    return _lock_engine


@contextmanager
def try_advisory_lock(key: LockKey) -> Iterator[bool]:
    """Try to hold ``key`` for the duration of the block; yield whether we got it.

    Never blocks: yields False straight away rather than waiting, and lets the
    caller decide what that means.

    The lock is held on a DEDICATED connection, never on a Session's. A
    session-level advisory lock belongs to the Postgres *connection*, and a
    Session returns its connection to the pool on every commit(), checking out a
    possibly different one for the next statement. A caller that commits inside
    the block (run_auto commits once per group) would take the lock on C1 and
    unlock on C2 — leaking it. pg_advisory_xact_lock() is no answer either: the
    first commit() drops it, and there are three per group.
    """
    conn = _get_lock_engine().connect()
    acquired = False
    try:
        acquired = bool(
            conn.execute(
                text("SELECT pg_try_advisory_lock(:cls, :key)"),
                {"cls": ADVISORY_LOCK_CLASS, "key": int(key)},
            ).scalar()
        )
        yield acquired
    finally:
        try:
            # Guarded by `acquired`: unlocking a lock we do not hold returns
            # false and logs a server-side WARNING for nothing.
            if acquired:
                conn.execute(
                    text("SELECT pg_advisory_unlock(:cls, :key)"),
                    {"cls": ADVISORY_LOCK_CLASS, "key": int(key)},
                )
        except Exception:
            # Best effort: close() below ends the Postgres session, which drops
            # the lock anyway. Never let the unlock mask the exception on its way
            # out of the block.
            logger.warning("pg_advisory_unlock(%s) failed", key.name, exc_info=True)
        finally:
            conn.close()  # NullPool → real close → Postgres releases the lock
