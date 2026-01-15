# main.py
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ========================================
# 🗄 IMPORT MODELS FIRST (CRITICAL)
# ========================================
import models  # noqa: F401
from database import Base, engine

# ========================================
# 🚀 FASTAPI APP
# ========================================
app = FastAPI(title="CoreFlex API", version="1.0.0")

# ========================================
# ✅ CREATE TABLES ON STARTUP (NOT PER REQUEST)
# ========================================
@app.on_event("startup")
def on_startup():
    try:
        Base.metadata.create_all(bind=engine)
        print("✅ DB tables ensured on startup")
    except Exception as e:
        print("❌ Startup create_all failed:", repr(e))

# ========================================
# 🌍 CORS (TEMP: OPEN FOR DEBUGGING)
# IMPORTANT: allow_credentials MUST be False with "*"
# ========================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
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
        status_code=500,
        content={"detail": "Internal Server Error", "error": repr(exc)},
    )

# ========================================
# 🔐 AUTH ROUTES
# ========================================
from auth_routes import router as auth_router
app.include_router(auth_router)

# ========================================
# 📊 MAIN DASHBOARD ROUTES
# ========================================
from routers.main_dashboard import router as main_dashboard_router
app.include_router(main_dashboard_router)

# ========================================
# 👤 USER PROFILE ROUTES
# ========================================
from routers.user_profile import router as user_profile_router
app.include_router(user_profile_router)

# ========================================
# 📍 CUSTOMER LOCATIONS ROUTES
# ========================================
from routers.customer_locations import router as customer_locations_router
app.include_router(customer_locations_router)

# ========================================
# ❤️ HEALTH CHECK
# ========================================
@app.get("/health")
def health():
    return {"ok": True, "status": "API running"}

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

@app.get("/devices")
def list_devices():
    return {"message": "Device database not enabled"}
