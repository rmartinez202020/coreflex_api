# utils/zhc1661_live_cache.py
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

from utils.redis_client import redis_client


REDIS_PREFIX = "zhc1661:live:"


@dataclass
class Zhc1661Live:
    device_id: str
    last_seen: Optional[str] = None
    status: str = "offline"

    ai1: Any = None
    ai2: Any = None
    ai3: Any = None
    ai4: Any = None

    ao1: Any = None
    ao2: Any = None


def _redis_key(device_id: str) -> str:
    return f"{REDIS_PREFIX}{device_id}"


def set_latest(device_id: str, payload: Dict[str, Any]) -> None:
    """Upsert latest snapshot for device_id into Redis."""
    device_id = str(device_id or "").strip()
    if not device_id:
        return

    existing = get_latest(device_id) or {}

    allowed = set(Zhc1661Live.__dataclass_fields__.keys())

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
    """Read latest snapshot for device_id from Redis."""
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