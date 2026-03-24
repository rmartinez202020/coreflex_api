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


def _normalize_dash_id(value) -> str:
    s = str(value or "").strip()
    return s if s else "main"


def _normalize_dash_folder(value) -> str:
    dash_id = _normalize_dash_id(value)
    if dash_id.lower() == "main":
        return "dash_main"
    return f"dash_{dash_id}"


def _post_json(url: str, payload: dict, timeout_sec: int = 20):
    return requests.post(url, json=payload, headers=_headers(), timeout=timeout_sec)


def _safe_json_response(r: requests.Response):
    try:
        return r.json()
    except Exception:
        return None


def _history_response_has_points(data: dict) -> bool:
    if not isinstance(data, dict):
        return False

    points = data.get("points")
    if isinstance(points, list) and len(points) > 0:
        return True

    count = data.get("count")
    try:
        return int(count or 0) > 0
    except Exception:
        return False


def _normalize_history_payload(data, *, fallback_error: str = "") -> dict:
    if not isinstance(data, dict):
        return {
            "ok": False,
            "error": fallback_error or "Invalid Node-RED history payload",
            "files": [],
            "points": [],
            "count": 0,
        }

    data.setdefault("ok", True)
    data.setdefault("files", [])
    data.setdefault("points", [])
    data.setdefault("count", len(data.get("points", []) or []))
    return data


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

    resolved_dash_id = _normalize_dash_id(dash_id)
    resolved_dash_folder = _normalize_dash_folder(dash_id)

    url = f"{NODE_RED_BASE_URL}/coreflex/graphics/stream/start"

    payload = {
        "userId": int(user_id),
        "dashId": resolved_dash_id,
        "dashFolder": resolved_dash_folder,
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
        dash_folder=payload["dashFolder"],
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

    resolved_dash_id = _normalize_dash_id(dash_id)
    resolved_dash_folder = _normalize_dash_folder(dash_id)

    url = f"{NODE_RED_BASE_URL}/coreflex/graphics/stream/visibility"

    payload = {
        "userId": int(user_id),
        "dashId": resolved_dash_id,
        "dashFolder": resolved_dash_folder,
        "widgetId": str(widget_id or "").strip(),
        "isVisible": bool(is_visible),
    }

    _dbg(
        "SET VISIBILITY REQUEST",
        node_red_base_url=NODE_RED_BASE_URL,
        url=url,
        user_id=user_id,
        dash_id=payload["dashId"],
        dash_folder=payload["dashFolder"],
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

    resolved_dash_id = _normalize_dash_id(dash_id)
    resolved_dash_folder = _normalize_dash_folder(dash_id)

    url = f"{NODE_RED_BASE_URL}/coreflex/graphics/stream/stop"
    payload = {
        "userId": int(user_id),
        "dashId": resolved_dash_id,
        "dashFolder": resolved_dash_folder,
        "widgetId": str(widget_id or "").strip(),
    }

    _dbg(
        "STOP GRAPHIC STREAM REQUEST",
        node_red_base_url=NODE_RED_BASE_URL,
        url=url,
        user_id=user_id,
        dash_id=payload["dashId"],
        dash_folder=payload["dashFolder"],
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
# ✅ Internal helper: single history read attempt
# =========================================================
def _get_graphic_history_once(*, user_id: int, dash_id: str, widget_id: str) -> dict:
    resolved_dash_id = _normalize_dash_id(dash_id)
    resolved_dash_folder = _normalize_dash_folder(dash_id)

    url = f"{NODE_RED_BASE_URL}/coreflex/graphics/history/read"
    payload = {
        "userId": int(user_id),
        "dashId": resolved_dash_id,
        "dashFolder": resolved_dash_folder,
        "widgetId": str(widget_id or "").strip(),
    }

    _dbg(
        "GET GRAPHIC HISTORY REQUEST",
        url=url,
        payload=payload,
        headers=_headers(),
    )

    try:
        r = _post_json(url, payload, timeout_sec=20)

        _dbg(
            "GET GRAPHIC HISTORY RAW RESPONSE",
            status_code=r.status_code,
            reason=getattr(r, "reason", ""),
            content_type=r.headers.get("content-type"),
            body_preview=r.text[:2000],
            dash_id_attempt=payload["dashId"],
            dash_folder_attempt=payload["dashFolder"],
        )

        if not (200 <= r.status_code < 300):
            return {
                "ok": False,
                "error": f"Node-RED bad response ({r.status_code})",
                "status_code": r.status_code,
                "body": r.text,
                "files": [],
                "points": [],
                "count": 0,
                "dashIdUsed": payload["dashId"],
                "dashFolderUsed": payload["dashFolder"],
            }

        parsed = _safe_json_response(r)
        data = _normalize_history_payload(
            parsed,
            fallback_error="Invalid JSON returned by Node-RED history endpoint",
        )
        data["dashIdUsed"] = payload["dashId"]
        data["dashFolderUsed"] = payload["dashFolder"]

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
            dash_id_attempt=payload["dashId"],
            dash_folder_attempt=payload["dashFolder"],
        )

        return data

    except Exception as e:
        print(f"[node-red] _get_graphic_history_once failed: {e}")
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
            "dashIdUsed": payload["dashId"],
            "dashFolderUsed": payload["dashFolder"],
        }


# =========================================================
# ✅ Helper: Read historian from Node-RED server
# ✅ IMPORTANT:
#   No fallback to main anymore.
#   Each dashboard must read its own folder only.
# =========================================================
def get_graphic_history(*, user_id: int, dash_id: str, widget_id: str) -> dict:
    requested_dash = _normalize_dash_id(dash_id)
    requested_folder = _normalize_dash_folder(dash_id)

    _dbg(
        "GET GRAPHIC HISTORY CALLED",
        node_red_base_url=NODE_RED_BASE_URL,
        user_id=user_id,
        dash_id=requested_dash,
        dash_folder=requested_folder,
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

    result = _get_graphic_history_once(
        user_id=user_id,
        dash_id=requested_dash,
        widget_id=widget_id,
    )

    result["requestedDashId"] = requested_dash
    result["requestedDashFolder"] = requested_folder
    result["resolvedByFallback"] = False

    return result


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