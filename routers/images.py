# routers/images.py

from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from sqlalchemy.orm import Session
from cloudinary.uploader import upload as cld_upload

from database import get_db
from models import ImageAsset
from auth_utils import get_current_user

router = APIRouter(prefix="/images", tags=["Images"])

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_MB = 12


@router.post("/upload")
async def upload_image(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    # ✅ Validate file type
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported file type")

    # ✅ Read bytes and validate size
    data = await file.read()
    if len(data) > MAX_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"File too large (max {MAX_MB}MB)")

    # ✅ Upload to Cloudinary (per-user folder)
    folder = f"coreflex/users/{current_user.id}/library"
    try:
        result = cld_upload(
            data,
            folder=folder,
            resource_type="image",
            overwrite=False,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cloudinary upload failed: {repr(e)}")

    url = result.get("secure_url")
    public_id = result.get("public_id")

    if not url or not public_id:
        raise HTTPException(status_code=500, detail="Cloudinary response missing url/public_id")

    # ✅ Save to DB (THIS enforces per-user isolation)
    row = ImageAsset(
        user_id=current_user.id,
        url=url,
        public_id=public_id,
        folder=folder,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    return {
        "id": row.id,
        "url": row.url,
        "public_id": row.public_id,
        "created_at": str(row.created_at),
    }


@router.get("")
def list_images(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    # ✅ Only return images belonging to THIS user
    rows = (
        db.query(ImageAsset)
        .filter(ImageAsset.user_id == current_user.id)
        .order_by(ImageAsset.created_at.desc())
        .all()
    )

    return [
        {
            "id": r.id,
            "url": r.url,
            "public_id": r.public_id,
            "created_at": str(r.created_at),
        }
        for r in rows
    ]

from cloudinary.uploader import destroy as cld_destroy

@router.delete("/{image_id}")
def delete_image(
    image_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    # ✅ Find image row AND enforce per-user access
    row = (
        db.query(ImageAsset)
        .filter(ImageAsset.id == image_id, ImageAsset.user_id == current_user.id)
        .first()
    )

    if not row:
        raise HTTPException(status_code=404, detail="Image not found")

    # ✅ Delete from Cloudinary first (best effort)
    try:
        cld_destroy(row.public_id, resource_type="image")
    except Exception as e:
        # If you prefer: still delete from DB even if Cloudinary fails
        raise HTTPException(status_code=500, detail=f"Cloudinary delete failed: {repr(e)}")

    # ✅ Delete from DB
    db.delete(row)
    db.commit()

    return {"ok": True, "deleted_id": image_id}
