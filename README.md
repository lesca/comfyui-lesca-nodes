[EN](README.md) | [中文](README.zh-CN.md)

# comfyui-lesca-nodes

Custom ComfyUI nodes for Lesca.

Currently provides **Load Video (Lesca)**, **Save Video (Lesca)**,
**Video Info (Lesca)**, **Video to Images (Lesca)** and
**Video Combine (Lesca)**.

## Load Video (Lesca)

ComfyUI's built-in video-load node (Config UI) lets you pick a video from a
fixed directory but does not let you specify an explicit path. This node adds a
`Load Video (Lesca)` node that loads a video from a file path you type in —
mirroring the output of ComfyUI's built-in `LoadVideo` node.

### Inputs

| Field      | Type | Meaning |
|------------|------|---------|
| `filename` | `String` | The video file **path** to load, e.g. `input/video/clip.mp4`. A relative path is resolved against the ComfyUI **root** directory (one level above `input`, where the Config UI source files live), so `input/clip.mp4` means `<root>/input/clip.mp4` and `output/clip.mp4` means `<root>/output/clip.mp4`. An absolute path is used as-is. A relative path that escapes the root directory is rejected. |

### Output

| Field   | Type | Meaning |
|---------|------|---------|
| `video` | `Video` | The loaded video — the same output type as ComfyUI's built-in `LoadVideo`. |

### Live preview

Below the `filename` field there is an **Update** button (this node ships a
small custom frontend, `web/extension.js`). Click it — or just edit the
filename — and the video shows up as a preview on the node **without running
it**, exactly like the built-in `Load Video` node's "Choose file to upload"
button.

Because this node accepts arbitrary and absolute paths (which ComfyUI's
built-in `/view` endpoint cannot serve), the preview is streamed from a custom
backend endpoint `/lesca/load_video/preview` that reuses the same path
resolution as the node's `execute` and supports HTTP Range (seeking). When the
preview cannot load (missing file or not a video) an error message is shown on
the node itself. The preview also works when `filename` is connected to a
string-producing node — the frontend reads the *source* node's widget value,
not the (stale) value on this node.

### When the file is missing

If `filename` points to a path that does not exist, the node does **not**
error the run. Instead `execute` emits a deterministic 16×16, 24-frame,
pure-white placeholder video, so the graph keeps running and every downstream
node still receives a valid `Video`. An empty path, a relative path that
escapes the ComfyUI root directory, and a file that is not recognised as a
video all still raise — only a genuinely missing file falls back.

The `filename` field can also be **connected** to any other string-producing
node (e.g. the built-in `Text` node or `PrimitiveString`). 

## Save Video (Lesca)

ComfyUI's built-in workflow needs two nodes to turn a batch of images into a
saved video file: `Create Video` (images → video, with optional audio) and
`Save Video` (write the video to the output directory, naming it with a
`_000001_`-style counter suffix). `Save Video (Lesca)` merges both into a single
node and adds two capabilities neither provides:

* **User-specified save path** — `filename` is the *path* of the video file to
  save, e.g. `output/video/clip.mp4`: a relative path resolves against the
  ComfyUI **root** directory, so `output/video/clip.mp4` means
  `<root>/output/video/clip.mp4`; an absolute path is used as-is. The file is
  written under exactly the given path — **no `_000001_` counter suffix**. An
  extension is appended automatically if the name has none, and the parent
  directory is created if missing.
* **Frame + audio trimming** — `trim_frames` drops the first N frames **and**
  the first `N / fps` seconds of audio (the same arithmetic ComfyUI's
  `TrimAudioDuration` node uses) before the video is encoded. The default `0`
  keeps everything.

The remaining inputs mirror the two source nodes: `fps`, `bit_depth`, and
`color_space` come from `Create Video`, and `format` (which nests `codec` →
`encoding` → `crf`) comes from `Save Video`. `Create Video`'s own `codec` input
is intentionally omitted — the output codec is chosen by `format`.

In addition to the saved `filename` path and the encoded `video`, the node also
passes through the **trimmed** `images` and `audio` (the data actually written
into the video), each with the same type as its corresponding input, so the
trimmed intermediates can be fed into other nodes.

### Inputs

| Field        | Type       | Meaning |
|--------------|------------|---------|
| `images`     | `Image`    | The image batch to create the video from (as `Create Video`). |
| `audio`      | `Audio`    | Optional audio to attach (as `Create Video`). |
| `filename`   | `String`   | The video file **path** to save, e.g. `output/video/clip.mp4` (the default). A relative path resolves against the ComfyUI **root** directory; an absolute path is used as-is. Saved under exactly this path (no counter suffix). The container's extension is appended if the name has none; the parent directory is created if missing. A relative path that escapes the root directory is rejected. |
| `trim_frames`| `Int`      | Drop the first N frames and the first `N / fps` seconds of audio (`round(N / fps * sample_rate)` samples — the same arithmetic as `TrimAudioDuration`). Default `0`. Raises if it is greater than or equal to the total frame count. |
| `fps`        | `Float`    | Output frame rate. Default `30`, range `1`–`120`. |
| `bit_depth`  | `Combo`    | `auto` / `8` / `10` (default `auto`). Auto uses 8-bit for sRGB and 10-bit for HDR. |
| `color_space`| `Combo`    | `sRGB` / `HDR` / `HDR PQ` (default `sRGB`). HDR selects BT.2020/HLG and HDR PQ selects BT.2020/PQ. |
| `format`     | `DynamicCombo` | `auto` / `mp4` / `mkv` / `webm`, each nesting `Codec` (`auto`/`h264`/`av1`; webm only `auto`/`av1`) → `Encoding` (`auto`/`re-encode`) → `CRF`. Auto uses MP4 for H.264 and WebM for AV1. |

As in the built-in `Save Video`, the saved file embeds the prompt and
`extra_pnginfo` metadata (suppressible with ComfyUI's `--disable-metadata`).

### Outputs

| Field    | Type    | Meaning |
|----------|---------|---------|
| `filename` | `String` | Absolute path of the saved video file, e.g. `/…/output/video/clip.mp4`. |
| `video`  | `Video`  | The saved video — the same output type as ComfyUI's built-in `Save Video`. |
| `images` | `Image`  | The trimmed images — the first `trim_frames` frames dropped. Same type as the `images` input. |
| `audio`  | `Audio`  | The trimmed audio — the first `trim_frames / fps` seconds dropped. Same type as the `audio` input (`None` when no audio is provided). |

## Video Info (Lesca)

Reports a video's properties as individual outputs, so a workflow can branch on
them (e.g. to pick a target resolution or re-encode parameters). The `Video`
input accepts the `video` output of **Load Video (Lesca)** — or the video output
of any other video node, including Save Video (Lesca). In addition to the
individual outputs, a `video_info` string output lists every field on its own
line (`fps`, `bit_depth`, `color_space`, `width`, `height`, `frame_count`), for
logging or display.

The node is an output node: queueing the graph runs it even when nothing is
wired to any of its outputs, so it can be used standalone to inspect a video.
A read-only text box embedded in the node displays the `video_info` string
produced by the most recent run (a placeholder is shown before the first run).

### Input

| Field   | Type    | Meaning |
|---------|---------|---------|
| `video` | `Video` | The video to inspect. |

### Outputs

| Field         | Type    | Meaning |
|---------------|---------|---------|
| `video_info`  | `String` | All other output fields joined into one string, one per line: fps, bit_depth, color_space, width, height, frame_count. |
| `fps`         | `Float` | Frame rate of the video, e.g. `24.0`. |
| `bit_depth`   | `String` | Bit depth as a string, e.g. `"8"` or `"10"`. |
| `color_space` | `String` | `sRGB`, `HDR`, `HDR PQ`, or `auto` when the video does not declare one. |
| `width`       | `Int`   | Width in pixels. |
| `height`      | `Int`   | Height in pixels. |
| `frame_count` | `Int`   | Total number of frames in the video. |

The values come from ComfyUI's standard video accessors
(`get_frame_rate` / `get_bit_depth` / `get_color_space` / `get_dimensions` /
`get_frame_count`), so file-based videos read container metadata directly
without decoding frames or loading the whole video into memory, and
tensor-created videos (e.g. from Save Video (Lesca)) report their in-memory
properties.

## Video to Images (Lesca)

Decodes a video into a batch of image frames, with optional frame-rate
re-sampling, head/tail trimming, frame capping and resizing. The `video` input
accepts the `video` output of **Load Video (Lesca)** (a lazy `VideoFromFile`) or
any other video node.

### Inputs

| Field           | Type    | Meaning |
|-----------------|---------|---------|
| `video`         | `Video` | The video to decode. |
| `frame_cap`     | `Int`   | Maximum number of frames to export. Default `0` = all frames. |
| `trim_mode`     | `Combo` | `head`: drop frames from the front; `tail`: drop frames from the back. Default `head`. |
| `trim_frames`   | `Int`   | Number of frames to drop, applied after the rate conversion. Default `0` = no trim. |
| `force_rate`    | `Int`   | Forced output frame rate. Default `0` = keep the source rate. With `force_rate = 24`, 30 fps content is re-sampled to a 24 fps frame sequence. |
| `custom_width`  | `Int`   | Target width in pixels. Default `0` = keep the source width. |
| `custom_height` | `Int`   | Target height in pixels. Default `0` = keep the source height. |

### Outputs

| Field    | Type    | Meaning |
|----------|---------|---------|
| `images` | `Image` | The decoded frames (resized if requested). |
| `audio`  | `Audio` | The audio stretch covered by the exported frames, `[q[0]/S, (q[-1]+1)/S)` with `S` the source frame rate. A head trim drops the audio prefix under the trimmed frames; a frame cap also drops the audio tail the unexported frames would have carried. Sliced at the source sample rate, not the output rate. `None` when the video has no audio track. |
| `fps`    | `Float` | The exported frame rate: `force_rate` when it is set, otherwise the source rate. |
| `video`  | `Video` | A rebuilt video sharing the exported frames and audio stretch. The exported frame count is not an output here — wire this `video` into **Video Info (Lesca)**'s `frame_count`. |

### Processing order and resize rules

The pipeline runs **force-rate → trim → frame-cap → resize**:

* **force-rate** — the content is first re-sampled to the target rate (output
  frame `i` takes source frame `round(i · S / T)`, clamped to the last frame),
  so `force_rate = 24` on 30 fps content exports a 24 fps frame sequence.
* **trim** — `trim_mode = head` drops the first `trim_frames` frames, `tail`
  drops the last. `trim_frames` must be smaller than the frame count after the
  rate conversion, otherwise the node raises.
* **frame-cap** — at most `frame_cap` frames are kept (`0` = no cap), applied
  after the rate conversion and trim.
* **resize** — `custom_width` / `custom_height`: both `0` keeps the source
  dimensions; exactly one `0` scales the other proportionally; both non-zero
  scales to exactly `(W, H)`.

### Windowed decode

For a plain file-backed `video` input, the node reads only the container
metadata (no decoding) and decodes just the window
`[q[0]/S, (q[-1]+1)/S)` that the exported frames span — exporting the first
second of a one-hour video no longer triggers a full-file decode and the
associated memory spike. Inputs where that is not possible (tensor
`VideoFromComponents` videos, videos already carrying a non-trivial trim
window, unknown frame rate or frame count in the metadata, or a decode that
comes back short for pts quirks) fall back to a full decode that applies the
same index map to the loaded tensors; both paths produce identical frames.

## Video Combine (Lesca)

Merges every same-prefix video file in a directory into one file, driven by a
trigger from any upstream output — e.g. wire the `filename` output of Save
Video (Lesca) into this node and every newly saved clip triggers a merge of the
directory it was saved into.

### Inputs

| Field    | Type | Meaning |
|----------|------|---------|
| `any`    | `Any` (optional) | A trigger: the output of **any** upstream type starts the node's run (e.g. `Save Video (Lesca)`'s `filename`, which fires the merge right after a new clip is saved). Optional — the node also runs standalone. |
| `folder` | `String` | The directory to scan, e.g. `output/video` (the default). A relative path resolves against the ComfyUI **root** directory; an absolute path is used as-is. May be connected to a string-producing node. |
| `prefix` | `String` | The filename prefix of the clips to merge, e.g. `dance`. May be connected to a string-producing node. |

### Output

| Field      | Type     | Meaning |
|------------|----------|---------|
| `filename` | `String` | Absolute path of the merged video file, e.g. `/…/output/video/dance_merged-2026_10_18_16_33_44.mp4`. |

### Behavior

* The node walks `folder`, keeps every **video** file whose name starts with
  `prefix` (non-video files such as `.txt` are skipped by ComfyUI's
  video-content-type filter), sorts them by **ascending filename**, and
  concatenates them into one `<prefix>_merged-YYYY_MM_DD_HH_MM_SS.mp4` in the
  same directory.
* Previously produced `<prefix>_merged-…mp4` outputs are **excluded from the
  scan**, so running the node on the same directory again merges the original
  clips, never an already-merged file.
* Merge strategy: ffmpeg-python's concat demuxer with `-c copy` splices the
  streams without decoding (lossless and fast, for clips from the same
  pipeline). When stream copy fails because the inputs are incompatible
  (different codecs, resolutions or frame rates), the node falls back
  automatically to a concat-filter re-encode into one H.264 stream (audio
  dropped). Both command lines are built with the ffmpeg-python package —
  which still requires the ffmpeg binary on PATH.
* The node is an output node: queueing the graph executes it even when nothing
  is wired to its `filename` output.
* Execution caching is keyed by a fingerprint of the matched source files'
  `(basename, mtime)` set: a new or modified clip produces a fresh merged file,
  while an unchanged directory reuses the cached output instead of writing
  another merged file on every run.
* Errors: a missing folder, a non-directory folder, an empty prefix, no video
  files matching the prefix, and a relative path escaping the ComfyUI root
  directory each raise with a descriptive message.

## Installation

Drop this folder into ComfyUI's custom-node directory:

```
ComfyUI/custom_nodes/comfyui-lesca-nodes/
```

Then restart ComfyUI. The nodes register through ComfyUI's V3
`comfy_entrypoint` mechanism (the same one used by
`comfy_extras/nodes_video.py`).

## Files

* `__init__.py` — package entry point; exposes `comfy_entrypoint` (a combined
  extension registering all five nodes and Load Video's preview route) and
  `WEB_DIRECTORY = "web"`.
* `lesca_load_video.py` — Load Video node, path-resolution logic, the
  `/lesca/load_video/preview` streaming endpoint, and the missing-file white
  placeholder fallback.
* `lesca_save_video.py` — Save Video node (Create Video + Save Video merged,
  with frame/audio trimming and a user-specified save path).
* `lesca_video_info.py` — Video Info node (video_info string / fps / bit depth /
  color space / width / height / frame count).
* `lesca_video_to_images.py` — Video to Images node (windowed decode,
  force-rate → trim → frame-cap → resize pipeline, audio span slicing).
* `lesca_video_combine.py` — Video Combine node (prefix-scan in ascending
  filename order, `<prefix>_merged-YYYY_MM_DD_HH_MM_SS.mp4` output with
  self-exclusion on re-runs, ffmpeg-python stream-copy merge with a re-encode
  fallback, source-set execution-cache fingerprint).
* `web/extension.js` — custom frontend: Load Video's **Update** button + live
  preview.
* `pyproject.toml` — node metadata for the ComfyUI custom-node manager
  (`[tool.comfyui] web = "web"` serves the frontend).
* `specs.md` — the original requirement spec (goals 1–5, Chinese).
