# SPDX-FileCopyrightText: (c) 2026 Tiny Tapeout B.V.
# SPDX-License-Identifier: Apache-2.0
"""End-to-end tests: simulate small designs with known output and check what vga_sim.py reports.

Run from the repository root:
    uv run --with-requirements requirements.txt python -m unittest discover -s tests -v
Needs Verilator on PATH, and ffmpeg for the video test.
"""

import array
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
DESIGNS = ROOT / "tests/designs/test_designs.v"
WHITE, GREY = (255, 255, 255), (64, 64, 64)


def colours(png: Path) -> set[tuple[int, int, int]]:
    with Image.open(png) as image:
        return {colour for _, colour in image.convert("RGB").getcolors(1 << 24)}


class DependenciesTest(unittest.TestCase):
    def test_requirements_match_script(self):
        # uv reads the list in the script's header, pip reads requirements.txt: keep them equal.
        header = re.search(r"# dependencies = \[(.*)\]", (ROOT / "vga_sim.py").read_text())[1]
        script = sorted(name.strip().strip('"') for name in header.split(","))
        lines = (ROOT / "requirements.txt").read_text().splitlines()
        requirements = sorted(line.strip() for line in lines if line.strip() and not line.startswith("#"))
        self.assertEqual(script, requirements)


class VgaSimTest(unittest.TestCase):
    def run_sim(self, *args: str) -> tuple[int, str, Path]:
        out = Path(tempfile.mkdtemp())
        result = subprocess.run(
            [sys.executable, str(ROOT / "vga_sim.py"), *args, "-o", str(out)],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode, result.stdout + result.stderr, out

    def run_design(self, top: str, *args: str) -> tuple[int, str, Path]:
        return self.run_sim("-f", str(DESIGNS), "--top", top, *args)

    def assert_white(self, png: Path, size: tuple[int, int]) -> None:
        with Image.open(png) as image:
            self.assertEqual(image.size, size)
        self.assertEqual(colours(png), {WHITE}, f"{png.name} is not all white")

    def test_640x480(self):
        code, output, out = self.run_design("tt_um_test_640x480", "-n", "2", "--check-reset")
        self.assertEqual(code, 0, output)
        self.assertIn("Mode: 640x480 @ 60 Hz", output)
        self.assertIn("Reset check passed", output)
        self.assertNotIn("Warning", output)
        self.assertNotIn("Note", output)
        self.assert_white(out / "frame_0000.png", (640, 480))
        self.assert_white(out / "frame_0001.png", (640, 480))

    def test_two_clocks_per_pixel(self):
        code, output, out = self.run_design("tt_um_test_2_clocks_per_pixel", "-n", "2")
        self.assertEqual(code, 0, output)
        self.assertIn("2 clocks per pixel", output)
        self.assertNotIn("Warning", output)
        self.assert_white(out / "frame_0000.png", (640, 480))
        self.assert_white(out / "frame_0001.png", (640, 480))

    def test_800x600(self):
        code, output, out = self.run_design("tt_um_test_800x600", "-n", "2")
        self.assertEqual(code, 0, output)
        self.assertIn("Mode: 800x600 @ 56 Hz", output)
        self.assertNotIn("Warning", output)
        self.assert_white(out / "frame_0000.png", (800, 600))
        self.assert_white(out / "frame_0001.png", (800, 600))

    def test_frame_numbers(self):
        # Frame k is drawn in colour k: frame 0 is the first frame after reset.
        code, output, out = self.run_design("tt_um_test_frame_number", "-n", "3", "--skip", "1")
        self.assertEqual(code, 0, output)
        for k in (1, 2, 3):
            expected = ((k >> 4 & 3) * 85, (k >> 2 & 3) * 85, (k & 3) * 85)
            self.assertEqual(colours(out / f"frame_{k:04d}.png"), {expected}, f"frame {k}")

    def test_frame_zero_after_mid_frame_reset(self):
        code, output, out = self.run_design("tt_um_test_reset_mid_frame", "-n", "2")
        self.assertEqual(code, 0, output)
        self.assertIn("rows 0 to 239 are not drawn", output)
        with Image.open(out / "frame_0000.png") as image:
            frame0 = image.convert("RGB")
        self.assertEqual({c for _, c in frame0.crop((0, 0, 640, 240)).getcolors()}, {GREY})
        self.assertEqual({c for _, c in frame0.crop((0, 240, 640, 480)).getcolors()}, {WHITE})
        self.assert_white(out / "frame_0001.png", (640, 480))

    def test_sync_polarity(self):
        code, output, _ = self.run_design("tt_um_test_active_high_sync")
        self.assertEqual(code, 0, output)
        self.assertIn("active high", output)
        self.assertIn("uses active-low sync pulses", output)

    def test_colour_one_clock_early(self):
        code, output, _ = self.run_design("tt_um_test_colour_one_clock_early")
        self.assertEqual(code, 0, output)
        self.assertIn("colour appears outside the visible area: x -1 to 638", output)

    def test_missing_reset(self):
        code, output, _ = self.run_design("tt_um_test_frame_counter_not_reset", "--check-reset")
        self.assertEqual(code, 1, output)
        self.assertIn("Reset check failed", output)

    def test_no_sync(self):
        code, output, _ = self.run_design("tt_um_test_no_sync")
        self.assertEqual(code, 1, output)
        self.assertIn("hsync never changes", output)

    def test_compare_with_reference(self):
        _, _, reference = self.run_design("tt_um_test_frame_number", "-n", "2")
        code, output, _ = self.run_design("tt_um_test_frame_number", "-n", "2", "--compare", str(reference))
        self.assertEqual(code, 0, output)
        self.assertIn("Compare passed: 2 frames match", output)
        # The playground export names its frames frame0.png, frame1.png, ...: give frame 1 the
        # picture of frame 0, which has another colour, under that name.
        (reference / "frame_0000.png").rename(reference / "frame1.png")
        (reference / "frame_0001.png").unlink()
        code, output, out = self.run_design(
            "tt_um_test_frame_number", "-n", "2", "--skip", "1", "--compare", str(reference)
        )
        self.assertEqual(code, 1, output)
        self.assertIn("Frame 1 differs", output)
        self.assertTrue((out / "diff_0001.png").exists())

    def test_example_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            beep, gif = Path(tmp) / "beep.wav", Path(tmp) / "hello.gif"
            args = ["-n", "4", "--skip", "31", "--wav", str(beep), "--gif", str(gif)]
            code, output, out = self.run_sim(str(ROOT / "examples/hello"), *args)
            self.assertEqual(code, 0, output)
            self.assertNotIn("Warning", output)
            self.assertEqual(min(p.name for p in out.glob("*.png")), "frame_0031.png")
            with wave.open(str(beep)) as wav:
                self.assertAlmostEqual(wav.getframerate(), 48000, delta=480)
                samples = wav.readframes(wav.getnframes())
            values = array.array("h", samples)
            self.assertGreater(max(values) - min(values), 10000, "silent")
            self.assertLess(abs(sum(values) / len(values)), 300, "audio has a DC offset")
            with Image.open(gif) as image:
                self.assertEqual(image.n_frames, 2)  # every second frame

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "hello.mp4"
            code, output, _ = self.run_sim(str(ROOT / "examples/hello"), "-n", "40", "--video", str(video))
            self.assertEqual(code, 0, output)
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "csv=p=0",
                    str(video),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(sorted(probe.stdout.split()), ["audio", "video"])


if __name__ == "__main__":
    unittest.main()
