# models.py
from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Float,
    DateTime,
    ForeignKey,
    Boolean,
    UniqueConstraint,
    Index,
)
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
    accepted_control_terms = Column(
        Boolean, nullable=False, server_default=func.false()
    )
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

    # ✅ NEW: one user -> many password reset codes
    password_reset_codes = relationship(
        "PasswordResetCode",
        back_populates="user",
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

    # ✅ one user -> many customer dashboards
    customer_dashboards = relationship(
        "CustomerDashboard",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # ✅ NEW: one user/admin -> many tenant users
    tenant_users = relationship(
        "TenantUser",
        back_populates="owner_user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # ✅ one user -> many claimed ZHC1921 devices
    zhc1921_devices = relationship(
        "ZHC1921Device",
        back_populates="claimed_by_user",
        passive_deletes=True,
    )

    # ✅ one user -> many claimed ZHC1661 devices
    zhc1661_devices = relationship(
        "ZHC1661Device",
        back_populates="claimed_by_user",
        passive_deletes=True,
    )

    # ✅ one user -> many claimed TP4000 devices
    tp4000_devices = relationship(
        "TP4000Device",
        back_populates="claimed_by_user",
        passive_deletes=True,
    )

    # ✅ one user -> many control bindings (Toggle / Push NO / Push NC, etc.)
    control_bindings = relationship(
        "ControlBinding",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # ✅ one user -> many graphic display bindings
    graphic_display_bindings = relationship(
        "GraphicDisplayBinding",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # ✅ NEW: one user -> many alarm log windows
    alarm_log_windows = relationship(
        "AlarmLogWindow",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# ===============================
# 🔐 PASSWORD RESET CODES
# Separate table for forgot-password flow
# One user -> many reset code rows
# ===============================
class PasswordResetCode(Base):
    __tablename__ = "password_reset_codes"

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    email = Column(String(255), nullable=False, index=True)
    code_hash = Column(String(255), nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    used = Column(Boolean, nullable=False, server_default=func.false())
    attempt_count = Column(Integer, nullable=False, server_default="0")
    created_at = Column(
        DateTime,
        nullable=False,
        server_default=func.now(),
    )

    user = relationship("User", back_populates="password_reset_codes")


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
# 🗂 DEVICE REGISTRY
# Central source of truth for:
# - device_id (serial number)
# - device_model
# - device_mac (stored lowercase)
# ===============================
class DeviceRegistry(Base):
    __tablename__ = "device_registry"

    id = Column(Integer, primary_key=True, index=True)

    # serial number / logical device identity
    device_id = Column(String(64), nullable=False, unique=True, index=True)

    # zhc1921 / zhc1661 / tp4000
    device_model = Column(String(64), nullable=False, index=True)

    # IMPORTANT: backend should always normalize/store lowercase
    device_mac = Column(String(32), nullable=False, unique=True, index=True)

    claimed_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    is_claimed = Column(Boolean, nullable=False, server_default=func.false())

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

    claimed_by_user = relationship("User")


# ===============================
# 🌐 GATEWAY DEVICE SEEN
# Stores gateway heartbeat / discovery data
# ONLY for devices whose MAC matches device_registry
# One row per (device_registry_id + gateway_id)
# ===============================
class GatewayDeviceSeen(Base):
    __tablename__ = "gateway_device_seen"

    id = Column(Integer, primary_key=True, index=True)

    device_registry_id = Column(
        Integer,
        ForeignKey("device_registry.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # copied from device_registry for fast reads / snapshots
    device_id = Column(String(64), nullable=False, index=True)
    device_model = Column(String(64), nullable=False, index=True)
    device_mac = Column(String(32), nullable=False, index=True)

    # gateway info
    gateway_id = Column(String(120), nullable=False, index=True)
    gateway_hostname = Column(String(120), nullable=True)
    gateway_tailscale_ip = Column(String(64), nullable=False)
    gateway_interface = Column(String(50), nullable=True)

    # device on local gateway LAN
    device_local_ip = Column(String(64), nullable=True)
    neighbor_state = Column(String(32), nullable=True)

    # heartbeat / status
    first_seen = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    last_seen = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    status = Column(String(32), nullable=False, server_default="online")

    # raw JSON sent by gateway
    raw_payload = Column(JSONB, nullable=True)

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

    __table_args__ = (
        UniqueConstraint(
            "device_registry_id",
            "gateway_id",
            name="uq_gateway_device_seen_registry_gateway",
        ),
        Index("ix_gateway_device_seen_mac", "device_mac"),
        Index("ix_gateway_device_seen_gateway_id", "gateway_id"),
        Index("ix_gateway_device_seen_last_seen", "last_seen"),
    )

    device_registry = relationship("DeviceRegistry")



# ===============================
# 🧾 ZHC1921 DEVICES TABLE (CF-2000)
# Authorized by OWNER, then claimed by a USER
# Live DI/DO/AI/status updated by Node-RED later
# ===============================
class ZHC1921Device(Base):
    __tablename__ = "zhc1921_devices"

    id = Column(Integer, primary_key=True, index=True)

    # ✅ owner adds this (unique)
    device_id = Column(String(64), unique=True, nullable=False, index=True)

    # ✅ when owner authorized/added
    authorized_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # ✅ when any user claims/uses it
    claimed_at = Column(DateTime(timezone=True), nullable=True)

    claimed_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    claimed_by_email = Column(String(120), nullable=True, index=True)

    # ✅ polled from Node-RED
    status = Column(String(32), nullable=False, server_default="offline")
    last_seen = Column(DateTime(timezone=True), nullable=True)

    # Digital Inputs (DI) ✅ ZHC1921 has 6 DI
    di1 = Column(Integer, nullable=False, server_default="0")
    di2 = Column(Integer, nullable=False, server_default="0")
    di3 = Column(Integer, nullable=False, server_default="0")
    di4 = Column(Integer, nullable=False, server_default="0")
    di5 = Column(Integer, nullable=False, server_default="0")
    di6 = Column(Integer, nullable=False, server_default="0")

    # Digital Outputs (DO)
    do1 = Column(Integer, nullable=False, server_default="0")
    do2 = Column(Integer, nullable=False, server_default="0")
    do3 = Column(Integer, nullable=False, server_default="0")
    do4 = Column(Integer, nullable=False, server_default="0")

    # Analog Inputs (AI)
    ai1 = Column(Float, nullable=True)
    ai2 = Column(Float, nullable=True)
    ai3 = Column(Float, nullable=True)
    ai4 = Column(Float, nullable=True)

    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    claimed_by_user = relationship("User")
    claimed_by_user = relationship("User", back_populates="zhc1921_devices")


# ===============================
# 🧾 ZHC1661 DEVICES TABLE (CF-1600)
# Authorized by OWNER, then claimed by a USER
# Live AI/AO/status updated by Node-RED later
# ===============================
class ZHC1661Device(Base):
    __tablename__ = "zhc1661_devices"

    id = Column(Integer, primary_key=True, index=True)

    # ✅ owner adds this (unique)
    device_id = Column(String(64), unique=True, nullable=False, index=True)

    # ✅ when owner authorized/added
    authorized_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # ✅ when any user claims/uses it
    claimed_at = Column(DateTime(timezone=True), nullable=True)

    claimed_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    claimed_by_email = Column(String(120), nullable=True, index=True)

    # ✅ polled from Node-RED
    status = Column(String(32), nullable=False, server_default="offline")
    last_seen = Column(DateTime(timezone=True), nullable=True)

    # Analog Inputs (AI) - 4 channels
    ai1 = Column(Float, nullable=True)
    ai2 = Column(Float, nullable=True)
    ai3 = Column(Float, nullable=True)
    ai4 = Column(Float, nullable=True)

    # Analog Outputs (AO) - 2 channels
    ao1 = Column(Float, nullable=True)
    ao2 = Column(Float, nullable=True)

    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    claimed_by_user = relationship("User", back_populates="zhc1661_devices")


# ===============================
# 🧾 TP-4000 DEVICES TABLE
# Authorized by OWNER, then claimed by a USER
# Live TE-101..TE-108/status updated by Node-RED later
# ===============================
class TP4000Device(Base):
    __tablename__ = "tp4000_devices"

    id = Column(Integer, primary_key=True, index=True)

    # ✅ owner adds this (unique)
    device_id = Column(String(64), unique=True, nullable=False, index=True)

    # ✅ when owner authorized/added
    authorized_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # ✅ when any user claims/uses it
    claimed_at = Column(DateTime(timezone=True), nullable=True)

    claimed_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    claimed_by_email = Column(String(120), nullable=True, index=True)

    # ✅ polled from Node-RED
    status = Column(String(32), nullable=False, server_default="offline")
    last_seen = Column(DateTime(timezone=True), nullable=True)

    # Temperature Elements (TE) - 8 channels
    te101 = Column(Float, nullable=True)
    te102 = Column(Float, nullable=True)
    te103 = Column(Float, nullable=True)
    te104 = Column(Float, nullable=True)
    te105 = Column(Float, nullable=True)
    te106 = Column(Float, nullable=True)
    te107 = Column(Float, nullable=True)
    te108 = Column(Float, nullable=True)

    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    claimed_by_user = relationship("User", back_populates="tp4000_devices")


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


# ===============================
# 🧩 CUSTOMER DASHBOARDS
# One user -> many customer dashboards (tenant isolated)
# Table name = customers_dashboards
# ===============================
class CustomerDashboard(Base):
    __tablename__ = "customers_dashboards"

    id = Column(Integer, primary_key=True, index=True)

    # 🔑 owner user
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # customer label (for now store name; later migrate to customer_id)
    customer_name = Column(String(160), nullable=False, index=True)

    # dashboard display name
    dashboard_name = Column(String(160), nullable=False)

    # ✅ NEW: slug for pretty public URL
    dashboard_slug = Column(String(220), nullable=True, index=True)

    # ✅ NEW: unique public launch token
    public_launch_id = Column(String(64), nullable=True, unique=True, index=True)

    # ✅ NEW: whether public launch is enabled
    is_public_launch_enabled = Column(
        Boolean,
        nullable=False,
        server_default=func.false(),
    )

    # 🧱 saved layout (same style as main dashboard)
    layout = Column(JSONB, nullable=False, default=dict)

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

    user = relationship("User", back_populates="customer_dashboards")

    # ✅ NEW: dashboard can be assigned to many tenant users
    tenant_access_rows = relationship(
        "TenantUserDashboardAccess",
        back_populates="dashboard",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# ===============================
# 👥 TENANT USERS
# Child users created by an admin/owner user
# Password is stored HASHED ONLY
# ===============================
class TenantUser(Base):
    __tablename__ = "tenant_users"

    id = Column(BigInteger, primary_key=True, index=True)

    owner_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    customer_name = Column(String(160), nullable=False, index=True)
    full_name = Column(String(160), nullable=False)
    email = Column(String(255), nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)

    # read | read_control
    access_level = Column(String(32), nullable=False, server_default="read")

    is_active = Column(Boolean, nullable=False, server_default=func.true())
    must_change_password = Column(
        Boolean, nullable=False, server_default=func.true()
    )

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

    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "email",
            name="uq_tenant_users_owner_email",
        ),
    )

    owner_user = relationship("User", back_populates="tenant_users")

    dashboard_access = relationship(
        "TenantUserDashboardAccess",
        back_populates="tenant_user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# ===============================
# 🔐 TENANT USER DASHBOARD ACCESS
# Which dashboards each tenant user can access
# ===============================
class TenantUserDashboardAccess(Base):
    __tablename__ = "tenant_user_dashboard_access"

    id = Column(BigInteger, primary_key=True, index=True)

    tenant_user_id = Column(
        BigInteger,
        ForeignKey("tenant_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    dashboard_id = Column(
        Integer,
        ForeignKey("customers_dashboards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_user_id",
            "dashboard_id",
            name="uq_tenant_user_dashboard_access",
        ),
    )

    tenant_user = relationship("TenantUser", back_populates="dashboard_access")
    dashboard = relationship("CustomerDashboard", back_populates="tenant_access_rows")


# ===============================
# 🎛 CONTROL BINDINGS (Toggle / Push NO / Push NC)
# One row per control widget instance that binds to a DO
# Enforces:
# - one widget row per (user + dashboard + widget)
# - one DO per user/device across all dashboards
# ===============================
class ControlBinding(Base):
    __tablename__ = "control_bindings"

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    dashboard_id = Column(String, nullable=False, index=True)
    # ✅ NEW: store dashboard display name so used-DO dropdowns can show
    # the dashboard NAME instead of only dashboard_id / numeric id
    dashboard_name = Column(String(160), nullable=True)

    widget_id = Column(String, nullable=False, index=True)

    # ✅ "toggle" | "push_no" | "push_nc" (future: selector, interlock, etc.)
    widget_type = Column(String, nullable=False, index=True)

    title = Column(String, nullable=True)

    bind_device_id = Column(String, nullable=True, index=True)
    bind_field = Column(String, nullable=True, index=True)  # do1..do4
    # ✅ NEW: display_output scaling
    scale_min = Column(Float, nullable=True)
    scale_max = Column(Float, nullable=True)
    ao_scale_min = Column(Float, nullable=True)
    ao_scale_max = Column(Float, nullable=True)

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "dashboard_id",
            "widget_id",
            name="uq_control_widget_once",
        ),
        # ✅ GLOBAL uniqueness per user/device/DO across ALL dashboards
        UniqueConstraint(
            "user_id",
            "bind_device_id",
            "bind_field",
            name="uq_control_do_once_per_user_device",
        ),
    )

    user = relationship("User", back_populates="control_bindings")


# ===============================
# 🔒 CONTROL ACTION LOCKS (prevents concurrent writes without holding DB connections)
# One row per device+field while an action is in progress
# Expires automatically via expires_at (TTL)
# ===============================
class ControlActionLock(Base):
    __tablename__ = "control_action_locks"

    id = Column(Integer, primary_key=True, index=True)

    # Unique lock key: "dev:<device_id>:<do1..do4>"
    lock_key = Column(String(200), nullable=False, unique=True, index=True)

    device_id = Column(String(80), nullable=False, index=True)
    field = Column(String(10), nullable=False, index=True)  # do1..do4

    # who triggered the lock (optional but useful)
    user_id = Column(Integer, nullable=True, index=True)

    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ===============================
# 📈 GRAPHIC DISPLAY BINDINGS (matches your DB table exactly)
# Table: public.graphic_display_bindings
# ===============================
class GraphicDisplayBinding(Base):
    __tablename__ = "graphic_display_bindings"

    id = Column(Integer, primary_key=True, index=True)

    # who / where
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dashboard_id = Column(String, nullable=False, index=True, server_default="main")
    widget_id = Column(String, nullable=False, index=True)

    # binding
    bind_model = Column(
        String, nullable=False, server_default="zhc1921"
    )  # zhc1921 / zhc1661 / tp4000
    bind_device_id = Column(String, nullable=False, index=True)
    bind_field = Column(
        String, nullable=False, server_default="ai1"
    )  # ai1/ai2/ai3/ai4...

    # display settings
    title = Column(String, nullable=False, server_default="Graphic Display")
    time_unit = Column(String, nullable=False, server_default="seconds")
    window_size = Column(Integer, nullable=False, server_default="60")
    sample_ms = Column(Integer, nullable=False, server_default="3000")
    y_min = Column(Float, nullable=False, server_default="0")
    y_max = Column(Float, nullable=False, server_default="100")
    line_color = Column(String, nullable=False, server_default="#0c5ac8")
    graph_style = Column(String, nullable=False, server_default="line")

    # math
    math_formula = Column(String, nullable=False, server_default="")

    # totalizer
    totalizer_enabled = Column(Boolean, nullable=False, server_default=func.false())
    totalizer_unit = Column(String, nullable=False, server_default="")

    # single units
    single_units_enabled = Column(Boolean, nullable=False, server_default=func.false())
    single_unit = Column(String, nullable=False, server_default="")

    # retention
    retention_days = Column(Integer, nullable=False, server_default="35")

    # soft control
    is_enabled = Column(Boolean, nullable=False, server_default=func.true())
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "dashboard_id",
            "widget_id",
            name="uq_gdb_user_dash_widget",
        ),
    )

    user = relationship("User", back_populates="graphic_display_bindings")


# ===============================
# 🚨 ALARM LOG WINDOWS
# One row per alarm log window per user/dashboard
# ===============================
class AlarmLogWindow(Base):
    __tablename__ = "alarm_log_windows"

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    dashboard_id = Column(
        String(255), nullable=False, server_default="main", index=True
    )

    # ✅ NEW: store dashboard display name
    dashboard_name = Column(
        String(255), nullable=True, server_default="Main Dashboard"
    )

    window_key = Column(
        String(100), nullable=False, server_default="alarmLog", index=True
    )
    title = Column(
        String(255), nullable=False, server_default="Alarms Log (DI-AI)"
    )

    pos_x = Column(Integer, nullable=False, server_default="140")
    pos_y = Column(Integer, nullable=False, server_default="90")
    width = Column(Integer, nullable=False, server_default="900")
    height = Column(Integer, nullable=False, server_default="420")

    is_open = Column(Boolean, nullable=False, server_default=func.true())
    is_minimized = Column(Boolean, nullable=False, server_default=func.false())
    is_launched = Column(Boolean, nullable=False, server_default=func.false())

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

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "dashboard_id",
            "window_key",
            name="uq_alarm_log_windows_user_dashboard_key",
        ),
    )

    user = relationship("User", back_populates="alarm_log_windows")


# ===============================
# 🚨 ALARM DEFINITIONS
# Stores alarm configuration created by users
# Alarm EVENTS will be stored later in AWS
# ===============================
class AlarmDefinition(Base):
    __tablename__ = "alarm_definitions"

    id = Column(Integer, primary_key=True, index=True)

    # owner of the alarm
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # device information
    device_id = Column(String(255), nullable=False, index=True)
    model = Column(String(120), nullable=True)

    # tag that triggers alarm
    tag = Column(String(120), nullable=False, index=True)

    # DI or AI
    alarm_type = Column(String(20), nullable=False)

    # ✅ for DI alarms
    contact_type = Column(String(5), nullable=True)

    # for AI alarms
    operator = Column(String(10), nullable=True)
    threshold = Column(Float, nullable=True)

    # optional math formula
    math_formula = Column(String, nullable=True)

    # ==========================================
    # 🔥 CRITICAL FIX: Alarm Log Isolation
    # ==========================================
    # Each Alarm Log window has its own key
    # This prevents alarms from mixing across logs
    alarm_log_key = Column(
        String(255),
        nullable=False,
        index=True,
        server_default="alarmLog",
    )

    # grouping / severity
    group_name = Column(String(120), nullable=True)
    severity = Column(String(50), nullable=True)

    # alarm message
    message = Column(String, nullable=False)

    # enable / disable
    enabled = Column(Boolean, nullable=False, server_default=func.true())

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

    # ==========================================
    # 🔥 OPTIONAL (GOOD PRACTICE)
    # ==========================================
    user = relationship("User")

    # ==========================================
    # 🔥 PERFORMANCE + DATA INTEGRITY
    # ==========================================
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "device_id",
            "tag",
            "alarm_log_key",
            name="uq_alarm_per_tag_per_log",
        ),
    )

# ===============================
# 💳 USER SUBSCRIPTIONS
# ===============================
class UserSubscription(Base):
    __tablename__ = "user_subscriptions"

    id = Column(BigInteger, primary_key=True, index=True)

    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        unique=True,
    )

    plan_key = Column(String(50), nullable=False, server_default="free")
    device_limit = Column(Integer, nullable=False, server_default="1")
    tenants_users_limit = Column(Integer, nullable=False, server_default="1")

    active_date = Column(DateTime(timezone=True), nullable=True)
    renewal_date = Column(DateTime(timezone=True), nullable=True)

    is_active = Column(Boolean, nullable=False, server_default=func.true())

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

    user = relationship("User")

# ===============================
# 🔐 USER ACTIVE SESSIONS
# Enforces one active browser/device per user
# Same browser_device_key = allowed
# Different browser_device_key = blocked while session is alive
# ===============================
class UserActiveSession(Base):
    __tablename__ = "user_active_sessions"

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    browser_device_key = Column(String(255), nullable=False, index=True)
    session_token = Column(String(255), nullable=False, unique=True, index=True)

    is_active = Column(Boolean, nullable=False, server_default=func.true())

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    last_seen_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    closed_at = Column(
        DateTime(timezone=True),
        nullable=True,
    )

    ip_address = Column(String(100), nullable=True)
    user_agent = Column(String, nullable=True)

    user = relationship("User")

    __table_args__ = (
        Index("ix_user_active_sessions_user_active", "user_id", "is_active"),
        Index("ix_user_active_sessions_user_last_seen", "user_id", "last_seen_at"),
    )