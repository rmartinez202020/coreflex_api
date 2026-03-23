# utils/email_service.py

import os
from datetime import datetime, timezone
from email.utils import format_datetime
from html import escape

import requests

RESEND_API_KEY = os.getenv("RESEND_API_KEY")


def _send_resend_email(payload: dict):
    if not RESEND_API_KEY:
        print("❌ RESEND_API_KEY missing")
        return False

    url = "https://api.resend.com/emails"

    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        print("📧 RESEND SEND START")
        print(f"📧 TO: {payload.get('to')}")
        print(f"📧 FROM: {payload.get('from')}")
        print(f"📧 SUBJECT: {payload.get('subject')}")
        print(f"📧 DATE HEADER: {payload.get('headers', {}).get('Date')}")

        response = requests.post(url, headers=headers, json=payload, timeout=20)

        print(f"📧 RESEND STATUS: {response.status_code}")
        print(f"📧 RESEND RESPONSE: {response.text}")

        if response.status_code >= 400:
            print("❌ RESEND ERROR:", response.text)
            return False

        print("✅ Email sent via Resend")
        return True

    except Exception as e:
        print("🔥 EMAIL ERROR:", e)
        return False


def send_reset_code_email(
    to_email: str,
    code: str,
    expires_minutes: int = 10,
):
    clean_to = str(to_email or "").strip().lower()
    if not clean_to:
        print("❌ Missing destination email")
        return False

    now_utc = datetime.now(timezone.utc)
    formatted_date = format_datetime(now_utc)

    data = {
        "from": "CoreFlex IIoTs Platform <noreply@coreflexiiotsplatform.com>",
        "to": [clean_to],
        "subject": "CoreFlex IIoTs Platform Password Reset Code",
        "headers": {
            "Date": formatted_date,
        },
        "text": (
            f"CoreFlex IIoTs Platform Password Reset\n\n"
            f"Your temporary password reset code is: {code}\n\n"
            f"This code will expire in {expires_minutes} minutes.\n\n"
            f"This is an automated message. Please do not reply.\n\n"
            f"If you did not request this, you can ignore this email."
        ),
        "html": f"""
        <div style="font-family:Arial,Helvetica,sans-serif;padding:20px;background:#f8fafc;">
            <div style="
                max-width:560px;
                margin:0 auto;
                background:#ffffff;
                border:1px solid #e5e7eb;
                border-radius:12px;
                padding:32px 28px;
                box-shadow:0 2px 8px rgba(0,0,0,0.04);
            ">
                <h2 style="margin:0 0 18px 0;color:#22c55e;">
                    CoreFlex IIoTs Platform Password Reset
                </h2>

                <p style="margin:0 0 12px 0;color:#111827;font-size:15px;line-height:1.6;">
                    Your temporary password reset code is:
                </p>

                <div style="
                    font-size:32px;
                    font-weight:bold;
                    letter-spacing:6px;
                    margin:20px 0 24px 0;
                    color:#22c55e;
                ">
                    {escape(str(code))}
                </div>

                <p style="margin:0 0 12px 0;color:#111827;font-size:15px;line-height:1.6;">
                    This code will expire in {expires_minutes} minutes.
                </p>

                <p style="margin:18px 0 0 0;color:#6b7280;font-size:13px;line-height:1.6;">
                    This is an automated message. Please do not reply.
                </p>

                <p style="margin:8px 0 0 0;color:#6b7280;font-size:13px;line-height:1.6;">
                    If you did not request this, you can ignore this email.
                </p>
            </div>
        </div>
        """,
    }

    return _send_resend_email(data)


def _normalize_dashboard_links(dashboard_links):
    out = []
    if not isinstance(dashboard_links, list):
        return out

    for item in dashboard_links:
        if isinstance(item, str):
            url = item.strip()
            if url:
                out.append({"name": "Dashboard", "url": url})
            continue

        if isinstance(item, dict):
            name = str(item.get("name") or item.get("dashboard_name") or "Dashboard").strip()
            url = str(item.get("url") or item.get("link") or "").strip()
            if url:
                out.append({"name": name or "Dashboard", "url": url})

    return out


def _build_dashboard_links_text(dashboard_links):
    items = _normalize_dashboard_links(dashboard_links)
    if not items:
        return ""

    lines = ["\nAccessible dashboard links:\n"]
    for idx, item in enumerate(items, start=1):
        lines.append(f"{idx}. {item['name']}: {item['url']}")
    return "\n".join(lines)


def _build_dashboard_links_html(dashboard_links):
    items = _normalize_dashboard_links(dashboard_links)
    if not items:
        return ""

    rows = []
    for item in items:
        name = escape(item["name"])
        url = escape(item["url"])
        rows.append(
            f"""
            <div style="margin:0 0 10px 0;padding:12px 14px;border:1px solid #e5e7eb;border-radius:10px;background:#f9fafb;">
                <div style="margin:0 0 6px 0;color:#111827;font-size:14px;font-weight:600;line-height:1.5;">
                    {name}
                </div>
                <a href="{url}" style="color:#2563eb;font-size:14px;line-height:1.6;word-break:break-all;text-decoration:none;">
                    {url}
                </a>
            </div>
            """
        )

    return f"""
        <div style="margin:22px 0 0 0;">
            <div style="margin:0 0 10px 0;color:#111827;font-size:15px;font-weight:700;line-height:1.6;">
                Accessible dashboard links
            </div>
            {''.join(rows)}
        </div>
    """


def send_tenant_credentials_email(
    to_email: str,
    temporary_password: str,
    tenant_name: str,
    admin_email: str = "",
    dashboard_links=None,
    portal_login_url: str = "",
):
    clean_to = str(to_email or "").strip().lower()
    clean_name = str(tenant_name or "").strip()
    clean_password = str(temporary_password or "").strip()
    clean_login_url = str(portal_login_url or "").strip()

    if not clean_to:
        print("❌ Missing destination email")
        return False

    if not clean_password:
        print("❌ Missing temporary password")
        return False

    now_utc = datetime.now(timezone.utc)
    formatted_date = format_datetime(now_utc)

    dashboard_links_text = _build_dashboard_links_text(dashboard_links)
    dashboard_links_html = _build_dashboard_links_html(dashboard_links)

    login_url_text = (
        f"\nPortal login: {clean_login_url}\n" if clean_login_url else ""
    )

    login_url_html = (
        f"""
        <div style="margin:18px 0 0 0;padding:14px 16px;border:1px solid #dbeafe;background:#f8fbff;border-radius:10px;">
            <div style="margin:0 0 6px 0;color:#1f2937;font-size:14px;font-weight:600;line-height:1.6;">
                Portal login
            </div>
            <a href="{escape(clean_login_url)}" style="color:#2563eb;font-size:14px;line-height:1.6;word-break:break-all;text-decoration:none;">
                {escape(clean_login_url)}
            </a>
        </div>
        """
        if clean_login_url
        else ""
    )

    data = {
        "from": "CoreFlex Access <access@coreflexiiotsplatform.com>",
        "to": [clean_to],
        "subject": "Your CoreFlex IIoTs Platform Access Credentials",
        "headers": {
            "Date": formatted_date,
        },
        "text": (
            f"CoreFlex IIoTs Platform Tenant Access\n\n"
            f"Hello {clean_name or clean_to},\n\n"
            f"Your tenant access account has been created.\n\n"
            f"Login email: {clean_to}\n"
            f"Temporary password: {clean_password}\n"
            f"{login_url_text}"
            f"\n"
            f"Use this temporary password to sign in.\n"
            f"If your account is configured for first-time password change, the system will ask you to create a new password after login.\n"
            f"{dashboard_links_text}\n\n"
            f"This password is shown only in this email and is not visible to other users.\n\n"
            f"This is an automated message from CoreFlex IIoTs Platform. Please do not reply."
        ),
        "html": f"""
        <div style="font-family:Arial,Helvetica,sans-serif;padding:20px;background:#f8fafc;">
            <div style="
                max-width:620px;
                margin:0 auto;
                background:#ffffff;
                border:1px solid #e5e7eb;
                border-radius:12px;
                padding:32px 28px;
                box-shadow:0 2px 8px rgba(0,0,0,0.04);
            ">
                <h2 style="margin:0 0 18px 0;color:#2563eb;">
                    CoreFlex IIoTs Platform Tenant Access
                </h2>

                <p style="margin:0 0 12px 0;color:#111827;font-size:15px;line-height:1.6;">
                    Hello <b>{escape(clean_name or clean_to)}</b>,
                </p>

                <p style="margin:0 0 12px 0;color:#111827;font-size:15px;line-height:1.6;">
                    Your tenant access account has been created.
                </p>

                <div style="
                    margin:18px 0;
                    padding:16px;
                    border:1px solid #dbeafe;
                    background:#eff6ff;
                    border-radius:10px;
                ">
                    <div style="margin:0 0 10px 0;color:#1f2937;font-size:14px;line-height:1.6;">
                        <b>Login email:</b> {escape(clean_to)}
                    </div>

                    <div style="margin:0;color:#1f2937;font-size:14px;line-height:1.6;">
                        <b>Temporary password:</b>
                    </div>

                    <div style="
                        font-size:28px;
                        font-weight:bold;
                        letter-spacing:2px;
                        margin:12px 0 0 0;
                        color:#2563eb;
                        word-break:break-word;
                    ">
                        {escape(clean_password)}
                    </div>
                </div>

                {login_url_html}

                <p style="margin:18px 0 12px 0;color:#111827;font-size:15px;line-height:1.6;">
                    Use this temporary password to sign in.
                </p>

                <p style="margin:0 0 12px 0;color:#111827;font-size:15px;line-height:1.6;">
                    If your account is configured for first-time password change, the system will ask you to create a new password after login.
                </p>

                {dashboard_links_html}

                <p style="margin:18px 0 0 0;color:#6b7280;font-size:13px;line-height:1.6;">
                    This password is shown only in this email and is not visible to other users.
                </p>

                <p style="margin:8px 0 0 0;color:#6b7280;font-size:13px;line-height:1.6;">
                    This is an automated message from CoreFlex IIoTs Platform. Please do not reply.
                </p>
            </div>
        </div>
        """,
    }

    return _send_resend_email(data)