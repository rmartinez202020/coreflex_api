# utils/zhc1921_live_cache.py
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from utils.redis_client import redis_client


REDIS_PREFIX = "zhc1921:live:"


@dataclass
class Zhc1921Live:
    device_id: str
    last_seen: Optional[str] = None
    status: str = "offline"

    di1: int = 0
    di2: int = 0
    di3: int = 0
    di4: int = 0
    di5: int = 0
    di6: int = 0

    do1: int = 0
    do2: int = 0
    do3: int = 0
    do4: int = 0

    ai1: Any = None
    ai2: Any = None
    ai3: Any = None
    ai4: Any = None


def _redis_key(device_id: str) -> str:
    return f"{REDIS_PREFIX}{device_id}"


def _normalize_last_seen(value: Any) -> Optional[str]:
    """
    Normalize last_seen before writing it to Redis.

    Redis stores JSON, so timestamps are stored as ISO-8601 strings.
    This accepts either a datetime or an existing timestamp string.
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat()

    if isinstance(value, str):
        value = value.strip()
        return value or None

    return str(value)


def set_latest(device_id: str, payload: Dict[str, Any]) -> None:
    """Upsert the latest ZHC1921 telemetry snapshot into Redis."""
    device_id = str(device_id or "").strip()
    if not device_id:
        return

    existing = get_latest(device_id) or {}

    allowed = set(Zhc1921Live.__dataclass_fields__.keys())

    cleaned = {
        k: v
        for k, v in payload.items()
        if k in allowed
    }

    # Keep timestamp representation predictable in Redis.
    if "last_seen" in cleaned:
        cleaned["last_seen"] = _normalize_last_seen(cleaned["last_seen"])

    # Normalize status to the values expected by the rest of the backend.
    if "status" in cleaned:
        status = str(cleaned["status"] or "").strip().lower()
        cleaned["status"] = status if status in {"online", "offline"} else "offline"

    merged = {
        **existing,
        **cleaned,
        "device_id": device_id,
    }

    redis_client.set(
        _redis_key(device_id),
        json.dumps(merged),
    )


def get_latest(device_id: str) -> Optional[Dict[str, Any]]:
    """Read the latest ZHC1921 telemetry snapshot from Redis."""
    device_id = str(device_id or "").strip()
    if not device_id:
        return None

    raw = redis_client.get(_redis_key(device_id))
    if not raw:
        return None

    try:
        data = json.loads(raw)

        if not isinstance(data, dict):
            return None

        return data
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
