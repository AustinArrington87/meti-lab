"""
Ecosystem services suitability scoring.
Ported from uffda_test/ecosystem_services_report.py — pure functions, no file I/O.
"""
import math

DROUGHT_SEVERITY = {
    "D0 — Abnormally Dry": 0.2,
    "D1 — Moderate Drought": 0.4,
    "D2 — Severe Drought": 0.6,
    "D3 — Extreme Drought": 0.8,
    "D4 — Exceptional Drought": 1.0,
}

GRASSLAND_TOKENS = ("grass", "pasture", "fallow", "idle", "shrub", "herbaceous")

PROGRAMS = {
    "soil_carbon": {
        "title": "Soil Carbon Sequestration (Cropland)",
        "weights": {
            "land_use_fit": 0.40,
            "carbon_headroom": 0.30,
            "agronomic_fit": 0.20,
            "moisture_fit": 0.10,
        },
    },
    "grassland_conservation": {
        "title": "Grassland Conservation & Avoided Conversion",
        "weights": {
            "grassland_cover": 0.35,
            "persistence": 0.30,
            "conversion_pressure": 0.20,
            "additionality": 0.15,
        },
    },
    "water_resilience": {
        "title": "Water & Drought Resilience",
        "weights": {
            "drought_need": 0.40,
            "soil_vulnerability": 0.30,
            "irrigation_context": 0.20,
            "precip_deficit": 0.10,
        },
    },
    "biodiversity_habitat": {
        "title": "Biodiversity & Habitat Connectivity",
        "weights": {
            "natural_cover": 0.35,
            "cover_diversity": 0.25,
            "protected_connectivity": 0.25,
            "water_presence": 0.15,
        },
    },
}

RATING_BANDS = [
    (80, "Excellent"),
    (60, "High"),
    (40, "Moderate"),
    (0, "Low"),
]

EXPECTED_LAYERS = [
    "drought", "land_cover", "crop_history", "soil",
    "forest_loss", "weather", "irrigation", "protected_area",
]


def clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def rating(score):
    for threshold, label in RATING_BANDS:
        if score >= threshold:
            return label
    return "Low"


def soil_metric(enr, key):
    return (((enr.get("soil") or {}).get("primary") or {})
            .get("metrics") or {}).get(key, {}).get("value")


def weather_metric(enr, key):
    return ((enr.get("weather") or {}).get("summary") or {}).get(key, {}).get("value")


def land_cover_props(enr):
    lc = enr.get("land_cover") or {}
    props = {}
    dom = lc.get("dominant") or {}
    if dom.get("value_label"):
        props[dom["value_label"]] = (dom.get("confidence") or {}).get("pct_in_class", 0) or 0
    for o in lc.get("others") or []:
        if o.get("label"):
            props[o["label"]] = o.get("pct", 0) or 0
    return props


def is_grassland_label(label):
    return bool(label) and any(t in label.lower() for t in GRASSLAND_TOKENS)


def crop_history_grass_fraction(enr):
    ch = enr.get("crop_history") or {}
    vals = [v for v in (ch.get("values") or []) if v.get("status") == "ok"]
    if not vals:
        return None
    grass = sum(1 for v in vals if is_grassland_label(v.get("value_label")))
    return grass / len(vals)


def shannon_evenness(props):
    total = sum(props.values())
    if total <= 0:
        return 0.0
    fracs = [p / total for p in props.values() if p > 0]
    if len(fracs) <= 1:
        return 0.0
    h = -sum(f * math.log(f) for f in fracs)
    return h / math.log(len(fracs))


def score_soil_carbon(enr):
    props = land_cover_props(enr)
    cropland_pct = props.get("Cropland", 0)
    irr = (enr.get("irrigation") or {}).get("dominant")

    if irr in ("Irrigated", "Rainfed"):
        land_use = clamp(60 + cropland_pct)
    else:
        land_use = clamp(cropland_pct * 1.5)

    som = soil_metric(enr, "som@0-30")
    headroom = 50.0 if som is None else clamp((4.0 - som) / 4.0 * 100)

    ph = soil_metric(enr, "phh2o@0-30")
    agronomic = 60.0 if ph is None else clamp(100 - max(0, 6.0 - ph) * 25 - max(0, ph - 7.5) * 25)

    sev = DROUGHT_SEVERITY.get((enr.get("drought") or {}).get("value_label"), 0)
    moisture = clamp(100 - sev * 70)

    return {
        "land_use_fit": land_use,
        "carbon_headroom": headroom,
        "agronomic_fit": agronomic,
        "moisture_fit": moisture,
    }


def score_grassland(enr):
    props = land_cover_props(enr)
    grass_cover = sum(p for label, p in props.items() if is_grassland_label(label))

    frac = crop_history_grass_fraction(enr)
    persistence = 50.0 if frac is None else clamp(frac * 100)

    conversion = clamp(props.get("Cropland", 0) * 1.5)

    pa = (enr.get("protected_area") or {}).get("state")
    additionality = 40.0 if pa == "overlap" else 100.0

    return {
        "grassland_cover": clamp(grass_cover),
        "persistence": persistence,
        "conversion_pressure": conversion,
        "additionality": additionality,
    }


def score_water(enr):
    sev = DROUGHT_SEVERITY.get((enr.get("drought") or {}).get("value_label"), 0)
    drought_need = clamp(sev * 100)

    sand = soil_metric(enr, "sand@0-30")
    cec = soil_metric(enr, "cec@0-30")
    vuln = 50.0
    if sand is not None:
        vuln = clamp(sand)
    if cec is not None:
        vuln = clamp(vuln * 0.6 + (100 - clamp(cec * 4)) * 0.4)

    irr = (enr.get("irrigation") or {}).get("dominant")
    if irr == "Irrigated":
        irr_ctx = clamp(50 + sev * 50)
    elif irr == "Rainfed":
        irr_ctx = clamp(60 + sev * 40)
    else:
        irr_ctx = 20.0

    precip = weather_metric(enr, "precip_total")
    deficit = 50.0 if precip is None else clamp((800 - precip) / 800 * 100)

    return {
        "drought_need": drought_need,
        "soil_vulnerability": vuln,
        "irrigation_context": irr_ctx,
        "precip_deficit": deficit,
    }


def score_biodiversity(enr):
    props = land_cover_props(enr)
    natural = sum(
        p for label, p in props.items()
        if is_grassland_label(label) or "tree" in label.lower() or "water" in label.lower()
    )
    built = sum(p for label, p in props.items() if "built" in label.lower())
    natural_cover = clamp(natural - built)

    diversity = clamp(shannon_evenness(props) * 100)

    pa = enr.get("protected_area") or {}
    if pa.get("state") == "overlap":
        connectivity = clamp(50 + (pa.get("overlap_pct", 0) or 0))
    else:
        connectivity = 25.0

    water = clamp(sum(p for label, p in props.items() if "water" in label.lower()) * 4)

    return {
        "natural_cover": natural_cover,
        "cover_diversity": diversity,
        "protected_connectivity": connectivity,
        "water_presence": water,
    }


SCORERS = {
    "soil_carbon": score_soil_carbon,
    "grassland_conservation": score_grassland,
    "water_resilience": score_water,
    "biodiversity_habitat": score_biodiversity,
}


def _weighted(subscores, weights):
    return clamp(sum(subscores[k] * w for k, w in weights.items()))


def _data_confidence(rec):
    enr = rec.get("enrichment") or {}
    present = sum(1 for layer in EXPECTED_LAYERS if enr.get(layer))
    base = present / len(EXPECTED_LAYERS) * 100
    if rec.get("errors"):
        base -= 10
    return clamp(base)


def _extract_crop_history(enr):
    """Return sorted list of {year, crop} from CDL crop_history layer."""
    ch = enr.get("crop_history") or {}
    vals = [v for v in (ch.get("values") or []) if v.get("status") == "ok"]
    return sorted(
        [{"year": v.get("year"), "crop": v.get("value_label")} for v in vals],
        key=lambda x: x["year"] or 0,
    )


def score_field(rec: dict) -> dict:
    enr = rec.get("enrichment") or {}
    programs = {}
    for key, scorer in SCORERS.items():
        subs = scorer(enr)
        total = round(_weighted(subs, PROGRAMS[key]["weights"]), 1)
        programs[key] = {"score": total, "rating": rating(total), "subs": subs}
    best = max(programs, key=lambda k: programs[k]["score"])
    crop_history = _extract_crop_history(enr)
    return {
        "id": rec.get("id"),
        "year": rec.get("year"),
        "area_ac": (rec.get("derived") or {}).get("area_ac"),
        "land_cover": ((enr.get("land_cover") or {}).get("dominant") or {}).get("value_label"),
        "drought": (enr.get("drought") or {}).get("value_label"),
        "irrigation": (enr.get("irrigation") or {}).get("dominant"),
        "som": soil_metric(enr, "som@0-30"),
        "ph": soil_metric(enr, "phh2o@0-30"),
        "forest_loss": ((enr.get("forest_loss") or {}).get("forest_loss_pct") or {}).get("value"),
        "crop_history": crop_history,
        "programs": programs,
        "best_fit": best,
        "best_fit_title": PROGRAMS[best]["title"],
        "best_fit_score": programs[best]["score"],
        "best_fit_rating": programs[best]["rating"],
        "confidence": _data_confidence(rec),
    }


def score_fields(records: list) -> list:
    return [score_field(r) for r in records]
