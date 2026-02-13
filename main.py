# main.py
from fastapi import FastAPI, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR
from sqlalchemy.orm import Session

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

# ========================================
# 🚀 FASTAPI APP
# ========================================
app = FastAPI(title="CoreFlex API", version="1.0.0")

# ========================================
# ✅ CREATE TABLES + INIT CLOUDINARY + START COUNTER TICK
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
        # Don't crash the whole app if Cloudinary is misconfigured;
        # you will still see the error in Render logs.
        print("❌ Cloudinary init failed:", repr(e))

    # 3) ✅ Start persistent counter engine (keeps counting even if UI is closed)
    try:
        start_device_counters_tick()
    except Exception as e:
        print("❌ start_device_counters_tick failed:", repr(e))


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
# 🌍 CORS (PRODUCTION SAFE)
# ✅ FIX: Your frontend is https://coreflexiotsplatform.com (ONE "i" after flex)
# so we must allow BOTH:
#   coreflexiotsplatform.com  and  coreflexiiotsplatform.com
# plus www variants, plus localhost
# ========================================
ALLOWED_ORIGINS = [
    # ✅ CURRENT FRONTEND DOMAIN (from your src/config/api.js)
    "https://coreflexiotsplatform.com",
    "https://www.coreflexiotsplatform.com",
    # ✅ KEEP THESE (older/alternate domain spelling)
    "https://coreflexiiotsplatform.com",
    "https://www.coreflexiiotsplatform.com",
    # ✅ Local dev
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
# 🖼 IMAGES ROUTES (Cloudinary Image Library)
# ========================================
from routers.images import router as images_router  # noqa: E402

app.include_router(images_router)

# ========================================
# ✅ ZHC1921 DEVICES ROUTES (CF-2000)
# endpoints:
#   GET    /zhc1921/devices          (OWNER)
#   POST   /zhc1921/devices          (OWNER)
#   POST   /zhc1921/claim            (USER)
#   DELETE /zhc1921/unclaim/{id}     (USER)
#   GET    /zhc1921/my-devices       (USER)
# ========================================
from routers.zhc1921_devices import router as zhc1921_router  # noqa: E402

app.include_router(zhc1921_router)

# ========================================
# ✅ ZHC1661 DEVICES ROUTES (CF-1600)
# endpoints:
#   GET    /zhc1661/devices          (OWNER)
#   POST   /zhc1661/devices          (OWNER)
#   POST   /zhc1661/claim            (USER)
#   DELETE /zhc1661/unclaim/{id}     (USER)
#   GET    /zhc1661/my-devices       (USER)
# ========================================
from routers.zhc1661_devices import router as zhc1661_router  # noqa: E402

app.include_router(zhc1661_router)

# ========================================
# ✅ TP-4000 DEVICES ROUTES
# endpoints (pattern identical to zhc1661):
#   GET    /tp4000/devices           (OWNER)
#   POST   /tp4000/devices           (OWNER)
#   POST   /tp4000/claim             (USER)
#   DELETE /tp4000/unclaim/{id}      (USER)
#   GET    /tp4000/my-devices        (USER)
# ========================================
from routers.tp4000_devices import router as tp4000_router  # noqa: E402

app.include_router(tp4000_router)

# ========================================
# ✅ DEVICE COUNTERS ROUTES (PERSISTENT COUNTERS)
# endpoints:
#   GET    /device-counters
#   GET    /device-counters/by-widget/{widget_id}
#   POST   /device-counters/upsert
#   POST   /device-counters/reset
#   DELETE /device-counters?widget_id=...&dashboard_id=...
# ========================================
from routers.device_counters import router as device_counters_router  # noqa: E402

app.include_router(device_counters_router)

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
from models import ZHC1921Device, ZHC1661Device, TP4000Device, User  # noqa: E402


@app.get("/devices")
def list_devices(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    out = []

    # ---- ZHC1921 (CF-2000) ----
    rows_1921 = (
        db.query(ZHC1921Device)
        .filter(ZHC1921Device.claimed_by_user_id == current_user.id)
        .order_by(ZHC1921Device.id.asc())
        .all()
    )
    for r in rows_1921:
        out.append(
            {
                "model": "ZHC1921",
                "deviceId": r.device_id,
                "addedAt": r.claimed_at.isoformat() if r.claimed_at else "—",
                "ownedBy": r.claimed_by_email or "—",
                "status": r.status or "offline",
                "lastSeen": r.last_seen.isoformat() if r.last_seen else "—",
                "in1": int(r.di1 or 0),
                "in2": int(r.di2 or 0),
                "in3": int(r.di3 or 0),
                "in4": int(r.di4 or 0),
                "do1": int(r.do1 or 0),
                "do2": int(r.do2 or 0),
                "do3": int(r.do3 or 0),
                "do4": int(r.do4 or 0),
                "ai1": r.ai1 if r.ai1 is not None else "",
                "ai2": r.ai2 if r.ai2 is not None else "",
                "ai3": r.ai3 if r.ai3 is not None else "",
                "ai4": r.ai4 if r.ai4 is not None else "",
            }
        )

    # ---- ZHC1661 (CF-1600) ----
    rows_1661 = (
        db.query(ZHC1661Device)
        .filter(ZHC1661Device.claimed_by_user_id == current_user.id)
        .order_by(ZHC1661Device.id.asc())
        .all()
    )
    for r in rows_1661:
        out.append(
            {
                "model": "ZHC1661",
                "deviceId": r.device_id,
                "addedAt": r.claimed_at.isoformat() if r.claimed_at else "—",
                "ownedBy": r.claimed_by_email or "—",
                "status": r.status or "offline",
                "lastSeen": r.last_seen.isoformat() if r.last_seen else "—",
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
        .filter(TP4000Device.claimed_by_user_id == current_user.id)
        .order_by(TP4000Device.id.asc())
        .all()
    )
    for r in rows_tp4000:
        out.append(
            {
                "model": "TP4000",
                "deviceId": r.device_id,
                "addedAt": r.claimed_at.isoformat() if r.claimed_at else "—",
                "ownedBy": r.claimed_by_email or "—",
                "status": r.status or "offline",
                "lastSeen": r.last_seen.isoformat() if r.last_seen else "—",
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
