from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ========================================
# 🗄 DATABASE SETUP (REQUIRED FOR POSTGRES)
# ========================================
from database import Base, engine   # <-- NEW (important)

# Create all tables in PostgreSQL automatically
Base.metadata.create_all(bind=engine)   # <-- NEW (critical fix)

# ========================================
# 🚀 FASTAPI APP
# ========================================
app = FastAPI()

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
app.include_router(auth_router, prefix="")      # Exposes /login and /register


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
