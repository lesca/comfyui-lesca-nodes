"""Save Video (Lesca) — merge ComfyUI's Create Video and Save Video into one node.

The built-in workflow needs two nodes: ``CreateVideo`` (turn a batch of image
tensors into a video, optionally with audio) and ``SaveVideo`` (write the video
to the output directory with a ``_000001_``-style counter suffix). This node
combines both and adds two capabilities that neither provides:

* ``filename`` — the *path* of the video file to save, e.g.
  ``output/video/clip.mp4``. A relative path resolves against the ComfyUI root
  directory (``folder_paths.base_path``), so ``output/video/clip.mp4`` means
  ``<root>/output/video/clip.mp4``; an absolute path is used as-is. The file is
  written under exactly the given path — no ``_000001_`` counter suffix — and
  the parent directory is created if missing.
* ``trim_frames`` — drop the first N frames and the corresponding stretch of
  audio (``N / fps`` seconds, computed the same way ``TrimAudioDuration`` does)
  before the video is encoded.

The remaining inputs mirror the two source nodes: ``fps`` / ``bit_depth`` /
``color_space`` from ``CreateVideo`` and ``format`` (which nests ``codec`` /
``encoding`` / ``crf``) from ``SaveVideo``. ``CreateVideo``'s own ``codec``
input is intentionally omitted — the output codec is chosen by ``format``.
"""

from __future__ import annotations

import os
from fractions import Fraction
from typing import Optional

import folder_paths
from comfy.cli_args import args
from comfy_api.latest import io, Input, InputImpl, Types


def _resolved_save_path(filename: str) -> str:
    """Resolve ``filename`` to an absolute save path (no side effects).

    ``filename`` is the *path* of the video file to save, e.g.
    ``output/video/clip.mp4``. A relative path resolves against the ComfyUI root
    directory (``folder_paths.base_path``), so ``output/video/clip.mp4`` means
    ``<root>/output/video/clip.mp4``; an absolute path is used as-is.

    Raises:
        ValueError: if the path is empty, or a relative path escapes the base
            directory.
    """
    filename = (filename or "").strip()
    if not filename:
        raise ValueError("Filename is empty; specify the video file path to save.")

    if os.path.isabs(filename):
        return os.path.abspath(filename)

    base_dir = folder_paths.base_path
    save_path = os.path.abspath(os.path.join(base_dir, filename))
    if not folder_paths.is_within_directory(base_dir, save_path):
        raise ValueError(f"Path {filename!r} escapes the base directory {base_dir!r}.")
    return save_path


def _resolve_save_path(filename: str, format_name: str) -> str:
    """Resolve ``filename`` to the absolute path the video is written to.

    If the name carries no extension, the container's extension (e.g. ``.mp4``
    / ``.webm``) is appended so the file is always a well-formed media file. The
    parent directory is created (with parents) if missing.
    """
    save_path = _resolved_save_path(filename)
    if not os.path.splitext(save_path)[1]:
        save_path += "." + Types.VideoContainer.get_extension(format_name)
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    return save_path


def _trim_video_head(images, audio, trim_frames: int, fps: float):
    """Drop the first ``trim_frames`` frames and the matching audio prefix.

    The audio prefix removed is ``trim_frames / fps`` seconds, converted to a
    sample count with the audio's own sample rate — the same arithmetic
    ``TrimAudioDuration`` uses (``start_index * sample_rate``). Returns the
    trimmed ``(images, audio)`` pair; ``audio`` becomes ``None`` if the whole
    clip was trimmed away.
    """
    if trim_frames <= 0:
        return images, audio

    total = images.shape[0]
    if trim_frames >= total:
        raise ValueError(
            f"trim_frames ({trim_frames}) must be less than the number of frames ({total})."
        )

    images = images[trim_frames:]

    if audio is not None:
        sample_rate = int(audio["sample_rate"])
        start_seconds = trim_frames / float(fps)
        start_sample = int(round(start_seconds * sample_rate))
        waveform = audio["waveform"]
        if start_sample >= waveform.shape[-1]:
            audio = None  # the entire audio was removed
        else:
            audio = {"waveform": waveform[..., start_sample:], "sample_rate": sample_rate}

    return images, audio


def _lesca_codec_input(supported_codecs, *, optional=False):
    """The nested ``codec`` DynamicCombo, mirroring SaveVideo's ``_save_video_codec_input``."""
    codec_options = []
    if "auto" in supported_codecs:
        codec_options.append(io.DynamicCombo.Option("auto", []))
    if "h264" in supported_codecs:
        codec_options.append(
            io.DynamicCombo.Option(
                "h264",
                [
                    io.DynamicCombo.Input(
                        "encoding",
                        display_name="encoding mode",
                        options=[
                            io.DynamicCombo.Option("auto", []),
                            io.DynamicCombo.Option(
                                "re-encode",
                                [
                                    io.Float.Input("crf", default=19.0, min=0.0, max=51.0, step=1.0,
                                                   tooltip="Lower values produce higher quality and larger files."),
                                ],
                            ),
                        ],
                        optional=True,
                        tooltip="Automatic preserves compatible H.264 streams. Re-encode applies custom encoding options.",
                    ),
                ],
            )
        )
    if "av1" in supported_codecs:
        codec_options.append(
            io.DynamicCombo.Option(
                "av1",
                [
                    io.DynamicCombo.Input(
                        "encoding",
                        display_name="encoding mode",
                        options=[
                            io.DynamicCombo.Option("auto", []),
                            io.DynamicCombo.Option(
                                "re-encode",
                                [
                                    io.Float.Input("crf", default=24.0, min=0.0, max=63.0, step=1.0,
                                                   tooltip="Lower values produce higher quality and larger files."),
                                ],
                            ),
                        ],
                        optional=True,
                        tooltip="Automatic preserves compatible AV1 streams. Re-encode applies custom encoding options.",
                    ),
                ],
            )
        )
    return io.DynamicCombo.Input(
        "codec",
        options=codec_options,
        optional=optional,
        tooltip="The output video codec. Auto uses H.264 (AV1 for WebM). H.264 and AV1 re-encoding support SDR, HDR (HLG), and HDR PQ.",
    )


class SaveVideoLesca(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="SaveVideoLesca",
            search_aliases=["save video lesca", "create and save video", "images to video file"],
            display_name="Save Video (Lesca)",
            category="video",
            description="Create a video from images (and optional audio) and save it to a user-specified file path.",
            is_output_node=True,
            has_intermediate_output=True,
            inputs=[
                io.Image.Input("images", tooltip="The images to create the video from."),
                io.Audio.Input("audio", optional=True, tooltip="The audio to add to the video."),
                io.String.Input(
                    "filename",
                    display_name="filename",
                    default="output/video/clip.mp4",
                    dynamic_prompts=True,
                    tooltip="Path of the video file to save, e.g. 'output/video/clip.mp4'. A relative path resolves against the ComfyUI root directory; an absolute path is used as-is. Saved under exactly this path (no counter suffix). An extension is appended if the name has none; the parent directory is created if missing.",
                ),
                io.Int.Input(
                    "trim_frames",
                    display_name="trim_frames",
                    default=0,
                    min=0,
                    tooltip="Drop the first N frames and the first N/fps seconds of audio before encoding. 0 (default) keeps everything.",
                ),
                io.Float.Input("fps", default=30.0, min=1.0, max=120.0, step=1.0,
                               tooltip="Frame rate of the output video."),
                io.Combo.Input(
                    "bit_depth",
                    options=["auto", 8, 10],
                    default="auto",
                    optional=True,
                    tooltip="Auto uses 8-bit for sRGB and 10-bit for HDR.",
                ),
                io.Combo.Input(
                    "color_space",
                    options=["sRGB", "HDR", "HDR PQ"],
                    default="sRGB",
                    optional=True,
                    tooltip="Colorspace of the input images. HDR selects BT.2020/HLG and HDR PQ selects BT.2020/PQ.",
                ),
                io.DynamicCombo.Input(
                    "format",
                    options=[
                        io.DynamicCombo.Option("auto", [_lesca_codec_input(["auto", "h264", "av1"])]),
                        io.DynamicCombo.Option("mp4", [_lesca_codec_input(["auto", "h264", "av1"])]),
                        io.DynamicCombo.Option("mkv", [_lesca_codec_input(["auto", "h264", "av1"])]),
                        io.DynamicCombo.Option("webm", [_lesca_codec_input(["auto", "av1"])]),
                    ],
                    tooltip="The output container (and codec / encoding / crf). Auto uses MP4 for Auto/H.264 and WebM for AV1.",
                ),
            ],
            hidden=[io.Hidden.prompt, io.Hidden.extra_pnginfo],
            outputs=[
                io.String.Output(display_name="filename", tooltip="Absolute path of the saved video file."),
                io.Video.Output(display_name="video", tooltip="The saved video."),
                io.Image.Output(display_name="images", tooltip="The trimmed images — the first trim_frames frames dropped. Same type as the images input."),
                io.Audio.Output(display_name="audio", tooltip="The trimmed audio — the first trim_frames/fps seconds dropped. Same type as the audio input (None when no audio is provided)."),
            ],
        )

    @classmethod
    def execute(
        cls,
        images: Input.Image,
        filename: str,
        trim_frames: int,
        fps: float,
        audio: Optional[Input.Audio] = None,
        bit_depth: int | str = "auto",
        color_space: str = "sRGB",
        format: io.DynamicCombo.Type | str = "auto",
    ) -> io.NodeOutput:
        # 1. Drop the head frames and the matching audio prefix.
        images, audio = _trim_video_head(images, audio, int(trim_frames or 0), float(fps))

        # 2. Resolve the bit depth (CreateVideo's auto logic).
        if bit_depth == "auto":
            bit_depth = 10 if color_space in ("HDR", "HDR PQ") else 8

        # 3. Build the video from components (CreateVideo's logic).
        video = InputImpl.VideoFromComponents(
            Types.VideoComponents(images=images, audio=audio, frame_rate=Fraction(fps)),
            bit_depth=bit_depth,
            color_space=color_space,
        )

        # 4. Resolve the output format / codec / crf (SaveVideo's logic).
        if isinstance(format, dict):
            format_name = format["format"]
            codec = format.get("codec")
        else:
            format_name = format
            codec = None
        if codec is None:
            codec = {"codec": "auto"}
        codec_name = codec["codec"]
        if format_name == "auto":
            format_name = "webm" if codec_name == "av1" else "mp4"
        encoding = codec.get("encoding") or {}
        crf = encoding.get("crf")

        # 5. Resolve the exact save path and write the file.
        save_path = _resolve_save_path(filename, format_name)

        metadata = None
        if not args.disable_metadata:
            metadata = {}
            if cls.hidden.extra_pnginfo is not None:
                metadata.update(cls.hidden.extra_pnginfo)
            if cls.hidden.prompt is not None:
                metadata["prompt"] = cls.hidden.prompt
            if not metadata:
                metadata = None

        video.save_to(
            save_path,
            format=Types.VideoContainer(format_name),
            codec=Types.VideoCodec(codec_name),
            metadata=metadata,
            crf=crf,
        )

        # 6. Return the saved path, the saved video, and the trimmed images /
        #    audio — the intermediate data actually encoded into the video.
        return io.NodeOutput(save_path, video, images, audio)

    @classmethod
    def validate_inputs(cls, filename: str | None, trim_frames: int | None, fps: float | None, **kwargs) -> bool | str:
        # Linked (unresolved) inputs arrive as None during prompt validation;
        # accept them, as Load Video (Lesca) does.
        if filename is None:
            return True
        if trim_frames is not None and int(trim_frames) < 0:
            return "trim_frames must be >= 0"
        try:
            _resolved_save_path(filename)
        except ValueError as e:
            return str(e)
        return True
