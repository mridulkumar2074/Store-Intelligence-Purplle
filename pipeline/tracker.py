from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass
class TrackState:
    track_id: int
    bbox: tuple[int, int, int, int]   # x1, y1, x2, y2
    visitor_id: str
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    lost_at: Optional[float] = None
    current_zone: Optional[str] = None
    zone_entered_at: Optional[float] = None
    dwell_seconds: float = 0.0
    appearance: Optional[np.ndarray] = None    # colour histogram feature vector
    is_staff: bool = False
    session_seq: int = 0


class ReIDManager:
    """
    Lightweight re-identification using colour histogram similarity.
    Keeps a window of recently exited tracks and matches new entries.
    """

    REENTRY_WINDOW_SECONDS = 300   # 5 minutes
    SIMILARITY_THRESHOLD = 0.82

    def __init__(self) -> None:
        self._active: dict[int, TrackState] = {}
        self._exited: list[TrackState] = []
        self._visitor_counter = 0
        self._id_to_visitor: dict[int, str] = {}
        # Cross-camera deduplication: shared appearance store
        self._cross_cam_exits: list[tuple[str, float, Optional[np.ndarray]]] = []

    def register_new_track(
        self,
        track_id: int,
        bbox: tuple[int, int, int, int],
        appearance: Optional[np.ndarray] = None,
    ) -> TrackState:
        visitor_id = self._match_reentry(appearance)
        is_reentry = visitor_id is not None
        if visitor_id is None:
            visitor_id = self._new_visitor_id()

        state = TrackState(
            track_id=track_id,
            bbox=bbox,
            visitor_id=visitor_id,
            appearance=appearance,
        )
        self._active[track_id] = state
        return state, is_reentry

    def update_track(
        self,
        track_id: int,
        bbox: tuple[int, int, int, int],
        appearance: Optional[np.ndarray] = None,
    ) -> Optional[TrackState]:
        state = self._active.get(track_id)
        if state is None:
            return None
        state.bbox = bbox
        state.last_seen = time.time()
        if appearance is not None:
            state.appearance = appearance
        return state

    def close_track(self, track_id: int) -> Optional[TrackState]:
        state = self._active.pop(track_id, None)
        if state is not None:
            state.lost_at = time.time()
            self._exited.append(state)
            self._cross_cam_exits.append((state.visitor_id, state.lost_at, state.appearance))
            self._prune_exited()
        return state

    def get_active(self, track_id: int) -> Optional[TrackState]:
        return self._active.get(track_id)

    def cross_camera_lookup(self, appearance: Optional[np.ndarray]) -> Optional[str]:
        """Return a visitor_id from cross-camera exits if appearance matches."""
        if appearance is None:
            return None
        now = time.time()
        for visitor_id, exit_time, feat in reversed(self._cross_cam_exits):
            if now - exit_time > self.REENTRY_WINDOW_SECONDS:
                continue
            if feat is not None and _cosine_sim(appearance, feat) >= self.SIMILARITY_THRESHOLD:
                return visitor_id
        return None

    def _match_reentry(self, appearance: Optional[np.ndarray]) -> Optional[str]:
        if appearance is None or not self._exited:
            return None
        now = time.time()
        best_score = 0.0
        best_id: Optional[str] = None
        for state in reversed(self._exited):
            if state.lost_at is None or now - state.lost_at > self.REENTRY_WINDOW_SECONDS:
                continue
            if state.appearance is None:
                continue
            score = _cosine_sim(appearance, state.appearance)
            if score > best_score and score >= self.SIMILARITY_THRESHOLD:
                best_score = score
                best_id = state.visitor_id
        return best_id

    def _new_visitor_id(self) -> str:
        self._visitor_counter += 1
        hex_part = hashlib.sha1(str(self._visitor_counter).encode()).hexdigest()[:6]
        return f"VIS_{hex_part}"

    def _prune_exited(self) -> None:
        now = time.time()
        self._exited = [
            s for s in self._exited
            if s.lost_at and now - s.lost_at <= self.REENTRY_WINDOW_SECONDS
        ]
        self._cross_cam_exits = [
            (vid, t, f) for vid, t, f in self._cross_cam_exits
            if now - t <= self.REENTRY_WINDOW_SECONDS
        ]


def extract_appearance(frame: "np.ndarray", bbox: tuple[int, int, int, int]) -> Optional[np.ndarray]:
    """
    Extract a colour histogram feature vector from the bounding box region.
    Returns a normalised 1D numpy array, or None if cv2 is unavailable.
    """
    try:
        import cv2
    except ImportError:
        return None

    x1, y1, x2, y2 = bbox
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return None
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    h_hist = cv2.calcHist([hsv], [0], None, [32], [0, 180]).flatten()
    s_hist = cv2.calcHist([hsv], [1], None, [16], [0, 256]).flatten()
    feat = np.concatenate([h_hist, s_hist])
    norm = np.linalg.norm(feat)
    return feat / norm if norm > 0 else feat


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape != b.shape:
        return 0.0
    dot = float(np.dot(a, b))
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return dot / denom if denom > 0 else 0.0
