"""Load Video (Lesca) — a Load Video node whose path is fully user-specified.

Unlike the built-in ``LoadVideo`` node (a combo dropdown over the input directory),
this node takes a single plain string input:

* ``filename`` – the *path* of the video file to load, e.g. ``input/video/clip.mp4``.
  A relative path resolves against the ComfyUI root directory
  (``folder_paths.base_path``, the directory containing the Config UI source
  files, one level above ``input``), so ``input/clip.mp4`` means
  ``<root>/input/clip.mp4`` and ``output/clip.mp4`` means ``<root>/output/clip.mp4``.
  An absolute path is used as-is.

The frontend resolves string-format tokens into a concrete path before the prompt
is queued. The backend therefore only ever sees a plain, already-resolved string
and just loads the file — no custom parsing is required.

The field may also be *connected* to another STRING-producing node (e.g. the
built-in ``Text`` node, ``PrimitiveString``). During prompt validation the server
has not executed the graph yet, so a connected input reaches ``validate_inputs``
/ ``fingerprint_inputs`` as ``None`` — both methods must treat that as "value
unknown until execute()" and not fail the run. ``execute`` itself always receives
the resolved string.

When the requested file does *not exist* (``FileNotFoundError``), the node does not
error the run. Instead ``execute`` emits a deterministic 16x16, 24-frame, pure-white
placeholder video (see ``_fallback_white_video``), so the graph keeps running and
downstream nodes still receive a valid ``io.Video``. An empty or out-of-bounds path
and a non-video target still raise as before — only a genuinely missing file falls
back.
"""

from __future__ import annotations

import os
from fractions import Fraction

import torch

import folder_paths
from comfy_api.latest import ComfyExtension, io, Input, InputImpl, Types


def _resolve_video_path(filename: str) -> str:
    """Resolve ``filename`` to an absolute path to an existing video.

    A relative path resolves against the ComfyUI root directory
    (``folder_paths.base_path`` — the directory where the Config UI source
    files live, one level above ``input``), so ``input/clip.mp4`` means
    ``<root>/input/clip.mp4``. Absolute paths are used as-is.

    Raises:
        ValueError: if the path is empty, or a relative path escapes the
            base directory.
        FileNotFoundError: if the target file does not exist.
        ValueError: if the target file is not recognised as a video.
    """
    filename = (filename or "").strip()
    if not filename:
        raise ValueError("Filename is empty; specify the video file path to load.")

    if os.path.isabs(filename):
        video_path = os.path.abspath(filename)
    else:
        base_dir = folder_paths.base_path
        video_path = os.path.abspath(os.path.join(base_dir, filename))
        if not folder_paths.is_within_directory(base_dir, video_path):
            raise ValueError(f"Path {filename!r} escapes the base directory {base_dir!r}.")

    if not os.path.isfile(video_path):
        raise FileNotFoundError(f"Video file not found: {video_path}")

    if not folder_paths.filter_files_content_types([os.path.basename(video_path)], ["video"]):
        raise ValueError(f"Not a recognised video file: {video_path}")

    return video_path


def _fallback_white_video(width: int = 16, height: int = 16, num_frames: int = 24, fps: float = 24.0) -> Input.Video:
    """Create a small pure-white placeholder video.

    Used as a graceful fallback when the requested video file does not exist, so
    downstream nodes still receive a valid ``io.Video`` instead of the run
    erroring out. The result is a ``num_frames``-frame video of ``height x
    width`` pixels at ``fps`` frames per second, every frame filled with white
    (RGB 255,255,255 → float 1.0 in the ``[0, 1]`` video-tensor range).
    """
    # Video image tensors are shaped (frames, height, width, channels), float
    # in [0, 1]; see VideoFromComponents.save_to (width=shape[2], height=shape[1])
    # and SaveWEBM (which maps the tensor with ``frame * 255``).
    images = torch.full((num_frames, height, width, 3), 1.0, dtype=torch.float32)
    components = Types.VideoComponents(images=images, frame_rate=Fraction(fps))
    return InputImpl.VideoFromComponents(components, bit_depth=8, color_space="sRGB")


class LoadVideoLesca(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="LoadVideoLesca",
            display_name="Load Video (Lesca)",
            category="video",
            search_aliases=["load video lesca", "load video from path", "video from file path"],
            description="Load a video from a user-specified file path.",
            has_intermediate_output=True,
            inputs=[
                io.String.Input(
                    "filename",
                    display_name="filename",
                    default="",
                    dynamic_prompts=True,
                    tooltip="Path to the video file, e.g. 'input/video/clip.mp4'. A relative path resolves against the ComfyUI root directory (one level above input); an absolute path is used as-is.",
                ),
            ],
            outputs=[
                io.Video.Output(display_name="video"),
            ],
        )

    @classmethod
    def execute(cls, filename: str) -> io.NodeOutput:
        try:
            video_path = _resolve_video_path(filename)
        except FileNotFoundError:
            # The requested file does not exist. Instead of erroring the run,
            # emit a small pure-white placeholder so downstream nodes still
            # receive a valid video.
            return io.NodeOutput(_fallback_white_video())
        source = InputImpl.VideoFromFile(video_path)
        return io.NodeOutput(source)

    @classmethod
    def fingerprint_inputs(cls, filename: str | None) -> float:
        # A connected input reaches this method as None (see validate_inputs):
        # the file cannot be stat'ed before the graph executes. Return a
        # constant; the output cache still invalidates when the linked value
        # changes, because the cache's input signature includes the linked
        # node's own signature (its widget value).
        if filename is None:
            return 0.0
        try:
            # Otherwise: like the built-in node, use the file's modification time
            # instead of hashing, so large videos are not rehashed on every run.
            return os.path.getmtime(_resolve_video_path(filename))
        except FileNotFoundError:
            # A missing file always yields the same deterministic white placeholder,
            # so a constant fingerprint is the correct (and cheap) cache key. Once
            # the file appears, its mtime becomes the fingerprint and the cached
            # placeholder is correctly invalidated.
            return 0.0

    @classmethod
    def validate_inputs(cls, filename: str | None) -> bool | str:
        # While the server validates the prompt the graph has not run yet, so a
        # *connected* input (e.g. a Text/PrimitiveString node wired into
        # filename) arrives here as None — execution.py resolves linked inputs
        # via the execution cache, which is empty at validation time. The
        # concrete string only exists once execute() runs, so a linked value
        # cannot be checked against the file system yet. Accept it; execute()
        # will report a genuine problem when it actually receives the string.
        if filename is None:
            return True
        try:
            _resolve_video_path(filename)
        except FileNotFoundError:
            # A missing file is not a validation error: execute() falls back to a
            # white placeholder video instead of erroring.
            return True
        except ValueError as e:
            return str(e)
        return True


class LoadVideoLescaExtension(ComfyExtension):
    async def on_load(self) -> None:
        """Register the streaming preview endpoint used by the frontend.

        The built-in ``/view`` endpoint only serves the managed input/output/temp
        directories, but this node's whole point is arbitrary user-specified paths
        (including absolute ones). So the frontend "Update" button fetches the
        video from this custom endpoint instead: it reuses the exact same path
        resolution as ``execute`` and streams the file with HTTP Range support
        (aiohttp's ``FileResponse`` handles seeking and infers the video
        content-type from the extension).

        ``on_load`` runs during ``nodes.init_extra_nodes`` — after the
        ``PromptServer`` is constructed and before ``add_routes`` merges the
        route table into the app — so the route is picked up normally (and also
        re-exposed under the ``/api`` prefix).
        """
        from aiohttp import web

        from server import PromptServer

        if PromptServer.instance is None:
            return

        routes = PromptServer.instance.routes

        @routes.get("/lesca/load_video/preview")
        async def preview(request: web.Request) -> web.StreamResponse:
            filename = request.rel_url.query.get("filename", "")
            try:
                video_path = _resolve_video_path(filename)
            except FileNotFoundError as e:
                return web.Response(status=404, text=str(e))
            except ValueError as e:
                return web.Response(status=400, text=str(e))
            return web.FileResponse(video_path)

    @classmethod
    async def get_node_list(cls) -> list[type[io.ComfyNode]]:
        return [LoadVideoLesca]


async def comfy_entrypoint() -> LoadVideoLescaExtension:
    return LoadVideoLescaExtension()
