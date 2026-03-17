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

        if not res.ok:
            raise HTTPException(
                status_code=502,
                detail=f"Node-RED read failed: {res.status_code} {res.text[:300]}",
            )

        try:
            data = res.json()
        except Exception:
            raise HTTPException(
                status_code=502,
                detail="Node-RED returned non-JSON response",
            )

        if not isinstance(data, list):
            raise HTTPException(
                status_code=502,
                detail="Node-RED returned invalid history payload",
            )

        return data

    except HTTPException:
        raise
    except requests.RequestException as e:
        raise HTTPException(
            status_code=502,
            detail=f"Alarm history request failed: {str(e)}",
        )