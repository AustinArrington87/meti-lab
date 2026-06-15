from fastapi import APIRouter, HTTPException

from backend.services import meti_client
from backend.services.claude_agent import _sessions

router = APIRouter()


@router.post("/sources/risk-check")
async def risk_check(session_id: str):
    """
    POST the current session's features to the METI sandbox endpoint to
    detect spatial/temporal conflicts. Returns per-feature risk status:
      green = no conflict, red = conflict detected, yellow = check unavailable.
    """
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    has_dates = all(
        f.get("meti_meta", {}).get("start_at") and f.get("meti_meta", {}).get("end_at")
        for f in session["features"]
    )

    risk_map = meti_client.check_conflicts_db(
        session_features=session["features"],
    )

    return {
        "risk_map": risk_map,
        "has_dates": has_dates,
        "feature_count": len(session["features"]),
        "summary": {
            "green": sum(1 for v in risk_map.values() if v["risk"] == "green"),
            "yellow": sum(1 for v in risk_map.values() if v["risk"] == "yellow"),
            "red": sum(1 for v in risk_map.values() if v["risk"] == "red"),
        },
    }


@router.get("/sources/check")
async def check_sources(session_id: str, feature_index: int = 0):
    """Check if a feature's geometry intersects existing METI sources (GET-based)."""
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
