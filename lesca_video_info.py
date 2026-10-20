"""Video Info (Lesca) — report a video's frame rate, bit depth, color space, dimensions and frame count.

Accepts any ``io.Video`` — in particular the ``video`` output of the Load Video
(Lesca) node — and exposes its properties as individual outputs:

* ``video_info`` — a single string listing every field below, one per line
  (``fps``, ``bit_depth``, ``color_space``, ``width``, ``height``,
  ``frame_count``).
* ``fps``        — the video's frame rate as a float (e.g. ``24.0``).
* ``bitDepth``   — the video's bit depth as a string (e.g. ``"8"`` / ``"10"``).
* ``colorSpace`` — the video's color space as a string: ``sRGB``, ``HDR``,
  ``HDR PQ``, or ``auto`` when the video does not declare one.
* ``width``      — the video's width in pixels.
* ``height``     — the video's height in pixels.
* ``frameCount`` — the video's total frame count as an integer.

All values come from the standard ``VideoInput`` accessors that ComfyUI's
own video nodes use (``get_frame_rate`` / ``get_bit_depth`` / ``get_color_space``
/ ``get_dimensions`` / ``get_frame_count``), so file-based videos read container
metadata directly (no frame decoding, no full in-memory load) and
tensor-based videos (e.g. the output of Save Video (Lesca)) report their
in-memory properties.
"""

from __future__ import annotations

from comfy_api.latest import io


class VideoInfoLesca(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="VideoInfoLesca",
            display_name="Video Info (Lesca)",
            category="video",
            search_aliases=["video info lesca", "video metadata", "video properties", "video fps bit depth"],
            description="Report a video's frame rate, bit depth, color space, width, height and total frame count.",
            inputs=[
                io.Video.Input(
                    "video",
                    display_name="video",
                    tooltip="The video to inspect. Accepts the video output of Load Video (Lesca) or any other video node.",
                ),
            ],
            outputs=[
                io.String.Output(
                    display_name="video_info",
                    tooltip="All other output fields joined into one string, one per line: fps, bit_depth, color_space, width, height, frame_count.",
                ),
                io.Float.Output(
                    display_name="fps",
                    tooltip="Frame rate of the video, e.g. 24.0.",
                ),
                io.String.Output(
                    display_name="bit_depth",
                    tooltip="Bit depth of the video as a string, e.g. '8' or '10'.",
                ),
                io.String.Output(
                    display_name="color_space",
                    tooltip="Color space of the video: 'sRGB', 'HDR', 'HDR PQ', or 'auto' when unspecified.",
                ),
                io.Int.Output(
                    display_name="width",
                    tooltip="Width of the video in pixels.",
                ),
                io.Int.Output(
                    display_name="height",
                    tooltip="Height of the video in pixels.",
                ),
                io.Int.Output(
                    display_name="frame_count",
                    tooltip="Total number of frames in the video.",
                ),
            ],
        )

    @classmethod
    def execute(cls, video: io.Video.Type) -> io.NodeOutput:
        width, height = video.get_dimensions()
        fps = float(video.get_frame_rate())
        bit_depth = str(video.get_bit_depth())
        color_space = str(video.get_color_space())
        width = int(width)
        height = int(height)
        frame_count = int(video.get_frame_count())
        video_info = "\n".join(
            [
                f"fps: {fps}",
                f"bit_depth: {bit_depth}",
                f"color_space: {color_space}",
                f"width: {width}",
                f"height: {height}",
                f"frame_count: {frame_count}",
            ]
        )
        return io.NodeOutput(
            video_info,
            fps,
            bit_depth,
            color_space,
            width,
            height,
            frame_count,
        )
