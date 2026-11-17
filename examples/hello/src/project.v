/*
 * Copyright (c) 2026 Tiny Tapeout B.V.
 * SPDX-License-Identifier: Apache-2.0
 *
 * Scrolling colour pattern with a beep, for the Tiny VGA and Audio Pmods.
 */

`default_nettype none

module tt_um_vga_hello (
    input  wire [7:0] ui_in,    // Dedicated inputs
    output wire [7:0] uo_out,   // Dedicated outputs
    input  wire [7:0] uio_in,   // IOs: Input path
    output wire [7:0] uio_out,  // IOs: Output path
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output)
    input  wire       ena,      // always 1 when the design is powered, so you can ignore it
    input  wire       clk,      // clock
    input  wire       rst_n     // reset_n - low to reset
);

  wire hsync, vsync, display_on;
  wire [9:0] hpos, vpos;
  hvsync_generator sync_gen (
      .clk(clk),
      .reset(~rst_n),
      .hsync(hsync),
      .vsync(vsync),
      .display_on(display_on),
      .hpos(hpos),
      .vpos(vpos)
  );

  // Frame counter, advanced once per frame at the start of vertical blanking
  reg [7:0] frame;
  wire frame_tick = (hpos == 0) && (vpos == 480);
  always @(posedge clk) begin
    if (~rst_n) frame <= 0;
    else if (frame_tick) frame <= frame + 1;
  end

  // Diagonal bands that scroll to the right, over a checkerboard
  wire [9:0] x = hpos - {1'b0, frame, 1'b0};
  wire [9:0] band = x + vpos;
  wire [5:0] rgb = {band[7:6], band[8:7] ^ {2{x[5] ^ vpos[5]}}, ~band[7:6]};

  // Registered colour, black outside the visible area
  reg [5:0] rgb_q;
  always @(posedge clk) rgb_q <= display_on ? rgb : 6'b0;
  wire [1:0] r = rgb_q[5:4], g = rgb_q[3:2], b = rgb_q[1:0];
  assign uo_out = {hsync, b[0], g[0], r[0], vsync, b[1], g[1], r[1]};

  // Beep on uio[7] for half a second every second: a square wave that flips every
  // 32 lines (about 490 Hz), so the sound is locked to the picture
  reg audio;
  always @(posedge clk) audio <= vpos[5] & frame[5];
  assign uio_out = {audio, 7'b0};
  assign uio_oe  = 8'b1000_0000;

  wire _unused = &{ena, ui_in, uio_in, 1'b0};

endmodule
