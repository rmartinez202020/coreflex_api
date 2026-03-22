# utils/email_service.py

import os
from datetime import datetime, timezone
from email.utils import format_datetime

import requests

RESEND_API_KEY = os.getenv("RESEND_API_KEY")


def send_reset_code_email(
    to_email: str,
    code: str,
    expires_minutes: int = 10,
):
    if not RESEND_API_KEY:
        print("❌ RESEND_API_KEY missing")
        return False

    clean_to = str(to_email or "").strip().lower()
    if not clean_to:
        print("❌ Missing destination email")
        return False

    url = "https://api.resend.com/emails"

    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
    }

    # ✅ Proper RFC 2822 Date header in UTC
    # Email clients should convert this to each user's local timezone.
    now_utc = datetime.now(timezone.utc)
    formatted_date = format_datetime(now_utc)

    data = {
        # ✅ use your verified domain sender
        "from": "CoreFlex IIoTs Platform <noreply@coreflexiiotsplatform.com>",
        "to": [clean_to],
        "subject": "CoreFlex IIoTs Platform Password Reset Code",
        # ✅ Explicit Date header so mail clients can render local time correctly
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
                    {code}
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

    try:
        print("📧 RESEND SEND START")
        print(f"📧 TO: {clean_to}")
        print(f"📧 FROM: {data['from']}")
        print(f"📧 SUBJECT: {data['subject']}")
        print(f"📧 DATE HEADER: {formatted_date}")

        response = requests.post(url, headers=headers, json=data, timeout=20)

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