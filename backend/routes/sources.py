from fastapi import APIRouter, HTTPException

from backend.services import meti_client
from backend.services.claude_agent import _sessions

router = APIRouter()


@router.get("/sources/check")
async def check_sources(session_id: str, feature_index: int = 0):
    """
    Check if a feature's geometry intersects with existing METI sources.
    Uses admin credentials for admin sessions; per-account creds otherwise.
    """
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    features = session["features"]
    target = next((f for f in features if f["index"] == feature_index), None)
    if not target:
        raise HTTPException(status_code=404, detail=f"Feature {feature_index} not found")

    meta = target.get("meti_meta", {})
    results = meti_client.check_intersection(
        geojson_geometry=target["geometry"],
        start_at=meta.get("start_at", ""),
        end_at=meta.get("end_at", ""),
        account_id=session.get("account_id"),
        is_admin=session.get("is_admin", False),
    )

    return {"feature_id": target["id"], "overlapping_sources": results}
