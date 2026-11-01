"""comfyui-lesca-nodes — custom ComfyUI node package.

Entry point for the ComfyUI custom-node loader. The loader imports this package
and looks for a ``comfy_entrypoint`` callable that returns a
``comfy_api.latest.ComfyExtension`` (the V3 registration mechanism, the same one
used by ComfyUI's own ``comfy_extras/nodes_video.py``).

Seven nodes are registered:

* ``LoadVideoLesca``    — Load Video (Lesca), a fully path-specifiable video loader.
* ``SaveVideoLesca``    — Save Video (Lesca), merges the built-in Create Video and
  Save Video into a single node with a user-specified save path and frame/audio
  trimming.
* ``VideoInfoLesca``    — Video Info (Lesca), reports a video's frame rate, bit
  depth, color space, width, height and total frame count.
* ``VideoToImagesLesca`` — Video to Images (Lesca), extracts video frames as an
  image batch with head/tail trimming, frame-rate re-sampling, frame capping and
  resizing; outputs the images, the trimmed audio, the effective frame rate and
  a reconstructed video sharing the same tensors (the exported frame count is
  available via the Video Info (Lesca) node's ``frame_count``).
* ``VideoCombineLesca`` — Video Combine (Lesca), concatenates every same-prefix
  video file in a directory (ascending filename order, previously merged
  ``<prefix>_merged-*.mp4`` outputs ignored) into one merged video file via the
  ffmpeg CLI (stream copy, with a re-encode fallback for incompatible streams);
  an arbitrary upstream output serves as a trigger for the run.
* ``VideoAddNoiseLesca`` — Video Add Noise (Lesca), cuts a head/tail block of
  frames from a video and adds independent per-frame Gaussian noise with a
  per-layer strength schedule (frame_num_list / strenth_list); an optional seed
  makes the per-frame noise reproducible (seed 0 is fully random).
* ``ContextClipsLesca`` — Context Clips (Lesca), plans the clip schedule for a
  target video: reports the target frame count, total clip count and last-clip
  frame count as pure arithmetic on the video's container metadata (no
  decoding); an output node that also shows the three fields in a read-only
  text box.

The Load Video module owns its node, its preview route, and its extension; the
combined extension below delegates to it so the package exposes one entry point.
The package's own directory is added to ``sys.path`` so that importing the sibling
modules works regardless of how the loader names the imported package.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from comfy_api.latest import ComfyExtension  # noqa: E402  (import after sys.path tweak)

from lesca_load_video import (  # noqa: E402
    LoadVideoLesca,
    LoadVideoLescaExtension,
)
from lesca_save_video import (  # noqa: E402
    SaveVideoLesca,
)
from lesca_video_info import (  # noqa: E402
    VideoInfoLesca,
)
from lesca_video_to_images import (  # noqa: E402
    VideoToImagesLesca,
)
from lesca_video_combine import (  # noqa: E402
    VideoCombineLesca,
)
from lesca_video_add_noise import (  # noqa: E402
    VideoAddNoiseLesca,
)
from lesca_context_clips import (  # noqa: E402
    ContextClipsLesca,
)

# Serve the custom frontend ("Update" button + live preview for Load Video
# (Lesca)). WEB_DIRECTORY is
# the classic custom-node registration mechanism and does NOT depend on
# pydantic-settings being installed (the newer `[tool.comfyui] web` field in
# pyproject.toml is a belt-and-suspenders backup for environments that do have it).
WEB_DIRECTORY = "web"


class LescaExtension(ComfyExtension):
    """Combined entry-point extension registering all Lesca nodes + routes."""

    async def on_load(self) -> None:
        # The Load Video module registers its preview route; Save Video has none.
        await LoadVideoLescaExtension().on_load()

    @classmethod
    async def get_node_list(cls) -> list:
        return [LoadVideoLesca, SaveVideoLesca, VideoInfoLesca, VideoToImagesLesca, VideoCombineLesca, VideoAddNoiseLesca, ContextClipsLesca]


async def comfy_entrypoint() -> LescaExtension:
    return LescaExtension()


__all__ = [
    "LoadVideoLesca",
    "LoadVideoLescaExtension",
    "SaveVideoLesca",
    "VideoInfoLesca",
    "VideoToImagesLesca",
    "VideoCombineLesca",
    "VideoAddNoiseLesca",
    "ContextClipsLesca",
    "LescaExtension",
    "comfy_entrypoint",
]
