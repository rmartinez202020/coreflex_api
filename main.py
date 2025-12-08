from fastapi import FastAPI
from pydantic import BaseModel
import datetime

# ========================================
# DATABASE SESSION
# ========================================
from database import SessionLocal

# ========================================
# 🚀 ENABLE CORS FOR REACT FRONTEND
# ========================================
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

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
# 🔐 IMPORT AUTH ROUTER (Register + Login)
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
# TEMP SENSOR ENDPOINT PLACEHOLDER
# (until real device backend is restored)
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
