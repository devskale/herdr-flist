#!/usr/bin/env python3
"""Render ANSI terminal text to a PNG, terminal-style.

Used to produce a README screenshot from the filelist plugin's actual output.
Kept out of the plugin itself (it's a docs asset generator, not part of the
plugin). Run: ./_screenshot.py
"""
import os
import re
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")

# Terminal palette (matches the filelist plugin's color codes + a dark bg).
BG = (24, 24, 27)            # near-black, zinc-900-ish
FG = (229, 229, 234)         # light text
# ansi 16-color + bright variants (indices 0-15)
PALETTE = [
    (0, 0, 0), (193, 39, 45), (37, 161, 73), (192, 151, 33),
    (40, 112, 200), (157, 79, 187), (28, 154, 154), (197, 197, 197),
    (90, 90, 90), (240, 80, 80), (80, 210, 110), (230, 190, 70),
    (90, 160, 240), (200, 120, 230), (60, 200, 200), (245, 245, 245),
]

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
CELL_W, CELL_H = 9, 18
PAD_X, PAD_Y = 16, 14

SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")


def parse_cmap(sgr):
    """Return a dict of attrs from an SGR parameter string (cumulative)."""
    out = {}
    for part in sgr.split(";"):
        p = part.strip()
        if p == "" or p == "0":
            out.clear()
            continue
        n = int(p)
        if n == 1:
            out["bold"] = True
        elif 30 <= n <= 37:
            out["fg"] = n - 30
        elif n == 38:
            out["ext_fg"] = True
        elif n == 39:
            out.pop("fg", None); out.pop("ext_fg", None)
        elif 40 <= n <= 47:
            out["bg"] = n - 40
        elif n == 48:
            out["ext_bg"] = True
        elif n in (90,) or 90 <= n <= 97:
            out["fg"] = n - 90 + 8
    return out


def parse_line(line):
    """Yield (text, {attrs}) segments for one logical line.

    Handles a trailing 38;5;N or 48;5;N by consuming the next two params inline.
    """
    pos = 0
    state = {}
    segments = []
    for m in SGR_RE.finditer(line):
        if m.start() > pos:
            segments.append((line[pos:m.start()], dict(state)))
        params = m.group(1)
        toks = params.split(";")
        i = 0
        while i < len(toks):
            t = toks[i].strip()
            if t == "" or t == "0":
                state.clear(); i += 1; continue
            n = int(t)
            if n == 1:
                state["bold"] = True; i += 1
            elif n in (38, 48) and i + 1 < len(toks) and toks[i + 1] == "5":
                color = int(toks[i + 2]) if i + 2 < len(toks) else 0
                key = "fg" if n == 38 else "bg"
                state[key] = color; i += 3
            elif 30 <= n <= 37:
                state["fg"] = n - 30; i += 1
            elif 90 <= n <= 97:
                state["fg"] = n - 90 + 8; i += 1
            elif n in (39, 49):
                state.pop("fg" if n == 39 else "bg", None); i += 1
            elif n == 0:
                state.clear(); i += 1
            else:
                i += 1
        pos = m.end()
    if pos < len(line):
        segments.append((line[pos:], dict(state)))
    if not segments:
        segments.append(("", dict(state)))
    return segments


def color_for(idx, bold=False):
    if idx < 0 or idx > 255:
        return FG
    if idx < 16:
        c = PALETTE[idx]
    else:
        # xterm 256 -> rgb
        if idx < 52:
            v = (idx - 16) // 36 * 0 + 0
            r = 8 if False else 0
        # standard 6x6x6 cube
        i = idx - 16
        if i < 216:
            r, g, b = (i // 36), ((i // 6) % 6), (i % 6)
            levels = [0, 95, 135, 175, 215, 255]
            c = (levels[r], levels[g], levels[b])
        else:
            g = 8 + (i - 216) * 10
            c = (g, g, g)
    if bold and idx < 8:
        c = tuple(min(255, int(x * 1.15) + 40) for x in c)
    return c


def draw_lines(draw, lines, x0, y0, col_w, font):
    """Render ANSI lines into a column of width col_w (cells), wrapping long lines."""
    row = 0
    for line in lines:
        # wrap long lines to col_w cells by char count (good enough for mono)
        wrapped = []
        if not line:
            wrapped = [""]
        else:
            # split into segments preserving ANSI state is complex; approximate
            # by wrapping on a soft limit. Filelist lines are short, so rare.
            plain = SGR_RE.sub("", line)
            limit = col_w
            if len(plain) <= limit:
                wrapped = [line]
            else:
                # naive wrap on the plain length
                wrapped = [line]
        for wline in wrapped:
            x = x0
            y = y0 + row * CELL_H
            for text, attrs in parse_line(wline):
                if not text:
                    continue
                fg = color_for(attrs.get("fg", -1), attrs.get("bold"))
                bg = color_for(attrs.get("bg", -2)) if "bg" in attrs else BG
                for ch in text:
                    w = font.getlength(ch)
                    if bg != BG:
                        draw.rectangle([x, y, x + w - 1, y + CELL_H - 1], fill=bg)
                    draw.text((x, y - 2), ch, fill=fg, font=font)
                    x += w
            row += 1
    return row


def render(pane_id, out_path, title=None):
    if os.path.exists(pane_id):
        ansi = open(pane_id).read()
    elif pane_id == "-":
        ansi = sys.stdin.read()
    else:
        ansi = subprocess.run(
            [HERDR, "pane", "read", pane_id, "--source", "visible", "--ansi"],
            capture_output=True, text=True,
        ).stdout
    ansi = ansi.replace("\r", "")
    lines = [l for l in ansi.split("\n") if l.strip()]

    font = ImageFont.truetype(FONT_PATH, 14)
    bold_font = ImageFont.truetype(FONT_PATH, 14)

    show_title = title is not None
    title_h = 26 if show_title else 0
    rows = len(lines) + (1 if show_title else 0)
    width = PAD_X * 2 + 72 * CELL_W
    height = PAD_Y * 2 + title_h + max(rows, 1) * CELL_H

    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, width - 1, height - 1], outline=(60, 60, 66))

    if show_title:
        for i, col in enumerate([(220, 80, 80), (220, 180, 70), (90, 200, 100)]):
            cx = 16 + i * 16
            draw.ellipse([cx, 9, cx + 10, 19], fill=col)
        tw = draw.textlength(title, font=bold_font)
        draw.text(((width - tw) / 2, 7), title, fill=(200, 200, 210), font=bold_font)

    draw_lines(draw, lines, PAD_X, PAD_Y + title_h, 72, font)
    img.save(out_path)
    print(f"wrote {out_path} ({width}x{height})")


def render_composite(editor_path, flist_ansi_path, out_path, title=None,
                     editor_w=96, flist_w=40):
    """Draw an editor pane (plain text) + filelist sidebar (ANSI) side by side.

    This is a composed representation of the herdr layout — herdr has no tab-
    level screen capture, so we render the two panes from their real content.
    """
    editor = open(editor_path).read().replace("\r", "").split("\n")
    editor = [l for l in editor if l.strip()]
    flist = open(flist_ansi_path).read().replace("\r", "").split("\n")
    flist = [l for l in flist if l.strip()]

    font = ImageFont.truetype(FONT_PATH, 14)
    bold_font = ImageFont.truetype(FONT_PATH, 14)

    show_title = title is not None
    title_h = 26 if show_title else 0
    rows = max(len(editor), len(flist), 8) + (1 if show_title else 0)
    # layout: pad | editor (editor_w) | gap+divider | flist (flist_w) | pad
    gap = 8
    width = PAD_X + editor_w * CELL_W + gap + 1 + gap + flist_w * CELL_W + PAD_X
    height = PAD_Y * 2 + title_h + rows * CELL_H

    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, width - 1, height - 1], outline=(60, 60, 66))

    if show_title:
        for i, col in enumerate([(220, 80, 80), (220, 180, 70), (90, 200, 100)]):
            cx = 16 + i * 16
            draw.ellipse([cx, 9, cx + 10, 19], fill=col)
        tw = draw.textlength(title, font=bold_font)
        draw.text(((width - tw) / 2, 7), title, fill=(200, 200, 210), font=bold_font)

    y0 = PAD_Y + title_h
    # editor pane (plain text, dimmed slightly)
    editor_x = PAD_X
    for r, line in enumerate(editor[:rows]):
        draw.text((editor_x, y0 + r * CELL_H - 2), line[:editor_w],
                  fill=(215, 215, 220), font=font)
    # vertical divider
    div_x = PAD_X + editor_w * CELL_W + gap // 2
    draw.line([div_x, y0, div_x, y0 + rows * CELL_H], fill=(60, 60, 66))
    # filelist sidebar (ANSI)
    flist_x = div_x + gap + 1 + gap
    draw_lines(draw, flist, flist_x, y0, flist_w, font)
    img.save(out_path)
    print(f"wrote {out_path} ({width}x{height})")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--composite":
        # _screenshot.py --composite <editor.txt> <flist.ansi> <out.png> [title]
        render_composite(sys.argv[2], sys.argv[3], sys.argv[4],
                         sys.argv[5] if len(sys.argv) > 5 else None)
    else:
        pane = sys.argv[1]
        out = sys.argv[2]
        title = sys.argv[3] if len(sys.argv) > 3 else None
        render(pane, out, title)
