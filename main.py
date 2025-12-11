from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ========================================
# 🗄 IMPORT MODELS FIRST (CRITICAL)
# ========================================
import models                     # <-- MUST be imported before Base.metadata
from database import Base, engine


# ========================================
# 🚀 FASTAPI APP
# ========================================
app = FastAPI()


# ========================================
# 🗄 CREATE TABLES ON STARTUP
# ========================================
@app.on_event("startup")
def startup_event():
    print(">>> Loading models and creating tables...")
    Base.metadata.create_all(bind=engine)
    print(">>> Tables created successfully!")


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
# 🔐 AUTH ROUTES  (LOAD AFTER STARTUP DECLARATION)
# ========================================
from auth_routes import router as auth_router
app.include_router(auth_router)


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
