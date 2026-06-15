import os
import uuid
from typing import Optional

from fastapi import APIRouter, File, Header, HTTPException, UploadFile

from backend.models.schemas import ParsedFeature, UploadResponse
from backend.services import claude_agent, gis_processor

router = APIRouter()

ALLOWED_EXTENSIONS = {".geojson", ".json", ".kml", ".shp", ".zip", ".gpx", ".gml"}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


@router.post("/upload", response_model=UploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    x_account_id: Optional[str] = Header(None),
    x_is_admin: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
):
    ext = os.path.splitext(file.filename.lower())[1]
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Accepted: {', '.join(ALLOWED_EXTENSIONS)}",
        )

    file_bytes = await file.read()
    if len(file_bytes) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File exceeds 50 MB limit")

    try:
        features = gis_processor.parse_file(file_bytes, file.filename)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not parse file: {exc}")

    if not features:
        raise HTTPException(status_code=422, detail="No features found in file")

    session_id = str(uuid.uuid4())
    is_admin = (x_is_admin or "").lower() == "true"
    claude_agent.init_session(
        session_id,
        features,
        account_id=x_account_id,
        is_admin=is_admin,
        user_email=x_user_email,
    )

    crs = features[0].get("crs_detected", "unknown") if features else "unknown"

    parsed = [
        ParsedFeature(
            index=f["index"],
            id=f["id"],
            geometry=f["geometry"] or {},
            geometry_type=f["geometry_type"],
            properties=f["properties"],
            hectares=f["hectares"],
            issues=f["issues"],
        )
        for f in features
    ]

    return UploadResponse(
        session_id=session_id,
        feature_count=len(features),
        features=parsed,
        file_name=file.filename,
        crs_detected=crs,
    )


@router.get("/session/{session_id}/opening")
async def get_opening(session_id: str):
    msg = claude_agent.get_opening_message(session_id)
    return {"message": msg}
