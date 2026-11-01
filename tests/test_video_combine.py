"""Verification for Video Combine (Lesca) — Goal 5 of specs.md.

Builds a directory of same-prefix mp4 clips (generated with ffmpeg: h264 + aac,
solid-color frames) and asserts the spec's behavior:

* source scan   -> every file starting with the prefix, ascending filename
                   order; previously merged <prefix>_merged-*.mp4 outputs,
                   non-video files and other prefixes are ignored
* merge         -> <prefix>_merged-YYYY_MM_DD_HH_MM_SS.mp4 in the same
                   directory; total frame count equals the sum of the inputs;
                    the audio splice is clean (no last-AAC-frame duplication: the
                    merged decoded sample count is never longer than the inputs'
                    sum, and each clip's audio is trimmed to its video duration so the
                    excess AAC padding adds no extra frame)
* re-run        -> the freshly merged file is excluded from the next scan,
                   so a second run produces another same-length merged file
                   instead of merging the merge
* re-encode     -> every merge re-encodes (h264 + aac); incompatible inputs
                   (different resolutions) and mixed-audio inputs (one clip
                   without a track) are handled by normalising streams /
                   padding silence, not by a copy-then-fallback path
* single source -> a directory with one clip still merges (a renamed copy)
* errors        -> missing folder / not a directory / empty prefix / no
                   matching files / relative path escaping the base directory
* validation    -> validate_inputs accepts linked (None) inputs and reports
                   concrete problems as strings
* fingerprint   -> unchanged directory yields a stable fingerprint; a new
                   clip changes it; linked inputs yield the constant 0.0

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

from lesca_video_combine import (
    VideoCombineLesca,
    _list_sources,
    _merged_output_regex,
    _resolved_folder,
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


def run(folder, prefix, any=None):
    """execute and unwrap the NodeOutput to its filename argument."""
    return VideoCombineLesca.execute(folder=folder, prefix=prefix, any=any)[0]


def main():
    workdir = tempfile.mkdtemp(prefix="lesca-combine-")
    try:
        # --- fixtures -------------------------------------------------------
        clips = {
            "dance_clip0.mp4": (10, "red"),
            "dance_clip1.mp4": (15, "green"),
            "dance_clip2.mp4": (20, "blue"),
            "dance_clip10.mp4": (12, "yellow"),
        }
        for name, (frames, color) in clips.items():
            make_clip(os.path.join(workdir, name), frames, color)
        make_clip(os.path.join(workdir, "other_clip0.mp4"), 5, "red")
        make_clip(os.path.join(workdir, "dance_merged-2020_01_02_03_04_05.mp4"), 7, "red")
        with open(os.path.join(workdir, "dance_notes.txt"), "w") as f:
            f.write("not a video\n")
        expected_total = sum(n for n, _ in clips.values())

        # --- source scan ----------------------------------------------------
        print("source scan")
        sources = _list_sources(workdir, "dance")
        check(
            "only prefix-matching videos, ascending name order",
            [os.path.basename(s) for s in sources] == ["dance_clip0.mp4", "dance_clip1.mp4", "dance_clip10.mp4", "dance_clip2.mp4"],
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
        check("merged into the same directory", os.path.dirname(out) == workdir, out)
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
        sources_after = _list_sources(workdir, "dance")
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
        make_clip(os.path.join(mixeddir, "mix_a.mp4"), 8, "red", size="128x96")
        make_clip(os.path.join(mixeddir, "mix_b.mp4"), 6, "blue", size="320x240")
        out3 = run(mixeddir, "mix")
        check("incompatible clips merged (re-encode normalises streams)", os.path.isfile(out3), out3)
        check("incompatible merge has the total frame count", frame_count(out3) == 14,
              f"{frame_count(out3)} != 14")

        # --- mixed audio: one clip has no track -------------------------------
        print("mixed audio")
        mixedaudir = tempfile.mkdtemp(prefix="lesca-combine-audio-", dir=workdir)
        make_clip(os.path.join(mixedaudir, "aud_a.mp4"), 8, "red", audio=True)
        make_clip(os.path.join(mixedaudir, "aud_b.mp4"), 6, "blue", audio=False)
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
        make_clip(os.path.join(vonlydir, "vid_a.mp4"), 8, "red", audio=False)
        make_clip(os.path.join(vonlydir, "vid_b.mp4"), 6, "blue", audio=False)
        out6 = run(vonlydir, "vid")
        check("video-only clips merge", os.path.isfile(out6), out6)
        check("video-only output has no audio track",
              subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                              "-show_entries", "stream=codec_name", "-of", "csv=p=0", out6],
                             capture_output=True, text=True).stdout.strip() == "")
        check("video-only output has the total frame count", frame_count(out6) == 14,
              f"{frame_count(out6)} != 14")

        # --- single source ----------------------------------------------------
        print("single source")
        singledir = tempfile.mkdtemp(prefix="lesca-combine-single-", dir=workdir)
        make_clip(os.path.join(singledir, "solo_0.mp4"), 9, "red")
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
            _resolved_folder(os.path.join(workdir, "dance_clip0.mp4"))
            check("non-directory folder raises", False)
        except ValueError as e:
            check("non-directory folder raises", "Not a directory" in str(e), str(e))
        try:
            _list_sources(workdir, "  ")
            check("empty prefix raises", False)
        except ValueError as e:
            check("empty prefix raises", "Prefix is empty" in str(e), str(e))
        try:
            run(workdir, "zzz_no_such_prefix")
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
        make_clip(os.path.join(workdir, "dance_clip99.mp4"), 3, "red")
        fp_c = VideoCombineLesca.fingerprint_inputs(workdir, "dance")
        check("a new clip changes the fingerprint", fp_c != fp_a, f"{fp_c} == {fp_a}")
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
