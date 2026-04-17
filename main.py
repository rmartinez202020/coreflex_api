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

# ========================================
# 🗄 IMPORT MODELS FIRST (CRITICAL)
# ========================================
import models  # noqa: F401
from database import Base, engine, get_db

# ========================================
# ☁️ CLOUDINARY INIT
# ========================================
from cloudinary_config import init_cloudinary  # noqa: E402

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
# ✅ Keep exact frontend origins
# ✅ Keep regex too
# ✅ Add explicit OPTIONS fallback below for stubborn preflight cases
# ========================================
ALLOWED_ORIGINS = [
    # ✅ CURRENT LIVE FRONTEND
    "https://www.coreflexiiotsplatform.com",
    "https://coreflexiiotsplatform.com",
    "http://www.coreflexiiotsplatform.com",
    "http://coreflexiiotsplatform.com",
    # ✅ optional older/alternate spelling support
    "https://www.coreflexiotsplatform.com",
    "https://coreflexiotsplatform.com",
    "http://www.coreflexiotsplatform.com",
    "http://coreflexiotsplatform.com",
    # ✅ local dev
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

# ========================================
# ✅ EXPLICIT OPTIONS FALLBACK
# Helps if browser preflight is still stubborn on some routes/proxies
# ========================================
@app.options("/{full_path:path}")
async def options_preflight_handler(full_path: str, request: Request):
    origin = request.headers.get("origin", "")
    allow_origin = origin if origin in ALLOWED_ORIGINS else ""

    # allow regex match too
    if not allow_origin:
        import re

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


# ========================================
# ✅ CREATE TABLES + INIT CLOUDINARY + START COUNTER TICK + ALARM ENGINE
# ========================================
@app.on_event("startup")
async def on_startup():
    # 1) Ensure DB tables
    try:
        Base.metadata.create_all(bind=engine)
        print("✅ DB tables ensured on startup")
    except Exception as e:
        print("❌ Startup create_all failed:", repr(e))

    # 2) Init Cloudinary (reads Render env vars)
    try:
        init_cloudinary()
        print("✅ Cloudinary initialized on startup")
    except Exception as e:
        print("❌ Cloudinary init failed:", repr(e))

    # 3) ✅ Start persistent counter engine (keeps counting even if UI is closed)
    try:
        start_device_counters_tick()
    except Exception as e:
        print("❌ start_device_counters_tick failed:", repr(e))

    # 4) ✅ Start alarm engine background loop
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


# ========================================
# ✅ STOP BACKGROUND TASKS ON SHUTDOWN
# ========================================
@app.on_event("shutdown")
async def on_shutdown():
    try:
        await stop_device_counters_tick()
    except Exception as e:
        print("❌ stop_device_counters_tick failed:", repr(e))


# ========================================
# ✅ GLOBAL ERROR HANDLER (so you SEE real errors)
# ========================================
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


# ========================================
# 🔐 AUTH ROUTES
# ========================================
from auth_routes import router as auth_router  # noqa: E402

app.include_router(auth_router)

# ========================================
# 📊 MAIN DASHBOARD ROUTES
# ========================================
from routers.main_dashboard import router as main_dashboard_router  # noqa: E402

app.include_router(main_dashboard_router)

# ========================================
# 🧩 CUSTOMER DASHBOARDS ROUTES
# ========================================
from routers.customers_dashboards import router as customers_dashboards_router  # noqa: E402

app.include_router(customers_dashboards_router)

# ========================================
# 👤 USER PROFILE ROUTES
# ========================================
from routers.user_profile import router as user_profile_router  # noqa: E402

app.include_router(user_profile_router)

# ========================================
# 📍 CUSTOMER LOCATIONS ROUTES
# ========================================
from routers.customer_locations import router as customer_locations_router  # noqa: E402

app.include_router(customer_locations_router)

# ========================================
# 👥 TENANT USERS ROUTES
# ✅ IMPORTANT: this was missing, so /tenant-users was not being registered
# ========================================
from routers.tenant_users import router as tenant_users_router  # noqa: E402

app.include_router(tenant_users_router)

# ========================================
# 💳 USER SUBSCRIPTIONS ROUTES
# ========================================
from routers.user_subscriptions import router as user_subscriptions_router  # noqa: E402

app.include_router(user_subscriptions_router)

# ========================================
# 💳 USER BILLING ROUTES
# Stripe payment-intent route for Proceed to Payment modal
# ========================================
from routers.billing import router as billing_router  # noqa: E402

app.include_router(billing_router)

# ========================================
# 💳 ADMIN BILLING ROUTES
# Stripe billing plans / addons sync
# ========================================
from routers.billing_admin import router as billing_admin_router  # noqa: E402

app.include_router(billing_admin_router)

# ========================================
# 🖼 IMAGES ROUTES (Cloudinary Image Library)
# ========================================
from routers.images import router as images_router  # noqa: E402

app.include_router(images_router)

# ========================================
# ✅ DEVICE REGISTRY ROUTES
# Central table for:
# - device_id
# - device_model
# - device_mac
# ========================================
from routers.device_registry import router as device_registry_router  # noqa: E402

app.include_router(device_registry_router)

# ========================================
# ✅ GATEWAY DEVICE SEEN ROUTES
# Receives gateway heartbeat / device-seen JSON
# Only stores rows when MAC exists in device_registry
# ========================================
from routers.gateway_device_seen import (  # noqa: E402
    router as gateway_device_seen_router,
)

app.include_router(gateway_device_seen_router)

# ========================================
# ✅ ZHC1921 DEVICES ROUTES (CF-2000)
# ========================================
from routers.zhc1921_devices import router as zhc1921_router  # noqa: E402

app.include_router(zhc1921_router)

# ========================================
# ✅ ZHC1661 DEVICES ROUTES (CF-1600)
# ========================================
from routers.zhc1661_devices import router as zhc1661_router  # noqa: E402

app.include_router(zhc1661_router)

# ========================================
# ✅ TP-4000 DEVICES ROUTES
# ========================================
from routers.tp4000_devices import router as tp4000_router  # noqa: E402

app.include_router(tp4000_router)

# ========================================
# ✅ DEVICE COUNTERS ROUTES (PERSISTENT COUNTERS)
# ========================================
from routers.device_counters import router as device_counters_router  # noqa: E402

app.include_router(device_counters_router)

# ========================================
# ✅ CONTROL BINDINGS ROUTES (DO UNIQUE PER DASHBOARD)
# ========================================
from routers.control_bindings import router as control_bindings_router  # noqa: E402

app.include_router(control_bindings_router)

# ========================================
# ✅ NODE-RED GRAPHICS ROUTES + HELPERS
# endpoints:
#   GET /node-red/ping
# (helpers used internally to start streams from Apply route)
# ========================================
from routers.node_red_graphics import router as node_red_graphics_router  # noqa: E402

app.include_router(node_red_graphics_router)

# ========================================
# ✅ GRAPHIC DISPLAY BINDINGS ROUTES
# ========================================
from routers.graphic_display_bindings import (  # noqa: E402
    router as graphic_display_bindings_router,
)

app.include_router(graphic_display_bindings_router)

# ========================================
# ✅ ALARM LOG WINDOWS ROUTES
# ========================================
from routers.alarm_log_windows import router as alarm_log_windows_router  # noqa: E402

app.include_router(alarm_log_windows_router)

# ========================================
# ✅ ALARM DEFINITIONS ROUTES
# ========================================
from routers.alarm_definitions import router as alarm_definitions_router  # noqa: E402

app.include_router(alarm_definitions_router)

# ========================================
# ✅ ALARM HISTORY ROUTES
# ========================================
from routers.alarm_history import router as alarm_history_router  # noqa: E402

app.include_router(alarm_history_router)

# ========================================
# ❤️ HEALTH CHECK
# ========================================
@app.get("/health")
def health():
    return {"ok": True, "status": "API running"}


# ========================================
# 🧪 CORS TEST ENDPOINT
# ========================================
@app.get("/cors-test")
def cors_test():
    return {"ok": True, "message": "CORS working"}


# ========================================
# 📡 TEMP SENSOR ENDPOINT
# ========================================
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
# ✅ /devices (FRONTEND COMPAT)
# Return the current user's CLAIMED devices (ZHC1921 + ZHC1661 + TP4000)
# ========================================
from auth_utils import get_current_user  # noqa: E402
from models import (  # noqa: E402
    ZHC1921Device,
    ZHC1661Device,
    TP4000Device,
    User,
    TenantUser,
    TenantUserDashboardAccess,
    CustomerDashboard,
)
from utils.zhc1921_live_cache import get_latest as get_latest_zhc1921  # noqa: E402

OFFLINE_AFTER_SECONDS = int(os.getenv("COREFLEX_OFFLINE_AFTER_SECONDS") or "10")


def _as_utc(dt: datetime | None) -> datetime | None:
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _compute_online_status(last_seen: datetime | None) -> str:
    ls = _as_utc(last_seen)
    if not ls:
        return "offline"

    now = datetime.now(timezone.utc)
    age = (now - ls).total_seconds()
    return "online" if age <= OFFLINE_AFTER_SECONDS else "offline"


def _last_seen_iso(last_seen: datetime | None) -> str:
    ls = _as_utc(last_seen)
    return ls.isoformat() if ls else "—"


def _append_claimed_devices_for_owner(db: Session, owner_user_id: int):
    out = []

    # ---- ZHC1921 (CF-2000) ----
    rows_1921 = (
        db.query(ZHC1921Device)
        .filter(ZHC1921Device.claimed_by_user_id == owner_user_id)
        .order_by(ZHC1921Device.id.asc())
        .all()
    )
    for r in rows_1921:
        cached = get_latest_zhc1921(r.device_id) or {}
        cache_ls = cached.get("last_seen")
        last_seen = cache_ls if isinstance(cache_ls, datetime) else r.last_seen
        status = _compute_online_status(last_seen)
        online = status == "online"

        out.append(
            {
                "model": "ZHC1921",
                "deviceId": r.device_id,
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
        last_seen = r.last_seen
        status = _compute_online_status(last_seen)
        online = status == "online"

        out.append(
            {
                "model": "ZHC1661",
                "deviceId": r.device_id,
                "addedAt": r.claimed_at.isoformat() if r.claimed_at else "—",
                "ownedBy": r.claimed_by_email or "—",
                "status": status,
                "online": online,
                "is_online": online,
                "lastSeen": _last_seen_iso(last_seen),
                "ai1": r.ai1 if r.ai1 is not None else "",
                "ai2": r.ai2 if r.ai2 is not None else "",
                "ai3": r.ai3 if r.ai3 is not None else "",
                "ai4": r.ai4 if r.ai4 is not None else "",
                "ao1": r.ao1 if r.ao1 is not None else "",
                "ao2": r.ao2 if r.ao2 is not None else "",
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
        last_seen = r.last_seen
        status = _compute_online_status(last_seen)
        online = status == "online"

        out.append(
            {
                "model": "TP4000",
                "deviceId": r.device_id,
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

    return out


def _resolve_public_tenant_owner_user_id(
    db: Session,
    dashboard_slug: str,
    public_launch_id: str,
    tenant_email: str,
) -> int:
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
    owner_user_id = _resolve_public_tenant_owner_user_id(
        db=db,
        dashboard_slug=dashboard_slug,
        public_launch_id=public_launch_id,
        tenant_email=tenant_email,
    )
    return _append_claimed_devices_for_owner(db, owner_user_id)