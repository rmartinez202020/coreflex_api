# routers/node_red_graphics.py
from fastapi import APIRouter, Depends
import os
import requests

# ✅ If you want auth on the optional test endpoint
from auth_utils import get_current_user
from models import User

router = APIRouter(prefix="/node-red", tags=["Node-RED Graphics"])

NODE_RED_BASE_URL = (os.getenv("NODE_RED_BASE_URL") or "").rstrip("/")
NODE_RED_KEY = (os.getenv("NODE_RED_COMMAND_KEY") or "").strip()


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if NODE_RED_KEY:
        headers["x-coreflex-key"] = NODE_RED_KEY  # ✅ required by node-red
    return headers


# =========================================================
# ✅ Helper: Start/Update stream on Node-RED
# Call this from your "Apply graphic binding" route AFTER db.commit()
# =========================================================
def start_graphic_stream(
    *,
    user_id: int,
    dash_id: str,
    widget_id: str,
    device_id: str,
    field: str,
    sample_ms: int,
    # ✅ NEW: pass math formula from backend -> node-red
    math_formula: str = "",
) -> bool:
    if not NODE_RED_BASE_URL:
        return False

    url = f"{NODE_RED_BASE_URL}/coreflex/graphics/stream/start"

    payload = {
        "userId": int(user_id),
        "dashId": (dash_id or "main"),
        "widgetId": str(widget_id),
        "deviceId": str(device_id),
        "field": str(field),
        "sampleMs": int(sample_ms or 3000),
        # ✅ NEW: node-red can compute/store this per stream
        "mathFormula": str(math_formula or ""),
    }

    try:
        r = requests.post(url, json=payload, headers=_headers(), timeout=3)
        return 200 <= r.status_code < 300
    except Exception as e:
        # ✅ never crash backend if node-red is down
        print(f"[node-red] start_graphic_stream failed: {e}")
        return False


# =========================================================
# ✅ Optional Helper: Stop stream (ONLY if you choose to support it later)
# Node-RED stop endpoint must exist if you use this.
# =========================================================
def stop_graphic_stream(*, user_id: int, dash_id: str, widget_id: str) -> bool:
    if not NODE_RED_BASE_URL:
        return False

    url = f"{NODE_RED_BASE_URL}/coreflex/graphics/stream/stop"
    payload = {
        "userId": int(user_id),
        "dashId": (dash_id or "main"),
        "widgetId": str(widget_id),
    }

    try:
        r = requests.post(url, json=payload, headers=_headers(), timeout=3)
        return 200 <= r.status_code < 300
    except Exception as e:
        print(f"[node-red] stop_graphic_stream failed: {e}")
        return False


# =========================================================
# ✅ Optional: Test endpoint (Backend -> Node-RED connectivity)
# Safe to remove anytime.
# GET /node-red/ping
# =========================================================
@router.get("/ping")
def ping_node_red(current_user: User = Depends(get_current_user)):
    if not NODE_RED_BASE_URL:
        return {"ok": False, "error": "NODE_RED_BASE_URL not set"}

    try:
        r = requests.get(NODE_RED_BASE_URL, timeout=3)
        return {"ok": True, "status_code": r.status_code, "base_url": NODE_RED_BASE_URL}
    except Exception as e:
        return {"ok": False, "error": str(e), "base_url": NODE_RED_BASE_URL}