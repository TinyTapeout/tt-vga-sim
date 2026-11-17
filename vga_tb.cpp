// SPDX-FileCopyrightText: (c) 2026 Tiny Tapeout B.V.
// SPDX-License-Identifier: Apache-2.0
//
// Verilator testbench for tt-vga-sim.
//
// It knows nothing about video modes. vga_sim.py runs it twice:
//   mode=measure  reports how long hsync and vsync stay at each level
//   mode=capture  samples the picture with the geometry of the detected mode
// Arguments are key=value pairs; the last line printed is a JSON summary.

#include <climits>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <string>
#include <vector>

#include <verilated.h>

#include "top.h"  // written by vga_sim.py: defines Vtop as the Verilated top module

// More than a frame of the largest mode (1024x768, 4 clocks per pixel) takes; a design without
// sync would otherwise run forever.
static const long MAX_CLOCKS_PER_FRAME = 10000000;
static const long RESET_CLOCKS = 10;

// Grey that no design can produce (the pins give 0, 85, 170 or 255 per channel), for the part
// of frame 0 that was drawn before reset ended.
static const uint8_t NOT_DRAWN = 64;

static std::map<std::string, std::string> args;

static long arg(const char *key, long fallback) {
  auto it = args.find(key);
  return it == args.end() ? fallback : strtol(it->second.c_str(), nullptr, 0);
}

static long required_arg(const char *key) {
  if (!args.count(key)) {
    fprintf(stderr, "missing argument %s=...\n", key);
    exit(2);
  }
  return arg(key, 0);
}

static long floor_div(long a, long b) { return a >= 0 ? a / b : -((-a + b - 1) / b); }

static void tick(Vtop *top) {
  top->clk = 1;
  top->eval();
  top->clk = 0;
  top->eval();
}

// Tiny VGA Pmod pinout: uo_out = {hsync, B0, G0, R0, vsync, B1, G1, R1}
static int hsync(uint8_t uo) { return (uo >> 7) & 1; }
static int vsync(uint8_t uo) { return (uo >> 3) & 1; }
static int red(uint8_t uo) { return ((uo << 1) & 2) | ((uo >> 4) & 1); }
static int green(uint8_t uo) { return (uo & 2) | ((uo >> 5) & 1); }
static int blue(uint8_t uo) { return ((uo >> 1) & 2) | ((uo >> 6) & 1); }

// Lengths of the high and low runs of one signal.
struct Runs {
  int level = -1;
  bool started = false;  // the first run after arming is partial and not recorded
  long length = 0;
  long min[2] = {LONG_MAX, LONG_MAX}, max[2] = {0, 0}, count[2] = {0, 0};

  void sample(int value, bool armed) {
    if (value != level) {
      if (started) {
        if (length < min[level]) min[level] = length;
        if (length > max[level]) max[level] = length;
        count[level]++;
      } else if (armed && level >= 0) {
        started = true;
      }
      level = value;
      length = 0;
    }
    length++;
  }

  void print(const char *name) const {
    printf("\"%s\":[", name);
    for (int l = 0; l < 2; l++)
      printf("%s{\"min\":%ld,\"max\":%ld,\"count\":%ld}", l ? "," : "", count[l] ? min[l] : 0, max[l],
             count[l]);
    printf("]");
  }
};

// Measure sync timing over two full frames, starting at the first vsync edge after reset.
static int measure(Vtop *top) {
  Runs h, v;
  const int vsync_at_reset = vsync(top->uo_out);
  bool armed = false;
  for (long t = 0; t < 3 * MAX_CLOCKS_PER_FRAME && !(v.count[0] >= 2 && v.count[1] >= 2); t++) {
    tick(top);
    if (vsync(top->uo_out) != vsync_at_reset) armed = true;
    v.sample(vsync(top->uo_out), armed);
    h.sample(hsync(top->uo_out), armed);
  }
  printf("{");
  h.print("hsync");
  printf(",");
  v.print("vsync");
  printf("}\n");
  return 0;
}

// Sample the picture like a monitor does: count from the start of each sync pulse.
// Frame k is the picture drawn before the (k+1)th vsync pulse after reset.
static int capture(Vtop *top) {
  const int hsync_active = required_arg("hsync_active"), vsync_active = required_arg("vsync_active");
  const long clocks_per_pixel = required_arg("clocks_per_pixel");
  const long clocks_per_line = required_arg("clocks_per_line");
  const long hsync_pulse_clocks = required_arg("hsync_pulse_clocks");
  const long first_pixel_clock = required_arg("first_pixel") * clocks_per_pixel;  // after hsync starts
  const long first_line = required_arg("first_line");  // hsync pulses after vsync starts
  const long lines_per_frame = required_arg("lines_per_frame");
  const long width = required_arg("width"), height = required_arg("height");
  const long frames = required_arg("frames"), skip = arg("skip", 0);
  const long clocks_per_audio_sample = arg("clocks_per_audio_sample", 0);  // 0: no audio
  const std::string out = args["out"];

  long xmin = LONG_MAX, xmax = LONG_MIN, ymin = LONG_MAX, ymax = LONG_MIN;
  std::vector<long> written;

  // One clock of output, at line y of a frame and dx clocks after the first pixel's start.
  auto sample = [&](std::vector<uint8_t> &image, long y, long dx, uint8_t uo) {
    const long x = floor_div(dx, clocks_per_pixel);
    const int r = red(uo), g = green(uo), b = blue(uo);
    if (r | g | b) {
      if (x < xmin) xmin = x;
      if (x > xmax) xmax = x;
      if (y < ymin) ymin = y;
      if (y > ymax) ymax = y;
    }
    // Sample on the last clock of each pixel, when a pixel-enable design has settled.
    if (dx >= 0 && dx % clocks_per_pixel == clocks_per_pixel - 1 && x < width && y >= 0 && y < height) {
      uint8_t *p = &image[(y * width + x) * 3];
      p[0] = r * 85;
      p[1] = g * 85;
      p[2] = b * 85;
    }
  };

  auto save_frame = [&](const std::vector<uint8_t> &image, long index) {
    char name[64];
    snprintf(name, sizeof name, "/frame_%04ld.rgb", index);
    FILE *f = fopen((out + name).c_str(), "wb");
    fwrite(image.data(), 1, image.size(), f);
    fclose(f);
    written.push_back(index);
  };

  // Frame 0 comes before the first vsync pulse, when nothing yet says where lines start. Keep its
  // output until the first hsync pulse after that vsync pulse fixes the timing, then place it.
  std::vector<uint8_t> frame0_output;
  const char *frame0_status = "ok";
  long grey_first = -1, grey_last = -1;
  auto place_frame0 = [&](long first_hsync) {
    std::vector<uint8_t> image(width * height * 3, NOT_DRAWN);
    const long count = frame0_output.size();
    bool regular = true;  // hsync must already repeat every line, in step with first_hsync
    for (long t = 0; t < count && regular; t++) {
      const long in_line = t - first_hsync - floor_div(t - first_hsync, clocks_per_line) * clocks_per_line;
      regular = hsync(frame0_output[t]) == (in_line < hsync_pulse_clocks ? hsync_active : !hsync_active);
    }
    if (!regular) {
      frame0_status = "irregular";
    } else {
      for (long t = 0; t < count; t++) {
        const long lines_before = floor_div(t - first_hsync, clocks_per_line);  // negative
        const long in_line = t - first_hsync - lines_before * clocks_per_line;
        // Counted from frame 1's vsync pulse this clock is on line lines_before + 1 (zero or less);
        // counted from frame 0's own vsync pulse, a frame earlier, that is lines_per_frame more.
        sample(image, lines_before + 1 + lines_per_frame - first_line, in_line - first_pixel_clock,
               frame0_output[t]);
      }
      for (long y = 0; y < height; y++)
        for (long x = 0; x < width; x++)
          if (image[(y * width + x) * 3] == NOT_DRAWN && image[(y * width + x) * 3 + 1] == NOT_DRAWN) {
            if (grey_first < 0) grey_first = y;
            grey_last = y;
            break;
          }
      if (grey_first >= 0) frame0_status = "partial";
    }
    save_frame(image, 0);
    std::vector<uint8_t>().swap(frame0_output);
  };

  std::vector<uint8_t> image(width * height * 3);
  FILE *audio = clocks_per_audio_sample ? fopen((out + "/audio.raw").c_str(), "wb") : nullptr;
  long audio_sum = 0, audio_clocks = 0, audio_samples = 0;

  long frame = 0;           // vsync pulses seen so far: the index of the frame being drawn
  long line = 0;            // hsync pulses since the vsync pulse started
  long clocks_in_line = 0;  // clocks since the hsync pulse started
  int last_h = !hsync_active, last_v = !vsync_active;
  const long max_clocks = MAX_CLOCKS_PER_FRAME * (frames + skip + 2);

  for (long t = 0; t < max_clocks && (long)written.size() < frames; t++) {
    tick(top);
    const uint8_t uo = top->uo_out;
    const int h = hsync(uo), v = vsync(uo);
    clocks_in_line++;

    if (v == vsync_active && last_v != vsync_active) {
      if (frame >= 1 && frame >= skip) save_frame(image, frame);
      frame++;
      line = 0;
      std::fill(image.begin(), image.end(), 0);
    }
    if (h == hsync_active && last_h != hsync_active) {
      line++;
      clocks_in_line = 0;
      if (frame == 1 && line == 1 && skip == 0) place_frame0(t);
    }
    last_h = h;
    last_v = v;

    if (audio && frame >= skip) {
      audio_sum += (top->uio_out >> 7) & 1;
      if (++audio_clocks == clocks_per_audio_sample) {
        // -32767 for a pin held low, +32767 for high; vga_sim.py removes the DC offset
        const int16_t level = (audio_sum * 2 - clocks_per_audio_sample) * 32767 / clocks_per_audio_sample;
        fwrite(&level, sizeof level, 1, audio);
        audio_samples++;
        audio_sum = audio_clocks = 0;
      }
    }
    if (frame == 0) {
      if (skip == 0) frame0_output.push_back(uo);
    } else if (frame >= skip) {
      sample(image, line - first_line, clocks_in_line - first_pixel_clock, uo);
    }
  }
  if (audio) fclose(audio);

  printf("{\"frames\":[");
  for (size_t i = 0; i < written.size(); i++) printf("%s%ld", i ? "," : "", written[i]);
  if (xmin <= xmax)
    printf("],\"colour_extent\":[%ld,%ld,%ld,%ld]", xmin, xmax, ymin, ymax);
  else
    printf("],\"colour_extent\":null");
  printf(",\"frame0_status\":\"%s\",\"grey_rows\":[%ld,%ld]", frame0_status, grey_first, grey_last);
  printf(",\"audio_samples\":%ld,\"uio_oe\":%d}\n", audio_samples, top->uio_oe);
  return (long)written.size() == frames ? 0 : 1;
}

int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);  // +verilator+seed+N and +verilator+rand+reset+N
  for (int i = 1; i < argc; i++) {
    const char *eq = strchr(argv[i], '=');
    if (eq && argv[i][0] != '+') args[std::string(argv[i], eq - argv[i])] = eq + 1;
  }

  Vtop *top = new Vtop;
  top->ena = 1;
  top->ui_in = arg("ui_in", 0);
  top->uio_in = arg("uio_in", 0);
  // Start out of reset and then assert it, so that `negedge rst_n` blocks see an edge.
  top->rst_n = 1;
  top->eval();
  top->rst_n = 0;
  for (long i = 0; i < RESET_CLOCKS; i++) tick(top);
  top->rst_n = 1;

  const int rc = args["mode"] == "measure" ? measure(top) : capture(top);
  top->final();
  delete top;
  return rc;
}
