# cloudinary_config.py
import os
import cloudinary


def _clean(v: str | None) -> str | None:
    if v is None:
        return None
    # remove hidden whitespace/newlines from copy/paste
    return v.strip().strip('"').strip("'")


def init_cloudinary() -> None:
    """
    Initializes Cloudinary using Render environment variables.

    Required env vars (your current setup):
      - CLOUDINARY_CLOUD_NAME
      - CLOUDINARY_API_KEY
      - CLOUDINARY_API_SECRET

    Optional alternative:
      - CLOUDINARY_URL (if you ever choose to use it)
    """
    # Prefer explicit vars first (matches your Render screenshot)
    cloud_name = _clean(os.getenv("CLOUDINARY_CLOUD_NAME"))
    api_key = _clean(os.getenv("CLOUDINARY_API_KEY"))
    api_secret = _clean(os.getenv("CLOUDINARY_API_SECRET"))

    # If any missing, fallback to CLOUDINARY_URL (optional)
    cloudinary_url = _clean(os.getenv("CLOUDINARY_URL"))

    if cloud_name and api_key and api_secret:
        cloudinary.config(
            cloud_name=cloud_name,
            api_key=api_key,
            api_secret=api_secret,
            secure=True,
        )
        masked = (api_key[:4] + "****") if api_key else "None"
        print(f"✅ Cloudinary configured via 3 vars: cloud_name={cloud_name}, api_key={masked}")
        return

    if cloudinary_url:
        # cloudinary SDK can parse CLOUDINARY_URL automatically
        cloudinary.config(cloudinary_url=cloudinary_url, secure=True)
        print("✅ Cloudinary configured via CLOUDINARY_URL")
        return

    raise RuntimeError(
        "Cloudinary not configured. Missing env vars. "
        "Set CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET "
        "(or CLOUDINARY_URL)."
    )
