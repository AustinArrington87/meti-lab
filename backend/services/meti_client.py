from typing import Optional
import requests

from backend.config import settings, MILLPONT_ACCOUNT_ID

BASE_URL = "https://api.millpont.com"
_token_cache: dict[str, str] = {}


def _fetch_token(client_id: str, client_secret: str) -> Optional[str]:
    """Get an Auth0 M2M access token for the given client credentials."""
    resp = requests.post(
        f"https://{settings.auth0_domain}/oauth/token",
        json={
            "client_id": client_id,
            "client_secret": client_secret,
            "audience": settings.auth0_audience,
            "grant_type": "client_credentials",
        },
        headers={"content-type": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("access_token")


def get_token_for_account(account_id: Optional[str], is_admin: bool = False) -> Optional[str]:
    """
    Return the correct M2M token for the given account context:
    - Admins (MillPont account) → METI_CLIENT_ID (full read access to all accounts)
    - Account users → per-account client credentials (scoped to their account)
    """
    if is_admin or account_id == MILLPONT_ACCOUNT_ID or not account_id:
        if not settings.meti_client_id:
            return None
        cache_key = "admin"
        if cache_key not in _token_cache:
            _token_cache[cache_key] = _fetch_token(settings.meti_client_id, settings.meti_client_secret)
        return _token_cache[cache_key]

    creds = settings.account_credentials.get(account_id)
    if not creds or not creds[0]:
        # Fall back to admin credentials if no per-account creds
        return get_token_for_account(account_id=None, is_admin=True)

    if account_id not in _token_cache:
        _token_cache[account_id] = _fetch_token(creds[0], creds[1])
    return _token_cache[account_id]


def clear_token_cache():
    """Clear cached tokens (call if you get a 401)."""
    _token_cache.clear()


def check_intersection(
    geojson_geometry: dict,
    start_at: str,
    end_at: str,
    account_id: Optional[str] = None,
    is_admin: bool = False,
) -> list:
    """
    Query METI /sandbox/sources for sources that could overlap.
    Returns list of source summaries (or empty list on error / no creds).
    """
    token = get_token_for_account(account_id, is_admin)
    if not token:
        return []

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    params = {}
    if not is_admin and account_id:
        params["account_id"] = account_id

    resp = requests.get(f"{BASE_URL}/sandbox/sources", headers=headers, params=params, timeout=15)
    if resp.status_code == 401:
        clear_token_cache()
    if not resp.ok:
        return []

    return resp.json()


def check_conflicts_db(session_features: list) -> dict:
    """
    Check uploaded features against ALL sources in the METI sources table using
    PostGIS ST_Intersects. Queries the DB directly so results are not scoped to
    a single account — every existing source across all accounts is checked.

    Returns a dict keyed by feature id:
      { "risk": "green" | "red" | "yellow",
        "conflict": bool,
        "conflict_with": [source_id, ...],
        "reason": str (on yellow only) }

    green  = no spatial overlap with any source in the ledger
    red    = geometry overlaps one or more existing sources
    yellow = DB unavailable or geometry is invalid
    """
    import json
    import logging
    from backend.services.db import get_db_connection

    log = logging.getLogger(__name__)

    try:
        conn = get_db_connection()
    except Exception as exc:
        log.error("check_conflicts_db: DB connect failed: %s", exc)
        return {
            f["id"]: {"risk": "yellow", "conflict": None, "conflict_with": [], "reason": "db_unavailable"}
            for f in session_features
        }

    result = {}
    try:
        with conn.cursor() as cur:
            for f in session_features:
                feature_id = f["id"]
                geom_dict = f.get("geometry")

                if not geom_dict:
                    result[feature_id] = {"risk": "yellow", "conflict": None, "conflict_with": [], "reason": "null_geometry"}
                    continue

                try:
                    geom_json = json.dumps(geom_dict)
                    # Two-pass PostGIS pattern: && does a fast bounding-box
                    # lookup via GiST index; ST_Intersects does the precise
                    # geometry check only on those candidates.
                    uploaded = "ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)"
                    cur.execute(
                        f"""
                        SELECT id::text FROM sources
                        WHERE geometry && {uploaded}
                          AND ST_Intersects(geometry, {uploaded})
                        """,
                        (geom_json, geom_json),
                    )
                    rows = cur.fetchall()
                    conflict_ids = [row[0] for row in rows]
                    result[feature_id] = {
                        "risk": "red" if conflict_ids else "green",
                        "conflict": bool(conflict_ids),
                        "conflict_with": conflict_ids,
                    }
                except Exception as exc:
                    log.error("check_conflicts_db: query failed for %s: %s", feature_id, exc)
                    result[feature_id] = {"risk": "yellow", "conflict": None, "conflict_with": [], "reason": "query_error"}
    finally:
        conn.close()

    return result
