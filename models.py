# models.py
from sqlalchemy import Column, Integer, String, Float, DateTime
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

    # ✅ MUST LIMIT LENGTH — bcrypt hashes are 60 bytes
    #    Give extra room, but enforce safe max
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
