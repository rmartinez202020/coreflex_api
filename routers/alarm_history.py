# routers/alarm_history.py

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import requests

from auth_utils import get_current_user
from models import User

router = APIRouter(prefix="/alarm-history", tags=["Alarm History"])

NODE_RED_READ_URL = "http://98.90.225.131:1880/alarm-history/read"
NODE_RED_COMMAND_KEY = "CFX_k29sLx92Jd8s1Qp4NzT7MartinezVx93LwQa2"


class AlarmHistoryReadBody(BaseModel):
    alarm_log_key: str


@router.post("/read")
def read_alarm_history(
    body: AlarmHistoryReadBody,
    current_user: User = Depends(get_current_user),
):
    alarm_log_key = str(body.alarm_log_key or "").strip()

    if not alarm_log_key:
        raise HTTPException(status_code=400, detail="alarm_log_key is required")

    try:
        res = requests.post(
            NODE_RED_READ_URL,
            json={
                "user_id": current_user.id,
                "alarm_log_key": alarm_log_key,
            },
            headers={
                "Content-Type": "application/json",
                "x-command-key": NODE_RED_COMMAND_KEY,
            },
            timeout=5,
        )

        # ✅ NEW:
        # If Node-RED/file is not ready yet, treat it as "no history yet"
        # instead of surfacing a hard error to the UI.
        if not res.ok:
            status = int(res.status_code or 0)
            text = str(res.text or "").strip().lower()

            missing_markers = [
                "enoent",
                "no such file",
                "not found",
                "file does not exist",
                "cannot find the file",
                "missing",
            ]

            if status in (404, 204):
                return []

            if any(marker in text for marker in missing_markers):
                return []

            raise HTTPException(
                status_code=502,
                detail=f"Node-RED read failed: {res.status_code} {res.text[:300]}",
            )

        try:
            data = res.json()
        except Exception:
            # ✅ NEW:
            # Empty/non-json response from a new log should behave like empty history.
            raw_text = str(getattr(res, "text", "") or "").strip()
            if raw_text == "":
                return []

            raise HTTPException(
                status_code=502,
                detail="Node-RED returned non-JSON response",
            )

        # ✅ NEW:
        # Allow a few safe shapes and normalize all of them to a list.
        if isinstance(data, list):
            return data

        if isinstance(data, dict):
            # common payload shapes
            if isinstance(data.get("items"), list):
                return data.get("items") or []

            if isinstance(data.get("history"), list):
                return data.get("history") or []

            if isinstance(data.get("rows"), list):
                return data.get("rows") or []

            # explicit empty / no-file style payloads
            if data.get("success") is True and (
                data.get("items") is None
                or data.get("history") is None
                or data.get("rows") is None
            ):
                msg = str(
                    data.get("message")
                    or data.get("detail")
                    or data.get("error")
                    or ""
                ).strip().lower()

                missing_markers = [
                    "enoent",
                    "no such file",
                    "not found",
                    "file does not exist",
                    "cannot find the file",
                    "missing",
                    "no history",
                    "empty",
                ]

                if not msg or any(marker in msg for marker in missing_markers):
                    return []

        raise HTTPException(
            status_code=502,
            detail="Node-RED returned invalid history payload",
        )

    except HTTPException:
        raise
    except requests.Timeout:
        # ✅ NEW:
        # For a brand-new alarm log with no history file yet, timeout should not
        # scream red error in the UI. Treat it as empty history for now.
        return []
    except requests.RequestException as e:
        raise HTTPException(
            status_code=502,
            detail=f"Alarm history request failed: {str(e)}",
        )