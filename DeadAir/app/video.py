"""FFprobe / FFmpeg operations: probe, detect silence, export edited video."""

from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable

from . import silence
from .utils import find_binary, has_allowed_extension, subprocess_kwargs

ProgressCB = Callable[[float], None]


class VideoError(Exception):
    """An error with a message that is safe and useful to show to the user."""


@dataclass
class VideoInfo:
    duration: float
    size_bytes: int
    width: int
    height: int
    fps: float | None
    has_audio: bool
    video_codec: str
    audio_codec: str | None
    audio_duration: float | None = None
    video_duration: float | None = None
    cfr: bool = False  # True if the frame rate looks constant

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- binaries

def _require(name: str) -> str:
    path = find_binary(name)
    if not path:
        raise VideoError(
            f"{name} was not found. Install FFmpeg and make sure {name} is on your PATH "
            "(see the README), then restart DeadAir."
        )
    return path


def _parse_rate(value: str | None) -> float | None:
    if not value or value in ("0/0", "N/A"):
        return None
    try:
        if "/" in value:
            num, den = value.split("/")
            return float(num) / float(den) if float(den) else None
        return float(value)
    except ValueError:
        return None


def _float(value) -> float | None:
    try:
        f = float(value)
        return f if math.isfinite(f) and f > 0 else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- probing

def probe(path: Path | str) -> VideoInfo:
    """Validate a file with ffprobe and return its metadata.

    Raises VideoError for unsupported extensions, unreadable/corrupt files,
    files without a video stream, or files with no determinable duration.
    """
    path = Path(path)
    if not has_allowed_extension(path.name):
        raise VideoError("Unsupported file type. Use MP4, MOV, MKV, WebM or AVI.")
    if not path.is_file():
        raise VideoError("The video file could not be found.")

    ffprobe = _require("ffprobe")
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, **subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired:
        raise VideoError("Reading the file took too long. It may be corrupt.")
    if proc.returncode != 0:
        raise VideoError("This file isn't a readable video. It may be corrupt or not a real video file.")
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        raise VideoError("This file isn't a readable video.")

    streams = data.get("streams", [])
    video = next(
        (s for s in streams
         if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")),
        None,
    )
    if video is None:
        raise VideoError("No video stream found in this file.")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        raise VideoError("Couldn't read the video dimensions. The file may be corrupt.")
    # Phone footage is often stored sideways with a rotation tag; FFmpeg
    # auto-rotates, so report (and plan for) the displayed size.
    rotation = _rotation(video)
    if abs(rotation) % 180 == 90:
        width, height = height, width

    fmt = data.get("format", {})
    duration = _float(fmt.get("duration")) or _float(video.get("duration"))
    if duration is None:
        duration = _measure_duration(path)           # e.g. browser-recorded WebM has no header duration
    if duration is None or duration <= 0:
        raise VideoError("Couldn't determine the video's duration. The file may be corrupt.")

    avg, real = _parse_rate(video.get("avg_frame_rate")), _parse_rate(video.get("r_frame_rate"))
    fps = avg or real
    cfr = bool(avg and real and abs(avg - real) / real < 0.005)

    return VideoInfo(
        duration=duration,
        size_bytes=int(_float(fmt.get("size")) or path.stat().st_size),
        width=width, height=height, fps=fps,
        has_audio=audio is not None,
        video_codec=video.get("codec_name", "unknown"),
        audio_codec=audio.get("codec_name") if audio else None,
        audio_duration=_float(audio.get("duration")) if audio else None,
        video_duration=_float(video.get("duration")),
        cfr=cfr,
    )


def _rotation(video_stream: dict) -> int:
    try:
        if "rotate" in video_stream.get("tags", {}):
            return int(float(video_stream["tags"]["rotate"]))
        for sd in video_stream.get("side_data_list", []):
            if "rotation" in sd:
                return int(float(sd["rotation"]))
    except (TypeError, ValueError):
        pass
    return 0


def _measure_duration(path: Path) -> float | None:
    """Fallback: stream-copy to null and read how far FFmpeg got."""
    ffmpeg = _require("ffmpeg")
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", str(path),
             "-map", "0:v:0", "-c", "copy", "-f", "null", "-progress", "pipe:1", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600, **subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return None
    last = None
    for line in proc.stdout.splitlines():
        if line.startswith("out_time_us="):
            try:
                last = int(line.split("=", 1)[1]) / 1e6
            except ValueError:
                pass
    return last


# --------------------------------------------------------------------------- running ffmpeg with real progress

def _run_ffmpeg(args: list[str], total_seconds: float, on_progress: ProgressCB | None) -> tuple[int, str]:
    """Run ffmpeg, reporting real progress from `-progress pipe:1`.

    stderr goes to a temp file (not a pipe) so reading stdout line-by-line can
    never deadlock. Returns (returncode, stderr_text).
    """
    ffmpeg = _require("ffmpeg")
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-progress", "pipe:1", *args]
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as err:
        with subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=err, text=True,
            encoding="utf-8", errors="replace", **subprocess_kwargs(),
        ) as proc:                                    # context manager closes the stdout pipe
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    if on_progress and total_seconds > 0 and line.startswith(("out_time_us=", "out_time_ms=")):
                        try:
                            done = int(line.split("=", 1)[1]) / 1e6      # both fields are microseconds
                        except ValueError:
                            continue
                        on_progress(min(max(done / total_seconds, 0.0), 0.99))
                proc.wait()
            except BaseException:
                proc.kill()
                raise
        err.seek(0)
        return proc.returncode, err.read()


# --------------------------------------------------------------------------- silence detection

def detect_silence(path: Path | str, info: VideoInfo, settings: silence.Settings,
                   on_progress: ProgressCB | None = None) -> list[tuple[float, float | None]]:
    """Run FFmpeg's real `silencedetect` filter on the first audio stream."""
    if not info.has_audio:
        raise VideoError("This video has no audio track, so there is no silence to detect.")
    settings.validate()
    af = f"silencedetect=noise={settings.threshold_db:g}dB:d={settings.min_silence:g}"
    code, stderr = _run_ffmpeg(
        ["-i", str(path), "-map", "0:a:0", "-vn", "-af", af, "-f", "null", "-"],
        info.duration, on_progress,
    )
    if code != 0:
        raise VideoError("FFmpeg couldn't analyze the audio. The file may be damaged. " + _tail(stderr))
    if on_progress:
        on_progress(1.0)
    return silence.parse_silencedetect(stderr)


# --------------------------------------------------------------------------- export

def snap_segments(keep: list[silence.Interval], info: VideoInfo) -> list[tuple[float, float | None]]:
    """Align cut points to the video frame grid (constant-frame-rate sources).

    Frame n has timestamp n/fps. Cutting at (n - 0.5)/fps selects frames n..m-1
    with no floating-point ties, so every segment is a whole number of frames
    long. Audio is trimmed at the *same* instants, so within a segment video
    and audio have identical length and no A/V drift accumulates across cuts.

    The last element's end is None when it runs to the end of the file.
    Zero-length segments produced by snapping are dropped.
    """
    fps = info.fps if (info.cfr and info.fps) else None
    out: list[tuple[float, float | None]] = []
    for s, e in keep:
        to_end = e >= info.duration - 1e-3
        if fps:
            a, b = round(s * fps), round(e * fps)
            s2 = 0.0 if a <= 0 else (a - 0.5) / fps
            e2 = None if to_end else (b - 0.5) / fps
            if not to_end and b <= a:
                continue
            if to_end and s2 >= info.duration:
                continue
        else:
            s2, e2 = s, None if to_end else e
        out.append((s2, e2))
    return out


def segments_duration(segments: list[tuple[float, float | None]], total: float) -> float:
    return sum((total if e is None else e) - s for s, e in segments)


def build_filter_graph(segments: list[tuple[float, float | None]], fix_odd_size: bool) -> str:
    """Build the filter_complex graph that physically removes everything outside `segments`.

    For each kept segment the video is cut with `trim` and the audio with
    `atrim`; both have their timestamps reset to zero (`setpts`/`asetpts`) and
    then the `concat` filter joins all segments back to back. Because video and
    audio are cut from the same instants and concatenated by the same filter
    they stay locked together, and nothing is frozen, muted or blacked out -
    the removed ranges simply no longer exist on the timeline.
    """
    n = len(segments)

    def rng(s: float, e: float | None) -> str:
        return f"start={s:.6f}" + (f":end={e:.6f}" if e is not None else "")

    parts: list[str] = []
    if n == 1:
        vsrc, asrc = ["[0:v:0]"], ["[0:a:0]"]
    else:
        parts.append("[0:v:0]split=%d%s" % (n, "".join(f"[vs{i}]" for i in range(n))))
        parts.append("[0:a:0]asplit=%d%s" % (n, "".join(f"[as{i}]" for i in range(n))))
        vsrc, asrc = [f"[vs{i}]" for i in range(n)], [f"[as{i}]" for i in range(n)]

    for i, (s, e) in enumerate(segments):
        parts.append(f"{vsrc[i]}trim={rng(s, e)},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"{asrc[i]}atrim={rng(s, e)},asetpts=PTS-STARTPTS[a{i}]")

    joined = "".join(f"[v{i}][a{i}]" for i in range(n))
    tail = ",scale=trunc(iw/2)*2:trunc(ih/2)*2" if fix_odd_size else ""
    parts.append(f"{joined}concat=n={n}:v=1:a=1[cv][outa]")
    parts.append(f"[cv]null{tail}[outv]")          # `null` keeps the label chain uniform
    return ";\n".join(parts)


def export(path: Path | str, info: VideoInfo, keep: list[silence.Interval], out_path: Path,
           on_progress: ProgressCB | None = None) -> dict:
    """Render `keep` ranges into a new MP4 (H.264 + AAC). Returns result details."""
    if not info.has_audio:
        raise VideoError("This video has no audio track.")
    if not keep:
        raise VideoError("The entire video was detected as silence, so there is nothing to export.")

    segments = snap_segments(keep, info)
    if not segments:
        raise VideoError("The entire video was detected as silence, so there is nothing to export.")
    expected = segments_duration(segments, info.duration)

    graph = build_filter_graph(segments, fix_odd_size=(info.width % 2 == 1 or info.height % 2 == 1))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = out_path.with_name(out_path.stem + ".partial" + out_path.suffix)

    # The graph goes in a script file: with many cuts it can exceed the Windows
    # command-line length limit.
    fd, script_name = tempfile.mkstemp(prefix="deadair_filter_", suffix=".txt")
    os.close(fd)
    script = Path(script_name)
    try:
        script.write_text(graph, encoding="utf-8")

        def attempt(script_flag: list[str]) -> tuple[int, str]:
            return _run_ffmpeg(
                ["-y", "-i", str(path), *script_flag, "-map", "[outv]", "-map", "[outa]",
                 # Quality-first re-encode; stream copy cannot cut accurately between keyframes.
                 "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "192k",
                 "-map_chapters", "-1", "-movflags", "+faststart",
                 "-f", "mp4", str(tmp_out)],
                expected, on_progress,
            )

        code, stderr = attempt(["-filter_complex_script", str(script)])
        if code != 0 and "nrecognized option" in stderr:      # newer FFmpeg renamed the flag
            code, stderr = attempt(["-/filter_complex", str(script)])
        if code != 0:
            tmp_out.unlink(missing_ok=True)
            raise VideoError("FFmpeg failed while exporting. " + _tail(stderr))
    finally:
        script.unlink(missing_ok=True)

    os.replace(tmp_out, out_path)

    # Verify what we actually wrote.
    try:
        out_info = probe(out_path)
    except VideoError as exc:
        out_path.unlink(missing_ok=True)
        raise VideoError(f"Export finished but the output could not be verified: {exc}")
    if not out_info.has_audio:
        out_path.unlink(missing_ok=True)
        raise VideoError("Export finished but the output has no audio. Please report this.")

    warnings = []
    if abs(out_info.duration - expected) > max(0.5, expected * 0.01):
        warnings.append(f"Final length differs from the plan by {abs(out_info.duration - expected):.2f}s.")
    if out_info.audio_duration and out_info.video_duration and \
            abs(out_info.audio_duration - out_info.video_duration) > 0.25:
        warnings.append("Audio and video lengths differ by more than 0.25s.")

    if on_progress:
        on_progress(1.0)
    return {
        "expected_duration": expected,
        "final_duration": out_info.duration,
        "output_size": out_path.stat().st_size,
        "segments": len(segments),
        "warnings": warnings,
    }


def _tail(stderr: str, lines: int = 4) -> str:
    tail = [ln.strip() for ln in stderr.strip().splitlines() if ln.strip()][-lines:]
    return ("Details: " + " | ".join(tail)) if tail else ""
