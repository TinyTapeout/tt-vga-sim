# Working on tt-vga-sim

Notes for anyone changing this tool, human or agent. To use it on a design, see the
[README](README.md) and the [`vga-output` skill](https://github.com/TinyTapeout/skills/tree/main/skills/vga-output).

## How it works

1. `vga_sim.py` reads `info.yaml` (or `-f` / `--top`), writes `top.h` into a build folder and runs
   `verilator --build` on the design plus `vga_tb.cpp`. Verilator skips rebuilding when nothing
   changed.
2. It runs the simulator with `mode=measure`: the testbench reports how long hsync and vsync stay
   at each level, as JSON on its last output line.
3. `detect_timing()` matches that timing to an entry in `MODES` and works out clocks per pixel and
   sync polarity; `Timing.capture_args()` turns that into the testbench's geometry.
4. It runs the simulator again with `mode=capture` and the mode's geometry. The testbench counts
   from the start of each sync pulse, as a monitor does, and writes raw `frame_NNNN.rgb` files and
   `audio.raw`; `vga_sim.py` turns them into PNG, WAV, GIF and MP4.
5. `--check-reset` runs the capture once more with the next seed and compares the raw frames;
   `--compare` diffs the PNGs against reference files.

Frame k is the picture drawn before the (k+1)th vsync pulse after reset, so frame 0 comes before
any vsync pulse. The testbench keeps the output from reset until the first hsync pulse after the
first vsync pulse, then places it using the line timing (`place_frame0`). Rows drawn before reset
ended stay grey (64, 64, 64), a colour the pins cannot produce.

The testbench knows nothing about video modes; all of that lives in `vga_sim.py`. Arguments to the
testbench are `key=value` pairs, and Verilator's own `+verilator+...` options set the power-up
state.

## Where to change things

- A new video mode: add it to `MODES` from the VESA DMT tables.
- Inputs that change over time (buttons, a scripted gamepad): `main()` in `vga_tb.cpp` sets
  `top->ui_in` once; set it inside the `capture()` loop instead, before `tick()`, and pass any new
  settings as `key=value` arguments.
- Other pins or extra output files: also in `capture()`, then convert them in `vga_sim.py`.

## Keep these stable

The `vga-output` skill and the README tell people what these mean, so keep their wording, or
update both in the same change:

- `Timing: ...` and `Mode: ...`
- `Warning: colour appears outside the visible area: x ... to ..., y ... to ...`
- `Reset check passed` / `Reset check failed`, `Compare passed` / `Frame N differs`
- `frame_NNNN.png` names, with frame 0 the first picture after reset
- exit code 0 on success, 1 on an error, a failed reset check or a changed frame

## Test

```bash
uv run --with-requirements requirements.txt python -m unittest discover -s tests -v
```

The tests simulate the designs in `tests/designs/test_designs.v`, whose output is known, and check
what `vga_sim.py` reports. Add a design there for each new check or mode. The video test needs
ffmpeg and is skipped without it. The dependencies are listed twice, in the header of
`vga_sim.py` (for uv) and in `requirements.txt` (for pip); a test checks that they match. Format
Python with `uvx ruff format` and keep files plain ASCII.
