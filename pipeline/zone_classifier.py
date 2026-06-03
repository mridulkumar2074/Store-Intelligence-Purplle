from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional


@dataclass
class Zone:
    zone_id: str
    zone_name: str
    zone_type: str
    is_revenue_zone: bool
    sku_zone: Optional[str]
    polygon: list[tuple[float, float]]


def _point_in_polygon(x: float, y: float, polygon: list[tuple[float, float]]) -> bool:
    """Ray-casting algorithm for point-in-polygon test."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


class ZoneClassifier:
    def __init__(self, layout_path: str, store_id: str, camera_id: str) -> None:
        with open(layout_path, encoding="utf-8") as f:
            layout = json.load(f)

        store = layout["stores"].get(store_id, {})
        self._zones: list[Zone] = []
        for z in store.get("zones", []):
            if z.get("camera_id") == camera_id:
                poly = [(float(p[0]), float(p[1])) for p in z["polygon"]]
                self._zones.append(
                    Zone(
                        zone_id=z["zone_id"],
                        zone_name=z["zone_name"],
                        zone_type=z["zone_type"],
                        is_revenue_zone=bool(z.get("is_revenue_zone", False)),
                        sku_zone=z.get("sku_zone"),
                        polygon=poly,
                    )
                )

    def classify(self, cx: float, cy: float) -> Optional[Zone]:
        for zone in self._zones:
            if _point_in_polygon(cx, cy, zone.polygon):
                return zone
        return None

    @property
    def zones(self) -> list[Zone]:
        return list(self._zones)
