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

Merge strategy: ffmpeg-python's concat demuxer with ``-c copy`` splices the
streams of the listed files without decoding (lossless, fast — right for
clips produced by the same pipeline). When stream copy fails because the
inputs are incompatible (different codecs / resolutions / frame rates), the
node falls back to a concat-filter re-encode into one H.264 stream (audio
dropped). ffmpeg-python builds both command lines; the ffmpeg binary it
shells out to must still be on PATH (checked up front).

The execution cache is keyed by ``fingerprint_inputs``, which hashes the set
of matching ``(basename, mtime)`` pairs: a new or modified clip invalidates
the cache so a fresh merged file is produced, while an unchanged directory
reuses the cached output instead of writing another merged file every run.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time

import ffmpeg
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
    """Ensure the ffmpeg binary is on PATH.

    ffmpeg-python builds the command lines but still shells out to the ``ffmpeg``
    binary to run them, so its absence is caught up front with a clear message.
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg not found on PATH; Video Combine (Lesca) needs the ffmpeg binary to merge files."
        )


def _stderr_text(error: ffmpeg.Error) -> str:
    """Decode an ``ffmpeg.Error``'s stderr bytes into a stripped string."""
    raw = error.stderr or b""
    return raw.decode(errors="replace").strip()


def _concat_ffmpeg(sources: list[str], out_path: str) -> None:
    """Concatenate ``sources`` into ``out_path`` with stream copy (no re-encode).

    Uses ffmpeg's concat demuxer (via ffmpeg-python), which splices the
    video/audio streams of the listed files in order without decoding them —
    lossless and fast. Requires compatible streams (same codec, resolution,
    frame rate — the case for clips produced by the same pipeline); incompatible
    inputs raise ``RuntimeError`` with ffmpeg's stderr attached, so the caller
    can fall back to ``_reencode_ffmpeg``.
    """
    _ffmpeg()
    out_dir = os.path.dirname(out_path)
    list_fd, list_path = tempfile.mkstemp(prefix="lesca-concat-", suffix=".txt", dir=out_dir)
    try:
        with os.fdopen(list_fd, "w") as list_file:
            for source in sources:
                # Single quotes inside a concat list are escaped as '\'' (the
                # standard ffmpeg quoting).
                list_file.write("file '" + source.replace("'", "'\\''") + "'\n")
        try:
            (
                ffmpeg.input(list_path, format="concat", safe=0)
                .output(out_path, c="copy")
                .overwrite_output()
                .run(quiet=True)
            )
        except ffmpeg.Error as error:
            raise RuntimeError(
                f"ffmpeg concat (stream copy) failed:\n{_stderr_text(error)}"
            ) from error
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass


def _reencode_ffmpeg(sources: list[str], out_path: str) -> None:
    """Fallback merge: decode every source and re-encode one H.264 stream.

    Used when stream copy fails because the inputs are incompatible
    (different codecs, resolutions or frame rates). Audio is dropped
    (``a=0``); the output is a fresh h264/yuv420p MP4.
    """
    _ffmpeg()
    try:
        (
            ffmpeg.concat(*(ffmpeg.input(source) for source in sources), v=1, a=0)
            .output(out_path, vcodec="libx264", pix_fmt="yuv420p")
            .overwrite_output()
            .run(quiet=True)
        )
    except ffmpeg.Error as error:
        raise RuntimeError(
            f"ffmpeg concat (re-encode fallback) failed:\n{_stderr_text(error)}"
        ) from error


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

        try:
            _concat_ffmpeg(sources, out_path)
        except RuntimeError:
            # Stream copy failed (incompatible streams) — re-encode fallback.
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
