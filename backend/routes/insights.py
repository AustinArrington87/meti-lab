import asyncio

from fastapi import APIRouter, HTTPException

from backend.config import settings
from backend.services import ecosystem_scorer, uffda_client
from backend.services.claude_agent import _sessions

router = APIRouter()

MAX_FEATURES = 60  # layer groups run concurrently; 60 fields ≈ 48 calls ≈ 50-60s


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

    if len(features) > MAX_FEATURES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Too many fields: {len(features)} uploaded, maximum is {MAX_FEATURES}. "
                "Remove some fields and try again, or split into multiple sessions."
            ),
        )

    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                uffda_client.enrich_features,
                features,
                settings.uffda_client_id,
            ),
            timeout=110,  # stay under the Flask 120s proxy timeout
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=(
                "UFFDA enrichment timed out. This usually happens with many large fields. "
                "Try with fewer fields (10 or fewer work most reliably)."
            ),
        )

    records = result["records"]
    failed_ids = result["failed_ids"]
    layer_errors = result["layer_errors"]

    if not records:
        raise HTTPException(
            status_code=502,
            detail=(
                "UFFDA returned no data for any fields. "
                "The service may be temporarily unavailable — please try again in a minute. "
                f"Failed fields: {', '.join(failed_ids) if failed_ids else 'unknown'}."
            ),
        )

    scored = ecosystem_scorer.score_fields(records)

    session["enrichment_results"] = {
        "records": records,
        "scored": scored,
    }

    response: dict = {
        "feature_count": len(features),
        "enriched_count": len(records),
        "scored": scored,
    }

    if failed_ids:
        response["failed_ids"] = failed_ids
        response["warning"] = (
            f"{len(failed_ids)} of {len(features)} field(s) returned no data from UFFDA "
            f"({', '.join(failed_ids)}). Results shown are for the remaining {len(records)} field(s)."
        )

    # Surface fields that had incomplete layer data (some but not all layers failed)
    partial = {fid: layers for fid, layers in layer_errors.items() if fid not in failed_ids}
    if partial:
        response["partial_layer_warnings"] = {
            fid: f"Missing layers: {', '.join(str(lg) for lg in layers)}"
            for fid, layers in partial.items()
        }

    return response
