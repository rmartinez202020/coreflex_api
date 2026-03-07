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


def _dbg(label: str, **kwargs):
    try:
        print("\n========== NODE-RED GRAPHICS DEBUG ==========")
        print(label)
        for k, v in kwargs.items():
            print(f"{k} = {v}")
        print("============================================\n")
    except Exception:
        pass


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

    _dbg(
        "START GRAPHIC STREAM REQUEST",
        node_red_base_url=NODE_RED_BASE_URL,
        url=url,
        user_id=user_id,
        dash_id=payload["dashId"],
        widget_id=payload["widgetId"],
        device_id=payload["deviceId"],
        field=payload["field"],
    )

    try:
        r = requests.post(url, json=payload, headers=_headers(), timeout=5)
        ok = 200 <= r.status_code < 300

        _dbg(
            "START GRAPHIC STREAM RESPONSE",
            status_code=r.status_code,
            ok=ok,
            body=r.text[:1000],
        )

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

    _dbg(
        "SET VISIBILITY REQUEST",
        node_red_base_url=NODE_RED_BASE_URL,
        url=url,
        user_id=user_id,
        dash_id=payload["dashId"],
        widget_id=payload["widgetId"],
        is_visible=payload["isVisible"],
    )

    try:
        r = requests.post(url, json=payload, headers=_headers(), timeout=5)
        ok = 200 <= r.status_code < 300

        _dbg(
            "SET VISIBILITY RESPONSE",
            status_code=r.status_code,
            ok=ok,
            body=r.text[:1000],
        )

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

    _dbg(
        "STOP GRAPHIC STREAM REQUEST",
        node_red_base_url=NODE_RED_BASE_URL,
        url=url,
        user_id=user_id,
        dash_id=payload["dashId"],
        widget_id=payload["widgetId"],
    )

    try:
        r = requests.post(url, json=payload, headers=_headers(), timeout=5)
        ok = 200 <= r.status_code < 300

        _dbg(
            "STOP GRAPHIC STREAM RESPONSE",
            status_code=r.status_code,
            ok=ok,
            body=r.text[:1000],
        )

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
    _dbg(
        "GET GRAPHIC HISTORY CALLED",
        node_red_base_url=NODE_RED_BASE_URL,
        user_id=user_id,
        dash_id=dash_id,
        widget_id=widget_id,
        node_red_key_present=bool(NODE_RED_KEY),
    )

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

    _dbg(
        "GET GRAPHIC HISTORY REQUEST",
        url=url,
        payload=payload,
        headers=_headers(),
    )

    try:
        r = requests.post(url, json=payload, headers=_headers(), timeout=20)

        _dbg(
            "GET GRAPHIC HISTORY RAW RESPONSE",
            status_code=r.status_code,
            reason=getattr(r, "reason", ""),
            content_type=r.headers.get("content-type"),
            body_preview=r.text[:2000],
        )

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
        except Exception as e:
            print("[node-red] get_graphic_history invalid JSON response")
            _dbg(
                "GET GRAPHIC HISTORY JSON PARSE ERROR",
                error=str(e),
                raw_text=r.text[:2000],
            )
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
            _dbg(
                "GET GRAPHIC HISTORY INVALID PAYLOAD TYPE",
                payload_type=str(type(data)),
                payload_value=data,
            )
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

        _dbg(
            "GET GRAPHIC HISTORY PARSED RESPONSE",
            ok=data.get("ok"),
            error=data.get("error"),
            historyDir=data.get("historyDir"),
            prefix=data.get("prefix"),
            allNames_count=len(data.get("allNames") or []),
            files_count=len(data.get("files") or []),
            points_count=len(data.get("points") or []),
            count=data.get("count"),
        )

        return data

    except Exception as e:
        print(f"[node-red] get_graphic_history failed: {e}")
        _dbg(
            "GET GRAPHIC HISTORY REQUEST FAILED",
            error=str(e),
            url=url,
            payload=payload,
        )
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