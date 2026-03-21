# utils/email_service.py

import os
import requests

RESEND_API_KEY = os.getenv("RESEND_API_KEY")


def send_reset_code_email(
    to_email: str,
    code: str,
    expires_minutes: int = 10,
):
    if not RESEND_API_KEY:
        print("❌ RESEND_API_KEY missing")
        return

    url = "https://api.resend.com/emails"

    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
    }

    data = {
        # ✅ default sender (works immediately)
        "from": "CoreFlex <onboarding@resend.dev>",
        "to": [to_email],
        "subject": "CoreFlex Password Reset Code",
        "html": f"""
        <div style="font-family:Arial;padding:20px;">
            <h2 style="color:#22c55e;">CoreFlex Password Reset</h2>

            <p>Your temporary password reset code is:</p>

            <div style="
                font-size:32px;
                font-weight:bold;
                letter-spacing:6px;
                margin:20px 0;
                color:#22c55e;
            ">
                {code}
            </div>

            <p>This code will expire in {expires_minutes} minutes.</p>

            <p style="color:#888;">
                If you did not request this, you can ignore this email.
            </p>
        </div>
        """,
    }

    try:
        response = requests.post(url, headers=headers, json=data)

        if response.status_code >= 400:
            print("❌ RESEND ERROR:", response.text)
        else:
            print("✅ Email sent via Resend")

    except Exception as e:
        print("🔥 EMAIL ERROR:", e)