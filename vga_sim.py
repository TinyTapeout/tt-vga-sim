#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml", "pillow"]
# ///
# SPDX-FileCopyrightText: (c) 2026 Tiny Tapeout B.V.
# SPDX-License-Identifier: Apache-2.0
"""Render the VGA output of a Tiny Tapeout project to PNG files, and its audio to WAV.

The design is compiled with Verilator and run from reset. The testbench (vga_tb.cpp) first
measures the sync timing, this script looks the timing up in MODES, and the testbench then
samples the picture the way a monitor does: counting from the start of each sync pulse.
"""

import argparse
import array
import hashlib
import itertools
import json
import math
import shutil
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

import yaml
from PIL import Image, ImageChops

HERE = Path(__file__).resolve().parent
MAX_CLOCKS_PER_PIXEL = 4
AUDIO_RATE = 48000


@dataclass(frozen=True)
class Mode:
    name: str
    pixel_mhz: float
    h: tuple[int, int, int, int]  # visible, front porch, sync, back porch (pixels)
    v: tuple[int, int, int, int]  # visible, front porch, sync, back porch (lines)
    active_low: bool  # standard sync polarity, for both hsync and vsync

    @property
    def width(self) -> int:
        return self.h[0]

    @property
    def height(self) -> int:
        return self.v[0]

    @property
    def pixels_per_line(self) -> int:
        return sum(self.h)

    @property
    def lines_per_frame(self) -> int:
        return sum(self.v)

    @property
    def frame_rate(self) -> float:
        return self.pixel_mhz * 1e6 / (self.pixels_per_line * self.lines_per_frame)


# Standard modes, matched on total pixels per line, sync width and lines per frame.
# A mode missing here? Add it from the VESA DMT tables.
MODES = [
    Mode("640x480 @ 60 Hz", 25.175, (640, 16, 96, 48), (480, 10, 2, 33), True),
    Mode("640x480 @ 72 Hz", 31.5, (640, 24, 40, 128), (480, 9, 3, 28), True),
    Mode("640x480 @ 75 Hz", 31.5, (640, 16, 64, 120), (480, 1, 3, 16), True),
    Mode("800x600 @ 56 Hz", 36.0, (800, 24, 72, 128), (600, 1, 2, 22), False),
    Mode("800x600 @ 60 Hz", 40.0, (800, 40, 128, 88), (600, 1, 4, 23), False),
    Mode("800x600 @ 72 Hz", 50.0, (800, 56, 120, 64), (600, 37, 6, 23), False),
    Mode("1024x768 @ 60 Hz", 65.0, (1024, 24, 136, 160), (768, 3, 6, 29), True),
]


@dataclass(frozen=True)
class Timing:
    """A design's measured video timing: the mode, how fast it is clocked, and its sync levels."""

    mode: Mode
    clocks_per_pixel: int
    hsync_active: int  # level of the hsync pulse
    vsync_active: int

    @property
    def clock_hz(self) -> float:
        return self.mode.pixel_mhz * 1e6 * self.clocks_per_pixel

    def capture_args(self) -> dict:
        """The testbench arguments that place the picture."""
        h, v = self.mode.h, self.mode.v
        return {
            "hsync_active": self.hsync_active,
            "vsync_active": self.vsync_active,
            "clocks_per_pixel": self.clocks_per_pixel,
            "clocks_per_line": self.mode.pixels_per_line * self.clocks_per_pixel,
            "hsync_pulse_clocks": h[2] * self.clocks_per_pixel,
            "first_pixel": h[2] + h[3],
            "first_line": v[2] + v[3],
            "lines_per_frame": self.mode.lines_per_frame,
            "width": self.mode.width,
            "height": self.mode.height,
        }


class SimError(Exception):
    pass


def warn(message: str) -> None:
    print(f"Warning: {message}")


def frame_file(out_dir: Path, frame: int, suffix: str) -> Path:
    """Where frame N goes; the testbench writes the same names with .rgb."""
    return out_dir / f"frame_{frame:04d}{suffix}"


def load_project(project: Path) -> tuple[str, list[Path], int]:
    """Top module, source files and clock_hz from a project's info.yaml."""
    info_path = project / "info.yaml"
    if not info_path.exists():
        raise SimError(f"{info_path} not found. Pass the project folder, or use -f and --top.")
    info = yaml.safe_load(info_path.read_text())["project"]
    sources = [project / "src" / name for name in info["source_files"]]
    for source in sources:
        if not source.exists():
            raise SimError(f"{source} is listed in info.yaml but does not exist")
    return info["top_module"], sources, int(info.get("clock_hz") or 0)


def build(top: str, sources: list[Path], build_dir: Path, verbose: bool) -> Path:
    """Compile the design and the testbench with Verilator; returns the simulator binary."""
    if not shutil.which("verilator"):
        raise SimError("verilator not found. Install Verilator 5.006 or newer.")
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "top.h").write_text(f'#include "V{top}.h"\nusing Vtop = V{top};\n')
    include_dirs = sorted({str(s.parent) for s in sources})
    command = [
        "verilator", "--cc", "--exe", "--build", "-O3", "-Wno-fatal",
        "--x-assign", "unique",
        "--x-initial", "unique",  # lets +verilator+rand+reset+2 randomize flip-flops at power-up
        "--top-module", top,
        "-Mdir", str(build_dir),
        "-o", "vga_sim",
        *(f"-I{d}" for d in include_dirs),
        *(str(s) for s in sources),
        str(HERE / "vga_tb.cpp"),
    ]  # fmt: skip
    if verbose:
        print(" ".join(command))
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    log = [line for line in result.stderr.splitlines() if not line.startswith("perl: warning")]
    warnings = [line for line in log if line.startswith("%Warning")]
    if result.returncode != 0:
        errors = [line for line in log if "%Error" in line or ": error:" in line]
        print("\n".join(log if verbose else errors[:30]), file=sys.stderr)
        raise SimError("Verilator could not build the design (errors above; -v shows the full log)")
    if verbose:
        print(result.stdout + "\n".join(log))
    elif warnings:
        print(f"Verilator warnings ({len(warnings)}, run with -v for details):")
        print("\n".join(f"  {w}" for w in warnings[:20]))
    return build_dir / "vga_sim"


def simulate(binary: Path, seed: int, **testbench_args) -> dict:
    """Run the simulator once and return the JSON summary it prints last."""
    command = [str(binary), "+verilator+rand+reset+2", f"+verilator+seed+{seed}"]
    command += [f"{key}={value}" for key, value in testbench_args.items()]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    lines = result.stdout.strip().splitlines()
    if lines[:-1]:  # $display output from the design
        print("\n".join(lines[:-1]))
    if not lines or not lines[-1].startswith("{"):
        raise SimError(f"the simulation stopped unexpectedly:\n{result.stderr}")
    if result.returncode != 0:
        raise SimError("the sync signals stopped before all frames were captured")
    return json.loads(lines[-1])


def detect_timing(runs: dict) -> Timing:
    """Match the measured hsync / vsync run lengths to a mode in MODES."""

    def high_low(name: str) -> tuple[int, int]:
        low, high = runs[name]
        if low["count"] == 0 or high["count"] == 0:
            pin = 7 if name == "hsync" else 3
            raise SimError(f"{name} never changes: check that it is on uo_out[{pin}]")
        for level, run in (("low", low), ("high", high)):
            if run["min"] != run["max"]:
                raise SimError(
                    f"{name} stays {level} for {run['min']} to {run['max']} clocks, it should "
                    "not vary: the picture would roll or tear on a monitor"
                )
        return low["min"], high["min"]

    h_low, h_high = high_low("hsync")
    v_low, v_high = high_low("vsync")
    hsync_active = 0 if h_low < h_high else 1  # the sync pulse is the shorter level
    vsync_active = 0 if v_low < v_high else 1
    line = h_low + h_high
    h_sync = min(h_low, h_high)
    v_sync, v_total = min(v_low, v_high), v_low + v_high

    print(
        f"Timing: hsync {line} clocks per line, pulse {h_sync} clocks "
        f"(active {'low' if hsync_active == 0 else 'high'}); vsync {v_total / line:g} lines per frame, "
        f"pulse {v_sync / line:g} lines (active {'low' if vsync_active == 0 else 'high'})"
    )
    if v_sync % line or v_total % line:
        raise SimError("vsync does not last a whole number of lines")

    for mode in MODES:
        clocks_per_pixel, rest = divmod(line, mode.pixels_per_line)
        if (
            rest == 0
            and 1 <= clocks_per_pixel <= MAX_CLOCKS_PER_PIXEL
            and h_sync == mode.h[2] * clocks_per_pixel
            and v_total // line == mode.lines_per_frame
            and v_sync // line == mode.v[2]
        ):
            if (hsync_active == 0) != mode.active_low or (vsync_active == 0) != mode.active_low:
                warn(
                    f"{mode.name} uses active-{'low' if mode.active_low else 'high'} sync pulses. "
                    "Many monitors still show the picture, but some do not."
                )
            return Timing(mode, clocks_per_pixel, hsync_active, vsync_active)
    raise SimError(f"no known video mode has this timing. Add it to MODES in {Path(__file__).name}.")


def warn_colour_outside_visible_area(colour_extent: list[int] | None, width: int, height: int) -> None:
    if colour_extent is None:
        warn("every captured frame is black")
        return
    xmin, xmax, ymin, ymax = colour_extent
    if xmin < 0 or xmax >= width or ymin < 0 or ymax >= height:
        warn(
            f"colour appears outside the visible area: x {xmin} to {xmax}, y {ymin} to {ymax} "
            f"(visible: x 0 to {width - 1}, y 0 to {height - 1}). A picture shifted by a pixel or "
            "two means colour and sync are out of step, for example colour from combinational "
            "logic next to a registered sync. Colour far outside means the design does not blank it."
        )


def check_reset(binary: Path, seed: int, capture_args: dict, frames: list[int], out_dir: Path) -> bool:
    """Run again from another power-up state; True if every frame comes out the same."""
    with tempfile.TemporaryDirectory() as other:
        simulate(binary, seed + 1, out=other, **{**capture_args, "clocks_per_audio_sample": 0})
        for frame in frames:
            a = frame_file(out_dir, frame, ".rgb").read_bytes()
            b = frame_file(Path(other), frame, ".rgb").read_bytes()
            if a != b:
                differ = sum(a[i : i + 3] != b[i : i + 3] for i in range(0, len(a), 3))
                print(
                    f"Reset check failed: frame {frame} differs in {differ} pixels between two "
                    "power-up states. Some state is not reset (fine only if that is on purpose, "
                    "such as a free-running random generator)."
                )
                return False
    print("Reset check passed: two different power-up states give the same frames.")
    return True


def save_pngs(frames: list[int], out_dir: Path, width: int, height: int) -> list[Path]:
    """Turn the testbench's raw frames into PNG files."""
    pngs = []
    for frame in frames:
        raw = frame_file(out_dir, frame, ".rgb")
        png = frame_file(out_dir, frame, ".png")
        Image.frombytes("RGB", (width, height), raw.read_bytes()).save(png)
        raw.unlink()
        pngs.append(png)
    return pngs


def to_line_out(raw: bytes, rate: int) -> bytes:
    """Turn the averaged pin level into a line-out signal, as the Audio Pmod does.

    The testbench writes -32767 for a pin held low and +32767 for high, so silence would be a large
    DC offset. A high-pass filter at 20 Hz removes it like the Pmod's AC coupling, and the level is
    scaled to a quarter so the first edge after a silence does not clip.
    """
    samples = array.array("h")
    samples.frombytes(raw)
    alpha = 1 / (1 + 2 * math.pi * 20 / rate)
    out = array.array("h", bytes(len(raw)))
    x_prev = y = 0.0
    for i, sample in enumerate(samples):
        x = sample / 32767
        y = alpha * (y + x - x_prev)
        x_prev = x
        out[i] = max(-32767, min(32767, round(y * 0.25 * 32767)))
    return out.tobytes()


def save_wav(raw: Path, path: Path, rate: int) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(to_line_out(raw.read_bytes(), rate))
    raw.unlink()


def save_gif(path: Path, pngs: list[Path], fps: float) -> None:
    """Animated GIF of every second frame, with delays that keep the real speed on average."""
    images = [Image.open(png).convert("RGB") for png in pngs[::2]]
    # GIF delays are whole hundredths of a second; round the running time so speed stays right.
    times = [round(i * 2 * 100 / fps) for i in range(len(images) + 1)]
    durations = [(b - a) * 10 for a, b in itertools.pairwise(times)]
    images[0].save(path, save_all=True, append_images=images[1:], duration=durations, loop=0)


def save_video(path: Path, out_dir: Path, first: int, fps: float, audio: Path | None) -> None:
    """MP4 of the saved frames, with the audio if there is any."""
    if not shutil.which("ffmpeg"):
        raise SimError("--video needs ffmpeg on PATH")
    command = ["ffmpeg", "-y", "-loglevel", "error", "-framerate", f"{fps:.6f}"]
    command += ["-start_number", str(first), "-i", str(out_dir / "frame_%04d.png")]
    if audio:
        command += ["-i", str(audio), "-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18"]
    command += ["-movflags", "+faststart", str(path)]  # lets a browser start before the download ends
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise SimError(f"ffmpeg could not write {path}:\n{result.stderr}")


def compare_with_reference(frames: list[int], out_dir: Path, reference: Path) -> bool:
    """Compare frames with reference PNGs; True if every frame that has a reference matches."""
    compared, same = 0, True
    for frame in frames:
        candidates = [frame_file(reference, frame, ".png"), reference / f"frame{frame}.png"]
        ref = next((c for c in candidates if c.exists()), None)
        if ref is None:
            continue
        compared += 1
        image = Image.open(frame_file(out_dir, frame, ".png")).convert("RGB")
        diff = ImageChops.difference(image, Image.open(ref).convert("RGB"))
        if diff.getbbox() is not None:
            same = False
            changed = sum(1 for pixel in diff.getdata() if pixel != (0, 0, 0))
            diff_path = out_dir / f"diff_{frame:04d}.png"
            diff.point(lambda value: 255 if value else 0).save(diff_path)
            print(f"Frame {frame} differs from {ref} in {changed} pixels: see {diff_path}")
    if compared == 0:
        raise SimError(f"no reference frames for the saved frames in {reference}")
    if same:
        print(f"Compare passed: {compared} frame{'s match' if compared > 1 else ' matches'} {reference}")
    return same


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render the VGA output of a Tiny Tapeout project to PNG files.",
        epilog="Example: vga_sim.py path/to/project -n 60 --gif frames/demo.gif",
    )
    add = parser.add_argument

    def number(text: str) -> int:  # accepts 0x.. and 0b.. too
        return int(text, 0)

    add("project", nargs="?", type=Path, default=Path("."), help="project folder with info.yaml")
    add("-f", "--files", nargs="+", type=Path, metavar="FILE", help="Verilog files, no info.yaml")
    add("--top", help="top module name, with -f")
    add("-n", "--frames", type=int, default=1, help="number of frames to save (default: 1)")
    add("--skip", type=int, default=0, help="frames to run before the first one saved")
    add("-o", "--out-dir", type=Path, default=Path("frames"), help="output folder (default: frames)")
    add("--wav", type=Path, help="also save the audio on uio_out[7] to this WAV file")
    add("--gif", type=Path, help="also save the frames as an animated GIF")
    add("--video", type=Path, help="also save an MP4 with sound (needs ffmpeg)")
    add("--compare", type=Path, metavar="DIR", help="compare the frames with reference PNGs in DIR")
    add("--ui-in", type=number, default=0, help="value on ui_in (default: 0)")
    add("--uio-in", type=number, default=0, help="value on uio_in (default: 0)")
    add("--seed", type=int, default=1, help="seed for the random power-up state (default: 1)")
    add("--check-reset", action="store_true", help="also run from another power-up state, compare")
    add("--build-dir", type=Path, help="Verilator build folder (default: a temporary folder)")
    add("-v", "--verbose", action="store_true", help="show the Verilator output")
    args = parser.parse_args()
    if args.files and not args.top:
        parser.error("--top is required with -f")
    return args


def run(args: argparse.Namespace) -> bool:
    """Everything the command does; False when a check failed."""
    clock_hz = 0
    if args.files:
        top, sources = args.top, [f.resolve() for f in args.files]
    else:
        top, sources, clock_hz = load_project(args.project.resolve())
    key = hashlib.sha1("\n".join([top, *map(str, sources)]).encode()).hexdigest()[:12]
    build_dir = args.build_dir or Path(tempfile.gettempdir()) / "tt-vga-sim" / key
    binary = build(top, sources, build_dir.resolve(), args.verbose)

    inputs = {"ui_in": args.ui_in, "uio_in": args.uio_in}
    timing = detect_timing(simulate(binary, args.seed, mode="measure", **inputs))
    mode, plural = timing.mode, "s" * (timing.clocks_per_pixel > 1)
    print(
        f"Mode: {mode.name}, {mode.pixel_mhz:g} MHz pixel clock, {timing.clocks_per_pixel} clock{plural} per pixel"
    )
    if clock_hz and abs(clock_hz - timing.clock_hz) > 0.02 * timing.clock_hz:
        warn(f"info.yaml has clock_hz: {clock_hz}, but this mode needs about {timing.clock_hz:.0f} Hz")

    record_audio = bool(args.wav or args.video)
    clocks_per_audio_sample = round(timing.clock_hz / AUDIO_RATE)
    capture_args = dict(
        mode="capture",
        frames=args.frames,
        skip=args.skip,
        clocks_per_audio_sample=clocks_per_audio_sample if record_audio else 0,
        **timing.capture_args(),
        **inputs,
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_dir = args.out_dir.resolve()
    result = simulate(binary, args.seed, out=out_dir, **capture_args)
    frames = result["frames"]
    if args.skip == 0 and result["frame0_status"] == "partial":
        first, last = result["grey_rows"]
        print(f"Note: frame 0 began before reset ended; rows {first} to {last} are not drawn (grey).")
    elif args.skip == 0 and result["frame0_status"] == "irregular":
        print("Note: hsync was irregular before the first vsync pulse, so frame 0 is not drawn (grey).")
    warn_colour_outside_visible_area(result["colour_extent"], mode.width, mode.height)
    ok = check_reset(binary, args.seed, capture_args, frames, out_dir) if args.check_reset else True

    pngs = save_pngs(frames, out_dir, mode.width, mode.height)
    listed = pngs[0].name if len(pngs) == 1 else f"{pngs[0].name} ... {pngs[-1].name}"
    print(f"Saved {len(pngs)} frame{'s' * (len(pngs) > 1)} to {args.out_dir}/: {listed}")

    wav = None
    if record_audio:
        wav = args.wav or out_dir / "audio.wav"
        rate = round(timing.clock_hz / clocks_per_audio_sample)
        save_wav(out_dir / "audio.raw", wav, rate)
        if args.wav:
            print(f"Saved {result['audio_samples'] / rate:.2f} s of audio to {args.wav}")
    audio_driven = bool(result["uio_oe"] & 0x80)
    if record_audio and not audio_driven:
        warn("uio_oe[7] is 0, so the audio pin is not driven on the chip")
    if args.gif:
        save_gif(args.gif, pngs, mode.frame_rate)
        print(f"Saved {args.gif}")
    if args.video:
        save_video(args.video, out_dir, frames[0], mode.frame_rate, wav if audio_driven else None)
        print(f"Saved {args.video}")
        if not args.wav:
            wav.unlink()
    if args.compare:
        ok = compare_with_reference(frames, out_dir, args.compare) and ok
    return ok


def main() -> int:
    try:
        return 0 if run(parse_args()) else 1
    except SimError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
