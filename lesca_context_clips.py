"""Context Clips (Lesca) — plan the clip schedule for a target video.

Accepts any ``io.Video`` (in particular the output of Load Video (Lesca) or
Video to Images (Lesca)) and reports, as three integer outputs, how a
clip-based generation pipeline should cover the target length:

* ``target_frame_count`` — total frames of the video re-sampled to
  ``target_fps``: ``int((frame_count / fps) * target_fps)``. ``target_fps = 0``
  means "keep the source rate", so the count equals ``frame_count``.
* ``total_clips`` — number of clips needed with a stride of
  ``clip_frames - context_frames``:
  ``(target_frame_count - clip_frames) // stride + 1 - init_index``
  (incremented by one when the remainder branch below keeps a last clip).
* ``last_clip_frames`` — frame count of the final clip, from
  ``remainder = (target_frame_count - clip_frames) % stride``:
  ``0`` when ``floor(remainder / 17) == 0`` (a remainder below 17 frames —
  including exactly covering the target — leaves no final clip); otherwise
  ``floor(remainder / 17) * 17 + context_frames`` and ``total_clips`` is
  incremented by one.

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

The math follows the reference formulas from specs.md (goal 7) verbatim:
``target_frame_count`` is truncated with ``int(...)`` (not rounded), and
``total_clips`` / ``remainder`` use Python integer ``//`` and ``%`` on the
already-int target frame count (a short target still keeps a non-negative
remainder, e.g. ``(120 - 124) % 102 == 98``). The only adaptation:
``target_fps = 0`` is interpreted as "use the source fps" (the raw formula
would yield 0 frames); substituting the source fps yields ``frame_count``.
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
                "run the train",
                "minimax h3",
            ],
            description="For MiniMax H3 action-migration run-the-train long-video generation: slice the video's frame count with a fixed-stride clip window, overlapping slices by context frames to cancel the 'Add Guide' / 'Motion Context' overlap-frame effect; report the run's total frame count, total clip count and final clip size.",
            is_output_node=True,
            inputs=[
                io.Video.Input(
                    "video",
                    display_name="video",
                    tooltip="The video to slice. The node reads its frame count and frame rate (the same accessors as Video Info, no decoding). Accepts the video output of Load Video (Lesca) or any other video node.",
                ),
                io.Int.Input(
                    "target_fps",
                    display_name="target fps",
                    default=0,
                    min=0,
                    tooltip="0 = keep the source frame rate (slice by the video's frame count); otherwise slicing uses the frame count converted to this rate.",
                ),
                io.Int.Input(
                    "clip_frames",
                    display_name="clip frames",
                    default=124,
                    min=1,
                    tooltip="Fixed-stride slicing window: frames per clip. Must be greater than context frames.",
                ),
                io.Int.Input(
                    "context_frames",
                    display_name="context frames",
                    default=22,
                    min=0,
                    tooltip="Overlap kept between adjacent slices — it cancels the overlap-frame effect of the 'Add Guide' / 'Motion Context' nodes.",
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
                    tooltip="The total frame count the run slices: the video's frame count, or the frame count converted to target fps.",
                ),
                io.Int.Output(
                    display_name="total_clips",
                    tooltip="The total number of clips the run-the-train generation needs (already reduced by init index).",
                ),
                io.Int.Output(
                    display_name="last_clip_frames",
                    tooltip="The frame count the final clip must carry: the tail aligned to the 17n+5 pattern (a multiple of 17 plus context frames, 22 by default); 0 when there is no final clip.",
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

        # 1. 计算目标总帧数（避免重复计算）—— int() 截断，非四舍五入
        target_frame_count = int((frame_count / fps) * effective_fps)

        # 2. 定义核心步长 (Stride)
        stride = int(clip_frames) - int(context_frames)

        # 3. 总片段数 total_clips（整数 // 整除）
        total_clips = (
            (target_frame_count - clip_frames) // stride + 1 - init_index
        )

        # 4. 最后片段帧数 last_clip_frames
        remainder = (target_frame_count - clip_frames) % stride
        if math.floor(remainder / 17) == 0:
            last_clip_frames = 0
        else:
            last_clip_frames = math.floor(remainder / 17) * 17 + context_frames
            total_clips += 1

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
