# main.py
from fastapi import FastAPI, Request, Depends, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR
from sqlalchemy.orm import Session
from datetime import datetime, timezone
import os
import threading
import re

# ========================================
# 🗄 IMPORT MODELS FIRST (CRITICAL)
# ========================================
import models  # noqa: F401
from database import Base, engine, get_db

# ========================================
# ☁️ CLOUDINARY INIT
# ========================================
from cloudinary_config import init_cloudinary  # noqa: E402

# ✅ REDIS CLIENT TEST
from utils.redis_client import redis_client  # noqa: E402

# ✅ NEW: background counter tick (persistent counters)
from routers.device_counters_tick import (  # noqa: E402
    start_device_counters_tick,
    stop_device_counters_tick,
)

# ✅ NEW: alarm engine background loop
from routers.alarm_engine import alarm_engine_loop  # noqa: E402

# ========================================
# 🚀 FASTAPI APP
# ========================================
app = FastAPI(title="CoreFlex API", version="1.0.0")

# ========================================
# 🌍 CORS
# ========================================
ALLOWED_ORIGINS = [
    "https://www.coreflexiiotsplatform.com",
    "https://coreflexiiotsplatform.com",
    "http://www.coreflexiiotsplatform.com",
    "http://coreflexiiotsplatform.com",
    "https://www.coreflexiotsplatform.com",
    "https://coreflexiotsplatform.com",
    "http://www.coreflexiotsplatform.com",
    "http://coreflexiotsplatform.com",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=r"https?://(www\.)?coreflexi{1,2}otsplatform\.com",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


@app.options("/{full_path:path}")
async def options_preflight_handler(full_path: str, request: Request):
    origin = request.headers.get("origin", "")
    allow_origin = origin if origin in ALLOWED_ORIGINS else ""

    if not allow_origin:
        if re.match(r"^https?://(www\.)?coreflexi{1,2}otsplatform\.com$", origin):
            allow_origin = origin

    headers = {
        "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": request.headers.get(
            "access-control-request-headers", "*"
        ),
        "Access-Control-Allow-Credentials": "true",
        "Vary": "Origin",
    }

    if allow_origin:
        headers["Access-Control-Allow-Origin"] = allow_origin

    return Response(status_code=200, headers=headers)


@app.on_event("startup")
async def on_startup():
    try:
        Base.metadata.create_all(bind=engine)
        print("✅ DB tables ensured on startup")
    except Exception as e:
        print("❌ Startup create_all failed:", repr(e))

    try:
        init_cloudinary()
        print("✅ Cloudinary initialized on startup")
    except Exception as e:
        print("❌ Cloudinary init failed:", repr(e))

    try:
        redis_client.set("startup_test", "coreflex")
        print("✅ Redis connected:", redis_client.get("startup_test"))
    except Exception as e:
        print("❌ Redis startup failed:", repr(e))

    try:
        start_device_counters_tick()
    except Exception as e:
        print("❌ start_device_counters_tick failed:", repr(e))

    try:
        alarm_thread = threading.Thread(
            target=alarm_engine_loop,
            daemon=True,
            name="alarm-engine-loop",
        )
        alarm_thread.start()
        print("✅ Alarm engine thread started")
    except Exception as e:
        print("❌ alarm_engine_loop failed to start:", repr(e))


@app.on_event("shutdown")
async def on_shutdown():
    try:
        await stop_device_counters_tick()
    except Exception as e:
        print("❌ stop_device_counters_tick failed:", repr(e))


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    print("❌ Unhandled error:", repr(exc))
    return JSONResponse(
        status_code=HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": "Internal Server Error",
            "error": repr(exc),
            "path": str(request.url.path),
        },
    )


from auth_routes import router as auth_router  # noqa: E402

app.include_router(auth_router)

from routers.main_dashboard import router as main_dashboard_router  # noqa: E402

app.include_router(main_dashboard_router)

from routers.customers_dashboards import router as customers_dashboards_router  # noqa: E402

app.include_router(customers_dashboards_router)

from routers.user_profile import router as user_profile_router  # noqa: E402

app.include_router(user_profile_router)

from routers.customer_locations import router as customer_locations_router  # noqa: E402

app.include_router(customer_locations_router)

from routers.tenant_users import router as tenant_users_router  # noqa: E402

app.include_router(tenant_users_router)

from routers.user_subscriptions import router as user_subscriptions_router  # noqa: E402

app.include_router(user_subscriptions_router)

from routers.admin_subscriptions import router as admin_subscriptions_router  # noqa: E402

app.include_router(admin_subscriptions_router)

from routers.billing import router as billing_router  # noqa: E402

app.include_router(billing_router)

from routers.subscription_agreements import (  # noqa: E402
    router as subscription_agreements_router,
)

app.include_router(subscription_agreements_router)

from routers.billing_admin import router as billing_admin_router  # noqa: E402

app.include_router(billing_admin_router)

from routers.logs_admin import router as logs_admin_router  # noqa: E402

app.include_router(logs_admin_router)

from routers.images import router as images_router  # noqa: E402

app.include_router(images_router)

from routers.device_registry import router as device_registry_router  # noqa: E402

app.include_router(device_registry_router)

from routers.gateway_device_seen import (  # noqa: E402
    router as gateway_device_seen_router,
)

app.include_router(gateway_device_seen_router)

from routers.zhc1921_devices import router as zhc1921_router  # noqa: E402

app.include_router(zhc1921_router)

from routers.zhc1661_devices import router as zhc1661_router  # noqa: E402

app.include_router(zhc1661_router)

from routers.tp4000_devices import router as tp4000_router  # noqa: E402

app.include_router(tp4000_router)

from routers.radar_level_sensors import router as radar_level_sensors_router  # noqa: E402

app.include_router(radar_level_sensors_router)

from routers.device_counters import router as device_counters_router  # noqa: E402

app.include_router(device_counters_router)

from routers.control_bindings import router as control_bindings_router  # noqa: E402

app.include_router(control_bindings_router)

from routers.node_red_graphics import router as node_red_graphics_router  # noqa: E402

app.include_router(node_red_graphics_router)

from routers.graphic_display_bindings import (  # noqa: E402
    router as graphic_display_bindings_router,
)

app.include_router(graphic_display_bindings_router)

from routers.alarm_log_windows import router as alarm_log_windows_router  # noqa: E402

app.include_router(alarm_log_windows_router)

from routers.alarm_definitions import router as alarm_definitions_router  # noqa: E402

app.include_router(alarm_definitions_router)

from routers.alarm_history import router as alarm_history_router  # noqa: E402

app.include_router(alarm_history_router)


@app.get("/health")
def health():
    return {"ok": True, "status": "API running"}


@app.get("/cors-test")
def cors_test():
    return {"ok": True, "message": "CORS working"}


class SensorUpdate(BaseModel):
    imei: str
    level: float
    temperature: float
    battery: float


@app.post("/api/update")
def update_sensor(data: SensorUpdate):
    print("Sensor received:", data)
    return {"status": "received", "imei": data.imei}


# ========================================
# ✅ /devices + /tenant-access/devices
# ========================================
from auth_utils import get_current_user  # noqa: E402
from routers.log_engine import read_logs  # noqa: E402
from models import (  # noqa: E402
    ZHC1921Device,
    ZHC1661Device,
    TP4000Device,
    RadarLevelSensor,
    User,
    TenantUser,
    TenantUserDashboardAccess,
    CustomerDashboard,
)
from utils.zhc1921_live_cache import get_latest as get_latest_zhc1921  # noqa: E402
from utils.zhc1661_live_cache import get_latest as get_latest_zhc1661  # noqa: E402

OFFLINE_AFTER_SECONDS = int(os.getenv("COREFLEX_OFFLINE_AFTER_SECONDS") or "10")


# ========================================
# ✅ LOGS & ACTIVITY READ
# ========================================
class LogsReadRequest(BaseModel):
    date: str | None = None


@app.post("/logs/read")
def read_current_user_logs(
    body: LogsReadRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Read the authenticated CoreFlex owner's audit logs.

    Security:
    - The frontend does NOT send user_id.
    - user_id comes only from the verified JWT/current_user.
    - Tenant activity is already stored under the owning user's folder.
    """
    result = read_logs(
        user_id=current_user.id,
        date=body.date,
    )

    if not result.get("ok"):
        status_code = result.get("status_code")

        # Preserve a client-side date validation error as 400.
        if result.get("error") == "Invalid log date. Expected YYYY-MM-DD":
            raise HTTPException(
                status_code=400,
                detail=result,
            )

        # Node-RED/read-side failures are upstream service failures.
        raise HTTPException(
            status_code=502 if not status_code else int(status_code),
            detail=result,
        )

    return result


def _parse_cached_datetime(value) -> datetime | None:
    if not value:
        return None

    if isinstance(value, datetime):
        return value

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _as_utc(dt: datetime | None) -> datetime | None:
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _compute_online_status(last_seen) -> str:
    ls = _as_utc(_parse_cached_datetime(last_seen))
    if not ls:
        return "offline"

    now = datetime.now(timezone.utc)
    age = (now - ls).total_seconds()
    return "online" if age <= OFFLINE_AFTER_SECONDS else "offline"


def _last_seen_iso(last_seen) -> str:
    ls = _as_utc(_parse_cached_datetime(last_seen))
    return ls.isoformat() if ls else "—"


def _normalize_public_model(raw) -> str:
    v = str(raw or "").strip().lower()

    if v in {"zhc1921", "cf-2000", "cf2000"}:
        return "zhc1921"

    if v in {"zhc1661", "cf-1600", "cf1600"}:
        return "zhc1661"

    if v in {"tp4000", "tp-4000"}:
        return "tp4000"

    if v in {"cfr100", "cf-r100", "cf_r100", "radar-level", "radar_level"}:
        return "cfr100"

    return v


def _normalize_public_device_id(value) -> str:
    return re.sub(r"\D", "", str(value or "").strip())


def _extract_dashboard_objects(layout):
    if not isinstance(layout, dict):
        return []

    objects = (
        layout.get("canvas", {}).get("objects")
        or layout.get("objects")
        or layout.get("droppedTanks")
        or []
    )

    return objects if isinstance(objects, list) else []


def _extract_bound_devices_from_dashboard_layout(layout):
    wanted = {
        "zhc1921": set(),
        "zhc1661": set(),
        "tp4000": set(),
        "cfr100": set(),
    }

    objects = _extract_dashboard_objects(layout)

    for obj in objects:
        if not isinstance(obj, dict):
            continue

        props = obj.get("properties") if isinstance(obj.get("properties"), dict) else {}
        tag = props.get("tag") or obj.get("tag") or None

        model = ""
        device_id = ""

        if isinstance(tag, dict):
            model = _normalize_public_model(tag.get("model"))
            device_id = _normalize_public_device_id(
                tag.get("deviceId") or tag.get("device_id") or ""
            )

        if not model or not device_id:
            model = _normalize_public_model(
                obj.get("bindModel")
                or props.get("bindModel")
                or obj.get("bind_model")
                or props.get("bind_model")
                or obj.get("deviceModel")
                or props.get("deviceModel")
                or obj.get("device_model")
                or props.get("device_model")
                or ""
            )

            device_id = _normalize_public_device_id(
                obj.get("bindDeviceId")
                or props.get("bindDeviceId")
                or obj.get("bind_device_id")
                or props.get("bind_device_id")
                or obj.get("bindImei")
                or props.get("bindImei")
                or obj.get("unitId")
                or props.get("unitId")
                or obj.get("raw_imei_bytes")
                or props.get("raw_imei_bytes")
                or ""
            )

        if model and device_id:
            if model not in wanted:
                wanted[model] = set()
            wanted[model].add(device_id)

    return wanted


def _device_allowed(wanted: dict | None, model_key: str, device_id: str) -> bool:
    if wanted is None:
        return True

    model = _normalize_public_model(model_key)
    did = _normalize_public_device_id(device_id)

    if not model or not did:
        return False

    return did in wanted.get(model, set())


def _append_claimed_devices_for_owner(
    db: Session,
    owner_user_id: int,
    wanted: dict | None = None,
):
    out = []

    # ---- ZHC1921 (CF-2000) ----
    rows_1921 = (
        db.query(ZHC1921Device)
        .filter(ZHC1921Device.claimed_by_user_id == owner_user_id)
        .order_by(ZHC1921Device.id.asc())
        .all()
    )

    for r in rows_1921:
        if not _device_allowed(wanted, "zhc1921", r.device_id):
            continue

        cached = get_latest_zhc1921(r.device_id) or {}
        last_seen = cached.get("last_seen") or r.last_seen
        status = _compute_online_status(last_seen)
        online = status == "online"

        out.append(
            {
                "model": "ZHC1921",
                "deviceModel": "ZHC1921",
                "device_model": "zhc1921",
                "bindModel": "zhc1921",
                "deviceId": r.device_id,
                "device_id": r.device_id,
                "addedAt": r.claimed_at.isoformat() if r.claimed_at else "—",
                "ownedBy": r.claimed_by_email or "—",
                "status": status,
                "online": online,
                "is_online": online,
                "lastSeen": _last_seen_iso(last_seen),
                "in1": int(cached.get("di1", r.di1 or 0) or 0),
                "in2": int(cached.get("di2", r.di2 or 0) or 0),
                "in3": int(cached.get("di3", r.di3 or 0) or 0),
                "in4": int(cached.get("di4", r.di4 or 0) or 0),
                "in5": int(cached.get("di5", getattr(r, "di5", 0) or 0) or 0),
                "in6": int(cached.get("di6", getattr(r, "di6", 0) or 0) or 0),
                "di1": int(cached.get("di1", r.di1 or 0) or 0),
                "di2": int(cached.get("di2", r.di2 or 0) or 0),
                "di3": int(cached.get("di3", r.di3 or 0) or 0),
                "di4": int(cached.get("di4", r.di4 or 0) or 0),
                "di5": int(cached.get("di5", getattr(r, "di5", 0) or 0) or 0),
                "di6": int(cached.get("di6", getattr(r, "di6", 0) or 0) or 0),
                "do1": int(cached.get("do1", r.do1 or 0) or 0),
                "do2": int(cached.get("do2", r.do2 or 0) or 0),
                "do3": int(cached.get("do3", r.do3 or 0) or 0),
                "do4": int(cached.get("do4", r.do4 or 0) or 0),
                "ai1": cached.get("ai1", r.ai1 if r.ai1 is not None else ""),
                "ai2": cached.get("ai2", r.ai2 if r.ai2 is not None else ""),
                "ai3": cached.get("ai3", r.ai3 if r.ai3 is not None else ""),
                "ai4": cached.get("ai4", r.ai4 if r.ai4 is not None else ""),
            }
        )

    # ---- ZHC1661 (CF-1600) ----
    rows_1661 = (
        db.query(ZHC1661Device)
        .filter(ZHC1661Device.claimed_by_user_id == owner_user_id)
        .order_by(ZHC1661Device.id.asc())
        .all()
    )

    for r in rows_1661:
        if not _device_allowed(wanted, "zhc1661", r.device_id):
            continue

        cached = get_latest_zhc1661(r.device_id) or {}
        last_seen = cached.get("last_seen") or r.last_seen
        status = _compute_online_status(last_seen)
        online = status == "online"

        out.append(
            {
                "model": "ZHC1661",
                "deviceModel": "ZHC1661",
                "device_model": "zhc1661",
                "bindModel": "zhc1661",
                "deviceId": r.device_id,
                "device_id": r.device_id,
                "addedAt": r.claimed_at.isoformat() if r.claimed_at else "—",
                "ownedBy": r.claimed_by_email or "—",
                "status": status,
                "online": online,
                "is_online": online,
                "lastSeen": _last_seen_iso(last_seen),
                "ai1": cached.get("ai1", r.ai1 if r.ai1 is not None else ""),
                "ai2": cached.get("ai2", r.ai2 if r.ai2 is not None else ""),
                "ai3": cached.get("ai3", r.ai3 if r.ai3 is not None else ""),
                "ai4": cached.get("ai4", r.ai4 if r.ai4 is not None else ""),
                "ao1": cached.get("ao1", r.ao1 if r.ao1 is not None else ""),
                "ao2": cached.get("ao2", r.ao2 if r.ao2 is not None else ""),
            }
        )

    # ---- TP-4000 ----
    rows_tp4000 = (
        db.query(TP4000Device)
        .filter(TP4000Device.claimed_by_user_id == owner_user_id)
        .order_by(TP4000Device.id.asc())
        .all()
    )

    for r in rows_tp4000:
        if not _device_allowed(wanted, "tp4000", r.device_id):
            continue

        last_seen = r.last_seen
        status = _compute_online_status(last_seen)
        online = status == "online"

        out.append(
            {
                "model": "TP4000",
                "deviceModel": "TP4000",
                "device_model": "tp4000",
                "bindModel": "tp4000",
                "deviceId": r.device_id,
                "device_id": r.device_id,
                "addedAt": r.claimed_at.isoformat() if r.claimed_at else "—",
                "ownedBy": r.claimed_by_email or "—",
                "status": status,
                "online": online,
                "is_online": online,
                "lastSeen": _last_seen_iso(last_seen),
                "te101": r.te101 if r.te101 is not None else "",
                "te102": r.te102 if r.te102 is not None else "",
                "te103": r.te103 if r.te103 is not None else "",
                "te104": r.te104 if r.te104 is not None else "",
                "te105": r.te105 if r.te105 is not None else "",
                "te106": r.te106 if r.te106 is not None else "",
                "te107": r.te107 if r.te107 is not None else "",
                "te108": r.te108 if r.te108 is not None else "",
            }
        )

    # ---- CFR100 / Radar Level Sensor ----
    rows_radar = (
        db.query(RadarLevelSensor)
        .filter(RadarLevelSensor.user_id == owner_user_id)
        .order_by(RadarLevelSensor.id.asc())
        .all()
    )

    for r in rows_radar:
        imei = _normalize_public_device_id(r.raw_imei_bytes)

        if not _device_allowed(wanted, "cfr100", imei):
            continue

        status = _compute_online_status(r.received_at)
        online = status == "online"

        temperature_c = (
            float(r.temperature_c) if r.temperature_c is not None else None
        )
        battery_v = float(r.battery_v) if r.battery_v is not None else None

        out.append(
            {
                "model": "cfr100",
                "deviceModel": "CFR100",
                "device_model": "cfr100",
                "bindModel": "cfr100",
                "deviceId": imei,
                "device_id": imei,
                "raw_imei_bytes": imei,
                "imei": imei,
                "status": status,
                "online": online,
                "is_online": online,
                "lastSeen": _last_seen_iso(r.received_at),
                "received_at": r.received_at.isoformat() if r.received_at else None,
                "height_mm": r.height_mm,
                "height": r.height_mm,
                "temperature_c": temperature_c,
                "temperature": temperature_c,
                "battery_v": battery_v,
                "battery": battery_v,
                "user_claimed_at": (
                    r.user_claimed_at.isoformat() if r.user_claimed_at else None
                ),
                "sensor_added_at": (
                    r.sensor_added_at.isoformat() if r.sensor_added_at else None
                ),
            }
        )

    return out


def _resolve_public_tenant_dashboard(
    db: Session,
    dashboard_slug: str,
    public_launch_id: str,
    tenant_email: str,
) -> CustomerDashboard:
    clean_slug = str(dashboard_slug or "").strip()
    clean_public_id = str(public_launch_id or "").strip()
    clean_email = str(tenant_email or "").strip().lower()

    if not clean_slug or not clean_public_id or not clean_email:
        raise HTTPException(
            status_code=400,
            detail="Missing tenant public access parameters.",
        )

    dashboard = (
        db.query(CustomerDashboard)
        .filter(CustomerDashboard.public_launch_id == clean_public_id)
        .filter(CustomerDashboard.dashboard_slug == clean_slug)
        .filter(CustomerDashboard.is_public_launch_enabled.is_(True))
        .first()
    )

    if not dashboard:
        raise HTTPException(status_code=404, detail="Public dashboard not found.")

    tenant = (
        db.query(TenantUser)
        .filter(TenantUser.owner_user_id == dashboard.user_id)
        .filter(TenantUser.customer_name.ilike(dashboard.customer_name))
        .filter(TenantUser.email.ilike(clean_email))
        .filter(TenantUser.is_active.is_(True))
        .first()
    )

    if not tenant:
        raise HTTPException(
            status_code=403,
            detail="Tenant user not authorized for this dashboard.",
        )

    has_access = (
        db.query(TenantUserDashboardAccess.id)
        .filter(TenantUserDashboardAccess.tenant_user_id == tenant.id)
        .filter(TenantUserDashboardAccess.dashboard_id == dashboard.id)
        .first()
    )

    if not has_access:
        raise HTTPException(
            status_code=403,
            detail="Tenant user not authorized for this dashboard.",
        )

    return dashboard


def _resolve_public_tenant_owner_user_id(
    db: Session,
    dashboard_slug: str,
    public_launch_id: str,
    tenant_email: str,
) -> int:
    dashboard = _resolve_public_tenant_dashboard(
        db=db,
        dashboard_slug=dashboard_slug,
        public_launch_id=public_launch_id,
        tenant_email=tenant_email,
    )
    return dashboard.user_id


@app.get("/devices")
def list_devices(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _append_claimed_devices_for_owner(db, current_user.id)


@app.get("/tenant-access/devices")
def list_tenant_public_devices(
    dashboard_slug: str,
    public_launch_id: str,
    tenant_email: str,
    db: Session = Depends(get_db),
):
    dashboard = _resolve_public_tenant_dashboard(
        db=db,
        dashboard_slug=dashboard_slug,
        public_launch_id=public_launch_id,
        tenant_email=tenant_email,
    )

    wanted = _extract_bound_devices_from_dashboard_layout(dashboard.layout)

    return _append_claimed_devices_for_owner(
        db=db,
        owner_user_id=dashboard.user_id,
        wanted=wanted,
    )