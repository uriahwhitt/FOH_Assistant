"""Ambient noise baseline capture and band-level correction."""

import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

AMBIENT_SNR_THRESHOLD_DB = 6.0    # min SNR above ambient for correction to apply


@dataclass
class AmbientBaseline:
    baseline_type: str              # "empty" | "crowd"
    bands: dict                     # band → dB level
    lufs: float
    rms_db: float
    timestamp: float
    timestamp_str: str


class AmbientCapture:
    def __init__(self):
        self._empty: Optional[AmbientBaseline] = None
        self._crowd: Optional[AmbientBaseline] = None

    def capture(self, capture_instance, analyzer,
                duration_s: int, baseline_type: str) -> AmbientBaseline:
        """Sample audio via the existing capture stream.  Does not open a second stream."""
        sr = capture_instance.sample_rate
        step_s = 0.5
        iterations = int(duration_s / step_s)
        all_audio: list = []

        print(f"  Capturing {duration_s}s of ambient audio", end="", flush=True)
        for i in range(iterations):
            buf, _ = capture_instance.get_buffer()
            all_audio.append(buf.copy())
            time.sleep(step_s)
            if (i + 1) % 20 == 0:
                print(".", end="", flush=True)
        print(" done")

        audio = np.concatenate(all_audio) if all_audio else np.zeros(sr, dtype=np.float32)
        window = audio[-min(len(audio), sr * 10):]
        analysis = analyzer.analyze(window)

        ts = time.time()
        bl = AmbientBaseline(
            baseline_type=baseline_type,
            bands=dict(analysis.bands),
            lufs=analysis.lufs,
            rms_db=analysis.rms_db,
            timestamp=ts,
            timestamp_str=time.strftime("%H:%M:%S", time.localtime(ts)),
        )

        if baseline_type == "empty":
            self._empty = bl
        else:
            self._crowd = bl

        return bl

    def active_baseline(self, is_show: bool = True) -> Optional[AmbientBaseline]:
        """Crowd baseline for show mode; empty for soundcheck.  Falls back if not captured."""
        if is_show:
            return self._crowd if self._crowd else self._empty
        return self._empty

    def get_band_offset(self, band: str, is_show: bool = True) -> float:
        bl = self.active_baseline(is_show)
        return bl.bands.get(band, -90.0) if bl else -90.0

    def get_lufs_offset(self, is_show: bool = True) -> float:
        bl = self.active_baseline(is_show)
        return bl.lufs if bl else -70.0

    def has_baseline(self) -> bool:
        return self._empty is not None or self._crowd is not None

    def has_crowd(self) -> bool:
        return self._crowd is not None

    def has_empty(self) -> bool:
        return self._empty is not None

    def to_log_dict(self) -> dict:
        result = {}
        for attr, key in [("_empty", "ambient_empty"), ("_crowd", "ambient_crowd")]:
            bl: Optional[AmbientBaseline] = getattr(self, attr)
            if bl:
                result[key] = {
                    "timestamp": bl.timestamp_str,
                    "lufs": bl.lufs,
                    "rms_db": bl.rms_db,
                    "bands": bl.bands,
                }
        return result
