# log_engine.py

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any


# ============================================================
# COREFLEX LOG ENGINE
# ============================================================
# Purpose:
# - Build one standard audit-log payload
# - Send important activity logs from FastAPI to Node-RED
# - Never interrupt the primary user action if logging fails
#
# Node-RED endpoint:
#   POST /coreflex/logs/write
#
# Required environment variable:
#   NODE_RED_BASE_URL
#
# Example:
#   NODE_RED_BASE_URL=http://98.90.225.131:1880
#
# The Log Engine automatically appends:
#   /coreflex/logs/write
#
# Optional environment variable:
#   COREFLEX_LOGS_API_KEY
# ============================================================


NODE_RED_BASE_URL_ENV = "NODE_RED_BASE_URL"
LOGS_API_KEY_ENV = "COREFLEX_LOGS_API_KEY"

LOGS_WRITE_PATH = "/coreflex/logs/write"

DEFAULT_TIMEOUT_SECONDS = 3.0


# ============================================================
# OFFICIAL LOG CATEGORIES
# ============================================================

LOG_CATEGORY_SECURITY = "SECURITY"
LOG_CATEGORY_DASHBOARD = "DASHBOARD"
LOG_CATEGORY_DEVICE = "DEVICE"
LOG_CATEGORY_USER = "USER"
LOG_CATEGORY_CONTROL = "CONTROL"
LOG_CATEGORY_SYSTEM = "SYSTEM"

VALID_LOG_CATEGORIES = {
    LOG_CATEGORY_SECURITY,
    LOG_CATEGORY_DASHBOARD,
    LOG_CATEGORY_DEVICE,
    LOG_CATEGORY_USER,
    LOG_CATEGORY_CONTROL,
    LOG_CATEGORY_SYSTEM,
}


# ============================================================
# OFFICIAL LOG STATUS VALUES
# ============================================================

LOG_STATUS_SUCCESS = "SUCCESS"
LOG_STATUS_FAILED = "FAILED"

VALID_LOG_STATUSES = {
    LOG_STATUS_SUCCESS,
    LOG_STATUS_FAILED,
}


# ============================================================
# OFFICIAL ACTOR TYPES
# ============================================================

LOG_ACTOR_OWNER = "OWNER"
LOG_ACTOR_TENANT = "TENANT"

VALID_LOG_ACTOR_TYPES = {
    LOG_ACTOR_OWNER,
    LOG_ACTOR_TENANT,
}


# ============================================================
# INTERNAL HELPERS
# ============================================================

def _utc_timestamp() -> str:
    """
    Return an ISO-8601 UTC timestamp.

    Example:
        2026-08-10T14:05:22.481Z
    """
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _clean_optional_text(value: Any) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    return text if text else None


def _clean_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_category(category: str) -> str:
    value = str(category or "").strip().upper()

    if value not in VALID_LOG_CATEGORIES:
        raise ValueError(f"Invalid log category: {category!r}")

    return value


def _normalize_status(status: str) -> str:
    value = str(status or "").strip().upper()

    if value not in VALID_LOG_STATUSES:
        raise ValueError(f"Invalid log status: {status!r}")

    return value


def _normalize_actor_type(actor_type: str | None) -> str:
    value = str(actor_type or LOG_ACTOR_OWNER).strip().upper()

    if value not in VALID_LOG_ACTOR_TYPES:
        raise ValueError(f"Invalid log actor_type: {actor_type!r}")

    return value


def _get_logs_write_url() -> str | None:
    """
    Build the Node-RED Logs endpoint from the existing
    NODE_RED_BASE_URL environment variable.

    Example:
        NODE_RED_BASE_URL=http://98.90.225.131:1880

    Result:
        http://98.90.225.131:1880/coreflex/logs/write
    """
    node_red_base_url = _clean_optional_text(
        os.getenv(NODE_RED_BASE_URL_ENV)
    )

    if not node_red_base_url:
        return None

    return f"{node_red_base_url.rstrip('/')}{LOGS_WRITE_PATH}"


# ============================================================
# PUBLIC LOG FUNCTION
# ============================================================

def send_log(
    *,
    user_id: int,
    user_email: str,
    category: str,
    action: str,
    status: str = LOG_STATUS_SUCCESS,
    message: str | None = None,

    # Actor identity
    # OWNER  -> normal CoreFlex account user
    # TENANT -> tenant user acting under the owner's account
    actor_type: str = LOG_ACTOR_OWNER,
    tenant_user_id: int | None = None,
    tenant_email: str | None = None,
    tenant_name: str | None = None,

    # Optional context
    customer_id: int | None = None,
    dashboard_id: int | str | None = None,
    device_id: str | None = None,
    gateway_id: str | None = None,

    # Optional control/change details
    field: str | None = None,
    old_value: Any = None,
    new_value: Any = None,

    # Optional request/security context
    ip_address: str | None = None,
    user_agent: str | None = None,

    # Optional timestamp override for special cases
    timestamp: str | None = None,
) -> bool:
    """
    Send one CoreFlex audit log to Node-RED.

    IMPORTANT OWNERSHIP RULE:
    -------------------------
    `user_id` and `user_email` identify the CoreFlex OWNER whose
    log file will store the event.

    For tenant activity:
      - user_id/user_email = the owning CoreFlex user
      - actor_type = "TENANT"
      - tenant_user_id / tenant_email / tenant_name identify the actor

    This guarantees that tenant activity is stored under the owner's
    logs and never becomes a separate cross-user log namespace.

    IMPORTANT SECURITY RULE:
    ------------------------
    `user_id` must come from trusted backend context:
      - current_user.id from JWT, or
      - an owner User row already resolved by the backend.

    Never trust a browser-provided user_id as the owner of a log.

    IMPORTANT RELIABILITY RULE:
    ---------------------------
    Logging is secondary to the primary platform action.
    If Node-RED is unavailable, this function returns False and does
    NOT raise an exception into the calling route.

    Returns:
        True  -> Node-RED accepted the log request with HTTP 2xx
        False -> log could not be sent
    """

    # A log must always belong to a real CoreFlex owner user.
    clean_user_id = _clean_optional_int(user_id)
    clean_user_email = _clean_optional_text(user_email)

    if clean_user_id is None:
        print("⚠️ LOG ENGINE: skipped log because user_id is missing/invalid")
        return False

    if not clean_user_email:
        print("⚠️ LOG ENGINE: skipped log because user_email is missing")
        return False

    try:
        clean_category = _normalize_category(category)
        clean_status = _normalize_status(status)
        clean_actor_type = _normalize_actor_type(actor_type)
    except ValueError as exc:
        print(f"⚠️ LOG ENGINE: {exc}")
        return False

    clean_action = _clean_optional_text(action)
    if not clean_action:
        print("⚠️ LOG ENGINE: skipped log because action is missing")
        return False

    # Tenant actor validation.
    clean_tenant_user_id = _clean_optional_int(tenant_user_id)
    clean_tenant_email = _clean_optional_text(tenant_email)
    clean_tenant_name = _clean_optional_text(tenant_name)

    if clean_actor_type == LOG_ACTOR_TENANT:
        if clean_tenant_user_id is None:
            print(
                "⚠️ LOG ENGINE: skipped tenant log because "
                "tenant_user_id is missing/invalid"
            )
            return False

        if not clean_tenant_email:
            print(
                "⚠️ LOG ENGINE: skipped tenant log because "
                "tenant_email is missing"
            )
            return False

    logs_write_url = _get_logs_write_url()
    if not logs_write_url:
        print(
            f"⚠️ LOG ENGINE: {NODE_RED_BASE_URL_ENV} is not configured; "
            "log was not sent"
        )
        return False

    payload = {
        # Ownership
        "user_id": clean_user_id,
        "user_email": clean_user_email,

        # Actor identity
        "actor_type": clean_actor_type,
        "tenant_user_id": (
            clean_tenant_user_id
            if clean_actor_type == LOG_ACTOR_TENANT
            else None
        ),
        "tenant_email": (
            clean_tenant_email
            if clean_actor_type == LOG_ACTOR_TENANT
            else None
        ),
        "tenant_name": (
            clean_tenant_name
            if clean_actor_type == LOG_ACTOR_TENANT
            else None
        ),

        # Core event
        "timestamp": _clean_optional_text(timestamp) or _utc_timestamp(),
        "category": clean_category,
        "action": clean_action,
        "status": clean_status,
        "message": _clean_optional_text(message),

        # Optional ownership/context
        "customer_id": _clean_optional_int(customer_id),
        "dashboard_id": (
            _clean_optional_text(dashboard_id)
            if dashboard_id is not None
            else None
        ),
        "device_id": _clean_optional_text(device_id),
        "gateway_id": _clean_optional_text(gateway_id),

        # Optional change/control details
        "field": _clean_optional_text(field),
        "old_value": old_value,
        "new_value": new_value,

        # Optional request/security context
        "ip_address": _clean_optional_text(ip_address),
        "user_agent": _clean_optional_text(user_agent),
    }

    try:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "CoreFlex-Log-Engine/1.0",
        }

        api_key = _clean_optional_text(os.getenv(LOGS_API_KEY_ENV))
        if api_key:
            headers["X-CoreFlex-Logs-Key"] = api_key

        request = urllib.request.Request(
            logs_write_url,
            data=body,
            headers=headers,
            method="POST",
        )

        with urllib.request.urlopen(
            request,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        ) as response:
            status_code = int(getattr(response, "status", 0) or 0)

        if 200 <= status_code < 300:
            return True

        print(
            "⚠️ LOG ENGINE: Node-RED returned unexpected status "
            f"{status_code}"
        )
        return False

    except urllib.error.HTTPError as exc:
        print(
            "⚠️ LOG ENGINE HTTP ERROR:",
            getattr(exc, "code", "unknown"),
            getattr(exc, "reason", ""),
        )
        return False

    except urllib.error.URLError as exc:
        print("⚠️ LOG ENGINE CONNECTION ERROR:", getattr(exc, "reason", exc))
        return False

    except Exception as exc:
        print("⚠️ LOG ENGINE ERROR:", exc)
        return False