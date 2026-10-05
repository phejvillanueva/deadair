"""Silence interval maths. Pure functions - no FFmpeg or file access here.

Pipeline:
    FFmpeg `silencedetect` output
      -> parse_silencedetect()      raw (start, end|None) pairs
      -> build_plan()               clamp, drop too-short, merge, apply padding
      -> Plan.removed / Plan.keep   the ranges that matter for export
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Anything shorter than about one video frame is not worth cutting / keeping.
MIN_SEGMENT = 0.04
_EPS = 1e-3

PRESETS = {
    "natural": {"threshold_db": -35.0, "min_silence": 0.7, "padding": 0.25},
    "balanced": {"threshold_db": -30.0, "min_silence": 0.5, "padding": 0.15},
    "aggressive": {"threshold_db": -25.0, "min_silence": 0.3, "padding": 0.10},
}

THRESHOLD_OPTIONS = [-20.0, -25.0, -30.0, -35.0, -40.0]
MIN_SILENCE_OPTIONS = [0.3, 0.5, 0.7, 1.0, 1.5, 2.0]
PADDING_OPTIONS = [0.0, 0.1, 0.15, 0.25, 0.5]

Interval = tuple[float, float]


@dataclass(frozen=True)
class Settings:
    threshold_db: float = -30.0
    min_silence: float = 0.5
    padding: float = 0.15

    def validate(self) -> "Settings":
        if not -90.0 <= self.threshold_db <= -5.0:
            raise ValueError("Silence threshold must be between -90 dB and -5 dB.")
        if not 0.05 <= self.min_silence <= 30.0:
            raise ValueError("Minimum silence must be between 0.05 s and 30 s.")
        if not 0.0 <= self.padding <= 5.0:
            raise ValueError("Padding must be between 0 s and 5 s.")
        return self


@dataclass
class Plan:
    duration: float
    removed: list[Interval] = field(default_factory=list)
    keep: list[Interval] = field(default_factory=list)

    @property
    def total_removed(self) -> float:
        return sum(e - s for s, e in self.removed)

    @property
    def final_duration(self) -> float:
        return sum(e - s for s, e in self.keep)

    @property
    def has_changes(self) -> bool:
        return bool(self.removed)

    @property
    def fully_silent(self) -> bool:
        return self.duration > 0 and not self.keep

    def to_dict(self) -> dict:
        return {
            "duration": self.duration,
            "silences": [{"start": s, "end": e, "duration": e - s} for s, e in self.removed],
            "keep": [{"start": s, "end": e, "duration": e - s} for s, e in self.keep],
            "total_silence": self.total_removed,
            "final_duration": self.final_duration,
            "has_changes": self.has_changes,
            "fully_silent": self.fully_silent,
        }


_START_RE = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?(?:e[-+]?\d+)?)", re.I)
_END_RE = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?(?:e[-+]?\d+)?)", re.I)


def parse_silencedetect(text: str) -> list[tuple[float, float | None]]:
    """Parse FFmpeg `silencedetect` log output.

    Returns (start, end) pairs. `end` is None when silence runs to the end of
    the stream (FFmpeg prints a silence_start with no matching silence_end).
    """
    raw: list[tuple[float, float | None]] = []
    open_start: float | None = None
    for line in text.splitlines():
        if "silencedetect" not in line:
            continue
        m = _START_RE.search(line)
        if m:
            if open_start is not None:          # unterminated previous start
                raw.append((open_start, None))
            open_start = float(m.group(1))
            continue
        m = _END_RE.search(line)
        if m and open_start is not None:
            raw.append((open_start, float(m.group(1))))
            open_start = None
    if open_start is not None:
        raw.append((open_start, None))
    return raw


def merge_intervals(intervals: list[Interval], gap: float = 1e-6) -> list[Interval]:
    """Sort and merge overlapping / touching intervals."""
    merged: list[list[float]] = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1] + gap:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def invert_intervals(removed: list[Interval], duration: float) -> list[Interval]:
    """Ranges of [0, duration] NOT covered by `removed` (assumed sorted/merged)."""
    keep: list[Interval] = []
    cursor = 0.0
    for s, e in removed:
        if s > cursor:
            keep.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < duration:
        keep.append((cursor, duration))
    return keep


def build_plan(raw: list[tuple[float, float | None]], duration: float, settings: Settings) -> Plan:
    """Turn raw silencedetect pairs into a removal list and its inverse keep list.

    Padding semantics: `padding` seconds of the silence are KEPT on each side,
    so a 2.0 s pause with 0.15 s padding removes the middle 1.7 s. Padding is
    not applied at the very start / end of the file (nothing to protect there),
    so leading/trailing silence is trimmed right up to the file edge.
    """
    if duration <= 0:
        return Plan(duration=max(duration, 0.0))

    # 1. Clamp to the file, close open-ended silence at the end of the file.
    clamped: list[Interval] = []
    for start, end in raw:
        s = min(max(start, 0.0), duration)
        e = duration if end is None else min(max(end, 0.0), duration)
        if e > s:
            clamped.append((s, e))

    # 2. Enforce the minimum silence length (FFmpeg already did, but the
    #    unterminated tail and clamping can produce shorter ones).
    clamped = [(s, e) for s, e in clamped if e - s >= settings.min_silence - _EPS]

    # 3. Merge overlapping / adjacent silences.
    merged = merge_intervals(clamped)

    # 4. Apply padding (shrink each silence), except at file boundaries.
    removed: list[Interval] = []
    for s, e in merged:
        s2 = s if s <= _EPS else s + settings.padding
        e2 = e if e >= duration - _EPS else e - settings.padding
        if e2 - s2 >= MIN_SEGMENT:
            removed.append((s2, e2))

    if not removed:
        return Plan(duration=duration, removed=[], keep=[(0.0, duration)])

    # 5. Drop kept slivers shorter than a frame, then re-derive removals.
    keep = [(s, e) for s, e in invert_intervals(removed, duration) if e - s >= MIN_SEGMENT]
    removed = invert_intervals(keep, duration)
    return Plan(duration=duration, removed=removed, keep=keep)
