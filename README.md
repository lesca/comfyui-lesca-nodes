[EN](README.md) | [中文](README.zh-CN.md)

# comfyui-lesca-nodes

Custom ComfyUI nodes for Lesca.

Currently provides **Load Video (Lesca)**, **Save Video (Lesca)**,
**Video Info (Lesca)**, **Video to Images (Lesca)**, **Video Combine
(Lesca)**, **Video Add Noise (Lesca)** and **Context Clips (Lesca)**.

## Node Overview

| Node | What it does | Key capabilities |
|------|--------------|------------------|
| [Load Video (Lesca)](#load-video-lesca) | Loads a video from an explicit file path, mirroring the built-in `LoadVideo` output | Any path (relative to the ComfyUI root, or absolute); live in-node preview without running the node; deterministic white placeholder when the file is missing |
| [Save Video (Lesca)](#save-video-lesca) | Merges the built-in `Create Video` + `Save Video` into a single node | User-specified save path (no `_000001_` counter suffix); frame + audio trimming; trimmed `images` / `audio` passed through as outputs |
| [Video Info (Lesca)](#video-info-lesca) | Reports a video's properties as individual outputs, for workflow branching | `fps` / `bit_depth` / `color_space` / `width` / `height` / `frame_count`, plus a one-per-line `video_info` string; output node, runs standalone |
| [Video to Images (Lesca)](#video-to-images-lesca) | Decodes a video into a batch of image frames | force-rate → trim → frame-cap → resize pipeline; windowed decode (no full-file decode); audio span sliced to match the exported frames |
| [Video Combine (Lesca)](#video-combine-lesca) | Merges every same-prefix video file in a directory into one file | Triggered by any upstream output; always re-encodes through the concat filter for a clean audio seam; previous merge outputs self-excluded from the scan; execution cache keyed by the source-file fingerprint |
| [Video Add Noise (Lesca)](#video-add-noise-lesca) | Cuts a head/tail block of frames and adds independent per-frame noise | Per-layer strength schedule (`frame_num_list` / `strenth_list`); optional seed for reproducible per-frame noise; short-video clamp |
| [Context Clips (Lesca)](#context-clips-lesca) | Plans the clip schedule for a target video as pure metadata arithmetic | `target_frame_count` / `total_clips` / `last_clip_frames` from `frame_count` + `fps`; `target_fps = 0` keeps the source rate; output node, runs standalone |

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
* Merge strategy: every merge is a **concat-filter re-encode** into one fresh
  H.264 + AAC file — the stream-copy path was dropped because the concat
  demuxer's `-c copy` duplicates the last AAC frame at each splice whenever a
  clip's audio length is not a whole number of AAC frames, which shows up as an
  audible dropout and repeat at the seam. The re-encode normalises each input's
  video to the first source's resolution and frame rate, and each input's audio
  to the first audio-bearing source's sample rate and channel layout, so it
  also handles incompatible inputs (different codecs, resolutions, frame rates).
  A source without an audio track contributes a silence stream of matching
  duration, and each clip's audio is trimmed to its own video duration so the
  excess AAC padding adds no extra frame and the splice stays clean. The command
  line is built directly and run with `subprocess` — the `ffmpeg` **and**
  `ffprobe` binaries must be on PATH (checked up front).
* The node is an output node: queueing the graph executes it even when nothing
  is wired to its `filename` output.
* Execution caching is keyed by a fingerprint of the matched source files'
  `(basename, mtime)` set: a new or modified clip produces a fresh merged file,
  while an unchanged directory reuses the cached output instead of writing
  another merged file on every run.
* Errors: a missing folder, a non-directory folder, an empty prefix, no video
  files matching the prefix, and a relative path escaping the ComfyUI root
  directory each raise with a descriptive message.

## Video Add Noise (Lesca)

Cuts a head or tail block of frames from a video and adds independent
per-frame noise to them, with a per-layer strength schedule. The `video`
input accepts the `video` output of **Load Video (Lesca)** (a lazy
`VideoFromFile`) or any other video node.

The node is a transform (non-output) node: it only emits the noise-added
`images` batch, which is meant to be fed into a downstream node.

### Inputs

| Field | Type | Meaning |
|-------|------|---------|
| `video` | `Video` | The video to cut frames from, e.g. straight from Load Video (Lesca). |
| `mode` | `Combo` | `head`: cut the first N frames (the default); `tail`: cut the last N. |
| `frame_num_list` | `String` | Frames per layer, e.g. `18 2 1 1` (whitespace- or comma-separated) — 4 layers, 22 frames total. |
| `strenth_list` | `String` | Noise strength per layer, e.g. `0.4 0.3 0.1 0.04` — one value per `frame_num_list` layer. |
| `seed` | `Int` | Optional, default `0`. `0` = fully random noise (different every run). `seed > 0` seeds frame `i` with `seed + i`, so the result is reproducible and every frame's noise still differs. |

### Output

| Field | Type | Meaning |
|-------|------|---------|
| `images` | `Image` | The noise-added frames, in cut order. |

### Behavior

* Frame cut: `total = sum(frame_num_list)` frames are cut from the head or tail.
  File-backed input uses the lazy `as_trimmed` window (the same reference as the
  built-in `VideoFrameSample` head/tail strategies), so only the needed frames
  are decoded; tensor-backed inputs or inputs already carrying a trim window are
  sliced directly instead of re-encoded.
* Per-frame noise: each frame is noised independently, the way the built-in
  `ImageAddNoise` does (`image + strength * randn`, clipped to [0, 1]) — but the
  noise is re-randomized per frame, so two frames at the same strength still
  differ. A frame's alpha channel (transparency, `C == 4`) is copied through
  untouched.
* Per-layer strength: frame `i` takes the strength of the layer whose cumulative
  `frame_num_list` count first exceeds `i` — the default `18 2 1 1` /
  `0.4 0.3 0.1 0.04` is a fade-out noise ramp.
* Layer count must match: `frame_num_list` and `strenth_list` must have the same
  number of layers, otherwise the node raises (the workflow cannot run).
* Short videos clamp: when the video has fewer frames than `total`, the cut
  takes every available frame (`take = min(total, N)`) and the extra frames are
  absorbed by the earlier layers.

## Context Clips (Lesca)

Plans the clip schedule for a target video as pure arithmetic on the video's
container metadata (`frame_count` and `fps`, read the same way Video Info
(Lesca) does — no frame decoding). The `video` input accepts the output of
**Load Video (Lesca)** or any other video node. It is an **output node**
(`is_output_node=True`), so the graph executes it even when nothing is wired to
its outputs.

### Inputs

| Field | Type | Meaning |
|-------|------|---------|
| `video` | `Video` | The video to plan clips for. |
| `target_fps` | `Int` | Default `0` = keep the source frame rate; otherwise re-sample the video to this rate. |
| `clip_frames` | `Int` | Default `124`. Frames per clip; must be greater than `context_frames` (the stride). |
| `context_frames` | `Int` | Default `22`. Context frames kept between clips; the stride is `clip_frames - context_frames`. |
| `init_index` | `Int` | Default `0`. Clips already consumed before this node runs; subtracted from the total. |

### Outputs

| Field | Type | Meaning |
|-------|------|---------|
| `target_frame_count` | `Int` | Total frames of the video at the target rate: `(frame_count / fps) * target_fps`, or `frame_count` when `target_fps` is `0`. |
| `total_clips` | `Int` | `ceil((target_frame_count - clip_frames) / stride) + 1 - init_index`, decremented by one when the final remainder (below 0.33 s) is dropped. |
| `last_clip_frames` | `Int` | `0` when `remainder == 0` or `remainder < 8` (a final clip shorter than 0.33 s is dropped); otherwise `ceil(remainder / 17) * 17 + context_frames`, where `remainder = (target_frame_count - clip_frames) % stride`. |

### Behavior

* The three values follow the reference formulas from `specs.md` (goal 7)
  verbatim, including Python's float `%` semantics. A remainder of `0` means
  the target is exactly covered (empty last clip); a remainder of `8` frames
  or less means the final clip is shorter than 0.33 s and is dropped
  (`last_clip_frames = 0` and one fewer clip); otherwise the last clip is
  rounded up to a multiple of `17` plus the context frames.
* `target_fps = 0` means "keep the source rate" (the raw formula would yield
  `0` frames); the source `fps` is substituted, so `target_frame_count` equals
  `frame_count`.
* The math is kept in float internally and the reported integers use
  `int(round(...))` of the float results.
* After a run the node shows the three fields, one per line, in a read-only
  text box on the node (a frontend-appended widget; a placeholder is shown
  before the first run).

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
  extension registering all seven nodes and Load Video's preview route) and
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
  self-exclusion on re-runs, always a concat-filter re-encode for a clean audio
  seam, source-set execution-cache fingerprint).
* `lesca_video_add_noise.py` — Video Add Noise node (head/tail frame cut via
  the lazy `as_trimmed` window, independent per-frame noise with a per-layer
  strength schedule, optional seed, short-video clamp).
* `lesca_context_clips.py` — Context Clips node (target frame count / total
  clips / last clip size computed from container metadata; `target_fps = 0`
  keeps the source rate; output node with a read-only text box).
* `web/extension.js` — custom frontend: Load Video's **Update** button + live
  preview, and read-only text boxes for the Video Info and Context Clips
  nodes.
* `pyproject.toml` — node metadata for the ComfyUI custom-node manager
  (`[tool.comfyui] web = "web"` serves the frontend).
* `specs.md` — the original requirement spec (goals 1–7, Chinese).
