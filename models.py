# models.py
from sqlalchemy import Column, Integer, String, Float, DateTime
import datetime

# ✅ Import the SAME Base used by database.py
from database import Base

# ===============================
# 👤 USER MODEL (Authentication)
# ===============================
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    company = Column(String, nullable=True)
    email = Column(String, nullable=False, unique=True, index=True)
    hashed_password = Column(String, nullable=False)


# ===============================
# 📡 DEVICE MODEL (Telemetry)
# ===============================
class Device(Base):
    __tablename__ = "devices"

    imei = Column(String, primary_key=True)
    level = Column(Float)
    temperature = Column(Float)
    battery = Column(Float)
    last_update = Column(DateTime, default=datetime.datetime.utcnow)
