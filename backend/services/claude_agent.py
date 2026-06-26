import json
import time
from typing import Any, Generator, List, Optional

import anthropic

from backend.config import settings

client = anthropic.Anthropic(api_key=settings.claude_api_key)

# In-memory session store: session_id → {features, messages, account_id, is_admin, export_ready}
_sessions: dict = {}

SYSTEM_PROMPT = """You are a GIS data specialist for the METI platform (Measurement, Evidence, and Transparency Initiative), operated by MillPont.

Your job is to help users prepare their field boundary data for submission to the METI source ledger. You:
1. Analyze uploaded GIS files (GeoJSON, KML, Shapefile, etc.)
2. Identify and fix geometry issues (self-intersections, invalid rings, wrong winding order)
3. Guide the user to provide all required and recommended metadata
4. Teach the user what each METI field means when asked
5. Produce a valid METI GeoJSON FeatureCollection ready for API submission

## METI Source Schema — Required Fields
- `id` (string): Unique identifier for the feature (e.g., "FARM-001"). Max 49 chars.
- `properties.start_at` (ISO 8601 UTC string): Start of the reporting period (e.g., "2024-01-01T00:00:00.000Z")
- `properties.end_at` (ISO 8601 UTC string): End of the reporting period. Must be after start_at.
- `geometry` (GeoJSON): Valid WGS84 Polygon or MultiPolygon.

## METI Source Schema — Optional but Recommended
- `methodology` (string): Environmental program type. Common values: "Agriculture", "Biochar Production", "Reforestation", "Wetland Restoration"
- `source_type` (enum): FIELD, FACILITY, DEVICE, PROGRAM, JURISDICTIONAL. Default: FIELD
- `attribute_type` (array): CARBON_REMOVAL, CARBON_AVOIDANCE, CI_SCORE, BIODIVERSITY, RENEWABLE_ENERGY, WATER_QUALITY, WATER_QUANTITY
- `geometry_source` (enum): CUSTODIAN_DRAWN, AUTHORITATIVE_GIS, EXTERNAL_REGISTRY
- `tags` (array of strings): Free-form labels
- `steward_id` (string): ID of the land steward / producer
- `project_id` (string): ID of the project or farm
- `outcome_reporting_year` (integer): The year outcomes are reported for

## Geometry Rules
- Coordinates must be WGS84 (longitude, latitude)
- Exterior rings must be counter-clockwise (CCW)
- No self-intersections
- Polygon must close (first == last coordinate)
- `end_at` must be strictly after `start_at`
- DEVICE source_type requires a measurement_point_id
- PROGRAM and JURISDICTIONAL types require constituent_ssids
- JURISDICTIONAL requires geometry_source = AUTHORITATIVE_GIS

## Workflow Rules
- When the user gives dates or any metadata that applies to ALL features (e.g. "Jan 1 – Dec 31 2024"), call `set_feature_metadata` with `feature_index: -1` to apply to every feature at once. Do NOT ask the user to repeat values per field.
- Always convert human-friendly dates to ISO 8601 UTC before saving (e.g. "Jan 1 2024" → "2024-01-01T00:00:00.000Z", "Dec 31 2024" → "2024-12-31T23:59:59.000Z").
- After saving metadata, call `get_feature_summary` so you can confirm what's still missing.
- Once required fields (start_at, end_at, geometry) are set, ask for the three recommended fields IN A SINGLE MESSAGE before exporting:
  1. **source_type** — "What type of source is this? Options: FIELD (default for agricultural land), FACILITY, DEVICE, PROGRAM, JURISDICTIONAL"
  2. **attribute_type** — "What environmental attributes apply? Pick one or more: CARBON_REMOVAL, CARBON_AVOIDANCE, CI_SCORE, BIODIVERSITY, RENEWABLE_ENERGY, WATER_QUALITY, WATER_QUANTITY"
  3. **geometry_source** — "How was the boundary geometry created? Options: CUSTODIAN_DRAWN (farmer/operator drew it), AUTHORITATIVE_GIS (from a government dataset), EXTERNAL_REGISTRY (from a third-party registry)"
- If the user skips any of these, save the rest and move on — do not block export on optional fields.
- After setting or updating `start_at` or `end_at`, call `check_spatial_conflicts` to get fresh conflict data. Do NOT claim any risk status before running it.
- Once recommended fields are collected (or skipped), call `check_spatial_conflicts` then report the results. If any features have `risk: "red"` (confirmed geometry + date overlap), you MUST tell the user before calling `export_meti_geojson`: state clearly which fields conflict and with which source IDs. Then ask if they still want to export. If `risk: "yellow"` (spatial overlap, dates unknown), mention it as a caution but do not block export. Only call `export_meti_geojson` after the user has acknowledged any red conflicts.

## Spatial Checks
All geometry is stored and processed server-side — it is never passed to you directly. To check whether uploaded boundaries conflict with existing METI sources, call `check_spatial_conflicts`. This runs a PostGIS ST_Intersects query plus temporal overlap detection and returns only risk metadata (green/yellow/red per feature, plus conflicting source IDs). No coordinate data is included in the result.
- Be concise. Don't repeat the full field list after every message — just tell the user what's still needed.
- If the user asks what any field means, call `explain_schema` with that field name.

## Field Insights (UFFDA Enrichment)
You can call `get_field_insights` to access environmental enrichment data and ecosystem-services scores for each field. This data is fetched via UFFDA and covers: land cover (CDL), soil organic matter, pH, drought status (USDM), crop history, irrigation regime, and protected area overlap.

Each field is scored 0–100 for four program archetypes:
- **Soil Carbon Sequestration (Cropland)**: High scores (≥60) indicate active cropland with room to build soil organic matter — strong candidate for programs like ESMC, Soil & Water Outcomes Fund, or voluntary carbon markets.
- **Grassland Conservation & Avoided Conversion**: High scores indicate intact grassland/pasture under conversion pressure — relevant for ACR or CAR grassland protocols.
- **Water & Drought Resilience**: High scores indicate drought-stressed fields with vulnerable soils — relevant for water quality/resilience programs.
- **Biodiversity & Habitat Connectivity**: High scores indicate natural cover diversity near protected lands.

Ratings: Excellent ≥ 80, High ≥ 60, Moderate ≥ 40, Low < 40.
Scores are a **screening heuristic** for triage — not a registry-level eligibility determination.
When discussing carbon market fit, cite the specific score and key drivers (e.g., "SOM at 1.8% means high carbon headroom").
If `get_field_insights` returns `status: not_fetched`, tell the user to click the "Get Insights" button to fetch UFFDA data first.
"""

# session_id is intentionally absent from all tool schemas —
# it is injected automatically by _dispatch_tool from the chat_turn context.
TOOLS = [
    {
        "name": "get_feature_summary",
        "description": "Get a summary of uploaded features: count, geometry types, detected issues, missing METI fields, and last-known conflict risk (use check_spatial_conflicts for a fresh check).",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "fix_geometry",
        "description": "Apply make_valid and CCW winding-order fix to a specific feature's geometry.",
        "input_schema": {
            "type": "object",
            "properties": {
                "feature_index": {"type": "integer", "description": "0-based index of the feature to fix"}
            },
            "required": ["feature_index"]
        }
    },
    {
        "name": "set_feature_metadata",
        "description": "Save METI metadata fields onto features. Use feature_index -1 to apply to ALL features at once (e.g. a shared reporting period). Use 0+ for a specific feature. Always convert human dates to ISO 8601 UTC before calling.",
        "input_schema": {
            "type": "object",
            "properties": {
                "feature_index": {
                    "type": "integer",
                    "description": "-1 = all features, 0+ = specific feature by index"
                },
                "fields": {
                    "type": "object",
                    "description": "METI fields to set, e.g. {\"start_at\": \"2025-01-01T00:00:00.000Z\", \"end_at\": \"2025-12-31T23:59:59.000Z\"}",
                    "additionalProperties": True
                }
            },
            "required": ["feature_index", "fields"]
        }
    },
    {
        "name": "export_meti_geojson",
        "description": "Assemble and validate the final METI GeoJSON FeatureCollection. Call this once all required fields (start_at, end_at, geometry) are set.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "explain_schema",
        "description": "Return a detailed explanation of a METI source schema field.",
        "input_schema": {
            "type": "object",
            "properties": {
                "field_name": {
                    "type": "string",
                    "description": "The METI field to explain (e.g. 'methodology', 'conflict', 'source_type')"
                }
            },
            "required": ["field_name"]
        }
    },
    {
        "name": "get_field_insights",
        "description": "Return UFFDA enrichment data and ecosystem-services program scores for all fields. Requires the user to have clicked 'Get Insights' first. Returns land cover, soil organic matter, pH, drought status, and carbon market eligibility scores (0-100) for each field.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "check_spatial_conflicts",
        "description": "Run a PostGIS spatial + temporal overlap check against the METI source ledger for all uploaded features. Returns per-feature risk status only — no geometry is included. Call this after setting start_at/end_at dates, or any time you need current conflict data before exporting.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
]

FIELD_EXPLANATIONS = {
    "methodology": "The environmental program or practice associated with this source boundary. Common values: 'Agriculture', 'Biochar Production', 'Reforestation', 'Wetland Restoration'. This tells METI what kind of environmental outcome is being tracked.",
    "conflict": "A boolean flag set automatically by METI when a submitted boundary overlaps with an existing source in the ledger for the same time period. You cannot set this manually — METI's conflict detection engine computes it on ingestion.",
    "source_type": "Describes the physical or organizational nature of the source. FIELD = agricultural field or land parcel; FACILITY = industrial or processing site; DEVICE = metering/sensor device; PROGRAM = aggregated program of many sub-sources; JURISDICTIONAL = a government-administered geography.",
    "attribute_type": "Array of environmental attribute classifications. Examples: CARBON_REMOVAL (sequestration), CARBON_AVOIDANCE (prevented emissions), BIODIVERSITY (habitat protection), RENEWABLE_ENERGY (clean power), WATER_QUALITY / WATER_QUANTITY (water stewardship), CI_SCORE (carbon intensity).",
    "geometry_source": "How the boundary geometry was produced. CUSTODIAN_DRAWN = farmer/operator drew it themselves; AUTHORITATIVE_GIS = sourced from a government or official GIS dataset; EXTERNAL_REGISTRY = sourced from a third-party registry.",
    "steward_id": "An identifier for the land steward, producer, or operator responsible for this source. This is free-form text — typically a producer ID from your program's registry.",
    "project_id": "An identifier for the project, farm, or program grouping this source belongs to. Used to query and filter sources by project.",
    "outcome_reporting_year": "The calendar year for which environmental outcomes are being claimed. Typically the year the activity occurred (e.g., 2024).",
    "alt_id": "An alternative identifier for the source, set automatically by METI. Derived from your submitted feature id. Max 49 characters.",
    "start_at": "The start of the time period this boundary is active / the reporting period begins. Must be a full ISO 8601 UTC timestamp, e.g., '2024-01-01T00:00:00.000Z'.",
    "end_at": "The end of the time period. Must be after start_at. e.g., '2024-12-31T23:59:59.000Z'.",
    "hectares": "The area of the boundary in hectares. METI computes this server-side from the submitted geometry.",
    "tags": "Free-form string labels you can attach to a source for filtering and organization. Example: ['organic', 'corn', 'iowa'].",
    "constituent_ssids": "Required for PROGRAM and JURISDICTIONAL source types. An array of source IDs (src_...) that this aggregated source encompasses.",
    "measurement_point_id": "Required for DEVICE source types. The ID of the measurement point (sensor, meter) associated with this device source.",
}


def _tool_get_feature_summary(session_id: str) -> dict:
    session = _sessions.get(session_id)
    if not session:
        return {"error": "Session not found"}
    features = session["features"]
    risk_results = session.get("risk_results", {})
    summary = {
        "feature_count": len(features),
        "features": []
    }
    for f in features:
        risk_info = risk_results.get(f["id"], {})
        entry = {
            "index": f["index"],
            "id": f["id"],
            "geometry_type": f["geometry_type"],
            "hectares": f["hectares"],
            "crs_detected": f.get("crs_detected", "unknown"),
            "issues": f["issues"],
            "metadata_present": {k: v for k, v in f.get("meti_meta", {}).items() if v is not None},
            "missing_required": [],
            "conflict_risk": risk_info.get("risk", "unchecked"),
            "conflict_with": risk_info.get("conflict_with", []),
        }
        meta = f.get("meti_meta", {})
        if not meta.get("start_at"):
            entry["missing_required"].append("start_at")
        if not meta.get("end_at"):
            entry["missing_required"].append("end_at")
        summary["features"].append(entry)
    return summary


def _tool_fix_geometry(session_id: str, feature_index: int) -> dict:
    from backend.services.gis_processor import _fix_geometry
    from shapely.geometry import shape, mapping

    session = _sessions.get(session_id)
    if not session:
        return {"error": "Session not found"}
    features = session["features"]
    target = next((f for f in features if f["index"] == feature_index), None)
    if not target:
        return {"error": f"Feature at index {feature_index} not found"}

    if not target["geometry"]:
        return {"error": "Feature has null geometry"}

    geom = shape(target["geometry"])
    fixed, issues = _fix_geometry(geom)
    if fixed is not None:
        from shapely.geometry import mapping as geom_mapping
        target["geometry"] = geom_mapping(fixed)
        target["geometry_type"] = fixed.geom_type
        target["issues"] = issues
    return {
        "feature_id": target["id"],
        "geometry_type": target["geometry_type"],
        "fixes_applied": issues,
        "valid": fixed.is_valid if fixed else False
    }


def _tool_set_feature_metadata(session_id: str, feature_index: int, fields: dict) -> dict:
    session = _sessions.get(session_id)
    if not session:
        return {"error": "Session not found"}

    targets = session["features"] if feature_index == -1 else [
        f for f in session["features"] if f["index"] == feature_index
    ]
    if not targets:
        return {"error": f"Feature at index {feature_index} not found"}

    for f in targets:
        if "meti_meta" not in f:
            f["meti_meta"] = {}
        f["meti_meta"].update(fields)

    # Dates changed → cached risk results are stale; clear them
    if "start_at" in fields or "end_at" in fields:
        session.pop("risk_results", None)

    # Auto-regenerate export payload if all required fields are now present
    all_ready = all(
        f.get("meti_meta", {}).get("start_at")
        and f.get("meti_meta", {}).get("end_at")
        and f.get("geometry")
        for f in session["features"]
    )
    if all_ready:
        _tool_export_meti_geojson(session_id)

    return {"updated": [f["id"] for f in targets], "fields_set": list(fields.keys())}


def _tool_export_meti_geojson(session_id: str) -> dict:
    session = _sessions.get(session_id)
    if not session:
        return {"error": "Session not found"}

    geo_features = []
    errors = []

    for f in session["features"]:
        meta = f.get("meti_meta", {})
        feature_id = f["id"]

        if not meta.get("start_at"):
            errors.append(f"Feature '{feature_id}': missing start_at")
        if not meta.get("end_at"):
            errors.append(f"Feature '{feature_id}': missing end_at")
        if not f["geometry"]:
            errors.append(f"Feature '{feature_id}': null geometry")

        props = {
            "start_at": meta.get("start_at"),
            "end_at": meta.get("end_at"),
        }

        geo_feature = {
            "type": "Feature",
            "id": feature_id,
            "properties": props,
            "geometry": f["geometry"]
        }
        geo_features.append(geo_feature)

    if errors:
        return {"error": "Validation failed", "details": errors}

    payload = {
        "type": "FeatureCollection",
        "features": geo_features
    }

    # Attach top-level METI fields from first feature's metadata
    first_meta = session["features"][0].get("meti_meta", {}) if session["features"] else {}
    top_level_fields = ["methodology", "source_type", "attribute_type", "geometry_source",
                        "tags", "steward_id", "project_id", "outcome_reporting_year"]
    meti_payload = {"feature_collection": payload}
    for field in top_level_fields:
        if first_meta.get(field):
            meti_payload[field] = first_meta[field]

    session["export_payload"] = meti_payload
    session["export_ready"] = True

    # Return a lightweight summary — no raw geometry (saves tokens)
    top_level_summary = {k: v for k, v in meti_payload.items() if k != "feature_collection"}
    feature_summary = [
        {"id": gf["id"], "start_at": gf["properties"]["start_at"], "end_at": gf["properties"]["end_at"]}
        for gf in geo_features
    ]
    return {
        "success": True,
        "feature_count": len(geo_features),
        "top_level_fields": top_level_summary,
        "features": feature_summary,
    }


def _tool_get_field_insights(session_id: str) -> dict:
    session = _sessions.get(session_id)
    if not session:
        return {"error": "Session not found"}
    results = session.get("enrichment_results")
    if not results:
        return {"status": "not_fetched", "message": "Click 'Get Insights' to fetch UFFDA enrichment data."}
    return {"status": "ok", "scored": results["scored"]}


def _tool_check_spatial_conflicts(session_id: str) -> dict:
    from backend.services import meti_client
    session = _sessions.get(session_id)
    if not session:
        return {"error": "Session not found"}

    risk_map = meti_client.check_conflicts_db(session_features=session["features"])
    session["risk_results"] = risk_map

    # Return only risk metadata — geometry stays server-side
    return {
        "risk_map": {
            fid: {
                "risk": info["risk"],
                "conflict_with": info.get("conflict_with", []),
            }
            for fid, info in risk_map.items()
        },
        "summary": {
            "green": sum(1 for v in risk_map.values() if v["risk"] == "green"),
            "yellow": sum(1 for v in risk_map.values() if v["risk"] == "yellow"),
            "red": sum(1 for v in risk_map.values() if v["risk"] == "red"),
        },
    }


def _strip_geometry(obj: Any) -> Any:
    """Recursively remove 'geometry' keys from tool results — coordinate arrays never reach the Claude API."""
    if isinstance(obj, dict):
        return {k: _strip_geometry(v) for k, v in obj.items() if k != "geometry"}
    if isinstance(obj, list):
        return [_strip_geometry(item) for item in obj]
    return obj


def _tool_explain_schema(field_name: str) -> dict:
    explanation = FIELD_EXPLANATIONS.get(field_name.lower())
    if explanation:
        return {"field": field_name, "explanation": explanation}
    return {
        "field": field_name,
        "explanation": f"No built-in explanation for '{field_name}'. It may be a custom or less-common METI field — refer to the METI API docs at api.millpont.com."
    }


def _dispatch_tool(tool_name: str, tool_input: dict, session_id: str) -> Any:
    """Dispatch a tool call, injecting session_id. All results pass through _strip_geometry
    so coordinate arrays can never accidentally reach the Claude API."""
    if tool_name == "get_feature_summary":
        result = _tool_get_feature_summary(session_id)
    elif tool_name == "fix_geometry":
        result = _tool_fix_geometry(session_id, **tool_input)
    elif tool_name == "set_feature_metadata":
        result = _tool_set_feature_metadata(session_id, **tool_input)
    elif tool_name == "export_meti_geojson":
        result = _tool_export_meti_geojson(session_id)
    elif tool_name == "explain_schema":
        result = _tool_explain_schema(**tool_input)
    elif tool_name == "get_field_insights":
        result = _tool_get_field_insights(session_id)
    elif tool_name == "check_spatial_conflicts":
        result = _tool_check_spatial_conflicts(session_id)
    else:
        result = {"error": f"Unknown tool: {tool_name}"}
    return _strip_geometry(result)


def init_session(
    session_id: str,
    features: List[dict],
    account_id: Optional[str] = None,
    is_admin: bool = False,
    user_email: Optional[str] = None,
) -> None:
    """Initialize a new agent session with parsed features and account context."""
    for f in features:
        f.setdefault("meti_meta", {})
    _sessions[session_id] = {
        "features": features,
        "messages": [],
        "account_id": account_id,
        "is_admin": is_admin,
        "user_email": user_email,
        "export_ready": False,
        "export_payload": None,
    }


def get_opening_message(session_id: str) -> str:
    """Generate the agent's first message summarizing the upload."""
    summary = _tool_get_feature_summary(session_id)
    if "error" in summary:
        return "Sorry, I couldn't load your session. Please try uploading again."

    features = summary["features"]
    count = summary["feature_count"]
    lines = [f"I've loaded **{count} field{'s' if count != 1 else ''}** from your file.\n"]

    for f in features:
        label = f"**{f['id']}**"
        ha = f"{f['hectares']} ha" if f['hectares'] else "area unknown"
        issues = f" ⚠️ Issues: {'; '.join(f['issues'])}" if f['issues'] else " ✓ Geometry looks clean"
        missing = f" | Missing: {', '.join(f['missing_required'])}" if f['missing_required'] else ""
        lines.append(f"- {label} ({f['geometry_type']}, {ha}){issues}{missing}")

    all_missing = set()
    for f in features:
        all_missing.update(f["missing_required"])

    if all_missing:
        lines.append(f"\nTo build a valid METI payload I'll need: **{', '.join(sorted(all_missing))}**.")
        lines.append("I'll also ask about optional fields like `methodology`, `steward_id`, and `project_id`.")
        lines.append("\nWhat's the **reporting period** for these fields? (start and end dates, e.g. Jan 1 – Dec 31, 2024)")
    else:
        lines.append("\nAll required fields look good! Want me to run a METI schema validation and export?")

    return "\n".join(lines)


def chat_turn(session_id: str, user_message: str) -> Generator[str, None, None]:
    """
    Stream a chat turn with the Claude agent.
    Yields chunks of text. Handles tool use internally.
    """
    session = _sessions.get(session_id)
    if not session:
        yield "Session not found. Please upload a file first."
        return

    session["messages"].append({"role": "user", "content": user_message})

    # Keep history bounded to avoid rate limits. Trim from the front but
    # only at a user-turn boundary so tool_use/tool_result pairs stay intact.
    MAX_MESSAGES = 30
    msgs = session["messages"]
    if len(msgs) > MAX_MESSAGES:
        trim_to = len(msgs) - MAX_MESSAGES
        while trim_to < len(msgs) and msgs[trim_to].get("role") != "user":
            trim_to += 1
        session["messages"] = msgs[trim_to:]

    while True:
        for _attempt in range(2):
            try:
                response = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=4096,
                    system=SYSTEM_PROMPT,
                    tools=TOOLS,
                    messages=session["messages"],
                )
                break  # success — exit retry loop
            except anthropic.RateLimitError as exc:
                if _attempt == 1:
                    yield "\n\nI've hit the API rate limit and couldn't recover. Please wait a minute and send your message again."
                    return
                wait = 60
                try:
                    wait = int(exc.response.headers.get("retry-after", 60))
                except Exception:
                    pass
                yield f"\n\n*Rate limit reached — retrying in {wait}s…*"
                time.sleep(wait)
            except anthropic.APIStatusError as exc:
                yield f"\n\nThe AI service returned an error (HTTP {exc.status_code}). Please try again."
                return
            except anthropic.APIError:
                yield "\n\nCouldn't reach the AI service. Please try again."
                return
        else:
            return  # both attempts exhausted

        # Collect assistant content blocks
        assistant_content = []
        text_parts = []
        tool_uses = []

        for block in response.content:
            assistant_content.append(block)
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append(block)

        session["messages"].append({"role": "assistant", "content": assistant_content})

        # Yield text to the client
        if text_parts:
            yield "".join(text_parts)

        # If no tool use, we're done
        if not tool_uses or response.stop_reason != "tool_use":
            break

        # Execute tools and add results
        tool_results = []
        for tool_block in tool_uses:
            result = _dispatch_tool(tool_block.name, tool_block.input, session_id)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_block.id,
                "content": json.dumps(result)
            })
            # Signal export readiness via a special marker
            if tool_block.name == "export_meti_geojson" and result.get("success"):
                session["export_ready"] = True
            # Signal that dates changed so the frontend can re-run the risk check
            if tool_block.name == "set_feature_metadata":
                fields = tool_block.input.get("fields", {})
                if "start_at" in fields or "end_at" in fields:
                    session["dates_updated"] = True

        session["messages"].append({"role": "user", "content": tool_results})
        # Continue loop so model can respond to tool results


def get_export_payload(session_id: str) -> Optional[dict]:
    """Return the assembled METI GeoJSON payload if ready."""
    session = _sessions.get(session_id)
    if not session:
        return None
    return session.get("export_payload")


def is_export_ready(session_id: str) -> bool:
    session = _sessions.get(session_id)
    return bool(session and session.get("export_ready"))


def pop_dates_updated(session_id: str) -> bool:
    """Return True (and reset the flag) if dates were set during the last turn."""
    session = _sessions.get(session_id)
    if not session:
        return False
    updated = session.pop("dates_updated", False)
    return bool(updated)
