"""Context Clips (Lesca) — plan the clip schedule for a target video.

Accepts any ``io.Video`` (in particular the output of Load Video (Lesca) or
Video to Images (Lesca)) and reports, as three integer outputs, how a
clip-based generation pipeline should cover the target length:

* ``target_frame_count`` — total frames of the video re-sampled to
  ``target_fps``: ``(frame_count / fps) * target_fps``. ``target_fps = 0``
  means "keep the source rate", so the count equals ``frame_count``.
* ``total_clips`` — number of clips needed with a stride of
  ``clip_frames - context_frames``:
  ``ceil((target_frame_count - clip_frames) / stride) + 1 - init_index``
  (decremented by one when the remainder branch below drops the last clip).
* ``last_clip_frames`` — frame count of the final clip, from
  ``remainder = (target_frame_count - clip_frames) % stride``:
  ``0`` when the remainder is ``0`` (the target is exactly covered) or when
  ``0 < remainder < 8`` (a final clip shorter than 0.33 s is dropped and
  ``total_clips`` is decremented); otherwise
  ``ceil(remainder / 17) * 17 + context_frames``.

All three values are pure arithmetic on the video's container metadata
(``get_frame_count`` / ``get_frame_rate``, the same accessors Video Info
(Lesca) uses — no frame decoding), so the node can be used to drive an
upstream generation node that works on clip-sized chunks.

The node is an output node (``is_output_node=True``): the graph executes it
even when nothing is wired to its outputs. On execution it also returns a UI
output (``{"context_clips": (info,)}``) which makes the backend send an
``executed`` websocket message; the frontend extension (``web/extension.js``)
listens for it and fills a read-only text box on the node with the three
fields, one per line.

The math follows the reference formulas from specs.md (goal 7) verbatim,
including Python's float ``%`` semantics (e.g. ``(-4) % 102 == 98``).
The only adaptations: ``target_fps = 0`` is interpreted as "use the source
fps" (the raw formula would yield 0 frames), and the reported integers use
``int(round(...))`` of the float results (rate conversions normally yield
integer-valued floats; rounding guards against float error).
"""

from __future__ import annotations

import math

from comfy_api.latest import io, VideoInput


class ContextClipsLesca(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="ContextClipsLesca",
            display_name="Context Clips (Lesca)",
            category="video",
            search_aliases=[
                "context clips lesca",
                "clip planning",
                "target frame count",
                "total clips",
                "context frames",
            ],
            description="Plan the clip schedule for a target video: target frame count, total clips and last clip size.",
            is_output_node=True,
            inputs=[
                io.Video.Input(
                    "video",
                    display_name="video",
                    tooltip="The video to plan clips for. Accepts the video output of Load Video (Lesca) or any other video node.",
                ),
                io.Int.Input(
                    "target_fps",
                    display_name="target fps",
                    default=0,
                    min=0,
                    tooltip="0 = keep the source frame rate, otherwise re-sample the video to this many frames per second.",
                ),
                io.Int.Input(
                    "clip_frames",
                    display_name="clip frames",
                    default=124,
                    min=1,
                    tooltip="Frames per clip. Must be greater than context frames (the stride).",
                ),
                io.Int.Input(
                    "context_frames",
                    display_name="context frames",
                    default=22,
                    min=0,
                    tooltip="Context frames kept between clips; the stride is clip frames - context frames.",
                ),
                io.Int.Input(
                    "init_index",
                    display_name="init index",
                    default=0,
                    min=0,
                    tooltip="Clips already consumed before this node runs; subtracted from the total clip count.",
                ),
            ],
            outputs=[
                io.Int.Output(
                    display_name="target_frame_count",
                    tooltip="Total frames of the video at the target rate: (frame_count / fps) * target_fps, or frame_count when target fps is 0.",
                ),
                io.Int.Output(
                    display_name="total_clips",
                    tooltip="Total number of clips: ceil((target frames - clip frames) / stride) + 1 - init index; decremented when the final remainder is dropped (less than 0.33 s).",
                ),
                io.Int.Output(
                    display_name="last_clip_frames",
                    tooltip="Frame count of the final clip: 0 when the remainder is 0 or below 0.33 s; otherwise ceil(remainder / 17) * 17 + context frames, where remainder = (target frames - clip frames) % stride.",
                ),
            ],
        )

    @classmethod
    def validate_inputs(cls, video, target_fps, clip_frames, context_frames,
                        init_index):
        # A connected input reaches this method as None (validation runs
        # without an execution list, so linked values are lazily marked
        # missing — see execution.get_input_data); accept None here and
        # check the type otherwise. Every input can be linked, so every one
        # needs the None guard.
        if video is not None and not isinstance(video, VideoInput):
            raise ValueError("Input must be io.Video.")
        if target_fps is not None and target_fps < 0:
            raise ValueError("target_fps must be >= 0.")
        if clip_frames is not None and clip_frames < 1:
            raise ValueError("clip_frames must be >= 1.")
        if context_frames is not None and context_frames < 0:
            raise ValueError("context_frames must be >= 0.")
        if init_index is not None and init_index < 0:
            raise ValueError("init_index must be >= 0.")
        if (clip_frames is not None and context_frames is not None
                and clip_frames <= context_frames):
            raise ValueError(
                f"clip_frames ({clip_frames}) must be greater than "
                f"context_frames ({context_frames}): the stride would be zero."
            )
        return True

    @classmethod
    def execute(cls, video, target_fps, clip_frames, context_frames,
                init_index):
        fps = float(video.get_frame_rate())
        frame_count = int(video.get_frame_count())

        # target_fps = 0 means "keep the source rate" (the raw formula would
        # give 0 frames); substituting the source fps yields frame_count.
        effective_fps = float(target_fps) if int(target_fps or 0) > 0 else fps
        # Keep the float for the downstream math (the reference formulas use
        # float %); only the reported values are rounded to int.
        target_frame_count = (frame_count / fps) * effective_fps
        stride = int(clip_frames) - int(context_frames)
        total_clips = math.ceil((target_frame_count - clip_frames) / stride) + 1 - init_index
        remainder = (target_frame_count - clip_frames) % stride
        if remainder == 0:
            last_clip_frames = 0
        elif remainder < 8:  # if last clip < 0.33s
            last_clip_frames = 0
            total_clips -= 1
        else:  # if last clip > 0.33s
            last_clip_frames = math.ceil(remainder / 17) * 17 + context_frames

        target_frame_count = int(round(target_frame_count))
        total_clips = int(round(total_clips))
        last_clip_frames = int(round(last_clip_frames))

        info = "\n".join(
            [
                f"target_frame_count: {target_frame_count}",
                f"total_clips: {total_clips}",
                f"last_clip_frames: {last_clip_frames}",
            ]
        )
        # The ``ui`` output is what makes the backend send an ``executed``
        # websocket message (execution.py only sends it when the node
        # returned a UI dict — same pattern as Video Info (Lesca)). The
        # frontend extension turns ``output.context_clips`` into the node's
        # read-only text box.
        return io.NodeOutput(
            target_frame_count,
            total_clips,
            last_clip_frames,
            ui={"context_clips": (info,)},
        )
