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


def _parse_dt(value: Optional[str]):
    """Parse an ISO 8601 date/datetime string; return None on failure."""
    if not value:
        return None
    from datetime import datetime, timezone
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _dates_overlap(
    feat_start: Optional[str], feat_end: Optional[str],
    src_start: Optional[str],  src_end: Optional[str],
) -> Optional[bool]:
    """
    True  → date ranges definitely overlap (confirmed conflict)
    False → date ranges definitely do NOT overlap (no conflict)
    None  → cannot determine (missing dates on either side)
    """
    fs, fe = _parse_dt(feat_start), _parse_dt(feat_end)
    ss, se = _parse_dt(src_start),  _parse_dt(src_end)
    if not fs or not fe or not ss or not se:
        return None
    return fs <= se and ss <= fe


def check_conflicts_db(session_features: list) -> dict:
    """
    Check uploaded features against ALL sources via the Supabase RPC endpoint
    (find_intersecting_sources). Uses HTTPS + service key so it works outside
    the VPC. The RPC runs ST_Intersects with a GiST index on the DB side and
    returns start_at/end_at so we can check temporal overlap too.

    Risk logic:
      green  = no spatial overlap, OR spatial overlap with confirmed non-overlapping dates
      yellow = spatial overlap but dates missing → potential conflict
      red    = spatial overlap AND date ranges confirmed overlapping → confirmed conflict
    """
    import json
    import logging

    log = logging.getLogger(__name__)

    supabase_url = settings.supabase_url.rstrip("/")
    supabase_key = settings.supabase_service_key

    if not supabase_url or not supabase_key:
        log.error("check_conflicts_db: SUPABASE_URL or SUPABASE_SERVICE_KEY not configured")
        return {
            f["id"]: {"risk": "yellow", "conflict": None, "conflict_with": []}
            for f in session_features
        }

    headers = {
        "Authorization": f"Bearer {supabase_key}",
        "apikey": supabase_key,
        "Content-Type": "application/json",
    }

    result = {}
    for f in session_features:
        feature_id = f["id"]
        geom_dict = f.get("geometry")

        if not geom_dict:
            result[feature_id] = {"risk": "yellow", "conflict": None, "conflict_with": []}
            continue

        feat_start = f.get("meti_meta", {}).get("start_at")
        feat_end   = f.get("meti_meta", {}).get("end_at")

        try:
            resp = requests.post(
                f"{supabase_url}/rest/v1/rpc/find_intersecting_sources",
                headers=headers,
                json={"geojson_geometry": json.dumps(geom_dict)},
                timeout=20,
            )
            if not resp.ok:
                log.error("check_conflicts_db: RPC error for %s: %s", feature_id, resp.status_code)
                result[feature_id] = {"risk": "yellow", "conflict": None, "conflict_with": []}
                continue

            spatial_matches = resp.json()

            if not spatial_matches:
                result[feature_id] = {"risk": "green", "conflict": False, "conflict_with": []}
                continue

            confirmed = []   # spatial + date overlap
            potential = []   # spatial overlap, dates unknown/missing

            for row in spatial_matches:
                overlap = _dates_overlap(feat_start, feat_end, row.get("start_at"), row.get("end_at"))
                if overlap is True:
                    confirmed.append(row["id"])
                elif overlap is None:
                    potential.append(row["id"])
                # overlap is False → different time period, not a conflict

            if confirmed:
                risk, conflict_ids = "red", confirmed
            elif potential:
                risk, conflict_ids = "yellow", potential
            else:
                risk, conflict_ids = "green", []

            result[feature_id] = {
                "risk": risk,
                "conflict": bool(conflict_ids),
                "conflict_with": conflict_ids,
            }

        except Exception as exc:
            log.error("check_conflicts_db: request failed for %s: %s", feature_id, exc)
            result[feature_id] = {"risk": "yellow", "conflict": None, "conflict_with": []}

    return result
