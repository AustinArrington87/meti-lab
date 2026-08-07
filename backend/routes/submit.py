import base64
import json
import logging
from typing import Optional

import requests as req
from fastapi import APIRouter, Header, HTTPException

from backend.config import settings, MILLPONT_ACCOUNT_ID
from backend.services.claude_agent import _sessions
from backend.services.meti_client import clear_token_cache, get_token_for_account

router = APIRouter()
log = logging.getLogger(__name__)
METI_API_URL = "https://api.millpont.com"


def _decode_jwt_payload(token: str) -> dict:
    """Extract claims from a JWT without verifying the signature."""
    try:
        parts = token.split(".")
        padding = "=" * (-len(parts[1]) % 4)
        return json.loads(base64.b64decode(parts[1] + padding))
    except Exception:
        return {}


@router.post("/submit")
async def submit_to_meti(
    session_id: str,
    x_account_id: Optional[str] = Header(None),
    x_is_admin: Optional[str] = Header(None),
):
    """
    Submit the session's export payload to the METI source ledger.
    Requires: linked account_id, per-account M2M credentials in .env,
    export ready, no confirmed red-risk spatial conflicts.
    """
    # 1. Must be linked to a METI account
    if not x_account_id:
        raise HTTPException(status_code=403, detail=(
            "You must be linked to a METI account to submit. "
            "Schedule a call with MillPont to get your account set up."
        ))

    # 2. Must have credentials configured in .env.
    # MillPont/admin accounts use METI_CLIENT_ID; other accounts use per-org credentials.
    is_admin_account = (x_is_admin or "").lower() == "true" or x_account_id == MILLPONT_ACCOUNT_ID
    if is_admin_account:
        if not settings.meti_client_id or not settings.meti_client_secret:
            raise HTTPException(status_code=403, detail=(
                "Submit privileges have not been configured for your account. "
                "Contact a METI Administrator to request access."
            ))
    else:
        creds = settings.account_credentials.get(x_account_id)
        if not creds or not creds[0] or not creds[1]:
            raise HTTPException(status_code=403, detail=(
                "Submit privileges have not been configured for your account. "
                "Contact a METI Administrator to request access."
            ))

    # 3. Session and export payload must exist
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    export_payload = session.get("export_payload")
    if not export_payload:
        raise HTTPException(status_code=400, detail=(
            "Export not ready. Ask the agent to set start_at/end_at dates and run the risk check first."
        ))

    # 4. Block on confirmed (red) spatial conflicts
    risk_results = session.get("risk_results", {})
    red_features = [fid for fid, r in risk_results.items() if r.get("risk") == "red"]
    if red_features:
        raise HTTPException(status_code=409, detail=(
            f"Cannot submit: {len(red_features)} field(s) have confirmed spatial conflicts with "
            f"existing METI sources ({', '.join(red_features)}). Resolve conflicts before submitting."
        ))

    yellow_features = [fid for fid, r in risk_results.items() if r.get("risk") == "yellow"]

    # 5. Get M2M access token
    is_admin = (x_is_admin or "").lower() == "true"
    token = get_token_for_account(x_account_id, is_admin)
    if not token:
        raise HTTPException(status_code=503, detail=(
            "Could not obtain a METI API access token. Try again in a moment."
        ))

    # 6. Decode JWT to get v1_client_id and account_id claims
    import uuid as _uuid
    claims = _decode_jwt_payload(token)
    log.info("METI JWT claims: %s", {k: v for k, v in claims.items() if "meti" in k.lower() or k in ("sub", "azp")})
    raw_v1 = claims.get("https://api.meti.millpont.com/v1_client_id")
    # Validate it's a real UUID — the @clients sub claim is NOT valid here
    v1_client_id = None
    if raw_v1:
        try:
            _uuid.UUID(str(raw_v1))
            v1_client_id = raw_v1
        except ValueError:
            log.warning("v1_client_id '%s' is not a valid UUID (got @clients sub?) — omitting created_by", raw_v1)
    account_id_from_token = (
        claims.get("https://api.meti.millpont.com/account_id") or x_account_id
    )

    # 7. Build submission payload (export_payload already has feature_collection + METI fields)
    submit_payload = dict(export_payload)
    submit_payload["account_id"] = account_id_from_token
    # created_by/updated_by must be a UUID present in the profiles table.
    # Only set when v1_client_id is a valid UUID from the JWT.
    if v1_client_id:
        submit_payload["created_by"] = v1_client_id
        submit_payload["updated_by"] = v1_client_id
    else:
        submit_payload.pop("created_by", None)
        submit_payload.pop("updated_by", None)

    # 8. POST to METI API
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        resp = req.post(
            f"{METI_API_URL}/sources",
            json=submit_payload,
            headers=headers,
            timeout=30,
        )
    except req.RequestException as exc:
        raise HTTPException(status_code=503, detail=f"METI API unreachable: {exc}")

    if resp.status_code == 401:
        clear_token_cache()
        raise HTTPException(status_code=401, detail=(
            "METI API rejected the token. Try again — if the problem persists, contact a METI Administrator."
        ))

    if resp.status_code == 403:
        raise HTTPException(status_code=403, detail=(
            "METI API denied the request. Your account may not have write:sources permission. "
            "Contact a METI Administrator."
        ))

    if not resp.ok:
        try:
            detail = resp.json().get("detail", resp.text[:400])
        except Exception:
            detail = resp.text[:400]
        raise HTTPException(status_code=resp.status_code, detail=f"METI API error: {detail}")

    sources = resp.json()

    # Mark session as submitted
    session["submitted"] = True
    session["submitted_source_ids"] = [s["id"] for s in sources]
    _sessions.save(session_id)

    return {
        "submitted_count": len(sources),
        "source_ids": [s["id"] for s in sources],
        "conflicts": [s["id"] for s in sources if s.get("conflict")],
        "warnings": (
            f"{len(yellow_features)} field(s) had potential (unconfirmed) spatial overlaps but were submitted."
            if yellow_features else None
        ),
    }
