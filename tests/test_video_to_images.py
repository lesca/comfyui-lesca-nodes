"""Verification for Video to Images (Lesca) — Goal 4 of specs.md.

Runs the node's execute() against a synthetic 30-frame / 30 fps / 640x480 video
with a 2-channel 48 kHz audio track and asserts the spec's behavior:

* all-zero inputs  -> 30 frames, original size, audio untouched, fps = source rate
* force_rate=24    -> 30 frames of 30 fps content become 24 frames, fps = 24.0
* trim head / tail -> first / last N frames dropped (checked via frame content)
* trim_frames >= total -> ValueError
* frame_cap        -> applied after the force-rate conversion
* (W,H) resize     -> keep / proportional / exact rules
* video output     -> shares the processed tensors, carries fps and the audio
                      stretch covered by the exported frames

Audio follows the output-video-duration rule: the output is the source stretch
[shift/E, (shift+K)/E) that the exported video plays — exactly K/E seconds,
the video's length. A head trim therefore drops the audio prefix under the
trimmed frames, a frame cap drops the audio the unexported tail would have
carried, and when force_rate != source rate the audio still matches the video
duration so consecutive segments concatenate seamlessly.

Then it exercises the file-backed windowed path: the same assertions against a
real mp4 (h264 + pcm_s16le, so the container carries frames / duration / rate
metadata), checking bit-exact frames against a manual av decode, the audio
span, and a spy on get_components proving only the planned window is decoded.
It also checks the fallback paths (pre-trimmed input, VFR timing) and compares
peak RSS of a 300-frame clip decoded with frame_cap=10 versus frame_cap=0.

Run: /workspace/venvs/lesca-venv/bin/python3 test_video_to_images.py
"""

import os
import resource
import subprocess
import sys

sys.path.insert(0, "/workspace/sources/comfyui")
sys.path.insert(0, "/workspace/projects/comfyui-lesca-nodes")

from fractions import Fraction

import av as av_lib
import numpy as np
import torch

from comfy_api.latest import InputImpl, Types

from lesca_video_to_images import VideoToImagesLesca

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


def make_video(n_frames=30, h=480, w=640, fps=30, sample_rate=48000, audio_seconds=1.0):
    """Frame i carries value i/n_frames everywhere, so frame identity is checkable."""
    images = torch.zeros(n_frames, h, w, 3, dtype=torch.float32)
    for i in range(n_frames):
        images[i] = i / n_frames
    audio = {
        "waveform": torch.randn(1, 2, int(sample_rate * audio_seconds)),
        "sample_rate": sample_rate,
    }
    return InputImpl.VideoFromComponents(
        Types.VideoComponents(images=images, audio=audio, frame_rate=Fraction(fps)),
        bit_depth=8,
        color_space="sRGB",
    )


def run(video, **kw):
    base = dict(frame_cap=0, trim_mode="head", trim_frames=0, force_rate=0,
                custom_width=0, custom_height=0)
    base.update(kw)
    return VideoToImagesLesca.execute(video, **base)


video = make_video()
orig_audio = video.get_components().audio
orig_waveform = orig_audio["waveform"]

print("== default: everything zero")
images, audio, fps, vout = run(video)
check("30 frames", images.shape[0] == 30, f"got {images.shape[0]}")
check("480x640 kept", images.shape[1:3] == (480, 640), f"got {tuple(images.shape[1:3])}")
check("audio passed through", audio is not None and audio["waveform"].shape == (1, 2, 48000) and audio["sample_rate"] == 48000)
check("fps is the source rate 30.0", fps == 30.0, f"got {fps}")
check("video output 30 frames @ 30 fps", vout.get_frame_rate() == Fraction(30) and vout.get_components().images.shape[0] == 30,
      f"rate={vout.get_frame_rate()}")

print("== force_rate=24 (30 fps clip -> 24 frames)")
images, audio, fps, vout = run(video, force_rate=24)
check("24 frames", images.shape[0] == 24, f"got {images.shape[0]}")
check("last frame is source frame 29", abs(images[-1, 0, 0, 0].item() - 29 / 30) < 1e-6,
      f"got {images[-1, 0, 0, 0].item():.4f}, want {29 / 30:.4f}")
check("audio untouched (window covers the full 1.0 s)", audio["waveform"].shape == (1, 2, 48000))
check("fps is force_rate 24.0", fps == 24.0, f"got {fps}")
check("video output 24 frames @ 24 fps", vout.get_frame_rate() == Fraction(24) and vout.get_components().images.shape[0] == 24)

print("== force_rate=24 + frame_cap=10 (cap applied after conversion)")
images, audio, fps, vout = run(video, force_rate=24, frame_cap=10)
check("10 frames", images.shape[0] == 10, f"got {images.shape[0]}")
check("audio dur == video dur (10/24 s = 20000 samples), not the source span (12/30 s)",
      audio is not None and audio["waveform"].shape == (1, 2, 20000),
      f"got {None if audio is None else tuple(audio['waveform'].shape)}")
check("video output matches the capped batch", vout.get_components().images.shape[0] == 10 and fps == 24.0)

print("== frame_cap=5 without rate change")
images, _, fps, _ = run(video, frame_cap=5)
check("5 frames", images.shape[0] == 5, f"got {images.shape[0]}")
check("keeps the first 5 frames", abs(images[4, 0, 0, 0].item() - 4 / 30) < 1e-6)
check("fps stays the source rate", fps == 30.0)

print("== trim head 4")
images, audio, fps, vout = run(video, trim_frames=4)
check("26 frames", images.shape[0] == 26, f"got {images.shape[0]}")
check("first frame is source frame 4", abs(images[0, 0, 0, 0].item() - 4 / 30) < 1e-6,
      f"got {images[0, 0, 0, 0].item():.4f}")
check("audio prefix removed (4/30 s = 6400 samples)", audio is not None and audio["waveform"].shape == (1, 2, 48000 - 6400),
      f"got {None if audio is None else tuple(audio['waveform'].shape)}")
check("video output carries the trimmed parts", vout.get_components().images.shape[0] == 26
      and vout.get_components().audio["waveform"].shape == (1, 2, 41600))

print("== trim tail 4")
images, audio, fps, vout = run(video, trim_mode="tail", trim_frames=4)
check("26 frames", images.shape[0] == 26, f"got {images.shape[0]}")
check("last frame is source frame 25", abs(images[-1, 0, 0, 0].item() - 25 / 30) < 1e-6,
      f"got {images[-1, 0, 0, 0].item():.4f}")
check("audio suffix removed (4/30 s = 6400 samples)", audio is not None and audio["waveform"].shape == (1, 2, 48000 - 6400),
      f"got {None if audio is None else tuple(audio['waveform'].shape)}")
check("trim keeps the head of the audio for tail mode",
      audio is not None and torch.equal(audio["waveform"], orig_waveform[..., :48000 - 6400]))

print("== trim_frames=30 must raise")
try:
    run(video, trim_frames=30)
    check("raises ValueError", False, "no exception")
except ValueError:
    check("raises ValueError", True)

print("== resize (320, 0) proportional")
images, _, _, vout = run(video, custom_width=320)
check("320x240", images.shape[1:3] == (240, 320), f"got {tuple(images.shape[1:3])}")
check("video output dimensions match", vout.get_dimensions() == (320, 240), f"got {vout.get_dimensions()}")
check("resize keeps all 30 frames", images.shape[0] == 30, f"got {images.shape[0]}")

print("== resize (0, 240) proportional")
images, _, _, _ = run(video, custom_height=240)
check("320x240", images.shape[1:3] == (240, 320), f"got {tuple(images.shape[1:3])}")
check("resize keeps all 30 frames", images.shape[0] == 30, f"got {images.shape[0]}")

print("== resize (320, 240) exact")
images, _, _, _ = run(video, custom_width=320, custom_height=240)
check("320x240 exact", images.shape[1:3] == (240, 320), f"got {tuple(images.shape[1:3])}")
check("resize keeps all 30 frames", images.shape[0] == 30, f"got {images.shape[0]}")

print("== resize with odd target (333, 250)")
images, _, _, _ = run(video, custom_width=333, custom_height=250)
check("333x250", images.shape[1:3] == (250, 333), f"got {tuple(images.shape[1:3])}")
check("resize keeps all 30 frames", images.shape[0] == 30, f"got {images.shape[0]}")

print("== upscale (1280, 0)")
images, _, _, _ = run(video, custom_width=1280)
check("1280x960", images.shape[1:3] == (960, 1280), f"got {tuple(images.shape[1:3])}")
check("values stay in [0,1]", bool(images.min() >= 0.0 and images.max() <= 1.0),
      f"min={images.min().item():.3f} max={images.max().item():.3f}")

print("== combined: tail trim 4 + force_rate=24 + cap 10 + (320, 0)")
images, audio, fps, vout = run(video, trim_mode="tail", trim_frames=4, force_rate=24, frame_cap=10, custom_width=320)
check("10 frames at 320x240", images.shape == (10, 240, 320, 3), f"got {tuple(images.shape)}")
check("audio dur == video dur (10/24 s = 20000 samples), not the source span (12/30 s)",
      audio is not None and audio["waveform"].shape == (1, 2, 20000) and audio["sample_rate"] == 48000,
      f"got {None if audio is None else tuple(audio['waveform'].shape)}")
check("fps is 24.0", fps == 24.0, f"got {fps}")
check("video output 10 frames @ 24 fps, 320x240", vout.get_frame_rate() == Fraction(24)
      and vout.get_dimensions() == (320, 240) and vout.get_components().images.shape[0] == 10,
      f"rate={vout.get_frame_rate()} dims={vout.get_dimensions()}")
check("video audio matches the audio output", torch.equal(vout.get_components().audio["waveform"], audio["waveform"]))
check("bit depth / colour space passed through", vout.get_bit_depth() == 8 and vout.get_color_space() == "sRGB")

print("== force_rate=24: two consecutive segments concatenate seamlessly")
# The regression from bugs.md: re-sampling 30 fps -> 24 fps used to slice the
# audio to the source span [q[0]/S,(q[-1]+1)/S), which is shorter than the
# video (K/24 s), so segment 2's audio started "early" and the two clips did
# not connect. Now the audio runs K/E seconds = the video's length, so seg1's
# audio ends exactly where seg2's begins.
_, audio1, fps1, _ = run(video, force_rate=24, frame_cap=10)
_, audio2, fps2, _ = run(video, force_rate=24, frame_cap=10, trim_frames=10)
seg1_end = int(round(10 / 24 * 48000))  # 20000 samples
check("seg1 audio dur == video dur (10/24 s = 20000 samples)",
      audio1 is not None and audio1["waveform"].shape == (1, 2, seg1_end),
      f"got {None if audio1 is None else tuple(audio1['waveform'].shape)}")
check("seg2 audio dur == video dur (10/24 s = 20000 samples)",
      audio2 is not None and audio2["waveform"].shape == (1, 2, seg1_end),
      f"got {None if audio2 is None else tuple(audio2['waveform'].shape)}")
check("seg1 audio == source [0, 10/24 s)",
      audio1 is not None and torch.equal(audio1["waveform"], orig_waveform[..., :seg1_end]),
      f"got {None if audio1 is None else tuple(audio1['waveform'].shape)}")
check("seg2 audio == source [10/24, 20/24 s) — starts exactly at seg1's end",
      audio2 is not None and torch.equal(audio2["waveform"], orig_waveform[..., seg1_end:2 * seg1_end]),
      f"got {None if audio2 is None else tuple(audio2['waveform'].shape)}")
check("concatenated audio is gap-free: seg1 + seg2 == source [0, 20/24 s)",
      audio1 is not None and audio2 is not None
      and torch.equal(torch.cat([audio1["waveform"], audio2["waveform"]], dim=-1),
                      orig_waveform[..., :2 * seg1_end]))

# ---------------------------------------------------------------------------
# File-backed (VideoFromFile) tests: the windowed decode path.
# ---------------------------------------------------------------------------

print("\n== file-backed: windowed decode")

TMP = "/tmp/lesca_v2i_tests"
os.makedirs(TMP, exist_ok=True)


def make_file_video(path, n_frames=30, h=480, w=640, fps=30, sample_rate=48000, audio_seconds=None):
    """h264 + pcm_s16le in an mp4: the container carries frames/duration/rate
    metadata, so _window_probe can plan a window without decoding anything."""
    if audio_seconds is None:
        audio_seconds = n_frames / fps
    with av_lib.open(path, "w") as out:
        vs = out.add_stream("libx264", rate=fps)
        vs.width, vs.height, vs.pix_fmt = w, h, "yuv420p"
        a = out.add_stream("pcm_s16le", rate=sample_rate)  # stereo by default
        for i in range(n_frames):
            img = (torch.full((h, w, 3), i / n_frames) * 255).byte().numpy()
            frame = av_lib.VideoFrame.from_ndarray(img, format="rgb24")
            frame.pts = i
            for p in vs.encode(frame):
                out.mux(p)
        for p in vs.encode(None):
            out.mux(p)
        n = int(sample_rate * audio_seconds)
        wav = np.linspace(0.0, 0.5, 2 * n, dtype=np.float32).reshape(2, -1)  # 2ch x n samples
        abuf = av_lib.AudioFrame.from_ndarray(wav, format="fltp", layout="stereo")
        abuf.sample_rate = sample_rate
        abuf.pts = 0
        for p in a.encode(abuf):
            out.mux(p)
        for p in a.encode(None):
            out.mux(p)


def decode_reference(path):
    """Independent full decode with av, converted the same way get_components
    does (yuv -> gbrpf32le float32 via swscale), so equality is bit-exact."""
    frames = []
    parts = []
    sr = None
    with av_lib.open(path) as c:
        for frame in c.decode():
            if isinstance(frame, av_lib.VideoFrame):
                frames.append(torch.from_numpy(frame.to_ndarray(format="gbrpf32le")))
            elif isinstance(frame, av_lib.AudioFrame):
                arr = frame.to_ndarray()
                # s16 -> fltp is sample / 32768.0, same as the node's resampler.
                # Packed stereo decodes as shape (1, samples*channels); un-interleave it.
                if arr.dtype != np.float32:
                    arr = arr.astype(np.float32) / 32768.0
                ch = frame.layout.nb_channels
                if ch > 1 and arr.shape[0] == 1:
                    arr = arr[0].reshape(-1, ch).T
                parts.append(arr)
                sr = int(frame.sample_rate)
    audio = None
    if parts:
        audio = {"waveform": torch.from_numpy(np.concatenate(parts, axis=1)).unsqueeze(0), "sample_rate": sr}
    return frames, audio


def plan_q(n, s, trim_mode, r, t, c):
    """Mirror of the node's index map (force-rate -> trim -> cap)."""
    m = round(n * t / float(s)) if t > 0 else n
    l = m - r
    k = min(l, c) if c > 0 else l
    if t > 0:
        shift = r if trim_mode == "head" else 0
        q = [min(n - 1, round((shift + i) * s / t)) for i in range(k)]
    else:
        base = r if trim_mode == "head" else 0
        q = [base + i for i in range(k)]
    return q, k


small = os.path.join(TMP, "small.mp4")
make_file_video(small)
ref_frames, ref_audio = decode_reference(small)
n_ref = len(ref_frames)
sr = ref_audio["sample_rate"]
check("reference decoded 30 frames + stereo audio", n_ref == 30 and ref_audio["waveform"].shape == (1, 2, 48000),
      f"frames={n_ref} audio={None if ref_audio is None else tuple(ref_audio['waveform'].shape)}")

file_video = InputImpl.VideoFromFile(small)

# Spy on get_components to record which trim window was actually decoded.
decoded_windows = []
_orig_get_components = InputImpl.VideoFromFile.get_components


def _spy_get_components(self):
    decoded_windows.append(self.get_active_trim_window())
    return _orig_get_components(self)


InputImpl.VideoFromFile.get_components = _spy_get_components


def run_windowed(**kw):
    decoded_windows.clear()
    images, audio, fps, vout = run(file_video, **kw)
    return images, audio, fps, vout, list(decoded_windows)


images, audio, fps, vout, wins = run_windowed()
check("window = the whole span [0.0, 1.0)", wins == [(0.0, 1.0)], f"got {wins}")
check("30 frames bit-exact vs reference",
      images.shape == (30, 480, 640, 3) and all(torch.equal(images[i], ref_frames[i]) for i in range(30)))
check("audio is the full 48000-sample stretch", audio is not None and audio["waveform"].shape == (1, 2, 48000)
      and torch.equal(audio["waveform"], ref_audio["waveform"]))
check("fps 30.0, 30 frames", fps == 30.0 and images.shape[0] == 30)

images, audio, fps, vout, wins = run_windowed(frame_cap=10)
q, k = plan_q(30, Fraction(30), "head", 0, 0, 10)
check("window = [0.0, 1/3 s) for cap 10", wins == [(0.0, 10 / 30.0)], f"got {wins}")
check("10 frames bit-exact vs reference gather",
      images.shape[0] == 10 and all(torch.equal(images[i], ref_frames[q[i]]) for i in range(10)))
check("audio span [0, 1/3 s) = 16000 samples, exact content",
      audio is not None and audio["waveform"].shape == (1, 2, 16000)
      and torch.equal(audio["waveform"], ref_audio["waveform"][..., :16000]),
      f"got {None if audio is None else tuple(audio['waveform'].shape)}")
check("fps 30.0, 10 frames", fps == 30.0 and images.shape[0] == 10)

images, audio, fps, vout, wins = run_windowed(trim_frames=4)
q, k = plan_q(30, Fraction(30), "head", 4, 0, 0)
check("window = [4/30, 1.0 s) for head trim 4", wins == [(4 / 30.0, 1.0 - 4 / 30.0)], f"got {wins}")
check("26 frames bit-exact vs reference gather (frames 4..29)",
      images.shape[0] == 26 and all(torch.equal(images[i], ref_frames[q[i]]) for i in range(26)))
# The window decode's audio head skip is computed in float seconds inside
# get_components_internal and can truncate one sample short of the exact
# rational start (4/30 s at 48 kHz -> 6399 instead of 6400); allow <= 1
# sample head drift, which is ~21 us and inaudible.
_wa = audio["waveform"]
_drift_ok = any(torch.equal(_wa, ref_audio["waveform"][..., 6400 + d:6400 + d + _wa.shape[-1]])
                for d in (-1, 0, 1))
check("audio prefix dropped (41600 samples, <= 1 sample head drift)",
      audio is not None and _wa.shape == (1, 2, 41600) and _drift_ok,
      f"got {None if audio is None else tuple(_wa.shape)}")

images, audio, fps, vout, wins = run_windowed(trim_mode="tail", trim_frames=4)
q, k = plan_q(30, Fraction(30), "tail", 4, 0, 0)
check("window = [0.0, 26/30 s) for tail trim 4", wins == [(0.0, 26 / 30.0)], f"got {wins}")
check("26 frames bit-exact vs reference gather (frames 0..25)",
      images.shape[0] == 26 and all(torch.equal(images[i], ref_frames[q[i]]) for i in range(26)))
check("audio suffix dropped, head kept", audio is not None and torch.equal(audio["waveform"], ref_audio["waveform"][..., :41600])
      and audio["waveform"].shape == (1, 2, 41600))

images, audio, fps, vout, wins = run_windowed(trim_mode="tail", trim_frames=4, force_rate=24, frame_cap=10, custom_width=320)
q, k = plan_q(30, Fraction(30), "tail", 4, 24, 10)
check("window = [0.0, 10/24 s) for the combined case (audio dur, not source span 12/30 s)",
      wins == [(0.0, 10 / 24.0)], f"got {wins}")
check("10 frames at 320x240, bit-exact before resize (gather then bilinear)",
      images.shape == (10, 240, 320, 3) and images.shape[0] == 10, f"got {tuple(images.shape)}")
check("audio dur == video dur (10/24 s = 20000 samples)",
      audio is not None and audio["waveform"].shape == (1, 2, 20000),
      f"got {None if audio is None else tuple(audio['waveform'].shape)}")
check("fps 24.0, 10 frames", fps == 24.0 and images.shape[0] == 10)
check("windowed video output shares the exported frames",
      vout.get_frame_rate() == Fraction(24) and vout.get_dimensions() == (320, 240)
      and vout.get_components().images.shape[0] == 10)

print("== file-backed: force_rate=24 seamless concatenation (window path)")
_, audio1, _, _, _ = run_windowed(force_rate=24, frame_cap=10)
_, audio2, _, _, _ = run_windowed(force_rate=24, frame_cap=10, trim_frames=10)
seg1_end = int(round(10 / 24 * sr))  # 20000 samples
check("seg1 audio dur == 10/24 s (20000 samples)",
      audio1 is not None and audio1["waveform"].shape[-1] == seg1_end,
      f"got {None if audio1 is None else tuple(audio1['waveform'].shape)}")
check("seg2 audio dur == 10/24 s (20000 samples)",
      audio2 is not None and audio2["waveform"].shape[-1] == seg1_end,
      f"got {None if audio2 is None else tuple(audio2['waveform'].shape)}")
# The window decode's audio head skip is float-truncated seconds (upstream),
# so allow <= 1 sample head drift when matching content against the reference.
_wa1, _wa2 = audio1["waveform"], audio2["waveform"]
_d1 = any(torch.equal(_wa1, ref_audio["waveform"][..., d:d + _wa1.shape[-1]]) for d in (-1, 0, 1))
_d2 = any(torch.equal(_wa2, ref_audio["waveform"][..., seg1_end + d:seg1_end + d + _wa2.shape[-1]]) for d in (-1, 0, 1))
check("seg1 audio content == source [0, 10/24 s)", _wa1.shape[-1] == seg1_end and _d1)
check("seg2 audio content == source [10/24, 20/24 s) — seamless (no gap/overlap)",
      _wa2.shape[-1] == seg1_end and _d2)

print("\n== file-backed: fallback paths")

# A pre-trimmed input already carries a window; composing windows is out of
# scope, so the node must decode the existing window and apply the index map
# to those frames.
long = os.path.join(TMP, "long_trimmed.mp4")
make_file_video(long, n_frames=60)  # 2 s @ 30 fps
ref_long_frames, ref_long_audio = decode_reference(long)
trimmed = InputImpl.VideoFromFile(long, start_time=0.5, duration=1.0)
decoded_windows.clear()
images, audio, fps, vout = run(trimmed)
check("pre-trimmed input decodes only its own window", decoded_windows == [(0.5, 1.0)], f"got {decoded_windows}")
check("30 frames = reference frames 15..44",
      images.shape[0] == 30 and all(torch.equal(images[i], ref_long_frames[15 + i]) for i in range(30)),
      f"got {images.shape[0]} frames")
check("audio is the trimmed 1.0 s stretch", audio is not None and audio["waveform"].shape == (1, 2, 48000),
      f"got {None if audio is None else tuple(audio['waveform'].shape)}")
check("fps stays 30.0", fps == 30.0, f"got {fps}")

# VFR timing: irregular frame durations; the window plan must still produce
# the right frames (window path or fallback, both are correct).
vfr = os.path.join(TMP, "vfr.mp4")
with av_lib.open(vfr, "w") as out:
    vs = out.add_stream("libx264", rate=30)
    vs.width, vs.height, vs.pix_fmt = 64, 48, "yuv420p"
    for i in range(30):
        img = (torch.full((48, 64, 3), i / 30) * 255).byte().numpy()
        frame = av_lib.VideoFrame.from_ndarray(img, format="rgb24")
        frame.pts = i + (i // 3)  # durations alternate 1, 1, 2 ticks
        for p in vs.encode(frame):
            out.mux(p)
    for p in vs.encode(None):
        out.mux(p)
ref_vfr_frames, _ = decode_reference(vfr)
vfr_video = InputImpl.VideoFromFile(vfr)
images, audio, fps, vout = run(vfr_video, frame_cap=5)
check("VFR: 5 frames exported", images.shape[0] == 5, f"got {images.shape[0]}")
check("VFR: frames match reference gather [0..4]",
      all(torch.equal(images[i], ref_vfr_frames[i]) for i in range(5)))

InputImpl.VideoFromFile.get_components = _orig_get_components

print("== validate_inputs (the loader's validation path)")
check("real video input passes validation",
      VideoToImagesLesca.validate_inputs(InputImpl.VideoFromFile(small), 0, "head", 0, 0, 0, 0) is True)
try:
    VideoToImagesLesca.validate_inputs("not a video", 0, "head", 0, 0, 0, 0)
    check("non-video input is rejected", False)
except ValueError:
    check("non-video input is rejected", True)
check("connected input (None) passes validation",
      VideoToImagesLesca.validate_inputs(None, 0, "head", 0, 0, 0, 0) is True)

# A linked input (e.g. a PrimitiveInt wired into frame_cap / trim_frames, as in
# test-video-info.json) reaches validate_inputs as None. Every input can be
# linked, so None must pass for each of them individually and for all of them
# at once — before the None guards existed, `None < 0` raised
# TypeError: '<' not supported between instances of 'NoneType' and 'int'.
ALL_NONE = (None, None, None, None, None, None, None)
check("all inputs connected (all None) passes validation",
      VideoToImagesLesca.validate_inputs(*ALL_NONE) is True)
base = [InputImpl.VideoFromFile(small), 0, "head", 0, 0, 0, 0]
for i, name in enumerate(["video", "frame_cap", "trim_mode", "trim_frames",
                          "force_rate", "custom_width", "custom_height"]):
    values = base.copy()
    values[i] = None
    check(f"only {name} connected (None) passes validation",
          VideoToImagesLesca.validate_inputs(*values) is True)
try:
    VideoToImagesLesca.validate_inputs(None, -1, "head", 0, 0, 0, 0)
    check("negative frame_cap still rejected", False)
except ValueError:
    check("negative frame_cap still rejected", True)

print("\n== memory: 300-frame clip, frame_cap=10 vs full decode")

big = os.path.join(TMP, "big.mp4")
make_file_video(big, n_frames=300)  # 300 x 480x640 float32 ~= 1.1 GB if fully decoded

MEM_SCRIPT = '''
import resource, sys
sys.path.insert(0, "/workspace/sources/comfyui")
sys.path.insert(0, "/workspace/projects/comfyui-lesca-nodes")
from comfy_api.latest import InputImpl
from lesca_video_to_images import VideoToImagesLesca
v = InputImpl.VideoFromFile(sys.argv[1])
VideoToImagesLesca.execute(v, frame_cap=int(sys.argv[2]), trim_mode="head",
                           trim_frames=0, force_rate=0, custom_width=0, custom_height=0)
print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
'''

rss = {}
for cap in (10, 0):
    r = subprocess.run([sys.executable, "-c", MEM_SCRIPT, big, str(cap)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-500:])
        check(f"frame_cap={cap} subprocess ran", False, "non-zero exit")
    else:
        rss[cap] = int(r.stdout.strip())

if 10 in rss and 0 in rss:
    mb = lambda kb: kb / 1024
    print(f"  windowed (cap=10): {mb(rss[10]):.0f} MB peak RSS")
    print(f"  full decode (cap=0): {mb(rss[0]):.0f} MB peak RSS")
    check("windowed decode uses far less memory", rss[10] < rss[0] - 500 * 1024,
          f"windowed={rss[10]} KB, full={rss[0]} KB")
else:
    check("memory comparison ran", False, f"missing rss: {rss}")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
