"""
UFFDA API client for field enrichment.
Ported from uffda_test/enrich_fields.py — same retry/batching logic.
"""
import time
from typing import Optional

import requests

UFFDA_API_URL = "https://uffda.ag/api/v1/fields/enrich"

LAYER_GROUPS = [
    ["crop_history", "drought", "land_cover"],
    ["soil", "weather", "forest_loss"],
    ["irrigation"],
    ["protected_area"],
]

BATCH_SIZE = 5
SLEEP_BETWEEN_CALLS = 0.5

DEFAULT_START_AT = "2025-01-01T00:00:00+00:00"
DEFAULT_END_AT = "2025-12-31T23:59:59+00:00"

CDL_LOOKBACK_LAYERS = {"crop_history", "land_cover"}
CDL_LOOKBACK_YEARS = 5


def _year_from_start_at(start_at: str) -> int:
    return int(start_at[:4])


def _cdl_years(year: int, layers: list) -> list:
    if CDL_LOOKBACK_LAYERS.intersection(layers):
        return list(range(year - CDL_LOOKBACK_YEARS + 1, year + 1))
    return [year]


def _build_payload(features: list, year: int, layers: list) -> dict:
    api_features = []
    for feat in features:
        props = feat.get("properties", {}) or {}
        fid = props.get("id") or feat.get("id") or feat.get("alt_id")
        api_features.append({
            "type": "Feature",
            "id": fid,
            "geometry": feat["geometry"],
            "properties": {
                "id": fid,
                "alt_id": props.get("alt_id") or fid,
            },
        })
    return {
        "type": "FeatureCollection",
        "features": api_features,
        "layers": layers,
        "options": {
            "cdl_years": _cdl_years(year, layers),
            "weather_window": {
                "start": f"{year}-01-01",
                "end": f"{year}-12-31",
            },
            "units": "metric",
        },
    }


def _post_batch(payload: dict, headers: dict, label: str) -> Optional[dict]:
    max_attempts = 5
    attempts = 0
    while attempts < max_attempts:
        try:
            resp = requests.post(UFFDA_API_URL, json=payload, headers=headers, timeout=35)
        except requests.exceptions.Timeout:
            attempts += 1
            time.sleep(15)
            continue
        except requests.exceptions.RequestException:
            attempts += 1
            time.sleep(15)
            continue

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 60))
            time.sleep(retry_after)
            continue  # not counted against attempts

        if resp.status_code in (504, 546):
            attempts += 1
            time.sleep(15)
            continue

        # Other 4xx/5xx — give up on this batch
        return None

    return None


def enrich_features(features: list, uffda_client_id: str) -> list:
    """
    Call UFFDA /v1/fields/enrich for all features across all layer groups.
    features: list of session feature dicts (each has id, geometry, meti_meta)
    Returns: [{id, alt_id, year, enrichment, derived, errors}, ...]
    """
    headers = {
        "Content-Type": "application/json",
        "X-UFFDA-Client": uffda_client_id,
    }

    # Build normalized feature list for the API
    api_features = []
    for feat in features:
        meta = feat.get("meti_meta") or {}
        start_at = meta.get("start_at") or DEFAULT_START_AT
        end_at = meta.get("end_at") or DEFAULT_END_AT
        fid = feat.get("id") or ""
        api_features.append({
            "id": fid,
            "alt_id": fid,
            "geometry": feat["geometry"],
            "properties": {
                "id": fid,
                "alt_id": fid,
                "start_at": start_at,
                "end_at": end_at,
            },
            "start_at": start_at,
        })

    # Partition by year
    by_year: dict = {}
    for feat in api_features:
        year = _year_from_start_at(feat["start_at"])
        by_year.setdefault(year, []).append(feat)

    # Build batch list
    batches = []
    for year in sorted(by_year):
        year_feats = by_year[year]
        for offset in range(0, len(year_feats), BATCH_SIZE):
            batches.append((year, year_feats[offset:offset + BATCH_SIZE]))

    results = []
    for batch_idx, (year, chunk) in enumerate(batches):
        by_id: dict = {}

        for layer_group in LAYER_GROUPS:
            payload = _build_payload(chunk, year, layer_group)
            resp = _post_batch(payload, headers, f"batch {batch_idx} {layer_group}")
            if resp is None:
                continue
            for feat in resp.get("features", []):
                props = feat.get("properties", {})
                fid = props.get("id") or feat.get("id")
                if fid not in by_id:
                    by_id[fid] = {
                        "id": fid,
                        "alt_id": props.get("alt_id") or fid,
                        "year": year,
                        "enrichment": {},
                        "derived": props.get("derived"),
                        "errors": props.get("errors"),
                    }
                by_id[fid]["enrichment"].update(props.get("enrichment") or {})
            time.sleep(SLEEP_BETWEEN_CALLS)

        results.extend(by_id.values())

        if batch_idx < len(batches) - 1:
            time.sleep(SLEEP_BETWEEN_CALLS)

    return results
