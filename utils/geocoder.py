# utils/geocoder.py
import asyncio
from typing import Optional, Tuple, Dict, Any

import httpx

# We will use OpenStreetMap Nominatim (free) for now.
# IMPORTANT: In production, you should eventually move to a paid geocoder (Google/Mapbox)
# if you need higher reliability and guaranteed SLA.

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


def build_address_string(
    street: str,
    city: str,
    state: str,
    zip_code: str,
    country: str = "United States",
) -> str:
    parts = [street, city, state, zip_code, country]
    parts = [p.strip() for p in parts if p and str(p).strip()]
    return ", ".join(parts)


async def geocode_address_async(
    address: str,
    *,
    timeout_seconds: float = 10.0,
) -> Tuple[Optional[float], Optional[float], str, Optional[str]]:
    """
    Returns:
      (lat, lng, status, raw_display_name)

    status values (string):
      - "ok"
      - "not_found"
      - "blocked_or_rate_limited"
      - "timeout"
      - "error"
    """

    if not address or not address.strip():
        return None, None, "not_found", None

    params = {
        "format": "json",
        "limit": 1,
        "q": address,
    }

    # Nominatim expects a valid User-Agent identifying your app.
    # Don't put secrets here. Keep it simple.
    headers = {
        "User-Agent": "CoreFlex-IIoT/1.0 (support@coreflexiiotsplatform.com)",
        "Accept": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, headers=headers) as client:
            res = await client.get(NOMINATIM_URL, params=params)

        # Common rate limit / block responses:
        # 429 Too Many Requests, 403 Forbidden
        if res.status_code in (403, 429):
            return None, None, "blocked_or_rate_limited", None

        if not (200 <= res.status_code < 300):
            return None, None, "error", None

        data = res.json()
        if not isinstance(data, list) or len(data) == 0:
            return None, None, "not_found", None

        first: Dict[str, Any] = data[0]
        lat_raw = first.get("lat")
        lon_raw = first.get("lon")
        display_name = first.get("display_name")

        if lat_raw is None or lon_raw is None:
            return None, None, "not_found", display_name

        lat = float(lat_raw)
        lng = float(lon_raw)

        # Basic sanity check (optional)
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
            return None, None, "error", display_name

        return lat, lng, "ok", display_name

    except httpx.TimeoutException:
        return None, None, "timeout", None
    except Exception:
        return None, None, "error", None


def geocode_address(
    address: str,
    *,
    timeout_seconds: float = 10.0,
) -> Tuple[Optional[float], Optional[float], str, Optional[str]]:
    """
    Sync wrapper so routers can call it without needing to be async.
    """
    return asyncio.run(geocode_address_async(address, timeout_seconds=timeout_seconds))
