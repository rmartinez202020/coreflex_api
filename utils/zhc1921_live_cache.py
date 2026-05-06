# utils/zhc1921_live_cache.py
from __future__ import annotations

import json
from dataclasses import dataclass
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


def set_latest(device_id: str, payload: Dict[str, Any]) -> None:
    """Upsert latest ZHC1921 snapshot into Redis."""
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

    merged = {
        **existing,
        **cleaned,
        "device_id": device_id,
    }

    redis_client.set(
        _redis_key(device_id),
        json.dumps(merged, default=str),
    )


def get_latest(device_id: str) -> Optional[Dict[str, Any]]:
    """Read latest ZHC1921 snapshot from Redis."""
    device_id = str(device_id or "").strip()
    if not device_id:
        return None

    raw = redis_client.get(_redis_key(device_id))
    if not raw:
        return None

    try:
        return json.loads(raw)
    except Exception:
        return None