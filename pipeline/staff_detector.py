from __future__ import annotations

import json
import os
from typing import Optional

import numpy as np

# Staff uniform HSV ranges — configurable per store via STAFF_UNIFORM_CONFIG env var.
# Default: typical retail staff wear bright magenta/pink or green uniforms.
_DEFAULT_UNIFORM_RANGES = [
    # (h_lo, h_hi, s_lo, s_hi, v_lo, v_hi) in OpenCV HSV scale (H: 0-179, S/V: 0-255)
    (140, 179, 80, 255, 60, 255),   # pink / magenta
    (0,   10,  80, 255, 60, 255),   # red (wraps at 0)
    (35,  85,  80, 255, 60, 255),   # green
]


def _load_ranges() -> list[tuple[int, int, int, int, int, int]]:
    cfg_path = os.getenv("STAFF_UNIFORM_CONFIG", "")
    if cfg_path and os.path.exists(cfg_path):
        with open(cfg_path) as f:
            return [tuple(r) for r in json.load(f)]
    return _DEFAULT_UNIFORM_RANGES


_UNIFORM_RANGES = _load_ranges()
_UNIFORM_PIXEL_THRESHOLD = 0.20   # ≥20% of upper-body pixels match uniform color = staff


def is_staff(frame: "np.ndarray", bbox: tuple[int, int, int, int]) -> tuple[bool, float]:
    """
    Returns (is_staff, confidence).
    bbox = (x1, y1, x2, y2) in pixel coordinates.
    """
    try:
        import cv2
    except ImportError:
        return False, 0.0

    x1, y1, x2, y2 = bbox
    h = y2 - y1
    # Analyse upper-body region (top 50% of the bounding box)
    upper_y2 = y1 + max(1, h // 2)
    crop = frame[y1:upper_y2, x1:x2]
    if crop.size == 0:
        return False, 0.0

    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    total_pixels = hsv.shape[0] * hsv.shape[1]

    match_pixels = 0
    for h_lo, h_hi, s_lo, s_hi, v_lo, v_hi in _UNIFORM_RANGES:
        lo = np.array([h_lo, s_lo, v_lo], dtype=np.uint8)
        hi = np.array([h_hi, s_hi, v_hi], dtype=np.uint8)
        mask = cv2.inRange(hsv, lo, hi)
        match_pixels += int(np.count_nonzero(mask))

    ratio = match_pixels / total_pixels if total_pixels > 0 else 0.0
    detected = ratio >= _UNIFORM_PIXEL_THRESHOLD
    confidence = min(1.0, ratio / _UNIFORM_PIXEL_THRESHOLD) if detected else ratio
    return detected, round(confidence, 3)
