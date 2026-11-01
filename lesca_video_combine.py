"""Video Combine (Lesca) — merge same-prefix videos in a directory into one file.

Scans a directory for every video file whose name starts with a given prefix
(in ascending filename order) and concatenates them into a single
``<prefix>_merged-YYYY_MM_DD_HH_MM_SS.mp4`` written back into the same
directory. Previously produced ``<prefix>_merged-<timestamp>.mp4`` outputs are
ignored on re-runs, so executing the node again never merges an already-merged
file (specs goal 5 requirement).

Inputs:

* ``any``    — an output of *any* upstream type used purely as a trigger that
  starts the node's run (e.g. the ``filename`` output of Save Video (Lesca)
  right after a new clip was saved). Optional, so the node also runs
  standalone; the node is an output node, so queueing the graph executes it
  even when nothing is wired to its ``filename`` output.
* ``folder`` — the directory to scan. A relative path resolves against the
  ComfyUI root directory (``folder_paths.base_path``), like the other Lesca
  nodes; an absolute path is used as-is. May be connected to a
  string-producing node.
* ``prefix`` — the filename prefix of the clips to merge, e.g. ``dance`` merges
  ``dance_clip0.mp4``, ``dance_clip1.mp4``, ... May be connected to a
  string-producing node.

Output:

* ``filename`` — the absolute path of the merged video file.

Merge strategy: every merge is a concat-filter re-encode into a fresh
H.264 + AAC file. The concat demuxer's stream copy (``-c copy``) is no
longer used: it duplicates the last AAC frame at each splice whenever a
clip's audio length is not a whole number of AAC frames, which shows up as
an audible dropout and repeat at the seam. Re-encoding through the concat
filter produces a clean splice. Each input's video is normalised to the
first source's resolution and frame rate, and each input's audio to the
first audio-bearing source's sample rate and channel layout; a source
without an audio track contributes a silence stream of matching duration so
every input aligns in the concat filter. The ffmpeg (and ffprobe) binaries
must be on PATH (checked up front).

The execution cache is keyed by ``fingerprint_inputs``, which hashes the set
of matching ``(basename, mtime)`` pairs: a new or modified clip invalidates
the cache so a fresh merged file is produced, while an unchanged directory
reuses the cached output instead of writing another merged file every run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

import folder_paths
from comfy_api.latest import io


def _resolved_folder(folder: str) -> str:
    """Resolve ``folder`` to an absolute directory path (no side effects).

    A relative path resolves against the ComfyUI root directory
    (``folder_paths.base_path``), so ``output/video`` means
    ``<root>/output/video``. An absolute path is used as-is. A relative path
    that escapes the root directory is rejected.

    Raises:
        ValueError: if the path is empty, escapes the base directory, or is
            not a directory.
        FileNotFoundError: if the path does not exist.
    """
    folder = (folder or "").strip()
    if not folder:
        raise ValueError("Folder is empty; specify the directory containing the videos to merge.")

    if os.path.isabs(folder):
        folder_path = os.path.abspath(folder)
    else:
        base_dir = folder_paths.base_path
        folder_path = os.path.abspath(os.path.join(base_dir, folder))
        if not folder_paths.is_within_directory(base_dir, folder_path):
            raise ValueError(f"Path {folder!r} escapes the base directory {base_dir!r}.")

    if not os.path.exists(folder_path):
        raise FileNotFoundError(f"Folder not found: {folder_path}")
    if not os.path.isdir(folder_path):
        raise ValueError(f"Not a directory: {folder_path}")
    return folder_path


def _merged_output_regex(prefix: str) -> "re.Pattern[str]":
    """Regex matching previously produced ``<prefix>_merged-<timestamp>.mp4`` files."""
    return re.compile(
        re.escape(prefix) + r"_merged-\d{4}_\d{2}_\d{2}_\d{2}_\d{2}_\d{2}\.mp4$"
    )


def _list_sources(folder_path: str, prefix: str) -> list[str]:
    """Absolute paths of the merge sources, ascending by filename.

    Keeps every regular file in ``folder_path`` whose name starts with
    ``prefix``, excluding previously produced merged outputs
    (``<prefix>_merged-YYYY_MM_DD_HH_MM_SS.mp4``) and everything ComfyUI does
    not recognise as a video (``folder_paths.filter_files_content_types``).
    """
    prefix = (prefix or "").strip()
    if not prefix:
        raise ValueError("Prefix is empty; specify the filename prefix of the videos to merge.")

    pattern = _merged_output_regex(prefix)
    names = [
        name
        for name in os.listdir(folder_path)
        if name.startswith(prefix)
        and not pattern.fullmatch(name)
        and os.path.isfile(os.path.join(folder_path, name))
    ]
    names = folder_paths.filter_files_content_types(names, ["video"])
    names.sort()
    return [os.path.join(folder_path, name) for name in names]


def _ffmpeg() -> None:
    """Ensure the ffmpeg and ffprobe binaries are on PATH.

    The merge shells out to ``ffmpeg`` (re-encode) and ``ffprobe`` (stream
    inspection) directly, so their absence is caught up front with a clear
    message.
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg not found on PATH; Video Combine (Lesca) needs the ffmpeg binary to merge files."
        )
    if shutil.which("ffprobe") is None:
        raise RuntimeError(
            "ffprobe not found on PATH; Video Combine (Lesca) needs ffprobe to inspect the clips being merged."
        )


def _parse_rate(value) -> float:
    """Parse an ffprobe ``avg_frame_rate`` such as ``'30/1'`` into a float fps."""
    if not value:
        return 0.0
    if isinstance(value, str) and "/" in value:
        num, _, den = value.partition("/")
        try:
            denom = float(den)
        except ValueError:
            return 0.0
        return float(num) / denom if denom else 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _channel_layout(channels) -> str:
    """Map a channel count to an ffmpeg channel layout name (default stereo)."""
    return {1: "mono", 2: "stereo", 6: "5.1"}.get(int(channels or 2), "stereo")


def _probe_source(path: str) -> dict:
    """Probe a clip's video/audio stream properties with ffprobe.

    Returns a dict with ``has_video`` / ``width`` / ``height`` / ``fps``,
    ``has_audio`` / ``sample_rate`` / ``channels`` and ``duration`` (seconds).
    """
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type,width,height,avg_frame_rate,sample_rate,channels,duration",
            "-show_entries", "format=duration",
            "-of", "json", path,
        ],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(out.stdout)
    info = {
        "has_video": False, "width": 0, "height": 0, "fps": 0.0, "video_duration": 0.0,
        "has_audio": False, "sample_rate": 0, "channels": 2,
        "duration": 0.0,
    }
    for stream in data.get("streams", []):
        kind = stream.get("codec_type")
        if kind == "video" and not info["has_video"]:
            info["has_video"] = True
            info["width"] = int(stream.get("width") or 0)
            info["height"] = int(stream.get("height") or 0)
            info["fps"] = _parse_rate(stream.get("avg_frame_rate"))
            info["video_duration"] = float(stream.get("duration") or 0.0)
        elif kind == "audio" and not info["has_audio"]:
            info["has_audio"] = True
            info["sample_rate"] = int(stream.get("sample_rate") or 44100)
            info["channels"] = int(stream.get("channels") or 2)
    info["duration"] = float(data.get("format", {}).get("duration") or 0.0)
    return info


def _reencode_ffmpeg(sources: list[str], out_path: str) -> None:
    """Merge ``sources`` into ``out_path`` by re-encoding one H.264 + AAC file.

    The only merge path. Every input's video is normalised to the first
    source's resolution and frame rate, and every input's audio to the first
    audio-bearing source's sample rate and channel layout, so the concat
    filter sees identical streams. A source without an audio track
    contributes a silence stream of its own duration. The output is a fresh
    h264/yuv420p video plus an AAC audio track (or video-only when no input
    has audio). Re-encoding — rather than the concat demuxer's stream copy —
    is what keeps the audio splice clean.
    """
    _ffmpeg()
    infos = [_probe_source(source) for source in sources]
    if any(not info["has_video"] for info in infos):
        raise RuntimeError(
            "A merge source has no video stream; only video files can be combined."
        )

    # Canonical video = the first source; canonical audio = the first audio.
    first = infos[0]
    width, height = first["width"], first["height"]
    canonical_fps = first["fps"] or 30.0
    fps_fmt = f"{canonical_fps:g}"
    any_audio = any(info["has_audio"] for info in infos)
    audio_source = next((i for i in infos if i["has_audio"]), None)
    sample_rate = audio_source["sample_rate"] if audio_source else 44100
    layout = _channel_layout(audio_source["channels"] if audio_source else 2)

    filters = []
    concat_pads = []
    for index, info in enumerate(infos):
        # Normalise the video to the canonical resolution / sar / time base.
        # The ``fps`` filter is applied only when the input's rate actually
        # differs: it resamples by timestamp and can add a frame at the
        # splice, so same-rate inputs keep their exact frame count.
        parts = [f"scale={width}:{height}:flags=lanczos"]
        if abs((info["fps"] or 0.0) - canonical_fps) >= 0.01:
            parts.append(f"fps={fps_fmt}")
        parts += ["setpts=PTS-STARTPTS", "setsar=1"]
        filters.append(f"[{index}:v]" + ",".join(parts) + f"[vi{index}]")
        concat_pads.append(f"[vi{index}]")
        if any_audio:
            # Trim each clip's audio to its own video duration first: AAC pads
            # audio to whole frames, so a clip's audio can run slightly longer
            # than its video, and that excess is what makes the concat filter
            # emit one extra video frame at the splice. Aligning audio to video
            # per clip keeps the frame count exact and the seam clean.
            video_dur = info["video_duration"] or info["duration"]
            if info["has_audio"]:
                filters.append(
                    f"[{index}:a]atrim=duration={video_dur},"
                    f"aformat=sample_fmts=fltp:sample_rates={sample_rate}"
                    f":channel_layouts={layout},asetpts=PTS-STARTPTS[ai{index}]"
                )
            else:
                # Silence of this source's video duration, so the concat filter
                # sees an audio pad for every input. ``atrim=duration=0`` means
                # "unlimited", which would make the infinite anullsrc hang the
                # merge, so a non-positive duration (a degenerate/empty file)
                # is rejected up front.
                if video_dur <= 0:
                    raise RuntimeError(
                        f"Cannot pad silence for a source without audio that has no duration: {sources[index]}."
                    )
                filters.append(f"anullsrc=r={sample_rate}:cl={layout}[sil{index}]")
                filters.append(
                    f"[sil{index}]atrim=duration={video_dur},"
                    f"asetpts=PTS-STARTPTS[ai{index}]"
                )
            concat_pads.append(f"[ai{index}]")

    filters.append(
        "".join(concat_pads)
        + f"concat=n={len(infos)}:v=1:a={1 if any_audio else 0}[vout]"
        + ("[aout]" if any_audio else "")
    )
    graph = ";".join(filters)

    command = ["ffmpeg", "-y", "-v", "error"]
    for source in sources:
        command += ["-i", source]
    command += ["-filter_complex", graph, "-map", "[vout]"]
    if any_audio:
        command += ["-map", "[aout]", "-c:a", "aac"]
    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p", out_path]

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg concat re-encode failed:\n{(result.stderr or '').strip()}"
        )


class VideoCombineLesca(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="VideoCombineLesca",
            display_name="Video Combine (Lesca)",
            category="video",
            search_aliases=["combine videos lesca", "merge videos", "concatenate video files", "join video clips"],
            description="Concatenate every same-prefix video file in a directory into a single merged video.",
            is_output_node=True,
            inputs=[
                io.AnyType.Input(
                    "any",
                    optional=True,
                    tooltip="Trigger input: the output of any upstream node starts this node's run (e.g. the filename output of Save Video (Lesca)). Optional — the node also runs standalone.",
                ),
                io.String.Input(
                    "folder",
                    display_name="folder",
                    default="output/video",
                    dynamic_prompts=True,
                    tooltip="Directory containing the videos to merge, e.g. 'output/video'. A relative path resolves against the ComfyUI root directory; an absolute path is used as-is. May be connected to a string-producing node.",
                ),
                io.String.Input(
                    "prefix",
                    display_name="prefix",
                    default="",
                    dynamic_prompts=True,
                    tooltip="Filename prefix of the videos to merge, e.g. 'dance' merges dance_clip0.mp4, dance_clip1.mp4, ... in ascending filename order. May be connected to a string-producing node.",
                ),
            ],
            outputs=[
                io.String.Output(
                    display_name="filename",
                    tooltip="Absolute path of the merged video file, e.g. .../output/video/dance_merged-2026_10_18_16_33_44.mp4.",
                ),
            ],
        )

    @classmethod
    def execute(cls, folder: str, prefix: str, any=None) -> io.NodeOutput:
        folder_path = _resolved_folder(folder)
        prefix = (prefix or "").strip()
        sources = _list_sources(folder_path, prefix)
        if not sources:
            raise ValueError(
                f"No video files starting with {prefix!r} found in {folder_path}."
            )

        # <prefix>_merged-YYYY_MM_DD_HH_MM_SS.mp4, in the same directory.
        out_name = f"{prefix}_merged-{time.strftime('%Y_%m_%d_%H_%M_%S')}.mp4"
        out_path = os.path.join(folder_path, out_name)
        # Same-second collision guard: never overwrite an existing file.
        stem, ext = os.path.splitext(out_path)
        counter = 1
        while os.path.exists(out_path):
            out_path = f"{stem}_{counter}{ext}"
            counter += 1

        # Always re-encode through the concat filter: a clean audio splice,
        # and compatible handling of mixed resolutions / frame rates / codecs.
        _reencode_ffmpeg(sources, out_path)

        return io.NodeOutput(out_path)

    @classmethod
    def fingerprint_inputs(cls, folder: str | None, prefix: str | None, any=None) -> float:
        # Linked (unresolved) inputs arrive as None before the graph executes —
        # the directory cannot be listed yet. Return a constant; the cache's
        # input signature still includes the linked node's own signature, so a
        # changed linked value invalidates the cached output.
        if folder is None or prefix is None:
            return 0.0
        try:
            folder_path = _resolved_folder(folder)
        except (ValueError, FileNotFoundError):
            return 0.0
        try:
            sources = _list_sources(folder_path, (prefix or "").strip())
        except ValueError:
            return 0.0
        # The merge output depends on the set of source files and their
        # content (mtime): a new or modified clip must invalidate the cache so
        # a fresh merged file is produced, while an unchanged directory reuses
        # the cached output instead of writing another merged file every run.
        signature = ",".join(
            f"{os.path.basename(source)}:{int(os.path.getmtime(source))}" for source in sources
        )
        return float(hash(signature)) % 2**53

    @classmethod
    def validate_inputs(cls, folder: str | None, prefix: str | None, any=None) -> bool | str:
        # Linked (unresolved) inputs arrive as None during prompt validation;
        # accept them, as Load Video (Lesca) does.
        if folder is None and prefix is None:
            return True
        try:
            if folder is not None:
                _resolved_folder(folder)
            if prefix is not None and not (prefix or "").strip():
                return "Prefix is empty; specify the filename prefix of the videos to merge."
        except (ValueError, FileNotFoundError) as e:
            return str(e)
        return True
