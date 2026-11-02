"""Current Clip Frames (Lesca) — resolve the clip size for a given index.

Accepts the ``clip_info`` dict from Context Clips (Lesca) and an ``index``,
then reports the frame count the clip at that index must carry: the regular
``clip_frames`` for all non-final clips, and ``last_clip_frames`` for the
final clip. This avoids the "The workflow has a circular node connection"
error that would occur when wiring ``last_clip_frames`` directly back into
the clip-size input of a generation node.

``current_clip_frames = clip_frames if index - init_index + 1 < total_clips else last_clip_frames``

The node is an output node (``is_output_node=True``): the graph executes it
even when nothing is wired to its output. On execution it returns a UI output
(``{"current_clip_frames": (info,)}``) which makes the backend send an
``executed`` websocket message; the frontend extension fills a read-only text
box on the node with the value.
"""

from __future__ import annotations

from comfy_api.latest import io


class CurrentClipFramesLesca(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="CurrentClipFramesLesca",
            display_name="Current Clip Frames (Lesca)",
            category="video",
            search_aliases=[
                "current clip frames lesca",
                "clip frames by index",
                "final clip frames",
            ],
            description="Given the clip_info dict from Context Clips (Lesca) and a clip index, reports the frame count that clip must carry: clip_frames for non-final clips, last_clip_frames for the final clip. Avoids circular node connections.",
            is_output_node=True,
            inputs=[
                io.Dict.Input(
                    "clip_info",
                    display_name="clip info",
                    tooltip="The clip_info dict produced by Context Clips (Lesca), carrying clip_frames, init_index, total_clips, last_clip_frames, target_frames.",
                ),
                io.Int.Input(
                    "index",
                    display_name="index",
                    default=0,
                    min=0,
                    tooltip="The clip index to resolve (0-based, matching init_index).",
                ),
            ],
            outputs=[
                io.Int.Output(
                    display_name="current_clip_frames",
                    tooltip="The frame count the clip at the given index must carry: clip_frames for all non-final clips, last_clip_frames for the final clip.",
                ),
            ],
        )

    @classmethod
    def validate_inputs(cls, clip_info, index):
        if clip_info is not None and not isinstance(clip_info, dict):
            raise ValueError("clip_info must be a dict (from Context Clips (Lesca)).")
        if clip_info is not None:
            for key in ("clip_frames", "init_index", "total_clips", "last_clip_frames"):
                if key not in clip_info:
                    raise ValueError(f"clip_info is missing required key: {key}")
        if index is not None and index < 0:
            raise ValueError("index must be >= 0.")
        return True

    @classmethod
    def execute(cls, clip_info, index):
        clip_frames = clip_info["clip_frames"]
        init_index = clip_info["init_index"]
        total_clips = clip_info["total_clips"]
        last_clip_frames = clip_info["last_clip_frames"]

        current_clip_frames = (
            clip_frames
            if index - init_index + 1 < total_clips
            else last_clip_frames
        )

        info = f"current_clip_frames: {current_clip_frames}"
        return io.NodeOutput(
            current_clip_frames,
            ui={"current_clip_frames": (info,)},
        )
