# routers/alarm_engine.py

from datetime import datetime
import time

import requests
from sqlalchemy.orm import Session

from database import SessionLocal
import models
from utils.zhc1921_live_cache import get_latest as get_latest_1921

# 🔗 Node-RED endpoint
NODE_RED_URL = "http://98.90.225.131:1880/alarm-log"
NODE_RED_COMMAND_KEY = "CFX_k29sLx92Jd8s1Qp4NzT7MartinezVx93LwQa2"

POLL_INTERVAL = 10  # seconds

# ==========================================
# 🧠 RUNTIME STATE MEMORY
# ✅ used to detect state transitions only
# key = (user_id, alarm_id)
# value = {
#   "state": "ACTIVE" | "NORMAL",
#   "last_value": ...,
#   "last_device_status": ...
# }
# ==========================================
ALARM_RUNTIME_STATE = {}


# ==========================================
# 🕒 HELPER — LOCAL TIME WITH TIMEZONE
# ✅ timezone-aware local server time
# ✅ frontend can display correctly without weird UTC shift
# ==========================================
def now_local_iso():
    return datetime.now().astimezone().isoformat()


# ==========================================
# 🔧 HELPER — APPLY MATH
# ==========================================
def apply_math(value, formula):
    if value is None:
        return None

    if not formula:
        return value

    try:
        expr = str(formula).lower().replace("value", str(value))
        return eval(expr)
    except Exception:
        return value


# ==========================================
# 🔧 HELPER — EVALUATE CONDITION
# ==========================================
def evaluate_alarm(alarm, value):
    if value is None:
        return False

    if alarm.alarm_type == "DI":
        try:
            v = int(value)
        except Exception:
            return False

        if alarm.contact_type == "NO":
            return v == 1
        if alarm.contact_type == "NC":
            return v == 0
        return False

    if alarm.alarm_type == "AI":
        try:
            v = float(value)
            threshold = float(alarm.threshold)
        except Exception:
            return False

        if alarm.operator == ">=":
            return v >= threshold
        if alarm.operator == "<=":
            return v <= threshold
        if alarm.operator == ">":
            return v > threshold
        if alarm.operator == "<":
            return v < threshold
        if alarm.operator in ("==", "="):
            return v == threshold

    return False


# ==========================================
# 🔧 HELPER — GET DEVICE DATA FROM LIVE CACHE
# ✅ ONLY CF-2000 / ZHC1921 FOR NOW
# ==========================================
def get_device_data_from_cache(alarm):
    model = str(alarm.model or "").strip().lower()
    device_id = str(alarm.device_id or "").strip()

    if not device_id:
        return None

    if model == "zhc1921":
        return get_latest_1921(device_id)

    # future:
    # if model == "zhc1661": ...
    # if model == "tp4000": ...
    return None


# ==========================================
# 🔧 HELPER — READ TAG VALUE FROM CACHE
# ==========================================
def get_tag_value_from_cache(data, tag):
    if not data or not tag:
        return None

    field = str(tag).strip().lower()
    return data.get(field)


# ==========================================
# 🚨 SEND TO NODE-RED / HISTORIAN
# ==========================================
def send_to_historian(alarm, raw_value, computed_value, state, device_status):
    payload = {
        # ✅ use local timezone-aware timestamp
        "ts": now_local_iso(),
        "user_id": alarm.user_id,
        "alarm_log_key": alarm.alarm_log_key,
        "alarm_definition_id": alarm.id,
        "device_id": alarm.device_id,
        "model": alarm.model,
        "tag": alarm.tag,
        "alarm_type": alarm.alarm_type,
        "message": alarm.message,
        "severity": alarm.severity,
        "group_name": alarm.group_name,
        "state": state,  # ACTIVE / RETURNED
        "device_status": device_status,
        "raw_value": raw_value,
        "computed_value": computed_value,
        "operator": alarm.operator,
        "threshold": alarm.threshold,
        "contact_type": alarm.contact_type,
    }

    headers = {
        "Content-Type": "application/json",
        "x-command-key": NODE_RED_COMMAND_KEY,
    }

    try:
        res = requests.post(
            NODE_RED_URL,
            json=payload,
            headers=headers,
            timeout=3,
        )

        if not res.ok:
            print(
                "❌ Node-RED historian rejected event:",
                res.status_code,
                res.text[:300],
            )
    except Exception as e:
        print("❌ Node-RED send failed:", e)


# ==========================================
# 🔁 PROCESS ONE ALARM
# ✅ send ACTIVE only on transition NORMAL -> ACTIVE
# ✅ send RETURNED only on transition ACTIVE -> NORMAL
# ✅ keep monitoring silently while alarm remains ACTIVE
# ✅ reads real values from live cache only
# ==========================================
def process_alarm(db: Session, alarm):
    _ = db  # keep signature consistent for future use

    key = (alarm.user_id, alarm.id)
    prev = ALARM_RUNTIME_STATE.get(key, {})
    prev_state = prev.get("state", "NORMAL")

    data = get_device_data_from_cache(alarm)

    # ✅ If cache entry does not exist yet, skip instead of false alarming.
    if not data:
        return

    device_status = str(data.get("status") or "").strip().lower()

    # 1) offline device alarm logic
    if device_status != "online":
        current_state = "ACTIVE"
        raw_value = None
        computed_value = None
    else:
        raw_value = get_tag_value_from_cache(data, alarm.tag)
        computed_value = apply_math(raw_value, alarm.math_formula)
        triggered = evaluate_alarm(alarm, computed_value)
        current_state = "ACTIVE" if triggered else "NORMAL"

    # --------------------------------------
    # ✅ Send ACTIVE only once when alarm becomes active
    # --------------------------------------
    if prev_state != "ACTIVE" and current_state == "ACTIVE":
        send_to_historian(
            alarm=alarm,
            raw_value=raw_value,
            computed_value=computed_value,
            state="ACTIVE",
            device_status=device_status,
        )
        print(
            f"🚨 ACTIVE -> user:{alarm.user_id} alarm:{alarm.id} "
            f"device:{alarm.device_id} tag:{alarm.tag}"
        )

    # --------------------------------------
    # ✅ Send RETURNED only once when alarm clears
    # --------------------------------------
    elif prev_state == "ACTIVE" and current_state == "NORMAL":
        send_to_historian(
            alarm=alarm,
            raw_value=raw_value,
            computed_value=computed_value,
            state="RETURNED",
            device_status=device_status,
        )
        print(
            f"✅ RETURNED -> user:{alarm.user_id} alarm:{alarm.id} "
            f"device:{alarm.device_id} tag:{alarm.tag}"
        )

    # --------------------------------------
    # ✅ Otherwise keep monitoring silently
    # ACTIVE -> ACTIVE  => do nothing
    # NORMAL -> NORMAL  => do nothing
    # --------------------------------------

    # update runtime state
    ALARM_RUNTIME_STATE[key] = {
        "state": current_state,
        "last_value": computed_value,
        "last_device_status": device_status,
        "updated_at": now_local_iso(),
    }


# ==========================================
# 🧹 OPTIONAL CLEANUP FOR REMOVED/DISABLED ALARMS
# ==========================================
def cleanup_runtime_state(active_alarm_keys):
    stale_keys = [k for k in ALARM_RUNTIME_STATE.keys() if k not in active_alarm_keys]
    for k in stale_keys:
        ALARM_RUNTIME_STATE.pop(k, None)


# ==========================================
# 🚀 MAIN ENGINE LOOP
# ==========================================
def alarm_engine_loop():
    print("🚀 Alarm Engine Started...")

    while True:
        db: Session = SessionLocal()

        try:
            alarms = (
                db.query(models.AlarmDefinition)
                .filter(models.AlarmDefinition.enabled == True)
                .all()
            )

            active_alarm_keys = set()

            for alarm in alarms:
                key = (alarm.user_id, alarm.id)
                active_alarm_keys.add(key)

                # skip unroutable alarms
                if not alarm.alarm_log_key:
                    continue

                process_alarm(db, alarm)

            cleanup_runtime_state(active_alarm_keys)

        except Exception as e:
            print("❌ Alarm Engine Error:", e)

        finally:
            db.close()

        time.sleep(POLL_INTERVAL)