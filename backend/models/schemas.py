from typing import Any, List, Optional
from pydantic import BaseModel


class FeatureIssue(BaseModel):
    field_index: int
    description: str


class ParsedFeature(BaseModel):
    index: int
    id: str
    geometry: dict
    geometry_type: str
    properties: dict
    hectares: Optional[float]
    issues: List[str]


class UploadResponse(BaseModel):
    session_id: str
    feature_count: int
    features: List[ParsedFeature]
    file_name: str
    crs_detected: str


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    reply: str
    tool_calls: List[dict] = []
    export_ready: bool = False


class ExportPayload(BaseModel):
    type: str = "FeatureCollection"
    features: List[dict]
    metadata: dict = {}


class SourceCheckRequest(BaseModel):
    session_id: str
    feature_index: int
