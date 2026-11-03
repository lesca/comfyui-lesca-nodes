"""Video to Images (Lesca) — turn a video into a batch of image frames.

This is goal 4 of specs.md. The input is ``io.Video`` (typically straight
from Load Video (Lesca), which produces a lazy ``VideoFromFile``); the
outputs are an ``io.Image`` batch, the matching ``io.Audio`` stretch, the
exported frame rate, and a rebuilt ``io.Video`` holding exactly the same
parts. (The exported frame count is not an output: wire the rebuilt video
into Video Info (Lesca)'s ``frame_count``.)

Decode window
-------------
A long input video used to be decoded end-to-end before the first frame
could be exported: one hour of 1080p video is tens of GB of pixels. The node
now computes the source-frame index of every exported frame from the inputs
alone — force-rate first (output frame ``i`` takes source frame
``round(i * S / T)``, clamped to the last source frame), then trim (``head``
drops the first ``R`` output frames, ``tail`` the last ``R``), then frame-cap
(at most ``C`` output frames, ``0`` = no cap) — and, for plain file-backed
input, decodes only the window ``[q[0]/S, (q[-1]+1)/S)`` that those frames
span. Exporting the first second of a one-hour
video then touches exactly those frames instead of the whole file.

VFR handling
------------
Variable-frame-rate videos (``average_rate`` differs from ``base_rate`` by
more than 1 %) are first converted to constant-frame-rate via ffmpeg into
ComfyUI's temp directory (``folder_paths.get_temp_directory()``). The target
CFR rate is ``force_rate`` when set, otherwise the ffprobe
``avg_frame_rate``. The converted file is cached: a deterministic filename
derived from the source path and target rate means the second run reuses the
existing file without re-encoding. After conversion the normal windowed-decode
path applies to the CFR file.

Inputs where windowed decode is not possible (tensor ``VideoFromComponents``
videos, videos already carrying a non-trivial trim window, or both PyAV and
ffprobe failing to provide metadata) fall back to the full decode path, which
applies the same index map to the already-loaded tensors. Both paths produce
identical frames.

Audio
-----
The audio output is the stretch of the source timeline that the exported
video plays, ``[shift/E, (shift+K)/E)``: output frame ``i`` plays at ``i/E``
seconds and maps to source frame ``q[i] ~ (shift+i)/E``, so the audio runs
exactly ``K/E`` seconds — the same length as the video, keeping them in sync
when ``force_rate`` differs from the source rate (slicing the source-frame
span ``[q[0]/S, (q[-1]+1)/S)`` instead is shorter by up to ``K*(1/T-1/S)``
when ``T != S``, which desyncs the trailing audio and breaks segment-to-
segment concatenation). ``shift`` is ``trim_frames`` for ``head`` mode and
``0`` for ``tail``; ``E`` is ``force_rate`` when positive, else the source
rate. A head trim drops the audio prefix that ran under the trimmed frames,
and a frame cap drops the audio tail the unexported frames would have
carried. On the window path the decode window covers the span (plus at most
``1/(2S)`` of head/tail padding for the frame boundaries) and the decoded
audio is sliced to it; on the fallback path the full audio is sliced to the
same span. A video without an audio track yields ``None``. The exported video
output carries the same images and audio stretch.

Resize
------
``(W, H)`` with both zero means keep the source dimensions; exactly one zero
means scale the other proportionally; both non-zero means scale to exactly
``(W, H)``. The exported frame rate is ``force_rate`` when it is positive,
otherwise the source rate.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from fractions import Fraction

import av
import torch
import torch.nn.functional as F

from comfy_api.latest import io, InputImpl, Types, VideoInput

try:
    import comfy.memory_management as _ram
except ImportError:  # standalone / non-ComfyUI context
    _ram = None


def _plan_indices(N, S, trim_mode, R, T, C):
    """Map every output frame to its source frame index.

    Implements the force-rate -> trim -> frame-cap pipeline exactly: the
    video is first re-sampled to ``T`` frames per second of content (frame
    ``i`` takes source frame ``round(i * S / T)``, clamped to the last frame),
    then ``R`` frames are dropped from the chosen end (``head``: the first
    ``R``, ``tail``: the last ``R``), then at most ``C`` frames are kept
    (``C = 0`` keeps all). Returns ``(q, K)``: ``q[i]`` is the source index of
    output frame ``i`` (monotone non-decreasing), ``K`` the output count.
    Raises ``ValueError`` when ``R > 0`` and ``R >= M`` (the trim would remove
    every frame). ``S`` is the source rate as a ``Fraction``, ``N`` the number
    of source frames.
    """
    M = round(N * T / float(S)) if T > 0 else N
    if R > 0 and R >= M:
        raise ValueError(
            f"trim_frames ({R}) must be less than the number of frames ({M})."
        )
    L = M - R
    K = min(L, C) if C > 0 else L
    if T > 0:
        shift = R if trim_mode == "head" else 0
        q = [min(N - 1, round((shift + i) * S / T)) for i in range(K)]
    else:
        base = R if trim_mode == "head" else 0
        q = [base + i for i in range(K)]
    return q, K


def _ffprobe_video_metadata(source):
    """Get (rate, frames, duration) via ffprobe when PyAV metadata is missing.

    Returns a ``(Fraction, int, float)`` tuple or ``None`` on failure.
    """
    import os
    import io as _io
    if isinstance(source, _io.BytesIO):
        return None  # ffprobe needs a file path
    if not isinstance(source, str) or not os.path.isfile(source):
        return None
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
           "-show_streams", "-select_streams", "v:0", source]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        data = json.loads(out.stdout)
        stream = data["streams"][0]
        rate_str = stream.get("avg_frame_rate", "0/0")
        num, _, den = rate_str.partition("/")
        num, den = int(num), (int(den) if den else 1)
        if num <= 0 or den <= 0:
            return None
        rate = Fraction(num, den)
        frames = int(stream.get("nb_frames", 0))
        duration = float(stream.get("duration", 0))
        if frames <= 0 and duration > 0:
            frames = int(round(duration * float(rate)))
        if frames <= 0:
            return None
        return rate, frames, duration
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError,
            IndexError, ValueError):
        return None


def _is_vfr(source):
    """Return True when the file-backed video is variable-frame-rate.

    A video is VFR when its actual average frame rate differs from the
    container's base (nominal) rate by more than 1 %. BytesIO sources and
    unreadable files are treated as not-VFR (the full decode fallback will
    handle them).
    """
    if not isinstance(source, str) or not os.path.isfile(source):
        return False
    try:
        with av.open(source, mode="r") as container:
            stream = container.streams.video[0]
            avg = stream.average_rate
            base = stream.base_rate
            if avg is None or base is None:
                return True
            return abs(float(avg) - float(base)) / float(base) > 0.01
    except (av.error.FFmpegError, OSError, ValueError):
        return False


def _ensure_cfr(source, target_rate=None):
    """Return a path to a CFR version of ``source`` at ``target_rate`` fps.

    If ``target_rate`` is None it is determined via ffprobe. If the CFR file
    already exists in ComfyUI's temp directory the cached path is returned
    immediately without opening the source. If the source is already CFR the
    original path is returned. Otherwise the file is converted via ffmpeg;
    a deterministic filename (hash of source path + target rate) means the
    second call reuses the cached file.
    """
    import folder_paths
    temp_dir = folder_paths.get_temp_directory()
    os.makedirs(temp_dir, exist_ok=True)
    if target_rate is None:
        meta = _ffprobe_video_metadata(source)
        if meta is None:
            return source
        target_rate = float(meta[0])
    key = hashlib.md5(f"{source}:{target_rate}".encode()).hexdigest()[:16]
    basename = os.path.splitext(os.path.basename(source))[0]
    cfr_path = os.path.join(temp_dir, f"{basename}_cfr_{key}.mp4")
    if os.path.isfile(cfr_path):
        return cfr_path
    if not _is_vfr(source):
        return source
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", source,
           "-r", str(target_rate), "-vsync", "cfr", cfr_path]
    result = subprocess.run(cmd, capture_output=True, timeout=300)
    if result.returncode != 0 or not os.path.isfile(cfr_path):
        raise ValueError(
            f"ffmpeg CFR conversion failed for {source}: "
            f"{result.stderr.decode(errors='replace')[:500]}"
        )
    return cfr_path


def _window_probe(video, R, T, C, trim_mode):
    """Probe a file-backed video's metadata and plan its minimal decode window.

    Returns ``(q, K, S)`` — the output->source index map, the output count and
    the source rate — or ``None`` when the windowed path cannot be used: the
    input is not a plain ``VideoFromFile`` (a tensor video is already in
    memory, so there is nothing to save), or the input already carries a
    non-trivial trim window (composing windows is out of scope).

    When PyAV's container metadata is missing (VFR, no frame count), falls
    back to ffprobe to recover the rate and frame count.
    """
    if not isinstance(video, InputImpl.VideoFromFile):
        return None
    start_time, duration = video.get_active_trim_window()
    if start_time != 0.0 or duration != 0.0:
        return None

    source = video.get_stream_source()
    S = None
    N = None
    try:
        with av.open(source, mode="r") as container:
            stream = container.streams.video[0]
            if stream.average_rate:
                S = Fraction(stream.average_rate)
            if stream.frames:
                N = int(stream.frames)
            elif stream.duration is not None and S is not None:
                N = int(round(float(stream.duration * stream.time_base) * float(S)))
    except (av.error.FFmpegError, OSError, ValueError):
        pass

    # PyAV metadata was incomplete — ask ffprobe.
    if S is None or N is None:
        meta = _ffprobe_video_metadata(source)
        if meta is not None:
            fp_rate, fp_frames, _ = meta
            if S is None:
                S = fp_rate
            if N is None:
                N = fp_frames

    if S is None or N is None or N <= 0:
        return None
    q, K = _plan_indices(N, S, trim_mode, R, T, C)
    return q, K, S


def _audio_span(audio, start_s, end_s):
    """Slice the audio to ``[start_s, end_s)`` seconds (the exported span)."""
    if audio is None:
        return None
    sr = int(audio["sample_rate"])
    w = audio["waveform"]
    total = w.shape[-1]
    a0 = max(0, min(total, int(round(start_s * sr))))
    a1 = max(a0, min(total, int(round(end_s * sr))))
    if a0 == 0 and a1 == total:
        return audio
    if a1 <= a0:
        return None
    return {"waveform": w[..., a0:a1], "sample_rate": sr}


def _apply_resize(images, custom_width, custom_height):
    """Apply the (W, H) rules: keep / proportional / exact (both non-zero)."""
    if images.ndim != 4:  # empty batch
        return images
    if custom_width <= 0 and custom_height <= 0:
        return images
    src_h, src_w = images.shape[1], images.shape[2]
    if custom_width > 0 and custom_height > 0:
        target_w, target_h = custom_width, custom_height
    elif custom_width > 0:
        target_w = custom_width
        target_h = max(1, round(src_h * custom_width / src_w))
    else:
        target_h = custom_height
        target_w = max(1, round(src_w * custom_height / src_h))
    if (target_w, target_h) == (src_w, src_h):
        return images
    return F.interpolate(
        images.permute(0, 3, 1, 2),
        size=(target_h, target_w),
        mode="bilinear",
        align_corners=False,
    ).permute(0, 2, 3, 1)


class VideoToImagesLesca(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="VideoToImagesLesca",
            display_name="Video to Images (Lesca)",
            category="video",
            search_aliases=["video to images lesca", "video to frames", "decode video", "video frames"],
            description="Decode a video into an image batch with optional frame-rate conversion, trimming and resizing.",
            inputs=[
                io.Video.Input(
                    "video",
                    display_name="video",
                    tooltip="The video to decode, e.g. straight from Load Video (Lesca).",
                ),
                io.Int.Input(
                    "frame_cap",
                    display_name="frame cap",
                    default=0,
                    min=0,
                    tooltip="0 = keep every frame, otherwise keep at most this many after the rate conversion and trim.",
                ),
                io.Combo.Input(
                    "trim_mode",
                    options=["head", "tail"],
                    display_name="trim mode",
                    default="head",
                    tooltip="'head' drops the first trim frames, 'tail' the last.",
                ),
                io.Int.Input(
                    "trim_frames",
                    display_name="trim frames",
                    default=0,
                    min=0,
                    tooltip="Number of frames to drop after the rate conversion (0 = no trim).",
                ),
                io.Float.Input(
                    "force_rate",
                    display_name="force rate",
                    default=0,
                    min=0,
                    tooltip="0 = keep the source frame rate, otherwise re-sample the content to this many frames per second.",
                ),
                io.Int.Input(
                    "custom_width",
                    display_name="width",
                    default=0,
                    min=0,
                    tooltip="0 = keep the source width.",
                ),
                io.Int.Input(
                    "custom_height",
                    display_name="height",
                    default=0,
                    min=0,
                    tooltip="0 = keep the source height.",
                ),
            ],
            outputs=[
                io.Image.Output(
                    display_name="images",
                    tooltip="The decoded frames, resized if requested, one tensor per frame.",
                ),
                io.Audio.Output(
                    display_name="audio",
                    tooltip="The audio stretch covered by the exported frames; None when the video has no audio.",
                ),
                io.Float.Output(
                    display_name="fps",
                    tooltip="Frame rate of the exported batch: force_rate when set, otherwise the source rate.",
                ),
                io.Video.Output(
                    display_name="video",
                    tooltip="A rebuilt video sharing the exported frames and audio stretch.",
                ),
            ],
        )

    @classmethod
    def validate_inputs(cls, video, frame_cap, trim_mode, trim_frames, force_rate,
                       custom_width, custom_height):
        # A connected input reaches this method as None: validation runs with
        # no execution list, so linked values are lazily marked missing
        # (see execution.get_input_data). The real object only exists at
        # execution time; accept None here and check the type otherwise.
        # Every input can be linked (an INT primitive into frame_cap, an
        # upstream video node into video, ...), so every one of them needs
        # the None guard — a missing guard compares None with a constant
        # and raises TypeError ('<' not supported between NoneType and int).
        if video is not None and not isinstance(video, VideoInput):
            raise ValueError("Input must be io.Video.")
        if frame_cap is not None and frame_cap < 0:
            raise ValueError("frame_cap must be >= 0.")
        if trim_frames is not None and trim_frames < 0:
            raise ValueError("trim_frames must be >= 0.")
        if force_rate is not None and force_rate < 0:
            raise ValueError("force_rate must be >= 0.")
        if (custom_width is not None and custom_width < 0) or (
            custom_height is not None and custom_height < 0
        ):
            raise ValueError("width/height must be >= 0.")
        if trim_mode is not None and trim_mode not in ("head", "tail"):
            raise ValueError("trim_mode must be 'head' or 'tail'.")
        return True

    @classmethod
    def execute(cls, video, frame_cap, trim_mode, trim_frames, force_rate,
                custom_width, custom_height):
        R = int(trim_frames or 0)
        T = float(force_rate or 0)
        C = int(frame_cap or 0)

        # VFR handling: variable-frame-rate videos (average_rate differs from
        # base_rate by > 1 %) are first converted to constant-frame-rate via
        # ffmpeg into ComfyUI's temp directory. The target CFR rate is
        # force_rate when set, otherwise the ffprobe avg_frame_rate. The
        # converted file is cached (deterministic filename), so a second run
        # skips the re-encode. The windowed decode then applies to the CFR
        # file. When the CFR file already exists the source is never opened.
        decode_video = video
        if isinstance(video, InputImpl.VideoFromFile):
            start_time, duration = video.get_active_trim_window()
            if start_time == 0.0 and duration == 0.0:
                source = video.get_stream_source()
                if isinstance(source, str):
                    cfr_path = _ensure_cfr(source, T if T > 0 else None)
                    if cfr_path != source:
                        decode_video = InputImpl.VideoFromFile(cfr_path)

        # The windowed decode allocates a fresh tensor the size of the clip
        # (e.g. ~12 GB for a 124-frame 2160x3840 clip).  If a previous run's
        # output tensor is still sitting in the execution cache, the two coexist
        # briefly and the peak can push the OS into swap, slowing the decode
        # (observed: 2nd run 15+s vs a clean 5s).  execution.py's ram_release
        # runs *after* the node, so the old tensor is freed only once the new
        # one is already allocated.  Asking ComfyUI to release inactive cache
        # first keeps the peak at one tensor.  free_active=False evicts only
        # older-generation entries, so it never drops a tensor this same run
        # produced.  extra_ram_release is a no-op when the callback isn't wired
        # (standalone use), so this is safe everywhere.
        if _ram is not None:
            # RAM_CACHE_HEADROOM (~6 GB on a 60 GB box) is below the available
            # memory, so the gate in ram_release (available >= target) is true
            # and the call is a no-op.  Nudge the target just above the current
            # available memory to force the gate false; the old-generation
            # tensor (highest oom_score) is evicted first and the loop stops as
            # soon as it fits again, so only the stale tensor is dropped.
            from comfy.system_memory import virtual_memory_available
            _ram.extra_ram_release(virtual_memory_available() + 1,
                                   free_active=False)

        # Preferred path: decode only the frames and audio the export needs.
        # The probe reads container metadata only (no decoding); the window
        # around [q[0]/S, (q[-1]+1)/S) covers exactly the exported frames and
        # their audio stretch, so a long input video costs O(window) memory
        # instead of O(video).
        plan = _window_probe(decode_video, R, T, C, trim_mode)
        images = audio = None
        S = None
        if plan is not None and plan[1] > 0:
            q, K, S = plan
            # The audio is stretched to the output video's duration, not the
            # source frames' span: output frame i plays at i/E seconds and maps
            # to source frame q[i] ~ (shift+i)/E, so the audio must cover
            # [shift/E, (shift+K)/E) — exactly K/E seconds, the video's length.
            # Slicing the source span [q[0]/S, (q[-1]+1)/S) instead is off by
            # up to K*(1/T-1/S) when T != S, desyncing the trailing audio and
            # breaking segment-to-segment concatenation.
            E = Fraction(T) if T > 0 else S
            shift = R if trim_mode == "head" else 0
            a0 = float(shift / E)
            a1 = float((shift + K) / E)
            # Decode a window covering both the needed source frames and the
            # audio span (they differ by at most 1/(2S) on each side).
            start = min(float(q[0] / S), a0)
            dur = max(float((q[-1] + 1) / S), a1) - start
            needed = q[-1] - q[0] + 1
            windowed = decode_video.as_trimmed(start, dur, strict_duration=False)
            comps = windowed.get_components()
            if comps.images.shape[0] >= needed:
                images = comps.images[[qi - q[0] for qi in q]]
                # The window decode covers [start, start + dur); its audio
                # head can drift by one sample when ComfyUI's per-frame
                # skip uses float-truncated seconds (upstream behaviour,
                # <= 21 us at 48k).  Slice the decoded audio down to the
                # exported span [a0, a1).
                audio = _audio_span(comps.audio, a0 - start, a1 - start)

        if images is None:
            # The windowed path is unavailable: the input is a tensor video
            # (already in memory, so a full decode costs nothing extra), or
            # both PyAV and ffprobe failed to provide metadata.  Apply the
            # same index map to the already-loaded tensors.
            components = video.get_components()
            full = components.images
            S = Fraction(components.frame_rate)
            q, K = _plan_indices(full.shape[0], S, trim_mode, R, T, C)
            images = full[q]
            # Audio stretched to the output video's duration [shift/E,
            # (shift+K)/E) so it stays in sync when force_rate != source rate.
            E = Fraction(T) if T > 0 else S
            shift = R if trim_mode == "head" else 0
            audio = _audio_span(components.audio, float(shift / E),
                                float((shift + K) / E)) if K else None

        E = Fraction(T) if T > 0 else S
        images = _apply_resize(images, int(custom_width or 0), int(custom_height or 0))
        fps = float(E)
        video_out = InputImpl.VideoFromComponents(
            Types.VideoComponents(images=images, audio=audio, frame_rate=Fraction(fps)),
            bit_depth=video.get_bit_depth(),
            color_space=video.get_color_space(),
        )
        return io.NodeOutput(images, audio, fps, video_out)
