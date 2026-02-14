# routers/device_counters_tick.py
import os
import asyncio
from contextlib import suppress
from typing import Optional, Dict, Tuple, List

from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine, text
from datetime import datetime, timezone

# Try to reuse your project's engine if available
try:
    from database import engine  # your project's engine
except Exception:
    engine = None


# ----------------------------
# ✅ Settings
# ----------------------------
DEFAULT_INTERVAL_SEC = float(os.getenv("CF_COUNTER_TICK_SEC", "2.0"))  # 2 seconds default


# ----------------------------
# ✅ Local session factory (safe for background task)
# ----------------------------
def _make_session_local():
    """
    Creates a sessionmaker for background tasks.
    Uses project's engine if available; otherwise tries DATABASE_URL.
    """
    global engine

    if engine is None:
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            raise RuntimeError("DATABASE_URL not set and database.engine not importable")
        engine = create_engine(db_url, pool_pre_ping=True)

    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


SessionLocal = _make_session_local()


# ----------------------------
# ✅ Field normalization (DI)
# ----------------------------
def _normalize_di_field(field: str) -> Optional[str]:
    """
    Accepts: di1..di6 OR in1..in6 (legacy)
    Returns: di1..di6 or None
    """
    if not field:
        return None

    f = str(field).strip().lower()

    # allow in1..in6 mapping
    if f.startswith("in") and len(f) == 3 and f[2].isdigit():
        f = "di" + f[2]

    if f in ("di1", "di2", "di3", "di4", "di5", "di6"):
        return f

    return None


def _to01(v) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return 1 if v > 0 else 0
    s = str(v).strip().lower()
    if s in ("1", "true", "on", "yes"):
        return 1
    if s in ("0", "false", "off", "no"):
        return 0
    try:
        n = float(s)
        return 1 if n > 0 else 0
    except Exception:
        return 1 if v else 0


def _clamp_int(v, lo=0, hi=10) -> int:
    try:
        n = int(v)
    except Exception:
        n = 0
    if n < lo:
        return lo
    if n > hi:
        return hi
    return n


# ----------------------------
# ✅ Bulk device read (tenant safe)
# NOTE: Only ZHC1921 has di1..di6 in your schema.
# ----------------------------
def _bulk_read_zhc1921_rows(db, pairs_list: List[Tuple[int, str]]):
    """
    pairs_list: [(user_id, device_id), ...]
    Returns: list of rows with user_id, device_id, di1..di6
    """
    if not pairs_list:
        return []

    values_sql_parts = []
    params = {}
    for i, (uid, did) in enumerate(pairs_list):
        values_sql_parts.append(f"(:u{i}, :d{i})")
        params[f"u{i}"] = int(uid)
        params[f"d{i}"] = str(did).strip()

    values_sql = ", ".join(values_sql_parts)

    q = f"""
    WITH want(user_id, device_id) AS (
      VALUES {values_sql}
    )
    SELECT z.claimed_by_user_id AS user_id,
           z.device_id,
           z.di1, z.di2, z.di3, z.di4, z.di5, z.di6
    FROM public.zhc1921_devices z
    JOIN want w
      ON w.user_id = z.claimed_by_user_id
     AND w.device_id = z.device_id
    """
    return db.execute(text(q), params).mappings().all()


# ----------------------------
# ✅ Tick one pass (fast)
# ----------------------------
def _tick_once(db, interval_sec: float) -> int:
    """
    Processes enabled counters.
    Returns number of updated counter rows.
    """
    # Safety clamp:
    # - if server sleeps, we don't want huge jumps
    # - allow up to max(5, ~3 ticks worth)
    max_delta = max(5, int(float(interval_sec) * 3))

    # 1) Load enabled counters (include timer fields)
    q = """
    SELECT id, user_id, device_id, field, count, prev01, run_seconds, last_tick_at
    FROM public.device_counters
    WHERE enabled = TRUE
    ORDER BY updated_at ASC
    """
    counters = db.execute(text(q)).mappings().all()
    if not counters:
        return 0

    # 2) Build unique (user_id, device_id) pairs + normalized DI field per counter
    pairs: set[Tuple[int, str]] = set()
    norm_field_by_counter: Dict[str, str] = {}

    for c in counters:
        device_id = (c["device_id"] or "").strip()
        di_field = _normalize_di_field((c["field"] or "").strip())
        if not device_id or not di_field:
            continue

        pairs.add((int(c["user_id"]), device_id))
        norm_field_by_counter[str(c["id"])] = di_field

    if not pairs:
        return 0

    # 3) Bulk read device rows (tenant safe) — ZHC1921 only
    rows = _bulk_read_zhc1921_rows(db, list(pairs))

    live: Dict[Tuple[int, str], Dict] = {}
    for r in rows:
        key = (int(r["user_id"]), str(r["device_id"]).strip())
        live[key] = r

    # 4) Apply rising-edge logic + running timer accumulation
    updates = 0
    now = datetime.now(timezone.utc)

    for c in counters:
        cid = str(c["id"])
        user_id = int(c["user_id"])
        device_id = (c["device_id"] or "").strip()

        di_field = norm_field_by_counter.get(cid)
        if not di_field:
            continue

        dev = live.get((user_id, device_id))
        if not dev:
            continue

        cur01 = _to01(dev.get(di_field))
        if cur01 is None:
            continue

        prev01 = int(c.get("prev01") or 0)
        old_count = int(c.get("count") or 0)
        run_seconds = int(c.get("run_seconds") or 0)
        last_tick_at = c.get("last_tick_at")

        # Initialize last_tick_at on first run (no time added)
        if last_tick_at is None:
            init_q = """
            UPDATE public.device_counters
            SET last_tick_at = NOW(),
                prev01 = :prev01,
                updated_at = NOW()
            WHERE id = :id
            """
            db.execute(text(init_q), {"id": c["id"], "prev01": cur01})
            updates += 1
            continue

        # Ensure tz-aware for safe subtraction
        try:
            if getattr(last_tick_at, "tzinfo", None) is None:
                last_tick_at = last_tick_at.replace(tzinfo=timezone.utc)
        except Exception:
            pass

        # Compute delta seconds since last tick
        try:
            delta = int((now - last_tick_at).total_seconds())
        except Exception:
            delta = 0

        delta = _clamp_int(delta, 0, max_delta)

        # Rising edge count 0 -> 1
        new_count = old_count
        if prev01 == 0 and cur01 == 1:
            new_count = old_count + 1

        # ✅ Timer: accumulate only while input is active
        if cur01 == 1 and delta > 0:
            run_seconds += delta

        need_update = (
            (new_count != old_count)
            or (prev01 != cur01)
            or (cur01 == 1 and delta > 0)
        )

        if need_update:
            upd = """
            UPDATE public.device_counters
            SET count = :count,
                prev01 = :prev01,
                run_seconds = :run_seconds,
                last_tick_at = NOW(),
                updated_at = NOW()
            WHERE id = :id
            """
            db.execute(
                text(upd),
                {
                    "id": c["id"],
                    "count": new_count,
                    "prev01": cur01,
                    "run_seconds": run_seconds,
                },
            )
            updates += 1

    if updates:
        db.commit()

    return updates


# ----------------------------
# ✅ Background task manager
# ----------------------------
_task: Optional[asyncio.Task] = None


async def _runner(interval_sec: float):
    while True:
        try:
            db = SessionLocal()
            try:
                _tick_once(db, interval_sec)
            finally:
                db.close()
        except Exception as e:
            print("❌ device_counters_tick error:", repr(e))

        await asyncio.sleep(interval_sec)


def start_device_counters_tick(interval_sec: float = DEFAULT_INTERVAL_SEC):
    """
    Call on FastAPI startup to begin background polling.
    """
    global _task
    if _task and not _task.done():
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()

    _task = loop.create_task(_runner(float(interval_sec)))
    print(f"✅ device_counters_tick started (interval={interval_sec}s)")


async def stop_device_counters_tick():
    global _task
    if not _task:
        return

    _task.cancel()
    with suppress(asyncio.CancelledError):
        await _task
    _task = None
    print("✅ device_counters_tick stopped")
