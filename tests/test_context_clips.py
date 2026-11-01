"""Verification for Context Clips (Lesca) — Goal 7 of specs.md.

Runs the node's execute() against a synthetic tensor video (frame count and
frame rate set via VideoFromComponents, so get_frame_count / get_frame_rate
return exactly the values we want — no file, no decoding) and asserts the
spec's formulas against an independent reference implementation copied from
specs.md:

* target_fps = 0   -> "keep unchanged": target_frame_count == frame_count
* target_fps > 0   -> target_frame_count == (frame_count / fps) * target_fps
* total_clips      -> ceil((target - clip_frames) / stride) + 1 - init_index
                      (minus one when the remainder branch drops the last clip)
* last_clip_frames -> 0 when remainder == 0 or 0 < remainder < 8; otherwise
                      ceil(remainder / 17) * 17 + context (Python float %)
* ui dict          -> the three fields joined one-per-line
* schema           -> is_output_node is True
* validate_inputs  -> linked (None) inputs accepted; clip_frames <=
                      context_frames rejected with a message

Run: /workspace/venvs/lesca-venv/bin/python3 tests/test_context_clips.py
"""

import math
import sys

sys.path.insert(0, "/workspace/sources/comfyui")
sys.path.insert(0, "/workspace/projects/2026_10_18-comfyui-lesca-nodes")

from fractions import Fraction

import torch

from comfy_api.latest import InputImpl, Types

from lesca_context_clips import ContextClipsLesca

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


def make_video(n_frames, fps):
    """A tensor video whose frame count and rate are exactly n_frames / fps."""
    images = torch.zeros(n_frames, 4, 4, 3, dtype=torch.float32)
    return InputImpl.VideoFromComponents(
        Types.VideoComponents(images=images, audio=None, frame_rate=Fraction(fps)),
        bit_depth=8,
        color_space="sRGB",
    )


def run(video, **kw):
    base = dict(target_fps=0, clip_frames=124, context_frames=22, init_index=0)
    base.update(kw)
    return ContextClipsLesca.execute(video, **base)


# The reference formulas from specs.md, goal 7 (kept verbatim).
def reference(frame_count, fps, target_fps, clip_frames, context_frames,
              init_index):
    eff = target_fps if target_fps > 0 else fps
    target_frame_count = (frame_count / fps) * eff
    stride = clip_frames - context_frames
    total_clips = math.ceil((target_frame_count - clip_frames) / stride) + 1 - init_index
    remainder = (target_frame_count - clip_frames) % stride
    if remainder == 0:
        last_clip_frames = 0
    elif remainder < 8:
        last_clip_frames = 0
        total_clips -= 1
    else:
        last_clip_frames = math.ceil(remainder / 17) * 17 + context_frames
    return (int(round(target_frame_count)), int(round(total_clips)),
            int(round(last_clip_frames)))


def expect(video, frame_count, fps, label, **kw):
    """Run the node and compare all three outputs to the reference formula."""
    out = run(video, **kw)
    got = (out[0], out[1], out[2])
    want = reference(frame_count, fps, kw.get("target_fps", 0),
                     kw.get("clip_frames", 124), kw.get("context_frames", 22),
                     kw.get("init_index", 0))
    check(label, got == want, f"got {got}, want {want}")
    return out


# ---------------------------------------------------------------------------
# target_fps = 0 -> keep unchanged (target == frame_count)
# ---------------------------------------------------------------------------
print("== target_fps = 0 keeps the source frame count")
v240 = make_video(240, 24)
out = expect(v240, 240, 24, "240 frames @ 24 fps -> target 240, 3 clips")
check("target_frame_count is 240", out[0] == 240, f"got {out[0]}")
check("total_clips is 3", out[1] == 3, f"got {out[1]}")
check("last_clip_frames is 39", out[2] == 39, f"got {out[2]}")

# ---------------------------------------------------------------------------
# target_fps > 0 -> resample the frame count
# ---------------------------------------------------------------------------
print("== target_fps = 12 resamples 240 frames @ 24 fps to 120")
out = expect(v240, 240, 24, "target 120 -> 1 clip, last clip 124", target_fps=12)
check("last_clip_frames is 124", out[2] == 124, f"got {out[2]}")
check("total_clips is 1", out[1] == 1, f"got {out[1]}")

# ---------------------------------------------------------------------------
# init_index subtracts from the total clip count
# ---------------------------------------------------------------------------
print("== init_index = 2 reduces total clips")
out = expect(v240, 240, 24, "init_index 2 -> 1 clip", init_index=2)
check("total_clips is 1", out[1] == 1, f"got {out[1]}")

# ---------------------------------------------------------------------------
# Python float % semantics on a short target (remainder stays non-negative,
# ceil-based last clip: ceil(28 / 17) * 17 + 22 == 56)
# ---------------------------------------------------------------------------
print("== short target: float % stays non-negative")
v50 = make_video(50, 10)
out = expect(v50, 50, 10, "50 frames @ 10 fps -> 1 clip, last clip 56")
check("last_clip_frames is 56", out[2] == 56, f"got {out[2]}")

# ---------------------------------------------------------------------------
# remainder == 0 -> the target is exactly covered, last clip is empty
# ---------------------------------------------------------------------------
print("== remainder == 0 -> last clip 0, no clip dropped")
v226 = make_video(226, 24)
out = expect(v226, 226, 24, "226 frames @ 24 fps -> 2 clips, last clip 0")
check("total_clips is 2", out[1] == 2, f"got {out[1]}")
check("last_clip_frames is 0", out[2] == 0, f"got {out[2]}")

# ---------------------------------------------------------------------------
# 0 < remainder < 8 -> final clip shorter than 0.33 s is dropped:
# last clip 0 AND total_clips is decremented by one
# ---------------------------------------------------------------------------
print("== 0 < remainder < 8 -> last clip 0, total clipped")
v231 = make_video(231, 24)
out = expect(v231, 231, 24, "231 frames @ 24 fps -> 2 clips, last clip 0")
check("total_clips is 2", out[1] == 2, f"got {out[1]}")
check("last_clip_frames is 0", out[2] == 0, f"got {out[2]}")

# ---------------------------------------------------------------------------
# the ui dict carries the three fields one-per-line
# ---------------------------------------------------------------------------
print("== ui dict content")
out = run(v240)
info = out.ui["context_clips"][0]
lines = info.split("\n")
check("three lines", len(lines) == 3, f"got {len(lines)}: {info!r}")
check("line 1 is target_frame_count", lines[0] == "target_frame_count: 240", f"got {lines[0]!r}")
check("line 2 is total_clips", lines[1] == "total_clips: 3", f"got {lines[1]!r}")
check("line 3 is last_clip_frames", lines[2] == "last_clip_frames: 39", f"got {lines[2]!r}")

# ---------------------------------------------------------------------------
# schema is an output node
# ---------------------------------------------------------------------------
print("== schema")
schema = ContextClipsLesca.define_schema()
check("is_output_node is True", schema.is_output_node is True, f"got {schema.is_output_node}")
check("node_id is ContextClipsLesca", schema.node_id == "ContextClipsLesca", f"got {schema.node_id}")

# ---------------------------------------------------------------------------
# validate_inputs: linked (None) accepted, stride zero rejected
# ---------------------------------------------------------------------------
print("== validate_inputs")
check("all-None (linked) inputs accepted",
      ContextClipsLesca.validate_inputs(None, None, None, None, None) is True)
check("valid ints accepted",
      ContextClipsLesca.validate_inputs(v240, 0, 124, 22, 0) is True)
try:
    ContextClipsLesca.validate_inputs(v240, 0, 22, 22, 0)  # stride 0
    check("clip_frames == context_frames raises", False, "no exception")
except ValueError as e:
    check("clip_frames == context_frames raises ValueError",
          "stride" in str(e), f"msg={e!r}")
try:
    ContextClipsLesca.validate_inputs(v240, 0, 20, 22, 0)  # clip < context
    check("clip_frames < context_frames raises", False, "no exception")
except ValueError as e:
    check("clip_frames < context_frames raises ValueError",
          "greater than" in str(e), f"msg={e!r}")
try:
    ContextClipsLesca.validate_inputs(v240, -1, 124, 22, 0)
    check("negative target_fps raises", False, "no exception")
except ValueError:
    check("negative target_fps raises ValueError", True)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
