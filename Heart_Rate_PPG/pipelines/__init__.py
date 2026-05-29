"""PPG peak-detection pipelines, one per algorithm.

All pipelines share the same cleaner (Butterworth BP 0.5-8 Hz, NK2 elgendi
default) so that benchmark differences reflect detector behaviour, not
preprocessing.

Available detectors:
    terma      — Elgendi 2013 TERMA + onset refine + slope-ratio gate (this project's TERMA)
    elgendi    — NeuroKit2 ppg_findpeaks(method='elgendi')
    bishop     — NeuroKit2 ppg_findpeaks(method='bishop')   — MSPTD
    charlton   — NeuroKit2 ppg_findpeaks(method='charlton') — MSPTDfast v2
"""
from __future__ import annotations

from .base import BasePipeline, PipelineResult
from .terma.pipeline import TermaPipeline
from .elgendi.pipeline import ElgendiPipeline
from .bishop.pipeline import BishopPipeline
from .charlton.pipeline import CharltonPipeline

REGISTRY: dict[str, type[BasePipeline]] = {
    "terma": TermaPipeline,
    "elgendi": ElgendiPipeline,
    "bishop": BishopPipeline,
    "charlton": CharltonPipeline,
}

ALL_NAMES = tuple(REGISTRY.keys())


def get(name: str) -> BasePipeline:
    if name not in REGISTRY:
        raise KeyError(f"unknown detector {name!r}; valid: {', '.join(ALL_NAMES)}")
    return REGISTRY[name]()


__all__ = [
    "BasePipeline",
    "PipelineResult",
    "TermaPipeline",
    "ElgendiPipeline",
    "BishopPipeline",
    "CharltonPipeline",
    "REGISTRY",
    "ALL_NAMES",
    "get",
]
