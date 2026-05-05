from dataclasses import dataclass, field
from typing import Optional


@dataclass
class InstrumentWeight:
    label: str
    priority: str                       # very_high | high | medium | low | none
    low_end_target_hz: Optional[float] = None
    acceptable_weight: Optional[str] = None


@dataclass
class GenreProfile:
    id: str                             # e.g. "Glam Metal"
    name: str
    examples: list[str]
    target_lufs: float
    dynamic_range: str                  # low | low-medium | medium | medium-high | high | very-high

    # Frequency targets: relative dB offsets from neutral, one per band
    # Keys: sub_bass bass low_mid mid high_mid presence air
    frequency_targets: dict[str, float] = field(default_factory=dict)

    instrument_weights: list[InstrumentWeight] = field(default_factory=list)
    notes: str = ""

    def target_for_band(self, band: str) -> float:
        return self.frequency_targets.get(band, 0.0)

    def weight_for_channel(self, label: str) -> Optional[InstrumentWeight]:
        for w in self.instrument_weights:
            if w.label == label:
                return w
        return None
