/*
 * Copyright (c) 2026 Tiny Tapeout B.V.
 * SPDX-License-Identifier: Apache-2.0
 *
 * Small designs with known output, used by test_vga_sim.py. By default: a white screen, with
 * colour and sync registered together.
 */

`default_nettype none

module test_core #(
    parameter CLOCKS_PER_PIXEL = 1,
    parameter H_VIS = 640, H_FP = 16, H_SYNC = 96, H_BP = 48,
    parameter V_VIS = 480, V_FP = 10, V_SYNC = 2, V_BP = 33,
    parameter ACTIVE_LOW = 1,
    parameter V_RESET = 0,  // line the counters start at after reset (0: top of the visible area)
    parameter SYNC = 1,  // 0: no sync at all
    parameter COLOUR_REGISTERED = 1,  // 0: colour from combinational logic, one clock ahead of sync
    parameter COLOUR_FROM_FRAME = 0,  // 1: frame k is drawn in colour k
    parameter RESET_FRAME_COUNTER = 1
) (
    input  wire       clk,
    input  wire       rst_n,
    output wire [7:0] uo_out
);
  localparam H_TOTAL = H_VIS + H_FP + H_SYNC + H_BP;
  localparam V_TOTAL = V_VIS + V_FP + V_SYNC + V_BP;

  reg [1:0] phase;
  reg [10:0] h, v;
  wire pixel_end = (phase == CLOCKS_PER_PIXEL - 1);
  always @(posedge clk) begin
    if (!rst_n) begin
      phase <= 0;
      h <= 0;
      v <= V_RESET;
    end else begin
      phase <= pixel_end ? 2'd0 : phase + 2'd1;
      if (pixel_end) begin
        if (h == H_TOTAL - 1) begin
          h <= 0;
          v <= (v == V_TOTAL - 1) ? 11'd0 : v + 11'd1;
        end else begin
          h <= h + 11'd1;
        end
      end
    end
  end

  wire visible = (h < H_VIS) && (v < V_VIS);
  wire in_hsync = (h >= H_VIS + H_FP) && (h < H_VIS + H_FP + H_SYNC);
  wire in_vsync = (v >= V_VIS + V_FP) && (v < V_VIS + V_FP + V_SYNC);
  wire hs = ACTIVE_LOW ? !in_hsync : in_hsync;
  wire vs = ACTIVE_LOW ? !in_vsync : in_vsync;

  reg [7:0] frame;
  always @(posedge clk) begin
    if (RESET_FRAME_COUNTER && !rst_n) frame <= 0;
    else if (pixel_end && h == 0 && v == V_VIS) frame <= frame + 8'd1;
  end

  wire [5:0] rgb = !visible ? 6'd0 : COLOUR_FROM_FRAME ? frame[5:0] : 6'h3f;

  reg [7:0] out_q;
  reg hs_q, vs_q;
  always @(posedge clk) begin
    out_q <= {hs, rgb[0], rgb[2], rgb[4], vs, rgb[1], rgb[3], rgb[5]};
    hs_q  <= hs;
    vs_q  <= vs;
  end

  assign uo_out = !SYNC ? 8'd0 :
                  COLOUR_REGISTERED ? out_q :
                  {hs_q, rgb[0], rgb[2], rgb[4], vs_q, rgb[1], rgb[3], rgb[5]};
endmodule

`define TEST_TOP(name, params) \
module name ( \
    input  wire [7:0] ui_in, output wire [7:0] uo_out, input wire [7:0] uio_in, \
    output wire [7:0] uio_out, output wire [7:0] uio_oe, \
    input  wire ena, input wire clk, input wire rst_n \
); \
  test_core params core (.clk(clk), .rst_n(rst_n), .uo_out(uo_out)); \
  assign uio_out = 0; \
  assign uio_oe = 0; \
  wire _unused = &{ena, ui_in, uio_in, 1'b0}; \
endmodule

`TEST_TOP(tt_um_test_640x480, #())
`TEST_TOP(tt_um_test_2_clocks_per_pixel, #(.CLOCKS_PER_PIXEL(2)))
`TEST_TOP(tt_um_test_800x600, #(.H_VIS(800), .H_FP(24), .H_SYNC(72), .H_BP(128), .V_VIS(600), .V_FP(1), .V_SYNC(2), .V_BP(22), .ACTIVE_LOW(0)))
`TEST_TOP(tt_um_test_active_high_sync, #(.ACTIVE_LOW(0)))
`TEST_TOP(tt_um_test_colour_one_clock_early, #(.COLOUR_REGISTERED(0)))
`TEST_TOP(tt_um_test_frame_counter_not_reset, #(.COLOUR_FROM_FRAME(1), .RESET_FRAME_COUNTER(0)))
`TEST_TOP(tt_um_test_no_sync, #(.SYNC(0)))
`TEST_TOP(tt_um_test_frame_number, #(.COLOUR_FROM_FRAME(1)))
`TEST_TOP(tt_um_test_reset_mid_frame, #(.V_RESET(240)))
