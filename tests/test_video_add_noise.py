"""Verification for Video Add Noise (Lesca) — Goal 6 of specs.md.

Runs the node's execute() against a synthetic 30-frame / 30 fps / 640x480 tensor
video (frame i carries value i/30 everywhere, so frame identity is checkable)
and asserts the spec's behavior:

* default          -> 22 frames (sum of "18 2 1 1"), head cut (the default), per-layer noise
* tail / head cut  -> last / first frames selected (checked with strength 0,
                      which leaves the source pixels untouched)
* per-layer strength-> high-strength frames deviate from the source, strength-0
                      frames are bit-identical to it (verifies the layer walk)
* per-frame noise  -> adjacent frames differ even at equal strength (a fresh
                      generator per frame), and every frame differs from source
* seed             -> same seed reproduces bit-exactly, a different seed
                      changes the noise, seed 0 is fully random (two runs differ)
* layer mismatch   -> frame_num_list / strenth_list of different length raise
                      ValueError (workflow cannot run); validate_inputs reports it
* short video      -> a 5-frame video with a 22-frame schedule clamps to 5
                      frames and does not raise
* alpha channel    -> a 4-channel video keeps its alpha untouched by the noise

Then it exercises the file-backed windowed path: a real mp4 (h264 + pcm_s16le,
so the container carries frames/rate metadata) with a spy on get_components
proving only the planned head/tail window is decoded, bit-exact frames against
an independent av decode at strength 0, and modified frames at non-zero strength.

Run: /workspace/venvs/lesca-venv/bin/python3 test_video_add_noise.py
"""

import os
import sys

sys.path.insert(0, "/workspace/sources/comfyui")
sys.path.insert(0, "/workspace/projects/2026_10_18-comfyui-lesca-nodes")

from fractions import Fraction

import av as av_lib
import numpy as np
import torch

from comfy_api.latest import InputImpl, Types

from lesca_video_add_noise import (
    VideoAddNoiseLesca,
    _frame_layers,
    _parse_float_list,
    _parse_int_list,
)

PASS = 0
FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}  {detail}")


def make_video(n_frames=30, h=480, w=640, fps=30, channels=3):
    """Frame i carries value i/n_frames everywhere, so frame identity is checkable."""
    images = torch.zeros(n_frames, h, w, channels, dtype=torch.float32)
    for i in range(n_frames):
        images[i] = i / n_frames
    return InputImpl.VideoFromComponents(
        Types.VideoComponents(images=images, audio=None, frame_rate=Fraction(fps)),
        bit_depth=8,
        color_space="sRGB",
    )


def run(video, **kw):
    base = dict(mode="head", frame_num_list="18 2 1 1",
                strenth_list="0.4 0.3 0.1 0.04", seed=0)
    base.update(kw)
    return VideoAddNoiseLesca.execute(video, **base)[0]  # unwrap NodeOutput -> images tensor


src = make_video()
src_frames = src.get_components().images
source = src_frames.clone()

print("== default: 22 frames, head cut, non-zero noise")
images = run(src)
check("22 frames", images.shape[0] == 22, f"got {images.shape[0]}")
check("size kept 480x640x3", images.shape[1:4] == (480, 640, 3), f"got {tuple(images.shape[1:4])}")
check("noisy frames differ from the source", bool((images - source[:22]).abs().max() > 0))

print("== tail selection (strength 0 leaves source untouched)")
images = run(src, mode="tail", strenth_list="0 0 0 0")
check("tail frame 0 == source frame 8", torch.equal(images[0], source[8]),
      f"got {images[0, 0, 0, 0].item():.4f}, want {8 / 30:.4f}")
check("tail frame 21 == source frame 29", torch.equal(images[21], source[29]),
      f"got {images[21, 0, 0, 0].item():.4f}, want {29 / 30:.4f}")
check("all 22 tail frames bit-exact", all(torch.equal(images[i], source[8 + i]) for i in range(22)))

print("== head selection (strength 0)")
images = run(src, mode="head", strenth_list="0 0 0 0")
check("head frame 0 == source frame 0", torch.equal(images[0], source[0]))
check("head frame 21 == source frame 21", torch.equal(images[21], source[21]),
      f"got {images[21, 0, 0, 0].item():.4f}, want {21 / 30:.4f}")
check("all 22 head frames bit-exact", all(torch.equal(images[i], source[i]) for i in range(22)))

print("== per-layer strength: only layer 0 (18 frames) noised")
images = run(src, mode="head", strenth_list="0.4 0 0 0")
check("layer-0 frames (0..17) deviate from source",
      all(bool((images[i] - source[i]).abs().max() > 0) for i in range(18)))
check("layers 1-3 frames (18..21) bit-exact (strength 0)",
      all(torch.equal(images[i], source[i]) for i in range(18, 22)),
      f"dev={max((images[i] - source[i]).abs().max().item() for i in range(18, 22))}")

print("== per-frame noise: adjacent frames differ at equal strength")
images = run(src, mode="head", strenth_list="0.4 0.4 0.4 0.4")
check("every frame differs from its source",
      all(bool((images[i] - source[i]).abs().max() > 0) for i in range(22)))
check("adjacent frames differ from each other",
      all(not torch.equal(images[i], images[i + 1]) for i in range(21)))

print("== seed: reproducible per-frame noise")
a = run(src, seed=42)
b = run(src, seed=42)
c = run(src, seed=43)
check("same seed reproduces bit-exactly", torch.equal(a, b))
check("different seed changes the noise", not torch.equal(a, c))
r1 = run(src, seed=0)
r2 = run(src, seed=0)
check("seed 0 is fully random (two runs differ)", not torch.equal(r1, r2))
check("seed 0 still noises every frame", all(bool((r1[i] - source[i]).abs().max() > 0) for i in range(22)))

print("== layer mismatch -> ValueError (workflow cannot run)")
try:
    run(src, strenth_list="0.4 0.3")  # 2 layers vs 4
    check("mismatch raises", False, "no exception")
except ValueError:
    check("mismatch raises ValueError", True)

print("== validate_inputs reports the mismatch")
check("validate accepts matching lists",
      VideoAddNoiseLesca.validate_inputs(None, "tail", "18 2 1 1", "0.4 0.3 0.1 0.04", 0) is True)
msg = VideoAddNoiseLesca.validate_inputs(None, "tail", "18 2 1 1", "0.4 0.3", 0)
check("validate rejects mismatch with a message", isinstance(msg, str) and "layers" in msg,
      f"got {msg!r}")
check("validate accepts linked (None) inputs",
      VideoAddNoiseLesca.validate_inputs(None, None, None, None, None) is True)

print("== short video clamps to available frames")
short = make_video(n_frames=5)
images = run(short)  # default 22-frame schedule on a 5-frame video
check("5-frame video yields 5 frames", images.shape[0] == 5, f"got {images.shape[0]}")
short0 = make_video(n_frames=5)
images = run(short0, strenth_list="0 0 0 0")  # strength 0 -> frames stay the source tail
check("short-video frame == source frame 4", torch.equal(images[4], short0.get_components().images[4]),
      f"got {images[4, 0, 0, 0].item():.4f}, want {4 / 5:.4f}")

print("== alpha channel is preserved")
alpha_src = make_video(n_frames=30, channels=4)
alpha_in = alpha_src.get_components().images.clone()
alpha_in[..., 3] = 0.5  # a distinct alpha value
alpha_src = InputImpl.VideoFromComponents(
    Types.VideoComponents(images=alpha_in, audio=None, frame_rate=Fraction(30)),
    bit_depth=8, color_space="sRGB")
images = run(alpha_src, mode="head", strenth_list="0.4 0.4 0.4 0.4")  # 22 head frames
check("alpha channel untouched", torch.equal(images[..., 3], alpha_in[:22, ..., 3]))
check("rgb channels noised", bool((images[..., :3] - alpha_in[:22, ..., :3]).abs().max() > 0))

# ---------------------------------------------------------------------------
# File-backed (VideoFromFile) tests: the windowed decode path.
# ---------------------------------------------------------------------------
print("\n== file-backed: windowed decode")

TMP = "/tmp/lesca_van_tests"
os.makedirs(TMP, exist_ok=True)


def make_file_video(path, n_frames=30, h=480, w=640, fps=30):
    """h264 mp4 whose container carries frames/rate metadata for the window probe."""
    with av_lib.open(path, "w") as out:
        vs = out.add_stream("libx264", rate=fps)
        vs.width, vs.height, vs.pix_fmt = w, h, "yuv420p"
        for i in range(n_frames):
            img = (torch.full((h, w, 3), i / n_frames) * 255).byte().numpy()
            frame = av_lib.VideoFrame.from_ndarray(img, format="rgb24")
            frame.pts = i
            for p in vs.encode(frame):
                out.mux(p)
        for p in vs.encode(None):
            out.mux(p)


def decode_reference(path):
    frames = []
    with av_lib.open(path) as c:
        for frame in c.decode():
            if isinstance(frame, av_lib.VideoFrame):
                frames.append(torch.from_numpy(frame.to_ndarray(format="gbrpf32le")))
    return frames


small = os.path.join(TMP, "small.mp4")
make_file_video(small)
ref = decode_reference(small)
file_video = InputImpl.VideoFromFile(small)

decoded_windows = []
_orig_get_components = InputImpl.VideoFromFile.get_components


def _spy_get_components(self):
    decoded_windows.append(self.get_active_trim_window())
    return _orig_get_components(self)


InputImpl.VideoFromFile.get_components = _spy_get_components


def run_windowed(**kw):
    decoded_windows.clear()
    images = run(file_video, **kw)
    return images, list(decoded_windows)


images, wins = run_windowed(mode="tail", strenth_list="0 0 0 0")  # tail 22 @ 30 fps -> window [8/30, 30/30)
check("tail window = [8/30 s, 1.0 s)", wins == [(8 / 30.0, 22 / 30.0)], f"got {wins}")
check("22 tail frames bit-exact vs reference",
      images.shape[0] == 22 and all(torch.equal(images[i], ref[8 + i]) for i in range(22)),
      f"got {images.shape[0]} frames")

images, wins = run_windowed(mode="head", strenth_list="0 0 0 0")
check("head window = [0.0, 22/30 s)", wins == [(0.0, 22 / 30.0)], f"got {wins}")
check("22 head frames bit-exact vs reference",
      images.shape[0] == 22 and all(torch.equal(images[i], ref[i]) for i in range(22)),
      f"got {images.shape[0]} frames")

images, wins = run_windowed(mode="head")  # non-zero noise
check("non-zero strength modifies the decoded frames",
      images.shape[0] == 22 and all(bool((images[i] - ref[i]).abs().max() > 0) for i in range(22)),
      f"window={wins}")

InputImpl.VideoFromFile.get_components = _orig_get_components

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
