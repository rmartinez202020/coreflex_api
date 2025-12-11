from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ========================================
# 🗄 DATABASE SETUP (REQUIRED FOR POSTGRES)
# ========================================
# IMPORTANT: import models FIRST so SQLAlchemy registers tables
from models import User          # <-- CRITICAL LINE
from database import Base, engine

# ========================================
# 🚀 FASTAPI APP
# ========================================
app = FastAPI()

# ========================================
# 🗄 CREATE TABLES ON STARTUP  (CRITICAL FIX)
# ========================================
@app.on_event("startup")
def create_tables():
    print(">>> Creating database tables...")
    try:
        Base.metadata.create_all(bind=engine)
        print(">>> Tables created successfully.")
    except Exception as e:
        print("❌ TABLE CREATION FAILED:", e)

# ========================================
# 🌍 CORS (Allow frontend URLs)
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
# 🔐 AUTH ROUTES (Register + Login)
# ========================================
from auth_routes import router as auth_router
app.include_router(auth_router)       # exposes /login and /register

# ========================================
# ❤️ HEALTH CHECK
# ========================================
@app.get("/health")
def health():
    return {"ok": True, "status": "API running"}

# ========================================
# 📡 TEMP SENSOR ENDPOINT PLACEHOLDER
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
