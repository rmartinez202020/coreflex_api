# routers/node_red_graphics.py
from fastapi import APIRouter, Depends
import os
import requests

from auth_utils import get_current_user
from models import User

router = APIRouter(prefix="/node-red", tags=["Node-RED Graphics"])

NODE_RED_BASE_URL = (os.getenv("NODE_RED_BASE_URL") or "").rstrip("/")
NODE_RED_KEY = (os.getenv("NODE_RED_COMMAND_KEY") or "").strip()


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if NODE_RED_KEY:
        headers["x-coreflex-key"] = NODE_RED_KEY
    return headers


# =========================================================
# ✅ Helper: Start/Update stream on Node-RED
# =========================================================
def start_graphic_stream(
    *,
    user_id: int,
    dash_id: str,
    widget_id: str,
    bind_model: str,
    device_id: str,
    field: str,
    title: str,
    time_unit: str,
    window_size: int,
    sample_ms: int,
    y_min: float,
    y_max: float,
    line_color: str,
    graph_style: str,
    math_formula: str = "",
    totalizer_enabled: bool = False,
    totalizer_unit: str = "",
    single_units_enabled: bool = False,
    single_unit: str = "",
    retention_days: int = 35,
) -> bool:
    if not NODE_RED_BASE_URL:
        print("[node-red] start_graphic_stream skipped: NODE_RED_BASE_URL not set")
        return False

    url = f"{NODE_RED_BASE_URL}/coreflex/graphics/stream/start"

    payload = {
        "userId": int(user_id),
        "dashId": str(dash_id or "main").strip() or "main",
        "widgetId": str(widget_id or "").strip(),
        "bindModel": str(bind_model or "").strip(),
        "deviceId": str(device_id or "").strip(),
        "field": str(field or "").strip(),
        "title": str(title or "Graphic Display").strip(),
        "timeUnit": str(time_unit or "seconds").strip(),
        "windowSize": max(5, int(window_size or 60)),
        "sampleMs": max(1000, int(sample_ms or 3000)),
        "yMin": float(y_min if y_min is not None else 0),
        "yMax": float(y_max if y_max is not None else 100),
        "lineColor": str(line_color or "#0c5ac8").strip(),
        "graphStyle": str(graph_style or "line").strip(),
        "mathFormula": str(math_formula or "").strip(),
        "totalizerEnabled": bool(totalizer_enabled),
        "totalizerUnit": str(totalizer_unit or "").strip(),
        "singleUnitsEnabled": bool(single_units_enabled),
        "singleUnit": str(single_unit or "").strip(),
        "retentionDays": max(1, min(366, int(retention_days or 35))),
    }

    try:
        r = requests.post(url, json=payload, headers=_headers(), timeout=5)
        ok = 200 <= r.status_code < 300

        if not ok:
            print(
                f"[node-red] start_graphic_stream bad response: "
                f"status={r.status_code} url={url} body={r.text}"
            )

        return ok
    except Exception as e:
        print(f"[node-red] start_graphic_stream failed: {e}")
        return False


# =========================================================
# ✅ Helper: Update visibility / active sample rate
# =========================================================
def set_graphic_stream_visibility(
    *,
    user_id: int,
    dash_id: str,
    widget_id: str,
    is_visible: bool,
) -> bool:
    if not NODE_RED_BASE_URL:
        print("[node-red] set_graphic_stream_visibility skipped: NODE_RED_BASE_URL not set")
        return False

    url = f"{NODE_RED_BASE_URL}/coreflex/graphics/stream/visibility"

    payload = {
        "userId": int(user_id),
        "dashId": str(dash_id or "main").strip() or "main",
        "widgetId": str(widget_id or "").strip(),
        "isVisible": bool(is_visible),
    }

    try:
        r = requests.post(url, json=payload, headers=_headers(), timeout=5)
        ok = 200 <= r.status_code < 300

        if not ok:
            print(
                f"[node-red] set_graphic_stream_visibility bad response: "
                f"status={r.status_code} url={url} body={r.text}"
            )

        return ok
    except Exception as e:
        print(f"[node-red] set_graphic_stream_visibility failed: {e}")
        return False


# =========================================================
# ✅ Helper: Stop stream
# =========================================================
def stop_graphic_stream(*, user_id: int, dash_id: str, widget_id: str) -> bool:
    if not NODE_RED_BASE_URL:
        print("[node-red] stop_graphic_stream skipped: NODE_RED_BASE_URL not set")
        return False

    url = f"{NODE_RED_BASE_URL}/coreflex/graphics/stream/stop"
    payload = {
        "userId": int(user_id),
        "dashId": str(dash_id or "main").strip() or "main",
        "widgetId": str(widget_id or "").strip(),
    }

    try:
        r = requests.post(url, json=payload, headers=_headers(), timeout=5)
        ok = 200 <= r.status_code < 300

        if not ok:
            print(
                f"[node-red] stop_graphic_stream bad response: "
                f"status={r.status_code} url={url} body={r.text}"
            )

        return ok
    except Exception as e:
        print(f"[node-red] stop_graphic_stream failed: {e}")
        return False


# =========================================================
# ✅ Helper: Read historian from Node-RED server
# =========================================================
def get_graphic_history(*, user_id: int, dash_id: str, widget_id: str) -> dict:
    if not NODE_RED_BASE_URL:
        print("[node-red] get_graphic_history skipped: NODE_RED_BASE_URL not set")
        return {
            "ok": False,
            "error": "NODE_RED_BASE_URL not set",
            "files": [],
            "points": [],
            "count": 0,
        }

    url = f"{NODE_RED_BASE_URL}/coreflex/graphics/history/read"
    payload = {
        "userId": int(user_id),
        "dashId": str(dash_id or "main").strip() or "main",
        "widgetId": str(widget_id or "").strip(),
    }

    try:
        r = requests.post(url, json=payload, headers=_headers(), timeout=20)

        if not (200 <= r.status_code < 300):
            print(
                f"[node-red] get_graphic_history bad response: "
                f"status={r.status_code} url={url} body={r.text}"
            )
            return {
                "ok": False,
                "error": f"Node-RED bad response ({r.status_code})",
                "status_code": r.status_code,
                "body": r.text,
                "files": [],
                "points": [],
                "count": 0,
            }

        try:
            data = r.json()
        except Exception:
            print("[node-red] get_graphic_history invalid JSON response")
            return {
                "ok": False,
                "error": "Invalid JSON returned by Node-RED history endpoint",
                "status_code": r.status_code,
                "body": r.text,
                "files": [],
                "points": [],
                "count": 0,
            }

        if not isinstance(data, dict):
            print("[node-red] get_graphic_history response is not an object")
            return {
                "ok": False,
                "error": "Invalid Node-RED history payload",
                "files": [],
                "points": [],
                "count": 0,
            }

        data.setdefault("ok", True)
        data.setdefault("files", [])
        data.setdefault("points", [])
        data.setdefault("count", len(data.get("points", []) or []))
        return data

    except Exception as e:
        print(f"[node-red] get_graphic_history failed: {e}")
        return {
            "ok": False,
            "error": str(e),
            "files": [],
            "points": [],
            "count": 0,
        }


# =========================================================
# ✅ Optional: Test endpoint
# =========================================================
@router.get("/ping")
def ping_node_red(current_user: User = Depends(get_current_user)):
    if not NODE_RED_BASE_URL:
        return {"ok": False, "error": "NODE_RED_BASE_URL not set"}

    try:
        r = requests.get(NODE_RED_BASE_URL, timeout=3)
        return {
            "ok": True,
            "status_code": r.status_code,
            "base_url": NODE_RED_BASE_URL,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "base_url": NODE_RED_BASE_URL}