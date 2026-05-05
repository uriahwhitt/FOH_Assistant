"""Loads and validates band.yaml, genre YAMLs, and setlist.yaml."""

import os
import yaml
from pathlib import Path
from models.genre_profile import GenreProfile, InstrumentWeight


CONFIG_DIR = Path(__file__).parent.parent / "config"


def load_band_config(path: Path = None) -> dict:
    p = path or CONFIG_DIR / "band.yaml"
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    _validate_band_config(cfg)
    return cfg


def load_genre_profiles(genres_dir: Path = None) -> dict[str, GenreProfile]:
    d = genres_dir or CONFIG_DIR / "genres"
    profiles: dict[str, GenreProfile] = {}
    for yaml_file in sorted(d.glob("*.yaml")):
        with open(yaml_file, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        profile = _parse_genre_profile(raw)
        profiles[profile.id] = profile
    if not profiles:
        raise RuntimeError(f"No genre YAML files found in {d}")
    return profiles


def load_setlist(path: Path = None) -> list[dict] | None:
    p = path or CONFIG_DIR / "setlist.yaml"
    if not p.exists():
        return None
    with open(p, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return raw.get("songs", []) if raw else []


def apply_band_overrides(profiles: dict[str, GenreProfile], band_cfg: dict) -> dict[str, GenreProfile]:
    """Apply per-genre and global band_overrides from band.yaml onto loaded genre profiles."""
    overrides = band_cfg.get("band_overrides", {})
    global_notes = overrides.get("global", {}).get("notes", "")

    for genre_id, profile in profiles.items():
        genre_override = overrides.get(genre_id, {})
        freq_overrides = genre_override.get("frequency_targets", {})
        for band, value in freq_overrides.items():
            if band in profile.frequency_targets:
                profile.frequency_targets[band] = float(str(value).lstrip("+"))
        if global_notes and profile.notes:
            profile.notes += f" | {global_notes}"
        elif global_notes:
            profile.notes = global_notes

    return profiles


def _validate_band_config(cfg: dict) -> None:
    required = ("band", "default_genre", "x32", "audio", "channels", "thresholds")
    for key in required:
        if key not in cfg:
            raise ValueError(f"band.yaml missing required key: '{key}'")
    channels = cfg["channels"]
    if not isinstance(channels, dict) or len(channels) == 0:
        raise ValueError("band.yaml 'channels' must be a non-empty mapping")


def _parse_genre_profile(raw: dict) -> GenreProfile:
    weights = []
    for item in raw.get("instrument_weights", []):
        weights.append(InstrumentWeight(
            label=item["label"],
            priority=item["priority"],
            low_end_target_hz=item.get("low_end_target_hz"),
            acceptable_weight=item.get("acceptable_weight"),
        ))

    freq_raw = raw.get("frequency_targets", {})
    freq_targets: dict[str, float] = {}
    for band, val in freq_raw.items():
        freq_targets[band] = float(str(val).lstrip("+"))

    return GenreProfile(
        id=raw["id"],
        name=raw["name"],
        examples=raw.get("examples", []),
        target_lufs=float(raw["target_lufs"]),
        dynamic_range=raw.get("dynamic_range", "medium"),
        frequency_targets=freq_targets,
        instrument_weights=weights,
        notes=raw.get("notes", ""),
    )
