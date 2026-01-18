# models.py
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Boolean
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
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

    # 🔐 Control & Automation Terms Acceptance (REGISTER PAGE)
    # NOTE: Use func.false() for a clean Postgres boolean default
    accepted_control_terms = Column(Boolean, nullable=False, server_default=func.false())
    control_terms_version = Column(String(20), nullable=True)
    control_terms_accepted_at = Column(
        DateTime(timezone=True),
        nullable=True,
    )

    # ✅ optional: convenient 1-to-1 relationship to profile
    profile = relationship(
        "UserProfile",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # ✅ one user -> many customer locations
    customer_locations = relationship(
        "CustomerLocation",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # ✅ one user -> many image assets (Cloudinary library)
    images = relationship(
        "ImageAsset",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# ===============================
# 🧾 USER PROFILE (Optional info)
# Saved ONLY when user clicks Save Changes
# ===============================
class UserProfile(Base):
    __tablename__ = "user_profiles"

    id = Column(Integer, primary_key=True, index=True)

    # ✅ One profile per user
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )

    full_name = Column(String(120), nullable=True)
    role_position = Column(String(120), nullable=True)

    # NOTE: This is profile email (can differ from login email if you want)
    email = Column(String(200), nullable=True)

    company = Column(String(160), nullable=True)
    company_address = Column(String(240), nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    user = relationship("User", back_populates="profile")


# ===============================
# 🏢 CUSTOMER / LOCATION MODEL
# Each user can save many customer sites.
# Future: can be linked to dashboards + map pins.
# ===============================
class CustomerLocation(Base):
    __tablename__ = "customer_locations"

    id = Column(Integer, primary_key=True, index=True)

    # 🔑 owner user (who created this location)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Basic customer/site info
    customer_name = Column(String(160), nullable=False)
    site_name = Column(String(160), nullable=False)

    # Address fields
    street = Column(String(200), nullable=False)
    city = Column(String(120), nullable=False)
    state = Column(String(120), nullable=False)
    zip = Column(String(30), nullable=False)
    country = Column(String(120), nullable=False, server_default="United States")

    # Optional notes
    notes = Column(String(500), nullable=True)

    # ✅ Backend-geocoded coordinates (stored in DB)
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)

    # ✅ Geocode tracking (helps debug + avoids confusion)
    # Examples: "ok", "no_results", "error"
    geocode_status = Column(String(60), nullable=True)
    geocoded_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    user = relationship("User", back_populates="customer_locations")


# ===============================
# 🖼 IMAGE ASSETS (Cloudinary)
# Each user can store many images in their library.
# Only URLs/public_ids are stored here (images live in Cloudinary).
# ===============================
class ImageAsset(Base):
    __tablename__ = "image_assets"

    id = Column(Integer, primary_key=True, index=True)

    # 🔑 owner user (who uploaded it)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ✅ Cloudinary info
    url = Column(String(700), nullable=False)
    public_id = Column(String(400), nullable=False, index=True)

    # optional grouping/folder label
    folder = Column(String(250), nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    user = relationship("User", back_populates="images")


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
# 📊 MAIN DASHBOARD MODEL
# ===============================
class MainDashboard(Base):
    __tablename__ = "main_dashboard"

    # 🔑 One dashboard per user (for now)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )

    # 🧱 Full dashboard layout (React canvas state)
    layout = Column(JSONB, nullable=False)

    # 🕒 Auto-updated timestamp
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
