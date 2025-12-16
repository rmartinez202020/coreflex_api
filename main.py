from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ========================================
# 🗄 IMPORT MODELS FIRST (CRITICAL)
# ========================================
import models                     # Load models before Base metadata
from database import Base, engine


# ========================================
# 🚀 FASTAPI APP
# ========================================
app = FastAPI()


# ========================================
# 🗄 FORCE TABLE CREATION BEFORE EVERY REQUEST
#   (Fix for Render Free tier shutdown)
# ========================================
@app.middleware("http")
async def ensure_tables_exist(request, call_next):
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as e:
        print("❌ Error creating tables:", e)

    response = await call_next(request)
    return response


# ========================================
# 🌍 CORS SETTINGS
# ========================================
origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "https://coreflexiiotsplatform.com",
    "https://www.coreflexiiotsplatform.com",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ========================================
# 🔐 AUTH ROUTES
# ========================================
from auth_routes import router as auth_router
app.include_router(auth_router)


# ========================================
# 📊 MAIN DASHBOARD ROUTES  ✅ NEW
# ========================================
from routers.main_dashboard import router as main_dashboard_router
app.include_router(main_dashboard_router)


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
