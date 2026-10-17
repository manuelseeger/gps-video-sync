from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone, timedelta
from typing import Any
import numpy as np

@dataclass
class Track:
    time: np.ndarray
    latitude: np.ndarray
    longitude: np.ndarray
    altitude: np.ndarray
    date: date | None = None
    altitude_source: str = "gps"

@dataclass
class Telemetry:
    time: np.ndarray
    channels: dict[str, np.ndarray]
    chapters: list[dict[str, Any]] = field(default_factory=list)
    gps_time: np.ndarray = field(default_factory=lambda: np.empty(0))
    gps_utc: np.ndarray = field(default_factory=lambda: np.empty(0))

    def slice(self, start: float, end: float) -> Telemetry:
        mask = (self.time >= start) & (self.time < end)
        gps = (self.gps_time >= start) & (self.gps_time < end)
        return Telemetry(self.time[mask] - start,
                         {k: v[mask] for k, v in self.channels.items()}, [],
                         self.gps_time[gps] - start, self.gps_utc[gps])

@dataclass
class SyncResult:
    status: str
    offset_seconds: float | None = None
    uncertainty_seconds: float | None = None
    method: str = "motion"
    confidence: dict[str, Any] = field(default_factory=dict)
    per_feature: dict[str, Any] = field(default_factory=dict)
    drift: dict[str, Any] = field(default_factory=dict)
    chapters: list[dict[str, Any]] = field(default_factory=list)
    date: date | None = None
    message: str = ""

    @property
    def video_start_utc(self) -> str | None:
        if self.date is None or self.offset_seconds is None:
            return None
        return (datetime.combine(self.date, datetime.min.time(), timezone.utc)
                + timedelta(seconds=self.offset_seconds)).isoformat()

    def to_dict(self) -> dict[str, Any]:
        obj = asdict(self)
        obj.pop("date")
        iso = self.video_start_utc
        rate = self.drift.get("clock_rate", 1.0)
        obj.update(schema_version="1.0.0", mapping="utc_seconds = video_seconds * clockRate + offset_seconds",
                   video_start_utc=iso, videoStartUtcSeconds=self.offset_seconds,
                   videoStartUtc=iso.split("T")[1].replace("+00:00", "") if iso else None,
                   dateUtc=iso[:10] if iso else (self.date.isoformat() if self.date else None),
                   utc_origin_date=self.date.isoformat() if self.date else None,
                   clockDriftDetected=self.drift.get("detected", False), clockRate=rate,
                   estimatedAbsoluteUncertaintySeconds=self.uncertainty_seconds)
        return obj
