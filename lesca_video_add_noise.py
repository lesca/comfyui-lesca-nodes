"""Video Add Noise (Lesca) — cut a head/tail segment of frames from a video and add independent per-frame noise.

This is goal 6 of specs.md. The input is ``io.Video`` (typically straight from
Load Video (Lesca), which produces a lazy ``VideoFromFile``). The node cuts a
contiguous block of frames from the head or tail of the video and adds Gaussian
noise to every frame, with a per-layer strength schedule: ``frame_num_list``
gives how many frames belong to each layer and ``strenth_list`` gives each
layer's noise strength. The default ``"18 2 1 1"`` / ``"0.4 0.3 0.1 0.04"``
cuts 22 frames — 18 at strength 0.4, then 2 at 0.3, 1 at 0.1 and 1 at 0.04 — a
fade-out noise ramp. The output is the noise-added image batch only.

Frame cut
---------
``total = sum(frame_num_list)`` frames are cut from the video's head (``mode ==
"head"``, the default) or tail (``mode == "tail"``). File-backed input uses the
lazy ``as_trimmed`` window exactly like the built-in ``VideoFrameSample``
head/tail strategies (no full-file decode — only the needed frames are read),
then ``get_components`` materializes that window, mirroring the built-in
``GetVideoComponents``. Tensor-backed input (``VideoFromComponents``) and
inputs that already carry a non-trivial trim window slice the in-memory tensor
directly instead, so nothing is re-encoded. When the video has fewer frames than
``total`` the cut clamps to every available frame (``take = min(total, N)``)
and the extra frames are absorbed by the earlier layers.

Noise
-----
Each frame is noised independently, the way the built-in ``ImageAddNoise`` does
(``image + strength * randn`` clipped to ``[0, 1]``) — except the noise is
re-randomized per frame, so two frames at the same strength still differ. With
``seed == 0`` a fresh unseeded ``torch.Generator`` is created for every frame,
so every run produces different noise. With ``seed > 0`` frame ``i`` is seeded
with ``seed + i``, which keeps the result reproducible while still making every
frame's noise distinct. A frame's alpha channel (transparency, ``C == 4``) is
copied through untouched, matching ``ImageAddNoise``.

Validation
----------
``frame_num_list`` and ``strenth_list`` must have the same number of layers;
otherwise the node raises ``ValueError`` and the workflow cannot run (a spec
requirement). List entries are split on whitespace and/or commas; frame counts
must be integers ``>= 1`` and strengths must be floats ``>= 0``.
"""

from __future__ import annotations

import random
import re

import torch

from comfy_api.latest import io, InputImpl, VideoInput


def _split_list(raw) -> list[str]:
    """Tokens of a list string, split on whitespace and/or commas."""
    return [tok for tok in re.split(r"[,\s]+", (raw or "").strip()) if tok]


def _parse_int_list(raw, name: str) -> list[int]:
    """Parse ``"18 2 1 1"`` into ``[18, 2, 1, 1]``; every entry must be ``>= 1``."""
    tokens = _split_list(raw)
    if not tokens:
        raise ValueError(f"{name} is empty; specify at least one layer.")
    try:
        values = [int(token) for token in tokens]
    except ValueError as error:
        raise ValueError(f"{name} has a non-integer entry: {raw!r}.") from error
    if any(value < 1 for value in values):
        raise ValueError(f"{name} entries must be >= 1 (got {values}).")
    return values


def _parse_float_list(raw, name: str) -> list[float]:
    """Parse ``"0.4 0.3 0.1 0.04"`` into ``[0.4, 0.3, 0.1, 0.04]``; every entry ``>= 0``."""
    tokens = _split_list(raw)
    if not tokens:
        raise ValueError(f"{name} is empty; specify at least one layer.")
    try:
        values = [float(token) for token in tokens]
    except ValueError as error:
        raise ValueError(f"{name} has a non-numeric entry: {raw!r}.") from error
    if any(value < 0 for value in values):
        raise ValueError(f"{name} entries must be >= 0 (got {values}).")
    return values


def _cut_frames(video, take: int, mode: str, fps: float, n_frames: int):
    """Return the ``take`` frames cut from the head or tail of ``video``.

    File-backed input with no pre-existing trim window uses the lazy
    ``as_trimmed`` window (reference ``VideoFrameSample`` head/tail), so only the
    cut frames are decoded. Anything else — tensor videos, inputs already
    carrying a trim window, or a windowed decode that comes back short for pts
    quirks — materializes the full tensor and slices it. Both paths return
    exactly ``take`` frames.
    """
    if (
        isinstance(video, InputImpl.VideoFromFile)
        and video.get_active_trim_window() == (0.0, 0.0)
    ):
        start = 0.0 if mode == "head" else (n_frames - take) / fps
        trimmed = video.as_trimmed(start, take / fps, strict_duration=False)
        images = trimmed.get_components().images
        if images.shape[0] >= take:
            return images[:take] if mode == "head" else images[-take:]
    images = video.get_components().images
    return images[:take] if mode == "head" else images[-take:]


def _frame_layers(take: int, counts: list[int]) -> list[int]:
    """Map each of the ``take`` frames to its layer via a cumulative walk.

    Frame ``i`` belongs to the first layer whose cumulative count exceeds ``i``.
    When ``take`` is smaller than the total (a short video), the later layers
    simply receive fewer — or zero — frames; the walk never runs past ``take``.
    """
    layers = [0] * take
    start = 0
    for layer, count in enumerate(counts):
        for i in range(start, min(start + count, take)):
            layers[i] = layer
        start += count
    return layers


class VideoAddNoiseLesca(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="VideoAddNoiseLesca",
            display_name="Video Add Noise (Lesca)",
            category="video",
            search_aliases=["video add noise lesca", "video noise", "add noise to video", "video film grain"],
            description="Cut head/tail frames from a video and add independent per-frame noise with per-layer strengths.",
            inputs=[
                io.Video.Input(
                    "video",
                    display_name="video",
                    tooltip="The video to cut frames from, e.g. straight from Load Video (Lesca).",
                ),
                io.Combo.Input(
                    "mode",
                    options=["head", "tail"],
                    display_name="mode",
                    default="head",
                    tooltip="'head' cuts the first N frames (the default), 'tail' the last N.",
                ),
                io.String.Input(
                    "frame_num_list",
                    display_name="frame num list",
                    default="18 2 1 1",
                    dynamic_prompts=True,
                    tooltip="Frames per layer, e.g. '18 2 1 1' cuts 22 frames (18+2+1+1). Whitespace or comma separated. May be connected to a string-producing node.",
                ),
                io.String.Input(
                    "strenth_list",
                    display_name="strenth list",
                    default="0.4 0.3 0.1 0.04",
                    dynamic_prompts=True,
                    tooltip="Noise strength per layer, e.g. '0.4 0.3 0.1 0.04' — one value per frame_num_list layer. May be connected to a string-producing node.",
                ),
                io.Int.Input(
                    "seed",
                    display_name="seed",
                    default=0,
                    min=0,
                    max=0xFFFFFFFFFFFFFFFF,
                    control_after_generate=True,
                    tooltip="0 = fully random noise (different every run). Otherwise frame i is seeded with seed + i, so the result is reproducible and every frame's noise still differs.",
                ),
            ],
            outputs=[
                io.Image.Output(
                    display_name="images",
                    tooltip="The noise-added frames, one tensor per frame, in cut order.",
                ),
            ],
        )

    @classmethod
    def validate_inputs(cls, video, mode, frame_num_list, strenth_list, seed):
        # Linked (unresolved) inputs arrive as None during prompt validation, so
        # each guard is optional; the real objects only exist at execution time.
        if video is not None and not isinstance(video, VideoInput):
            return "video must be io.Video."
        if mode is not None and mode not in ("head", "tail"):
            return "mode must be 'head' or 'tail'."
        if seed is not None and seed < 0:
            return "seed must be >= 0."
        if frame_num_list is not None:
            try:
                _parse_int_list(frame_num_list, "frame_num_list")
            except ValueError as error:
                return str(error)
        if strenth_list is not None:
            try:
                _parse_float_list(strenth_list, "strenth_list")
            except ValueError as error:
                return str(error)
        if frame_num_list is not None and strenth_list is not None:
            if len(_parse_int_list(frame_num_list, "frame_num_list")) != len(
                _parse_float_list(strenth_list, "strenth_list")
            ):
                return "frame_num_list and strenth_list must have the same number of layers."
        return True

    @classmethod
    def execute(cls, video, mode, frame_num_list, strenth_list, seed):
        counts = _parse_int_list(frame_num_list, "frame_num_list")
        strengths = _parse_float_list(strenth_list, "strenth_list")
        if len(counts) != len(strengths):
            raise ValueError(
                f"frame_num_list ({len(counts)} layers) and strenth_list "
                f"({len(strengths)} layers) must have the same number of layers."
            )

        total = sum(counts)
        n_frames = int(video.get_frame_count())
        take = min(total, n_frames)
        if take <= 0:
            raise ValueError("The video has no frames to add noise to.")
        fps = float(video.get_frame_rate())

        frames = _cut_frames(video, take, mode, fps, n_frames)
        layers = _frame_layers(take, counts)

        seed = int(seed or 0)
        noisy = torch.empty_like(frames)
        for i in range(take):
            frame = frames[i]
            if seed > 0:
                generator = torch.Generator().manual_seed(seed + i)
            else:
                # An unseeded torch.Generator() is deterministic within a process
                # (two of them produce identical noise), so seed each frame from
                # Python's OS-entropy RNG to keep seed 0 fully random per run.
                generator = torch.Generator().manual_seed(random.getrandbits(63))
            noise = torch.randn(frame.size(), generator=generator, device="cpu").to(frame)
            added = torch.clip(frame + strengths[layers[i]] * noise, 0.0, 1.0)
            if frame.shape[-1] == 4:  # alpha stores transparency, not color
                added[..., 3] = frame[..., 3]
            noisy[i] = added
        return io.NodeOutput(noisy)
