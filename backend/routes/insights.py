from fastapi import APIRouter, HTTPException

from backend.config import settings
from backend.services import uffda_client, ecosystem_scorer
from backend.services.claude_agent import _sessions

router = APIRouter()


@router.post("/insights/enrich")
async def enrich_insights(session_id: str):
    """
    Fetch UFFDA enrichment data for all session features, score them for
    ecosystem-services program fit, and cache results in the session.
    """
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    features = [f for f in session["features"] if f.get("geometry")]
    if not features:
        raise HTTPException(status_code=400, detail="No features with geometry to enrich")

    import asyncio
    loop = asyncio.get_event_loop()
    records = await loop.run_in_executor(
        None,
        uffda_client.enrich_features,
        features,
        settings.uffda_client_id,
    )

    scored = ecosystem_scorer.score_fields(records)

    session["enrichment_results"] = {
        "records": records,
        "scored": scored,
    }

    return {
        "feature_count": len(features),
        "enriched_count": len(records),
        "scored": scored,
    }
