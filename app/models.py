from typing import Any
from pydantic import BaseModel, Field


class PolygonRequest(BaseModel):
    polygon: dict[str, Any] = Field(..., description="GeoJSON Polygon geometry or Feature")


class Capture(BaseModel):
    image_id: str
    lat: float
    lon: float
    captured_at: str
    year: int
    is_pano: bool


class ImagesResponse(BaseModel):
    captures: list[Capture]
    years_available: list[int]
    truncated: bool = False


class AnalyzeResponse(BaseModel):
    roads: dict[str, Any]
    road_count: int


class ImageDetail(BaseModel):
    url: str
    captured_at: str
    lat: float
    lon: float
    is_pano: bool = False
