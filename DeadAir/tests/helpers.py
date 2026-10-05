"""Shared test helpers: generate synthetic videos with KNOWN silence and analyse results.

Synthetic "speech": a 440 Hz tone that alternates every 0.5 s between LOUD
(peak ~0.6) and QUIET (peak ~0.1, still above -30 dB). The video luma follows
the same pattern (bright when loud, mid-grey when quiet). Silent ranges are
digital silence (optionally with a very quiet noise floor). After an edit, loud
audio must still line up with bright video - that is how we verify sync using
nothing but the standard library.
"""

from __future__ import annotations

import array
import subprocess
from pathlib import Path

from app.utils import find_binary

FFMPEG = find_binary("ffmpeg")
FFPROBE = find_binary("ffprobe")

LOUD_QUIET_PERIOD = 1.0   # seconds; first half loud, second half quiet


def _between(ranges):
    return "+".join(f"between(t,{a},{b})" for a, b in ranges) or "0"


def make_video(path: Path, duration: float, silences=(), *, fps: int = 25, size: str = "320x240",
               noise_floor: bool = False, audio: bool = True, sample_rate: int = 44100,
               vcodec: str | None = None, acodec: str | None = None, pix_fmt: str = "yuv420p",
               extra_out=()) -> Path:
    """Create a video where `silences` (list of (start, end)) are silent."""
    path = Path(path)
    ext = path.suffix.lower()
    default_v = {".mp4": "libx264", ".mov": "libx264", ".mkv": "libx264",
                 ".webm": "libvpx", ".avi": "mpeg4"}[ext]
    default_a = {".mp4": "aac", ".mov": "aac", ".mkv": "aac",
                 ".webm": "libvorbis", ".avi": "libmp3lame"}[ext]

    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i",
           f"color=c=black:s={size}:r={fps}:d={duration},"
           f"geq=lum='if(lt(mod(T,{LOUD_QUIET_PERIOD}),{LOUD_QUIET_PERIOD / 2}),235,100)':cb=128:cr=128,format={pix_fmt}"]
    if audio:
        gain = (f"if({_between(silences)},0,"
                f"if(lt(mod(t,{LOUD_QUIET_PERIOD}),{LOUD_QUIET_PERIOD / 2}),4.8,0.8))")
        graph = (f"sine=frequency=440:sample_rate={sample_rate}:duration={duration},"
                 f"volume=volume='{gain}':eval=frame[tone]")
        if noise_floor:
            graph += (f";anoisesrc=amplitude=0.003:sample_rate={sample_rate}:duration={duration}[n];"
                      "[tone][n]amix=inputs=2:normalize=0[aout]")
        else:
            graph = graph.replace("[tone]", "[aout]")
        cmd += ["-filter_complex", graph, "-map", "0:v", "-map", "[aout]"]
        cmd += ["-c:a", acodec or default_a]
    else:
        cmd += ["-map", "0:v"]
    cmd += ["-c:v", vcodec or default_v, *extra_out, "-t", str(duration), str(path)]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return path


def make_silent_video(path: Path, duration: float = 5.0) -> Path:
    """Video whose entire audio track is digital silence."""
    return make_video(path, duration, silences=[(0, duration + 1)])


def luma_series(path: Path) -> list[int]:
    """Average luma (0-255) of every output frame, in order."""
    out = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(path), "-map", "0:v:0", "-vf", "scale=1:1,format=gray",
         "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    return list(out)


def audio_rms_series(path: Path, fps: float, rate: int = 8000) -> list[float]:
    """RMS of the audio in consecutive 1/fps windows (0..1)."""
    raw = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", str(rate),
         "-f", "s16le", "-"], capture_output=True, check=True).stdout
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) // 2 * 2])
    win = int(rate / fps)
    out = []
    for i in range(0, len(samples) - win + 1, win):
        chunk = samples[i:i + win]
        out.append((sum(x * x for x in chunk) / len(chunk)) ** 0.5 / 32768)
    return out


def sync_mismatches(path: Path, fps: float, guard: int = 3) -> tuple[int, int]:
    """Compare 'loud audio' with 'bright video' frame by frame.

    Returns (mismatched, compared). Frames within `guard` frames of a
    loud/quiet transition (and the file edges) are ignored.
    """
    luma = luma_series(path)
    rms = audio_rms_series(path, fps)
    n = min(len(luma), len(rms))
    bright = [v > 170 for v in luma[:n]]
    loud = [v > 0.12 for v in rms[:n]]          # loud ~0.42 rms, quiet ~0.07 rms
    mism = compared = 0
    for i in range(guard, n - guard):
        window_b = bright[i - guard:i + guard + 1]
        window_l = loud[i - guard:i + guard + 1]
        if len(set(window_b)) > 1 or len(set(window_l)) > 1:
            continue
        compared += 1
        mism += bright[i] != loud[i]
    return mism, compared
