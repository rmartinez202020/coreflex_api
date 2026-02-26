# utils/zhc1921_live_cache.py
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from threading import RLock
from typing import Any, Dict, Optional


@dataclass
class Zhc1921Live:
    device_id: str
    last_seen: Optional[datetime] = None
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


_LOCK = RLock()
_LATEST: Dict[str, Zhc1921Live] = {}


def set_latest(device_id: str, payload: Dict[str, Any]) -> None:
    """Upsert latest snapshot for device_id."""
    device_id = str(device_id or "").strip()
    if not device_id:
        return

    with _LOCK:
        cur = _LATEST.get(device_id)
        if cur is None:
            cur = Zhc1921Live(device_id=device_id)
            _LATEST[device_id] = cur

        # only update keys we know
        for k, v in payload.items():
            if hasattr(cur, k):
                setattr(cur, k, v)


def get_latest(device_id: str) -> Optional[Dict[str, Any]]:
    device_id = str(device_id or "").strip()
    if not device_id:
        return None
    with _LOCK:
        v = _LATEST.get(device_id)
        return asdict(v) if v else None