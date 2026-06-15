import io
import json
import os
import tempfile
import uuid
import zipfile
from typing import List, Tuple

import fiona
import geopandas as gpd
from shapely.geometry import mapping, shape
from shapely.geometry.multipolygon import MultiPolygon
from shapely.geometry.polygon import Polygon, orient
from shapely.ops import unary_union
from shapely.validation import make_valid

# Enable KML driver
fiona.drvsupport.supported_drivers["KML"] = "rw"
fiona.drvsupport.supported_drivers["LIBKML"] = "rw"

SUPPORTED_EXTENSIONS = {".geojson", ".json", ".kml", ".shp", ".gpx", ".gml", ".zip"}


def _fix_geometry(geom) -> Tuple[any, List[str]]:
    """Fix common geometry issues. Returns (fixed_geom, list_of_issues_found)."""
    issues = []

    if geom is None:
        return geom, ["Null geometry — cannot process"]

    if not geom.is_valid:
        issues.append(f"Invalid geometry ({geom.geom_type}): self-intersection or duplicate points — auto-fixed with make_valid")
        geom = make_valid(geom)

    # Extract only polygon types from GeometryCollections
    if geom.geom_type == "GeometryCollection":
        polys = [g for g in geom.geoms if isinstance(g, (Polygon, MultiPolygon))]
        if not polys:
            return None, issues + ["GeometryCollection contained no Polygon/MultiPolygon parts"]
        geom = unary_union(polys)
        issues.append("GeometryCollection simplified — extracted Polygon/MultiPolygon parts only")

    # Normalize winding order (CCW exterior rings)
    if isinstance(geom, Polygon):
        geom = orient(geom, sign=1.0)
    elif isinstance(geom, MultiPolygon):
        geom = MultiPolygon([orient(p, sign=1.0) for p in geom.geoms])

    return geom, issues


def _compute_hectares(geom) -> float:
    """Approximate hectares from WGS84 geometry using geodesic area."""
    try:
        from pyproj import Geod
        geod = Geod(ellps="WGS84")
        if isinstance(geom, Polygon):
            area, _ = geod.geometry_area_perimeter(geom)
        elif isinstance(geom, MultiPolygon):
            area = sum(geod.geometry_area_perimeter(p)[0] for p in geom.geoms)
        else:
            return 0.0
        return round(abs(area) / 10_000, 4)
    except Exception:
        return 0.0


def _load_geodataframe(file_bytes: bytes, filename: str) -> gpd.GeoDataFrame:
    """Load a GeoDataFrame from raw file bytes, handling zip/KML/SHP/GeoJSON."""
    ext = os.path.splitext(filename.lower())[1]

    if ext == ".zip":
        with tempfile.TemporaryDirectory() as tmpdir:
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
                zf.extractall(tmpdir)
            return _load_from_directory(tmpdir)

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    try:
        if ext in {".kml"}:
            gdf = gpd.read_file(tmp_path, driver="KML")
        else:
            gdf = gpd.read_file(tmp_path)
        return gdf
    finally:
        os.unlink(tmp_path)


def _load_from_directory(dirpath: str) -> gpd.GeoDataFrame:
    """
    Walk a directory and load all supported GIS files.
    Multiple .shp files are concatenated into one GeoDataFrame,
    with a 'source_file' column tracking which file each feature came from.
    """
    shp_files = []
    other_files = []

    for root, _, files in os.walk(dirpath):
        for f in sorted(files):
            fext = os.path.splitext(f.lower())[1]
            full_path = os.path.join(root, f)
            if fext == ".shp":
                shp_files.append(full_path)
            elif fext in {".geojson", ".json", ".kml", ".gml"}:
                other_files.append((fext, full_path))

    if not shp_files and not other_files:
        raise ValueError("No supported GIS files found (expected .shp, .geojson, .kml, or .gml)")

    gdfs = []

    for shp_path in shp_files:
        try:
            gdf = gpd.read_file(shp_path)
            # Tag each row with the stem of the shapefile it came from
            stem = os.path.splitext(os.path.basename(shp_path))[0]
            gdf["_source_file"] = stem
            gdfs.append(gdf)
        except Exception as exc:
            raise ValueError(f"Could not read shapefile '{os.path.basename(shp_path)}': {exc}")

    for fext, fpath in other_files:
        try:
            kwargs = {"driver": "KML"} if fext == ".kml" else {}
            gdf = gpd.read_file(fpath, **kwargs)
            stem = os.path.splitext(os.path.basename(fpath))[0]
            gdf["_source_file"] = stem
            gdfs.append(gdf)
        except Exception as exc:
            raise ValueError(f"Could not read '{os.path.basename(fpath)}': {exc}")

    if len(gdfs) == 1:
        return gdfs[0]

    # Align CRS before concat — reproject all to the first file's CRS
    base_crs = gdfs[0].crs
    aligned = []
    for gdf in gdfs:
        if gdf.crs and base_crs and not gdf.crs.equals(base_crs):
            gdf = gdf.to_crs(base_crs)
        aligned.append(gdf)

    combined = gpd.pd.concat(aligned, ignore_index=True)
    return gpd.GeoDataFrame(combined, crs=base_crs)


def parse_file(file_bytes: bytes, filename: str) -> List[dict]:
    """
    Parse any supported GIS file into a list of normalized feature dicts.
    Each dict: {index, id, geometry (GeoJSON dict), geometry_type, properties, hectares, issues}
    """
    gdf = _load_geodataframe(file_bytes, filename)

    # Reproject to WGS84 if needed
    if gdf.crs is None:
        crs_detected = "unknown (assumed WGS84)"
    else:
        crs_detected = gdf.crs.to_string()
        if not gdf.crs.equals("EPSG:4326"):
            gdf = gdf.to_crs("EPSG:4326")

    features = []
    for i, row in gdf.iterrows():
        raw_geom = row.geometry
        fixed_geom, issues = _fix_geometry(raw_geom)

        if fixed_geom is None:
            geom_dict = None
            geom_type = "null"
            hectares = 0.0
        else:
            geom_dict = mapping(fixed_geom)
            geom_type = fixed_geom.geom_type
            hectares = _compute_hectares(fixed_geom)

        # Build properties from non-geometry columns
        props = {}
        for col in gdf.columns:
            if col in {"geometry", "_source_file"}:
                continue
            val = row[col]
            if hasattr(val, "item"):
                val = val.item()
            props[col] = val if val is not None else None

        # Feature id: prefer explicit id/fid, then shapefile stem, then index
        source_stem = row.get("_source_file") if "_source_file" in gdf.columns else None
        raw_id = props.pop("id", None) or props.pop("fid", None)
        feature_id = str(raw_id) if raw_id else (source_stem or f"FIELD_{i+1:03d}")

        features.append({
            "index": i,
            "id": feature_id,
            "geometry": geom_dict,
            "geometry_type": geom_type,
            "properties": props,
            "hectares": hectares,
            "issues": issues,
            "crs_detected": crs_detected,
        })

    return features
