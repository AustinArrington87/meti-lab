"""
UFFDA API client for field enrichment.
Ported from uffda_test/enrich_fields.py — same retry/batching logic,
with concurrent layer-group calls per batch via ThreadPoolExecutor.
"""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
MAX_CONCURRENT = 6  # concurrent UFFDA calls; stay within rate limits

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
    max_rate_limit_retries = 3
    attempts = 0
    rate_limit_hits = 0
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
            rate_limit_hits += 1
            if rate_limit_hits > max_rate_limit_retries:
                return None
            retry_after = int(resp.headers.get("Retry-After", 60))
            time.sleep(retry_after)
            continue

        if resp.status_code in (504, 546):
            attempts += 1
            time.sleep(15)
            continue

        return None

    return None


def _call_one(args: tuple) -> tuple:
    """Worker: call UFFDA for one (batch, layer_group) combination."""
    batch_idx, year, chunk, layer_group, headers = args
    payload = _build_payload(chunk, year, layer_group)
    resp = _post_batch(payload, headers, f"batch {batch_idx} {layer_group}")
    chunk_ids = [f["id"] for f in chunk]
    return batch_idx, year, layer_group, chunk_ids, resp


def enrich_features(features: list, uffda_client_id: str) -> dict:
    """
    Call UFFDA /v1/fields/enrich for all features across all layer groups.
    Layer groups are called concurrently (up to MAX_CONCURRENT workers) to
    keep total time well under the HTTP timeout even for 60 fields.

    Returns: {"records": [...], "failed_ids": [...], "layer_errors": {...}}
    """
    headers = {
        "Content-Type": "application/json",
        "X-UFFDA-Client": uffda_client_id,
    }

    # Normalize features; ensure every feature has a unique non-empty ID
    api_features = []
    for idx, feat in enumerate(features):
        meta = feat.get("meti_meta") or {}
        start_at = meta.get("start_at") or DEFAULT_START_AT
        fid = feat.get("id") or feat.get("alt_id") or f"field-{idx}"
        api_features.append({
            "id": fid,
            "alt_id": fid,
            "geometry": feat["geometry"],
            "properties": {"id": fid, "alt_id": fid},
            "start_at": start_at,
        })

    all_input_ids = {f["id"] for f in api_features}

    # Partition by year → batches
    by_year: dict = {}
    for feat in api_features:
        year = _year_from_start_at(feat["start_at"])
        by_year.setdefault(year, []).append(feat)

    batches = []
    for year in sorted(by_year):
        year_feats = by_year[year]
        for offset in range(0, len(year_feats), BATCH_SIZE):
            batches.append((year, year_feats[offset:offset + BATCH_SIZE]))

    # Build flat task list: one entry per (batch, layer_group) combination
    tasks = [
        (batch_idx, year, chunk, layer_group, headers)
        for batch_idx, (year, chunk) in enumerate(batches)
        for layer_group in LAYER_GROUPS
    ]

    # Run all tasks concurrently, bounded by MAX_CONCURRENT
    raw: list = []
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as executor:
        futures = [executor.submit(_call_one, t) for t in tasks]
        for future in as_completed(futures):
            raw.append(future.result())

    # Merge: batch_data[batch_idx][fid] = record
    batch_data: dict = {}
    layer_errors: dict = {}

    for batch_idx, year, layer_group, chunk_ids, resp in raw:
        if resp is None:
            for fid in chunk_ids:
                layer_errors.setdefault(fid, []).append(layer_group)
            continue
        bd = batch_data.setdefault(batch_idx, {})
        for feat in resp.get("features", []):
            props = feat.get("properties", {})
            fid = props.get("id") or feat.get("id")
            if not fid:
                continue
            if fid not in bd:
                # Determine year from the batch this feature belongs to
                feat_year = next(
                    (y for bi, (y, _) in enumerate(batches) if bi == batch_idx),
                    year,
                )
                bd[fid] = {
                    "id": fid,
                    "alt_id": props.get("alt_id") or fid,
                    "year": feat_year,
                    "enrichment": {},
                    "derived": props.get("derived"),
                    "errors": props.get("errors"),
                }
            bd[fid]["enrichment"].update(props.get("enrichment") or {})

    results = [rec for bd in batch_data.values() for rec in bd.values()]
    returned_ids = {r["id"] for r in results}
    failed_ids = list(all_input_ids - returned_ids)

    return {
        "records": results,
        "failed_ids": failed_ids,
        "layer_errors": layer_errors,
    }
