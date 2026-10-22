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
span (padded by at most ``1/(2S)`` on each side so the audio stretch also
fits — see the Audio section). Exporting the first second of a one-hour
video then touches exactly those frames instead of the whole file. Inputs where that is not possible
(tensor ``VideoFromComponents`` videos, videos already carrying a non-trivial
trim window, unknown frame rate or frame count in the container metadata,
or a decode that comes back short for pts quirks) fall back to the full
decode path, which applies the same index map to the already-loaded tensors.
Both paths produce identical frames.

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

from fractions import Fraction

import av
import torch
import torch.nn.functional as F

from comfy_api.latest import io, InputImpl, Types, VideoInput


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


def _window_probe(video, R, T, C, trim_mode):
    """Probe a file-backed video's metadata and plan its minimal decode window.

    Returns ``(q, K, S)`` — the output->source index map, the output count and
    the source rate — or ``None`` when the windowed path cannot be used: the
    input is not a plain ``VideoFromFile`` (a tensor video is already in
    memory, so there is nothing to save), the input already carries a
    non-trivial trim window (composing windows is out of scope), or the frame
    rate or frame count is not available from container metadata (the probe
    would need a full decode of its own).
    """
    if not isinstance(video, InputImpl.VideoFromFile):
        return None
    start_time, duration = video.get_active_trim_window()
    if start_time != 0.0 or duration != 0.0:
        return None

    source = video.get_stream_source()
    try:
        with av.open(source, mode="r") as container:
            stream = container.streams.video[0]
            if not stream.average_rate:
                return None  # VFR / unknown rate: a pts window would be a guess
            S = Fraction(stream.average_rate)
            if stream.frames:
                N = int(stream.frames)
            elif stream.duration is not None:
                N = int(round(float(stream.duration * stream.time_base) * float(S)))
            else:
                return None  # frame count unknown without a full decode
    except (av.error.FFmpegError, OSError, ValueError):
        return None
    if N <= 0:
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
                io.Int.Input(
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
        T = int(force_rate or 0)
        C = int(frame_cap or 0)

        # Preferred path: decode only the frames and audio the export needs.
        # The probe reads container metadata only (no decoding); the window
        # around [q[0]/S, (q[-1]+1)/S) covers exactly the exported frames and
        # their audio stretch, so a long input video costs O(window) memory
        # instead of O(video).
        plan = _window_probe(video, R, T, C, trim_mode)
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
            windowed = video.as_trimmed(start, dur, strict_duration=False)
            comps = windowed.get_components()
            # A pts quirk (VFR, container metadata lying) can shrink the
            # decoded window; then the full decode below applies the same
            # index map to the already-loaded tensors.
            if comps.images.shape[0] >= q[-1] - q[0] + 1:
                images = comps.images[[qi - q[0] for qi in q]]
                # The window decode covers [start, start + dur); its audio head
                # can drift by one sample when ComfyUI's per-frame skip uses
                # float-truncated seconds (upstream behaviour, <= 21 us at 48k).
                # Slice the decoded audio down to the exported span [a0, a1).
                audio = _audio_span(comps.audio, a0 - start, a1 - start)

        if images is None:
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
