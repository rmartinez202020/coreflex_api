# models.py
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
import datetime

# ✅ Import the SAME Base object from database.py
from database import Base

# ===============================
# 👤 USER MODEL (Authentication)
# ===============================
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False)
    company = Column(String(120), nullable=True)
    email = Column(String(120), nullable=False, unique=True, index=True)

    # ✅ bcrypt hashes are ~60 chars, give safe room
    hashed_password = Column(String(128), nullable=False)


# ===============================
# 📡 DEVICE MODEL (Telemetry)
# ===============================
class Device(Base):
    __tablename__ = "devices"

    imei = Column(String(50), primary_key=True)
    level = Column(Float)
    temperature = Column(Float)
    battery = Column(Float)
    last_update = Column(DateTime, default=datetime.datetime.utcnow)


# ===============================
# 📊 MAIN DASHBOARD MODEL (NEW)
# ===============================
class MainDashboard(Base):
    __tablename__ = "main_dashboard"

    # 🔑 One dashboard per user (for now)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        index=True
    )

    # 🧱 Full dashboard layout (React canvas state)
    layout = Column(JSONB, nullable=False)

    # 🕒 Auto-updated timestamp
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now()
    )
