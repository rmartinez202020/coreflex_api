# routers/device_counters_tick.py
import os
import asyncio
from contextlib import suppress
from typing import Optional, Dict

from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine, text
from datetime import datetime, timezone

from utils.zhc1921_live_cache import get_latest as get_latest_zhc1921

# Try to reuse your project's engine if available
try:
    from database import engine  # your project's engine
except Exception:
    engine = None


# ----------------------------
# ✅ Settings
# ----------------------------
DEFAULT_INTERVAL_SEC = float(
    os.getenv("CF_COUNTER_TICK_SEC", "2.0")
)  # 2 seconds default

# Maximum age allowed for Redis telemetry before we treat it as stale/offline.
# Defaults to the same offline threshold used by the rest of the platform.
LIVE_MAX_AGE_SEC = float(
    os.getenv(
        "CF_COUNTER_LIVE_MAX_AGE_SEC",
        os.getenv("COREFLEX_OFFLINE_AFTER_SECONDS", "10"),
    )
)


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
# ✅ Field normalization (DI + DO)
# ----------------------------
def _normalize_counter_field(field: str) -> Optional[str]:
    """
    Accepts:
      - di1..di6
      - in1..in6 (legacy -> di1..di6)
      - do1..do4

    Returns normalized field name or None.
    """
    if not field:
        return None

    f = str(field).strip().lower()

    # allow legacy in1..in6 mapping
    if f.startswith("in") and len(f) == 3 and f[2].isdigit():
        f = "di" + f[2]

    if f in (
        "di1",
        "di2",
        "di3",
        "di4",
        "di5",
        "di6",
        "do1",
        "do2",
        "do3",
        "do4",
    ):
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
# ✅ Redis telemetry helpers
# ----------------------------
def _parse_datetime(value) -> Optional[datetime]:
    if not value:
        return None

    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def _redis_snapshot_is_fresh(snapshot: Dict) -> bool:
    """
    True only when the Redis snapshot has a recent last_seen timestamp.

    This prevents a stale cached ON value from continuing to accumulate
    running hours after a device has stopped reporting.
    """
    if not snapshot:
        return False

    last_seen = _parse_datetime(snapshot.get("last_seen"))
    if last_seen is None:
        return False

    age = (datetime.now(timezone.utc) - last_seen).total_seconds()

    return 0 <= age <= LIVE_MAX_AGE_SEC


# ----------------------------
# ✅ Tick one pass (Redis live telemetry)
# ----------------------------
def _tick_once(db, interval_sec: float) -> int:
    """
    Processes enabled counters.

    Source of truth for current DI/DO state:
      Redis key zhc1921:live:<device_id>

    Counter behavior:
      - Rising edge 0 -> 1 increments count by 1.
      - run_seconds increases only while selected DI/DO is active (1).
      - Missing/stale Redis telemetry freezes the counter.
      - A rising edge itself does not add elapsed OFF time to run_seconds.

    Returns number of updated counter rows.
    """

    # Safety clamp:
    # - if server sleeps, we don't want huge jumps
    # - allow up to max(5, ~3 ticks worth)
    max_delta = max(5, int(float(interval_sec) * 3))

    # 1) Load enabled counters
    q = """
    SELECT id, user_id, device_id, field, count, prev01, run_seconds, last_tick_at
    FROM public.device_counters
    WHERE enabled = TRUE
    ORDER BY updated_at ASC
    """

    counters = db.execute(text(q)).mappings().all()

    if not counters:
        return 0

    # 2) Normalize configured fields and collect unique device IDs
    norm_field_by_counter: Dict[str, str] = {}
    device_ids = set()

    for c in counters:
        device_id = str(c.get("device_id") or "").strip()
        counter_field = _normalize_counter_field(
            str(c.get("field") or "").strip()
        )

        if not device_id or not counter_field:
            continue

        norm_field_by_counter[str(c["id"])] = counter_field
        device_ids.add(device_id)

    if not device_ids:
        return 0

    # 3) Read each device snapshot once from Redis for this tick
    live_by_device: Dict[str, Dict] = {}

    for device_id in device_ids:
        try:
            snapshot = get_latest_zhc1921(device_id) or {}
        except Exception as exc:
            print(
                f"❌ device_counters_tick Redis read failed "
                f"device_id={device_id}: {repr(exc)}"
            )
            snapshot = {}

        if not snapshot:
            continue

        if not _redis_snapshot_is_fresh(snapshot):
            continue

        live_by_device[device_id] = snapshot

    # 4) Apply rising-edge logic + running timer accumulation
    updates = 0
    now = datetime.now(timezone.utc)

    for c in counters:
        cid = str(c["id"])
        device_id = str(c.get("device_id") or "").strip()

        counter_field = norm_field_by_counter.get(cid)
        if not counter_field:
            continue

        live = live_by_device.get(device_id)

        # Missing/stale Redis telemetry:
        # freeze count + timer; do not fall back to database values.
        if not live:
            continue

        cur01 = _to01(live.get(counter_field))
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

            db.execute(
                text(init_q),
                {
                    "id": c["id"],
                    "prev01": cur01,
                },
            )

            updates += 1
            continue

        # Ensure tz-aware for safe subtraction
        try:
            if getattr(last_tick_at, "tzinfo", None) is None:
                last_tick_at = last_tick_at.replace(tzinfo=timezone.utc)
            else:
                last_tick_at = last_tick_at.astimezone(timezone.utc)
        except Exception:
            pass

        # Compute delta seconds since last successful tick
        try:
            delta = int((now - last_tick_at).total_seconds())
        except Exception:
            delta = 0

        delta = _clamp_int(delta, 0, max_delta)

        # Rising edge count 0 -> 1
        is_rising_edge = prev01 == 0 and cur01 == 1

        new_count = old_count

        if is_rising_edge:
            new_count = old_count + 1

        # ✅ Running timer:
        # Accumulate ONLY while the selected DI/DO is active.
        #
        # IMPORTANT:
        # On the exact 0 -> 1 transition we do NOT add delta because the
        # elapsed period before that transition belonged to the OFF state.
        if cur01 == 1 and not is_rising_edge and delta > 0:
            run_seconds += delta

        # We update whenever:
        # - pulse count changed
        # - selected signal changed state
        # - active signal accumulated runtime
        need_update = (
            (new_count != old_count)
            or (prev01 != cur01)
            or (cur01 == 1 and not is_rising_edge and delta > 0)
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

    print(
        "✅ device_counters_tick started "
        f"(interval={interval_sec}s, live_max_age={LIVE_MAX_AGE_SEC}s)"
    )


async def stop_device_counters_tick():
    global _task

    if not _task:
        return

    _task.cancel()

    with suppress(asyncio.CancelledError):
        await _task

    _task = None

    print("✅ device_counters_tick stopped")
