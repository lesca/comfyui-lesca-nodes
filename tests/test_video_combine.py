"""Verification for Video Combine (Lesca) — Goal 5 of specs.md.

Walks ``folder/prefix`` (the ``prefix`` subdirectory under ``folder``) for every
video file whose name starts with ``prefix``, and asserts the spec's behavior:

* source scan   -> every file in folder/prefix starting with the prefix,
                    ascending clip-suffix-number (natural) order; previously
                    merged <prefix>_merged-*.mp4 outputs, non-video files and
                    other prefixes are ignored
* merge         -> <prefix>_merged-YYYY_MM_DD_HH_MM_SS.mp4 written back into
                    folder/prefix; total frame count equals the sum of the
                    inputs; the audio splice is clean (no last-AAC-frame
                    duplication: the merged decoded sample count is never longer
                    than the inputs' sum, and each clip's audio is trimmed to its
                    video duration so the excess AAC padding adds no extra frame)
* re-run        -> the freshly merged file is excluded from the next scan, so a
                    second run produces another same-length merged file instead
                    of merging the merge
* re-encode     -> every merge re-encodes (h264 + aac); incompatible inputs
                    (different resolutions) and mixed-audio inputs (one clip
                    without a track) are handled by normalising streams /
                    padding silence, not by a copy-then-fallback path
* single source -> a folder/prefix with one clip still merges (a renamed copy)
* errors        -> missing folder / missing folder/prefix / not a directory /
                    empty prefix / no matching files / relative path escaping the
                    base directory
* validation    -> validate_inputs accepts linked (None) inputs and reports
                    concrete problems as strings
* fingerprint   -> unchanged directory yields a stable fingerprint; a new clip
                    changes it; a resolved video changes it; linked inputs yield
                    the constant 0.0

Run: /workspace/venvs/lesca-venv/bin/python3 test_video_combine.py
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, "/workspace/sources/comfyui")
sys.path.insert(0, "/workspace/projects/2026_10_18-comfyui-lesca-nodes")

from comfy_api.input_impl import VideoFromFile

from lesca_video_combine import (
    VideoCombineLesca,
    _extract_audio,
    _list_sources,
    _merged_output_regex,
    _resolved_folder,
    _scan_directory,
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


def make_clip(path, frames, color, size="128x96", fps=30, audio=True):
    """A ``frames``-long h264 clip whose frames are solid ``color``.

    With ``audio`` the clip also carries a 440 Hz sine aac track; without it
    the clip is video-only.
    """
    command = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"color=c={color}:s={size}:r={fps}:d={frames / fps}",
    ]
    if audio:
        command += [
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={frames / fps}",
            "-c:a", "aac", "-shortest",
        ]
    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p", path]
    subprocess.run(command, check=True, capture_output=True)


def frame_count(path):
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0",
            "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", path,
        ],
        capture_output=True, text=True, check=True,
    )
    return int(out.stdout.strip().splitlines()[0])


def audio_samples(path):
    """Total decoded PCM audio samples in ``path`` (0 when it has no audio).

    Decodes to mono s16le so the sample count is simply half the byte count;
    this is the quantity the clean-splice invariant is asserted on.
    """
    out = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1",
            "-f", "s16le", "-acodec", "pcm_s16le", "-",
        ],
        capture_output=True, check=True,
    )
    return len(out.stdout) // 2


def probe_duration(path):
    """Container-level duration of ``path`` in seconds."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def run(folder, prefix, any=None, video=None, skip_frames=0):
    """execute and unwrap the NodeOutput to its filename argument."""
    return VideoCombineLesca.execute(folder=folder, prefix=prefix, any=any,
                                     video=video, skip_frames=skip_frames)[0]


def make_scan(base, prefix, clips):
    """Create ``base/prefix`` and populate it with ``clips`` (name -> (frames,
    color)); return the scan directory ``base/prefix``."""
    scan = os.path.join(base, prefix)
    os.makedirs(scan, exist_ok=True)
    for name, (frames, color) in clips.items():
        make_clip(os.path.join(scan, name), frames, color)
    return scan


def main():
    workdir = tempfile.mkdtemp(prefix="lesca-combine-")
    try:
        # --- fixtures -------------------------------------------------------
        dance_dir = make_scan(
            workdir, "dance",
            {
                "dance_clip0.mp4": (10, "red"),
                "dance_clip1.mp4": (15, "green"),
                "dance_clip2.mp4": (20, "blue"),
                "dance_clip10.mp4": (12, "yellow"),
            },
        )
        make_clip(os.path.join(dance_dir, "other_clip0.mp4"), 5, "red")
        make_clip(os.path.join(dance_dir, "dance_merged-2020_01_02_03_04_05.mp4"), 7, "red")
        with open(os.path.join(dance_dir, "dance_notes.txt"), "w") as f:
            f.write("not a video\n")
        expected_total = 10 + 15 + 20 + 12

        # --- source scan ----------------------------------------------------
        print("source scan")
        sources = _list_sources(dance_dir, "dance")
        check(
            "only prefix-matching videos, natural clip-suffix order",
            [os.path.basename(s) for s in sources] == ["dance_clip0.mp4", "dance_clip1.mp4", "dance_clip2.mp4", "dance_clip10.mp4"],
            str([os.path.basename(s) for s in sources]),
        )
        check("old merged output ignored", all("_merged-" not in os.path.basename(s) for s in sources))
        check("non-video file ignored", all(not s.endswith(".txt") for s in sources))
        check("other prefix ignored", all(os.path.basename(s).startswith("dance_") for s in sources))

        # --- merge ----------------------------------------------------------
        print("merge")
        out = run(workdir, "dance")
        check("returns an absolute path", os.path.isabs(out), out)
        check("merged file exists", os.path.isfile(out), out)
        check(
            "output name is <prefix>_merged-YYYY_MM_DD_HH_MM_SS.mp4",
            re.fullmatch(r"dance_merged-\d{4}_\d{2}_\d{2}_\d{2}_\d{2}_\d{2}\.mp4", os.path.basename(out)),
            os.path.basename(out),
        )
        check("merged into folder/prefix", os.path.dirname(out) == dance_dir, out)
        check("total frame count = sum of inputs", frame_count(out) == expected_total,
              f"{frame_count(out)} != {expected_total}")
        check("audio stream preserved",
              subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                              "-show_entries", "stream=codec_name", "-of", "csv=p=0", out],
                             capture_output=True, text=True).stdout.strip() != "")
        # Seam cleanliness. The old stream-copy bug duplicated the last AAC
        # frame at the splice, so the merged audio ran LONGER than the inputs'
        # sum by a full 1024-sample frame. The re-encode instead trims each
        # clip's audio to its own video duration (AAC pads audio up to whole
        # frames, so that trim removes a little padding per clip), which makes
        # the merged audio at most equal to -- usually slightly shorter than --
        # the raw inputs' sum. So: never longer than the sum + one frame (no
        # duplication), and never shorter than the sum minus one frame per
        # source (not over-trimmed).
        seam_expected = sum(audio_samples(s) for s in sources)
        seam_got = audio_samples(out)
        check("clean audio seam (no last-frame duplication)",
              seam_expected - len(sources) * 1024 <= seam_got <= seam_expected + 1024,
              f"{seam_got} vs {seam_expected} (diff {seam_got - seam_expected})")

        # --- re-run ---------------------------------------------------------
        print("re-run")
        sources_after = _list_sources(dance_dir, "dance")
        check("newly merged file is excluded from the next scan",
              len(sources_after) == 4 and not any(out in s for s in sources_after),
              str([os.path.basename(s) for s in sources_after]))
        out2 = run(workdir, "dance")
        check("second run writes a fresh merged file", out2 != out, out2)
        check("second merge has the same frame count (no merge-of-merge)",
              frame_count(out2) == expected_total, f"{frame_count(out2)} != {expected_total}")

        # --- incompatible streams (different resolutions) --------------------
        print("incompatible streams")
        mixeddir = tempfile.mkdtemp(prefix="lesca-combine-mixed-", dir=workdir)
        mixscan = os.path.join(mixeddir, "mix")
        os.makedirs(mixscan, exist_ok=True)
        make_clip(os.path.join(mixscan, "mix_a.mp4"), 8, "red", size="128x96")
        make_clip(os.path.join(mixscan, "mix_b.mp4"), 6, "blue", size="320x240")
        out3 = run(mixeddir, "mix")
        check("incompatible clips merged (re-encode normalises streams)", os.path.isfile(out3), out3)
        check("incompatible merge has the total frame count", frame_count(out3) == 14,
              f"{frame_count(out3)} != 14")

        # --- mixed audio: one clip has no track -------------------------------
        print("mixed audio")
        mixedaudir = tempfile.mkdtemp(prefix="lesca-combine-audio-", dir=workdir)
        audscan = os.path.join(mixedaudir, "aud")
        os.makedirs(audscan, exist_ok=True)
        make_clip(os.path.join(audscan, "aud_a.mp4"), 8, "red", audio=True)
        make_clip(os.path.join(audscan, "aud_b.mp4"), 6, "blue", audio=False)
        out5 = run(mixedaudir, "aud")
        check("mixed-audio clips merge", os.path.isfile(out5), out5)
        check("mixed-audio output keeps an audio track",
              subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                              "-show_entries", "stream=codec_name", "-of", "csv=p=0", out5],
                             capture_output=True, text=True).stdout.strip() != "")
        check("mixed-audio output has the total frame count", frame_count(out5) == 14,
              f"{frame_count(out5)} != 14")

        # --- video only: no clip has a track -----------------------------------
        print("video only")
        vonlydir = tempfile.mkdtemp(prefix="lesca-combine-video-", dir=workdir)
        vidscan = os.path.join(vonlydir, "vid")
        os.makedirs(vidscan, exist_ok=True)
        make_clip(os.path.join(vidscan, "vid_a.mp4"), 8, "red", audio=False)
        make_clip(os.path.join(vidscan, "vid_b.mp4"), 6, "blue", audio=False)
        out6 = run(vonlydir, "vid")
        check("video-only clips merge", os.path.isfile(out6), out6)
        check("video-only output has no audio track",
              subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                              "-show_entries", "stream=codec_name", "-of", "csv=p=0", out6],
                             capture_output=True, text=True).stdout.strip() == "")
        check("video-only output has the total frame count", frame_count(out6) == 14,
              f"{frame_count(out6)} != 14")

        # --- external audio: provided video replaces the clips' audio --------
        print("external audio")
        extdir = tempfile.mkdtemp(prefix="lesca-combine-ext-", dir=workdir)
        eclipscan = os.path.join(extdir, "eclip")
        os.makedirs(eclipscan, exist_ok=True)
        for name, (frames, color) in {
            "eclip0.mp4": (10, "red"),
            "eclip1.mp4": (15, "green"),
            "eclip2.mp4": (20, "blue"),
            "eclip10.mp4": (12, "yellow"),
        }.items():
            make_clip(os.path.join(eclipscan, name), frames, color)
        # External video: 880 Hz, 48000 Hz mono, 10 seconds -- longer than the
        # merged clips (~1.9 s) so the replacement and the trim-to-video-length
        # are both observable at once. It lives in the base dir, not in eclip.
        ext_video = os.path.join(extdir, "ext_source.mp4")
        subprocess.run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=purple:s=128x96:r=30:d=10",
            "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=10",
            "-ac", "1", "-c:a", "aac", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-shortest", ext_video,
        ], check=True, capture_output=True)
        out7 = run(extdir, "eclip", video=VideoFromFile(ext_video), skip_frames=0)
        check("external-audio merge writes a file", os.path.isfile(out7), out7)
        check("external-audio merge keeps the clips' frame count",
              frame_count(out7) == 57, f"{frame_count(out7)} != 57")
        ar = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                             "-show_entries", "stream=sample_rate,channels", "-of", "csv=p=0", out7],
                            capture_output=True, text=True).stdout.strip().splitlines()[0]
        sr, ch = ar.split(",")
        check("merged audio is the external video's 48000 Hz mono (not the clips' 44100 stereo)",
              sr == "48000" and ch == "1", ar)
        vid_dur = probe_duration(out7)
        got_samples = audio_samples(out7)
        check("external audio trimmed to the merged video's duration (not the source's 10 s)",
              vid_dur * 48000 - 4096 <= got_samples <= vid_dur * 48000 + 4096,
              f"{got_samples} samples vs video {vid_dur:.2f}s (~{vid_dur*48000:.0f} expected)")

        # --- skip_frames: drop the first N frames before stripping audio -----
        print("skip_frames")
        skip_src = os.path.join(extdir, "skip_source.mp4")
        subprocess.run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=orange:s=128x96:r=30:d=2",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2",
            "-ac", "2", "-c:a", "aac", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-shortest", skip_src,
        ], check=True, capture_output=True)
        sv = VideoFromFile(skip_src)
        with tempfile.TemporaryDirectory(prefix="lesca-combine-skip-") as wdir:
            dur0 = probe_duration(_extract_audio(sv, 0, wdir))
            dur30 = probe_duration(_extract_audio(sv, 30, wdir))  # 30 frames @ 30 fps = 1 s
        check("skip_frames=0 keeps the full audio (~2 s)", dur0 > 1.5, f"{dur0:.2f}s")
        check("skip_frames=30 (1 s) drops the first second (~1 s left)", dur30 < 1.2, f"{dur30:.2f}s")

        # --- no-audio video: clear error --------------------------------------
        print("no-audio video")
        noaud = os.path.join(extdir, "noaudio.mp4")
        subprocess.run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=gray:s=128x96:r=30:d=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", noaud,
        ], check=True, capture_output=True)
        try:
            run(extdir, "eclip", video=VideoFromFile(noaud), skip_frames=0)
            check("no-audio video raises", False)
        except ValueError as e:
            check("no-audio video raises", "no audio track" in str(e), str(e))

        # --- single source ----------------------------------------------------
        print("single source")
        singledir = tempfile.mkdtemp(prefix="lesca-combine-single-", dir=workdir)
        make_scan(singledir, "solo", {"solo_0.mp4": (9, "red")})
        out4 = run(singledir, "solo")
        check("single clip still merges", os.path.isfile(out4) and frame_count(out4) == 9, out4)

        # --- errors -----------------------------------------------------------
        print("errors")
        try:
            _resolved_folder(os.path.join(workdir, "does-not-exist"))
            check("missing folder raises", False)
        except FileNotFoundError as e:
            check("missing folder raises", "Folder not found" in str(e), str(e))
        try:
            _scan_directory(workdir, "does-not-exist")
            check("missing folder/prefix raises", False)
        except FileNotFoundError as e:
            check("missing folder/prefix raises", "Folder/prefix not found" in str(e), str(e))
        try:
            _resolved_folder(os.path.join(dance_dir, "dance_clip0.mp4"))
            check("non-directory folder raises", False)
        except ValueError as e:
            check("non-directory folder raises", "Not a directory" in str(e), str(e))
        try:
            _list_sources(dance_dir, "  ")
            check("empty prefix raises", False)
        except ValueError as e:
            check("empty prefix raises", "Prefix is empty" in str(e), str(e))
        # A real folder/prefix directory that holds no prefix-matching clips.
        nomatchdir = tempfile.mkdtemp(prefix="lesca-combine-nomatch-", dir=workdir)
        nomatchscan = os.path.join(nomatchdir, "zzz")
        os.makedirs(nomatchscan, exist_ok=True)
        make_clip(os.path.join(nomatchscan, "unrelated.mp4"), 5, "red")
        try:
            run(nomatchdir, "zzz")
            check("no matching files raises", False)
        except ValueError as e:
            check("no matching files raises", "No video files" in str(e), str(e))
        try:
            _resolved_folder("../escape")
            check("escaping relative path raises", False)
        except ValueError as e:
            check("escaping relative path raises", "escapes the base directory" in str(e), str(e))

        # --- validate_inputs ---------------------------------------------------
        print("validate_inputs")
        check("linked (None, None) accepted",
              VideoCombineLesca.validate_inputs(None, None) is True)
        check("concrete folder, linked prefix accepted",
              VideoCombineLesca.validate_inputs(workdir, None) is True)
        v = VideoCombineLesca.validate_inputs(workdir, "   ")
        check("empty prefix reported as string", isinstance(v, str) and "Prefix is empty" in v, str(v))
        v = VideoCombineLesca.validate_inputs(os.path.join(workdir, "nope"), "dance")
        check("missing folder reported as string", isinstance(v, str) and "Folder not found" in v, str(v))
        v = VideoCombineLesca.validate_inputs(workdir, "dance")
        check("valid inputs accepted", v is True, str(v))

        # --- fingerprint ---------------------------------------------------------
        print("fingerprint")
        fp_a = VideoCombineLesca.fingerprint_inputs(workdir, "dance")
        fp_b = VideoCombineLesca.fingerprint_inputs(workdir, "dance")
        check("stable for an unchanged directory", fp_a == fp_b, f"{fp_a} != {fp_b}")
        make_clip(os.path.join(dance_dir, "dance_clip99.mp4"), 3, "red")
        fp_c = VideoCombineLesca.fingerprint_inputs(workdir, "dance")
        check("a new clip changes the fingerprint", fp_c != fp_a, f"{fp_c} == {fp_a}")
        fp_no_video = VideoCombineLesca.fingerprint_inputs(extdir, "eclip")
        fp_with_video = VideoCombineLesca.fingerprint_inputs(extdir, "eclip", video=VideoFromFile(ext_video))
        check("a resolved video changes the fingerprint", fp_no_video != fp_with_video,
              f"{fp_no_video} == {fp_with_video}")
        check("linked inputs yield 0.0", VideoCombineLesca.fingerprint_inputs(None, None) == 0.0)
        check("merged output regex shape",
              _merged_output_regex("dance").fullmatch("dance_merged-2026_10_18_16_33_44.mp4")
              and not _merged_output_regex("dance").fullmatch("dance_merged-x.mp4"))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
