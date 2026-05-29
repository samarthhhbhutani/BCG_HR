"""Per-species signal-processing configuration.

The firmware and pipeline are species-agnostic — only these numbers change
between human / dog / cat. Picked at session start (CLI flag) and passed
to the HR worker; nothing about the subject is hardcoded downstream.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass(slots=True)
class SubjectConfig:
    name: str
    hr_min_bpm: float
    hr_max_bpm: float
    ibi_min_ms: float
    ibi_max_ms: float
    bandpass_low_hz: float
    bandpass_high_hz: float
    # Finger-presence thresholds (raw IR counts from MAX30102)
    contact_ir_min: float
    contact_ir_max: float
    contact_ac_min: float  # min peak-to-peak AC amplitude (counts) of band-passed IR

    def to_dict(self) -> dict:
        return asdict(self)


HUMAN = SubjectConfig(
    name="human",
    # Wide bounds: subject may be sedentary (50 bpm) or just off a treadmill
    # (150+ bpm). HR plausibility is enforced morphologically in hr_worker
    # (dicrotic-notch rejection adapts to the running median IBI), not by
    # clamping a narrow range here.
    hr_min_bpm=40.0,
    hr_max_bpm=200.0,
    ibi_min_ms=300.0,
    ibi_max_ms=1500.0,
    # 4 Hz upper edge keeps the systolic upstroke sharp at HR up to ~200 bpm
    # while still attenuating most of the dicrotic notch energy. The
    # morphology gate downstream handles whatever notch energy remains.
    bandpass_low_hz=0.5,
    bandpass_high_hz=4.0,
    contact_ir_min=50_000.0,
    contact_ir_max=260_000.0,
    contact_ac_min=200.0,
)

DOG = SubjectConfig(
    name="dog",
    hr_min_bpm=40.0,
    hr_max_bpm=220.0,
    ibi_min_ms=270.0,
    ibi_max_ms=1500.0,
    bandpass_low_hz=0.5,
    bandpass_high_hz=6.0,
    contact_ir_min=30_000.0,
    contact_ir_max=260_000.0,
    contact_ac_min=150.0,
)

CAT = SubjectConfig(
    name="cat",
    hr_min_bpm=60.0,
    hr_max_bpm=250.0,
    ibi_min_ms=240.0,
    ibi_max_ms=1000.0,
    bandpass_low_hz=0.7,
    bandpass_high_hz=8.0,
    contact_ir_min=30_000.0,
    contact_ir_max=260_000.0,
    contact_ac_min=120.0,
)

PRESETS = {"human": HUMAN, "dog": DOG, "cat": CAT}


def load(name_or_path: str) -> SubjectConfig:
    """Load by preset name (human/dog/cat) or from a JSON file path."""
    if name_or_path in PRESETS:
        return PRESETS[name_or_path]
    p = Path(name_or_path)
    if p.exists():
        d = json.loads(p.read_text())
        return SubjectConfig(**d)
    raise ValueError(f"Unknown subject: {name_or_path}")
