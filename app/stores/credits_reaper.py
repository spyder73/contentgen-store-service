"""The stale-hold reaper (clipstyle render Phase 4, "before a second user"): releases open holds of
listed kinds older than their rule's age. A hold whose flow was cut off by a backend restart (a LoRA
training job nobody polls again, a builder classify whose request died) would otherwise keep its
credits reserved forever. Each release goes through credits.release() with a deterministic
idempotency key, so a hold is reaped at most once, and a hold closed meanwhile is left alone."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.stores import credits

logger = logging.getLogger(__name__)

MAX_REAP = 500

_STALE_SQL = text(
    """
    SELECT h.user_id, h.pipeline_run_id, h.checkpoint_id, h.attempt, SUM(h.delta) AS amount
      FROM credits_ledger h
     WHERE h.kind = 'hold'
       AND h.created_at < :cutoff
       AND h.pipeline_run_id IS NOT NULL
       AND (CASE WHEN :prefix THEN substr(h.checkpoint_id, 1, :plen) = :pattern ELSE h.checkpoint_id = :checkpoint END)
       AND NOT EXISTS (SELECT 1 FROM credits_ledger c
                        WHERE c.user_id = h.user_id AND c.pipeline_run_id = h.pipeline_run_id
                          AND c.kind IN ('debit', 'release') AND c.checkpoint_id = h.checkpoint_id AND c.attempt = h.attempt)
       AND NOT EXISTS (SELECT 1 FROM credits_ledger l
                        WHERE l.user_id = h.user_id AND l.pipeline_run_id = h.pipeline_run_id
                          AND l.kind = 'release' AND l.checkpoint_id IS NULL AND l.created_at >= h.created_at)
     GROUP BY h.user_id, h.pipeline_run_id, h.checkpoint_id, h.attempt
     ORDER BY MIN(h.created_at)
     LIMIT :limit
    """
    # PF4-9: bind the cutoff as a tz-aware DateTime, so the store's Python 3.13 venv (aiosqlite)
    # never emits "the default datetime adapter is deprecated" for this query.
).bindparams(bindparam("cutoff", type_=DateTime(timezone=True)))


async def reap_stale_holds(session: AsyncSession, rules: list[dict], *, now: datetime, limit: int = MAX_REAP) -> list[dict[str, Any]]:
    reaped: list[dict[str, Any]] = []
    for rule in rules:
        if len(reaped) >= limit:
            break
        prefix = rule["match"] == "prefix"
        params = {"cutoff": now - timedelta(seconds=int(rule["older_than_s"])), "prefix": prefix,
                  "pattern": rule["checkpoint"] + ":", "plen": len(rule["checkpoint"]) + 1,  # M11: a literal prefix, no LIKE wildcards
                  "checkpoint": rule["checkpoint"], "limit": limit - len(reaped)}
        stale = (await session.execute(_STALE_SQL, params)).all()
        for user_id, run_id, checkpoint_id, attempt, amount in stale:
            try:
                out = await credits.release(session, user_id=str(user_id), pipeline_run_id=str(run_id), reason="stale_hold_reaped",
                                            idempotency_key=f"reap:{run_id}:{checkpoint_id}:{attempt}",
                                            checkpoint_id=checkpoint_id, attempt=int(attempt))
            except Exception:
                # PF4-15: one bad hold (a stale or failed release) never aborts the rest of the pass.
                # Roll back any partial state the failed release left on the shared session before
                # moving on, so the next hold's release starts clean.
                await session.rollback()
                logger.warning(
                    "stale-hold reaper: could not release hold run=%s checkpoint=%s attempt=%s",
                    run_id, checkpoint_id, attempt, exc_info=True,
                )
                continue
            if out.get("status") == "released":
                reaped.append({"user_id": str(user_id), "pipeline_run_id": str(run_id), "checkpoint_id": checkpoint_id,
                               "attempt": int(attempt), "credits": int(amount)})
    return reaped
