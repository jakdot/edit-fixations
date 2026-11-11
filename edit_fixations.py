#!/usr/bin/env python3
"""
Interactive fixation editor for EyeLink ASC files.

Controls
--------
  Left / Right arrow     : previous / next fixation (when one is selected)
                           previous / next trial (when nothing is selected)
  Ctrl+Left / Right      : previous / next trial (always)
  Click fixation         : select it (turns yellow, x/y/AOI shown in status bar)
  Drag fixation          : move it to a new position
  Drag on empty space    : box-select the fixations inside the box (they turn magenta)
  Shift+drag fixation    : move all fixations vertically (vertical drift correction);
                           only the box-selected ones if there is a box selection
  Z+drag fixation        : move all fixations horizontally (horizontal drift correction);
                           only the box-selected ones if there is a box selection
  Escape                 : deselect the fixation and clear the box selection
  Delete (key or button) : remove the selected fixation from the file
  A (key or button)      : auto-align: shift (and if needed vertically scale) the
                           fixations so that most land on text lines; only the
                           box-selected ones if there is a box selection
  Ctrl+Z                 : undo last move, delete or auto-align (per trial)
  R                      : reset current trial to original positions
  B                      : toggle word AOI boxes on/off
  Mouse wheel            : zoom in / out
  Ctrl+S                 : save corrected ASC file

Output
------
  A new ASC file (default: 001_corrected.asc) where moved fixations have
  updated coordinates and deleted fixations are commented out.
"""

from __future__ import annotations

import argparse
import bisect
import re
from pathlib import Path

import tkinter as tk
from tkinter import messagebox

from PIL import Image, ImageTk

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FIX_R_PX = 18     # fixed circle radius for all fixations (in image-pixel space)


def dur_to_radius(dur_ms: int) -> float:
    return FIX_R_PX


def fix_color(index: int, total: int, modified: bool, selected: bool, dur_ms: int = 200,
              grouped: bool = False) -> str:
    if selected:
        return "#ffff00"
    if grouped:
        return "#ff44ff"
    if modified:
        return "#ff8800"
    # Color encodes position in sequence: first = blue, last = red
    t = index / max(1, total - 1)
    r = int(30 + t * 225)
    g = int(60 * (1 - t))
    b = int(220 * (1 - t))
    return f"#{r:02x}{g:02x}{b:02x}"


# ---------------------------------------------------------------------------
# Automatic alignment
# ---------------------------------------------------------------------------

# Fixations are fitted to text lines, not word boxes: a fixation counts as on a
# line when it lies close to the line's vertical middle and within the line's
# horizontal extent. Vertical settings are fractions of the line pitch (the
# distance between consecutive lines in the OBT file), so they adapt to the layout.
ALIGN_DY_STEP = 5               # px between tried vertical shifts (range: +-1 pitch)
ALIGN_TOL = 0.25                # vertical tolerance around a line's middle, in pitches
ALIGN_UNDERSHOOT_CHARS = 3      # allowed undershoot left of a line's first word, in characters
ALIGN_DX = range(-60, 61, 10)   # tried horizontal shifts, px
ALIGN_MIN_FIX = 10
# Horizontal correction only when clearly needed: it is more subjective, and
# fixations left of a line's first word (undershoot) are normal reading
ALIGN_SHIFT_MIN_H = 50          # px
ALIGN_GAIN_MIN_H = 0.20         # extra share of fixations on lines
# Vertical scaling, for recordings whose calibration stretches or compresses
# the fixations vertically: used only when it clearly beats a plain shift, and
# only on pages with enough text lines to tell a stretch from a shift
ALIGN_SCALES = [round(0.70 + 0.05 * k, 2) for k in range(13) if k != 6]   # 0.70..1.30 without 1.0
ALIGN_GAIN_MIN_SCALE = 0.10     # extra share of fixations on lines over the best shift
ALIGN_SCALE_MIN_LINES = 4


def text_lines(words: list[dict]) -> tuple[list[tuple], float]:
    """Return (lines, pitch) built from the OBT words.

    Each line is (y_mid, x1, x2): the middle of its OBT band and its horizontal
    extent, with an undershoot margin on the left. pitch is the median distance
    between consecutive line middles (the band height if there is one line).
    """
    by_line: dict[int, list[dict]] = {}
    for w in words:
        by_line.setdefault(w["line"], []).append(w)
    if not by_line:
        return [], 0.0
    # OBT width includes the space after the word, length does not
    char_ws = sorted(w["width"] / (w["length"] + 1) for w in words if w["width"] > 0)
    char_w = char_ws[len(char_ws) // 2] if char_ws else 0.0
    lines = []
    for ws in by_line.values():
        y_mid = sum(w["y"] + w["height"] / 2 for w in ws) / len(ws)
        x1 = min(w["x"] for w in ws) - ALIGN_UNDERSHOOT_CHARS * char_w
        x2 = max(w["x"] + w["width"] for w in ws)
        lines.append((y_mid, x1, x2))
    lines.sort()
    gaps = sorted(b[0] - a[0] for a, b in zip(lines, lines[1:]))
    if gaps:
        pitch = gaps[len(gaps) // 2]
    else:
        pitch = sorted(w["height"] for w in words)[len(words) // 2]
    return lines, pitch


def share_on_lines(lines: list[tuple], tol: float, points: list[tuple], dx: float, dy: float,
                   scale: float = 1.0, y0: float = 0.0) -> float:
    """Share of points on a line after y -> y0 + (y - y0) * scale + dy, x -> x + dx."""
    mids = [ln[0] for ln in lines]
    hits = 0
    for x, y in points:
        x, y = x + dx, y0 + (y - y0) * scale + dy
        k = bisect.bisect_left(mids, y)
        # nearest line middle (tol is below half a pitch, so it is the only candidate)
        near = min((j for j in (k - 1, k) if 0 <= j < len(lines)),
                   key=lambda j: abs(mids[j] - y))
        y_mid, x1, x2 = lines[near]
        if abs(y - y_mid) <= tol and x1 <= x <= x2:
            hits += 1
    return hits / len(points)


def best_along(shifts, score) -> tuple[float, int]:
    """Return (best score, shift); smaller shifts win ties."""
    best = None
    for d in sorted(shifts, key=abs):
        v = score(d)
        if best is None or v > best[0]:
            best = (v, d)
    return best


def best_shift(lines: list[tuple], pitch: float, points: list[tuple]) -> dict:
    """Fit the vertical correction first, then a horizontal shift on top of it.

    Vertically, the best shift dy within +-1 pitch is found; on pages with
    enough lines, a vertical scaling around the middle of the text block (y0)
    with its own best shift replaces it if it puts clearly more points on lines.

    Returns share0 (share of points on lines now), scale, y0, dy and share_v
    (after the vertical correction), dx and share_h (after both), and
    apply_dx: whether the horizontal shift passes the stricter criterion.
    """
    tol = ALIGN_TOL * pitch
    dys = range(-int(pitch), int(pitch) + 1, ALIGN_DY_STEP)
    share0 = share_on_lines(lines, tol, points, 0, 0)
    y0 = (lines[0][0] + lines[-1][0]) / 2
    share_v, dy = best_along(dys, lambda d: share_on_lines(lines, tol, points, 0, d))
    scale = 1.0
    if len(lines) >= ALIGN_SCALE_MIN_LINES:
        best = None
        for sc in ALIGN_SCALES:
            v, d = best_along(dys,
                              lambda d: share_on_lines(lines, tol, points, 0, d, sc, y0))
            if best is None or v > best[0]:
                best = (v, sc, d)
        if best[0] >= share_v + ALIGN_GAIN_MIN_SCALE:
            share_v, scale, dy = best
    share_h, dx = best_along(ALIGN_DX,
                             lambda d: share_on_lines(lines, tol, points, d, dy, scale, y0))
    apply_dx = abs(dx) >= ALIGN_SHIFT_MIN_H and share_h - share_v >= ALIGN_GAIN_MIN_H
    return dict(share0=share0, scale=scale, y0=y0, dy=dy, share_v=share_v,
                dx=dx, share_h=share_h, apply_dx=apply_dx)


# ---------------------------------------------------------------------------
# ASC parser
# ---------------------------------------------------------------------------

# EFIX line format (verified against SR Research documentation):
#   EFIX  eye  stime  etime  dur  axp  ayp  aps  [xr  yr]
#   - stime : fixation start time (ms)
#   - etime : fixation end time (ms)
#   - dur   : fixation duration (ms)
#   - axp   : average x gaze position (pixels)
#   - ayp   : average y gaze position (pixels)
#   - aps   : average pupil size
#   - xr/yr : x/y resolution in pixels/degree (optional, absent here)
# Sources:
#   https://www.sr-research.com/download/dispdoc/page25.html
#   https://cran.r-project.org/web/packages/eyelinker/vignettes/format.html
EFIX_RE = re.compile(
    r"^EFIX\s+R\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)"
)
ESACC_RE = re.compile(
    r"^ESACC\s+R\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)"
)
EBLINK_RE = re.compile(r"^EBLINK\s+R\s+(\d+)\s+(\d+)\s+(\d+)")

TRIALBEG_RE = re.compile(r"^MSG\s+\d+\s+trialbeg\s+(\d+)\s+(\d+)\s+\d+\s+(\S+)")
TRIALEND_RE = re.compile(r"^MSG\s+(\d+)\s+trialend")
DISPLAY_RE  = re.compile(r"^MSG\s+(\d+)\s+-?\d+\s+DISPLAY ON")
PLAFILE_RE  = re.compile(r"^MSG\s+\d+\s+plafile\s+(\S+)")


def _find_image(screenshots_dir: Path, stem: str) -> Path | None:
    """Return the image path for stem, trying .bmp then .png. Returns None if neither exists."""
    for ext in (".bmp", ".png"):
        p = screenshots_dir / f"{stem}{ext}"
        if p.exists():
            return p
    return None


def parse_asc(asc_path: Path, screenshots_dir: Path) -> tuple[list[str], list[dict], list[str]]:
    """Return (raw_lines, valid_trials, missing_images).

    Each trial dict has keys:
        trial_id, stim_id, condition, image_path, onset, end, fixations, saccades, blinks
    onset is the first DISPLAY ON (the text appears); end is the next DISPLAY ON
    (the question screen) or else trialend.

    Each fixation dict has keys:
        line_no, start, end, dur, x, y, pupil,
        x0, y0  (originals for reset),
        deleted, modified, blink

    missing_images is a list of stim_ids for which no image file was found (non-FILL trials only).
    """
    with asc_path.open(encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    all_trials: list[dict] = []
    current: dict | None = None

    for i, raw in enumerate(lines):
        s = raw.rstrip("\n")

        m = TRIALBEG_RE.match(s)
        if m:
            if current is not None:
                all_trials.append(current)
            current = dict(
                trial_id=int(m.group(2)),   # sequential trial number
                stim_id=m.group(1),         # stimulus number within condition
                condition=m.group(3),       # e.g. MA_MI, MI_MA, FILL
                image_path=None, onset=None, end=None,
                fixations=[], saccades=[], blinks=[],
            )
            continue

        if current is None:
            continue

        m = PLAFILE_RE.match(s)
        if m:
            stem = Path(m.group(1)).stem   # e.g. "MA_MI027" from "MA_MI027.bmp"
            current["image_path"] = _find_image(screenshots_dir, stem)
            current["stim_id"] = stem      # use full stem (condition+number) as stim_id
            continue

        m = DISPLAY_RE.match(s)
        if m:
            if current["onset"] is None:
                current["onset"] = int(m.group(1))
            elif current["end"] is None:
                current["end"] = int(m.group(1))
            continue

        m = EFIX_RE.match(s)
        if m:
            x, y = float(m.group(4)), float(m.group(5))
            current["fixations"].append(dict(
                line_no=i,
                start=int(m.group(1)), end=int(m.group(2)), dur=int(m.group(3)),
                x=x, y=y, pupil=float(m.group(6)),
                x0=x, y0=y,
                deleted=False, modified=False,
            ))
            continue

        m = ESACC_RE.match(s)
        if m:
            current["saccades"].append(dict(
                line_no=i,
                start=int(m.group(1)), end=int(m.group(2)), dur=int(m.group(3)),
                x1=float(m.group(4)), y1=float(m.group(5)),
                x2=float(m.group(6)), y2=float(m.group(7)),
                amplitude=m.group(8), peak_vel=m.group(9),
            ))
            continue

        m = EBLINK_RE.match(s)
        if m:
            current["blinks"].append(i)   # store line_no only; used for adjacency
            continue

        m = TRIALEND_RE.match(s)
        if m:
            if current["end"] is None:
                current["end"] = int(m.group(1))
            if current is not None:
                all_trials.append(current)
            current = None

    if current is not None:
        all_trials.append(current)

    for trial in all_trials:
        blink_lines = trial["blinks"]   # list of EBLINK line numbers
        fixations = trial["fixations"]
        for fix in fixations:
            fix["blink"] = False
        if not blink_lines or not fixations:
            continue
        fix_lns = [f["line_no"] for f in fixations]
        for bln in blink_lines:
            # Fixation immediately before this blink in the file
            prev_idx = next((k for k in range(len(fix_lns) - 1, -1, -1) if fix_lns[k] < bln), None)
            # Fixation immediately after this blink in the file
            next_idx = next((k for k, ln in enumerate(fix_lns) if ln > bln), None)
            if prev_idx is not None:
                fixations[prev_idx]["blink"] = True
            if next_idx is not None:
                fixations[next_idx]["blink"] = True

    missing = [
        t["stim_id"] for t in all_trials
        if t["image_path"] is None and t["fixations"] and t["condition"] != "FILL"
    ]
    valid = [t for t in all_trials if t["image_path"] and t["fixations"]]
    return lines, valid, missing


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class FixationEditor:

    def __init__(self, root: tk.Tk, asc_path: Path, screenshots_dir: Path,
                 out_path: Path, obt_dir: Path | None = None):
        self.root = root
        self.asc_path = asc_path
        self.out_path = out_path
        self.obt_dir = obt_dir

        self.root.title(f"Fixation Editor  –  {asc_path.name}")
        self.root.geometry("1400x860")
        self.root.minsize(900, 600)

        # Parse
        self.raw_lines, self.trials, missing = parse_asc(asc_path, screenshots_dir)
        if not self.trials:
            messagebox.showerror("No trials", "No trials with screenshots found.")
            root.destroy()
            return
        if missing:
            messagebox.showwarning(
                "Missing images",
                f"No image file (.bmp or .png) found for {len(missing)} trial(s):\n"
                + "\n".join(missing),
            )

        # Display state
        self.trial_idx = 0
        self.zoom = 1.0           # 1.0 = fit-to-window; user can change
        self.fit_scale = 1.0      # scale that makes image fit the canvas
        self.img_x0 = 0           # top-left of image in canvas coords
        self.img_y0 = 0
        self._photo = None
        self._orig_pil = None     # PIL image for current trial (full res)

        # Interaction state
        self.show_words = True             # toggle word boxes (from OBT)
        self.box_expand_top = 0            # px to extend boxes upward
        self.box_expand_bot = 0            # px to extend boxes downward
        self.selected: int | None = None   # index into trial['fixations']
        self._drag_screen_start: tuple | None = None
        self._drag_fix_start: tuple | None = None
        self._drag_all: bool = False       # True when Shift is held (move all fixations vertically)
        self._drag_horiz: bool = False     # True when Z is held (move all fixations horizontally)
        self._z_held: bool = False         # True while the Z key is pressed
        self._drag_all_origins: list = []  # [(x0, y0), ...] for every fixation when drag started
        self.group: set[int] = set()       # box-selected fixations (indices into trial['fixations'])
        self._box_start: tuple | None = None   # screen coords where a box selection started
        self._undo_stack: list = []        # list of (index, old_x, old_y, was_deleted)
        self._status_note = ""             # message kept in the status bar (box selection)

        self._build_ui()
        self.root.update_idletasks()   # ensure canvas has real dimensions before first draw
        self._load_trial()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        # ---- Toolbar ----
        tb = tk.Frame(self.root, relief=tk.RAISED, bd=1, pady=5)
        tb.grid(row=0, column=0, columnspan=2, sticky="ew")

        tk.Button(tb, text="◀ Prev", width=8, command=self._prev).pack(side=tk.LEFT, padx=4)
        tk.Button(tb, text="Next ▶", width=8, command=self._next).pack(side=tk.LEFT, padx=4)

        self.lbl_trial = tk.Label(tb, text="", font=("Helvetica", 11, "bold"), width=50,
                                  anchor="w")
        self.lbl_trial.pack(side=tk.LEFT, padx=12)

        self.btn_del = tk.Button(tb, text="Delete fix  [Del]", command=self._delete_selected,
                                 state=tk.DISABLED, bg="#cc3333", fg="white", width=14)
        self.btn_del.pack(side=tk.LEFT, padx=4)

        tk.Button(tb, text="Reset trial  [R]", command=self._reset_trial,
                  width=14).pack(side=tk.LEFT, padx=4)

        tk.Button(tb, text="Undo  [Ctrl+Z]", command=self._undo,
                  width=14).pack(side=tk.LEFT, padx=4)

        tk.Button(tb, text="Auto-align  [A]", command=self._auto_align,
                  width=14).pack(side=tk.LEFT, padx=4)

        self.btn_words = tk.Button(tb, text="Word AOIs: ON  [B]", command=self._toggle_words,
                                   width=18, bg="#225566", fg="white")
        self.btn_words.pack(side=tk.LEFT, padx=4)

        tk.Label(tb, text=f"Box ↑: {self.box_expand_top}").pack(side=tk.LEFT, padx=(8, 4))
        tk.Label(tb, text=f"Box ↓: {self.box_expand_bot}").pack(side=tk.LEFT, padx=(4, 4))

        tk.Button(tb, text="Help  [F1]", command=self._show_help,
                  width=10).pack(side=tk.RIGHT, padx=4)

        self.btn_save = tk.Button(tb, text="Save ASC  [Ctrl+S]", command=self._save,
                                  bg="#1a55aa", fg="white",
                                  font=("Helvetica", 10, "bold"), width=16)
        self.btn_save.pack(side=tk.RIGHT, padx=10)

        self.lbl_saved = tk.Label(tb, text="", fg="#00aa00", width=8, anchor="e",
                                  font=("Helvetica", 10, "bold"))
        self.lbl_saved.pack(side=tk.RIGHT, padx=4)

        # ---- Canvas ----
        self.canvas = tk.Canvas(self.root, bg="#1a1a1a", cursor="crosshair",
                                highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky="nsew")

        vbar = tk.Scrollbar(self.root, orient=tk.VERTICAL, command=self.canvas.yview)
        vbar.grid(row=1, column=1, sticky="ns")
        hbar = tk.Scrollbar(self.root, orient=tk.HORIZONTAL, command=self.canvas.xview)
        hbar.grid(row=2, column=0, sticky="ew")
        self.canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)

        # ---- Status bar ----
        self.lbl_status = tk.Label(self.root, text="", anchor="w", bd=1,
                                   relief=tk.SUNKEN, font=("Courier", 10))
        self.lbl_status.grid(row=3, column=0, columnspan=2, sticky="ew")

        # ---- Bindings ----
        self.canvas.bind("<Configure>", lambda e: self._redraw())
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_hover)
        self.canvas.bind("<MouseWheel>", self._on_scroll)         # Windows / macOS
        self.canvas.bind("<Button-4>", self._on_scroll)           # Linux scroll up
        self.canvas.bind("<Button-5>", self._on_scroll)           # Linux scroll down

        self.root.bind("<Left>", lambda e: self._nav_left())
        self.root.bind("<Right>", lambda e: self._nav_right())
        self.root.bind("<Control-Left>", lambda e: self._prev())
        self.root.bind("<Control-Right>", lambda e: self._next())
        self.root.bind("<Delete>", lambda e: self._delete_selected())
        self.root.bind("<r>", lambda e: self._reset_trial())
        self.root.bind("<Control-s>", lambda e: self._save())
        self.root.bind("<Control-z>", lambda e: self._undo())
        self.root.bind("<Escape>", lambda e: self._escape())
        self.root.bind("<b>", lambda e: self._toggle_words())
        self.root.bind("<a>", lambda e: self._auto_align())
        self.root.bind("<KeyPress-z>", lambda e: setattr(self, "_z_held", True))
        self.root.bind("<KeyRelease-z>", lambda e: setattr(self, "_z_held", False))
        self.root.bind("<F1>", lambda e: self._show_help())

    # ------------------------------------------------------------------
    # Trial loading & drawing
    # ------------------------------------------------------------------

    def _load_words(self, trial: dict) -> list[dict]:
        """Load word bounding boxes from the OBT file for this trial, or [] if unavailable.

        OBT column layout (1-based):
          1  word text
          2  word length (chars, no spaces)
          3  object number in line
          4  number of objects in line
          5  line length
          6  object number in text
          7  line number
          8  number of lines in text
          9  stimulus number
          10 x1  upper-left x (pixels)
          11 y1  upper-left y (pixels)
          12 x2  x1 + width including spaces
          13 y2  y1 + height
          14 object code 1
          15 object code 2
        """
        if self.obt_dir is None or trial.get("stim_id") is None:
            return []
        obt_path = self.obt_dir / f"{trial['stim_id']}.obt"
        if not obt_path.exists():
            return []
        words = []
        with obt_path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 13:
                    continue
                x1, y1, x2, y2 = (int(parts[9]), int(parts[10]),
                                   int(parts[11]), int(parts[12]))
                code1 = int(parts[13])
                code2 = int(parts[14]) if len(parts) > 14 else 0
                region = code1 if code1 != 0 else code2
                words.append(dict(
                    text=parts[0],
                    x=x1, y=y1,
                    width=x2 - x1,
                    height=y2 - y1,
                    region=region,
                    length=int(parts[1]),
                    line=int(parts[6]),
                ))
        return words

    def _load_trial(self):
        self._status_note = ""
        self._deselect()
        self.group.clear()
        self._undo_stack.clear()
        trial = self.trials[self.trial_idx]
        trial["words"] = self._load_words(trial)
        self._orig_pil = Image.open(trial["image_path"]).convert("RGB")
        self.zoom = 1.0   # reset zoom to fit-to-window on each trial
        self._update_title()
        self._redraw()

    def _update_title(self):
        trial = self.trials[self.trial_idx]
        n_del = sum(1 for f in trial["fixations"] if f["deleted"])
        n_mod = sum(1 for f in trial["fixations"] if f["modified"])
        n_vis = sum(1 for f in trial["fixations"] if not f["deleted"])
        info = (f"Trial {trial['trial_id']}   {trial['stim_id']}   "
                f"{self.trial_idx + 1}/{len(self.trials)}   "
                f"fixations: {n_vis} shown")
        if n_del:
            info += f"  {n_del} deleted"
        if n_mod:
            info += f"  {n_mod} moved"
        self.lbl_trial.config(text=info)

    def _redraw(self):
        if self._orig_pil is None:
            return
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw <= 1 or ch <= 1:
            # Window not yet rendered; retry after layout pass
            self.root.after(50, self._redraw)
            return
        iw, ih = self._orig_pil.size

        # fit-to-window scale
        self.fit_scale = min(cw / iw, ch / ih)
        effective_scale = self.fit_scale * self.zoom

        disp_w = int(iw * effective_scale)
        disp_h = int(ih * effective_scale)

        # Centre the image in the canvas
        self.img_x0 = max(0, (cw - disp_w) // 2)
        self.img_y0 = max(0, (ch - disp_h) // 2)

        img_resized = self._orig_pil.resize((disp_w, disp_h), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(img_resized)

        self.canvas.delete("all")
        self.canvas.config(scrollregion=(0, 0, max(cw, disp_w), max(ch, disp_h)))
        self.canvas.create_image(self.img_x0, self.img_y0, anchor="nw", image=self._photo)

        self._draw_fixations(effective_scale)

    def _draw_fixations(self, effective_scale: float):
        self.canvas.delete("fix")
        self.canvas.delete("fix_lbl")
        self.canvas.delete("scanpath")
        self.canvas.delete("aoi")
        self.canvas.delete("word_box")
        trial = self.trials[self.trial_idx]

        # Word AOI boxes (from OBT)
        if self.show_words:
            for word in trial.get("words", []):
                sx1, sy1 = self._to_screen(
                    word["x"], word["y"] - self.box_expand_top)
                sx2, sy2 = self._to_screen(
                    word["x"] + word["width"], word["y"] + word["height"] + self.box_expand_bot)
                self.canvas.create_rectangle(
                    sx1, sy1, sx2, sy2,
                    outline="#00cccc", width=1, dash=(3, 3),
                    tags="word_box",
                )

        visible = [(i, f) for i, f in enumerate(trial["fixations"]) if not f["deleted"]]
        n = len(visible)

        # Scanpath polyline
        pts = []
        for _, fix in visible:
            sx, sy = self._to_screen(fix["x"], fix["y"])
            pts.extend([sx, sy])
        if len(pts) >= 4:
            self.canvas.create_line(pts, fill="#ffcc00", width=max(1, int(effective_scale)),
                                    tags="scanpath")

        # Find the sequence position of the selected fixation
        sel_seq = next((s for s, (oi, _) in enumerate(visible) if oi == self.selected), None)

        # Highlight lines to/from the selected fixation
        if sel_seq is not None:
            lw_hi = max(3, int(effective_scale * 2.5))
            sx_sel, sy_sel = self._to_screen(visible[sel_seq][1]["x"], visible[sel_seq][1]["y"])
            if sel_seq > 0:
                sx_p, sy_p = self._to_screen(visible[sel_seq - 1][1]["x"], visible[sel_seq - 1][1]["y"])
                self.canvas.create_line(sx_p, sy_p, sx_sel, sy_sel,
                                        fill="#ffff00", width=lw_hi, tags="scanpath")
            if sel_seq < n - 1:
                sx_n, sy_n = self._to_screen(visible[sel_seq + 1][1]["x"], visible[sel_seq + 1][1]["y"])
                self.canvas.create_line(sx_sel, sy_sel, sx_n, sy_n,
                                        fill="#ffff00", width=lw_hi, tags="scanpath")

        # Fixation circles + labels
        for seq, (orig_i, fix) in enumerate(visible):
            sx, sy = self._to_screen(fix["x"], fix["y"])
            r = max(8, int(dur_to_radius(fix["dur"]) * effective_scale))
            sel = (orig_i == self.selected)
            color = fix_color(seq, n, fix["modified"], sel, fix["dur"], orig_i in self.group)
            outline = "#ffffff" if not sel else "#ffff00"
            lw = 1 if not sel else 3
            tag = f"fix_{orig_i}"
            self.canvas.create_oval(sx - r, sy - r, sx + r, sy + r,
                                    fill=color, outline=outline, width=lw,
                                    tags=("fix", tag))
            fsize = max(7, int(10 * effective_scale))
            self.canvas.create_text(sx, sy, text=str(seq + 1),
                                    fill="white",
                                    font=("Helvetica", fsize, "bold"),
                                    tags=("fix_lbl", tag))

    # ------------------------------------------------------------------
    # Coordinate helpers
    # ------------------------------------------------------------------

    def _effective_scale(self) -> float:
        return self.fit_scale * self.zoom

    def _to_screen(self, ax: float, ay: float) -> tuple[float, float]:
        s = self._effective_scale()
        return self.img_x0 + ax * s, self.img_y0 + ay * s

    def _to_asc(self, sx: float, sy: float) -> tuple[float, float]:
        s = self._effective_scale()
        return (sx - self.img_x0) / s, (sy - self.img_y0) / s

    def _box_info(self, x: float, y: float) -> str:
        """Return which word and/or line AOI box contains point (x, y)."""
        trial = self.trials[self.trial_idx]
        parts = []

        # Word-level hit (report word text and region)
        for word in trial.get("words", []):
            y1_exp = word["y"] - self.box_expand_top
            y2_exp = word["y"] + word["height"] + self.box_expand_bot
            if word["x"] <= x <= word["x"] + word["width"] and y1_exp <= y <= y2_exp:
                region = word.get("region")
                region_str = f"  region {region}" if region else ""
                parts.append(f'word: "{word["text"]}"{region_str}')
                break  # report first match only

        if parts:
            return "  |  AOI: " + ",  ".join(parts)
        return "  |  AOI: none"

    def _hit_test(self, sx: float, sy: float) -> int | None:
        """Return index of fixation circle under (sx, sy) in canvas coords."""
        trial = self.trials[self.trial_idx]
        best_i, best_d = None, float("inf")
        s = self._effective_scale()
        for i, fix in enumerate(trial["fixations"]):
            if fix["deleted"]:
                continue
            fx, fy = self._to_screen(fix["x"], fix["y"])
            r = max(8, dur_to_radius(fix["dur"]) * s)
            d = ((sx - fx) ** 2 + (sy - fy) ** 2) ** 0.5
            if d <= r + 5 and d < best_d:
                best_d, best_i = d, i
        return best_i

    # ------------------------------------------------------------------
    # Mouse events
    # ------------------------------------------------------------------

    def _on_press(self, event):
        hit = self._hit_test(event.x, event.y)
        if hit is not None:
            self.selected = hit
            fix = self.trials[self.trial_idx]["fixations"][hit]
            self._drag_screen_start = (event.x, event.y)
            self._drag_fix_start = (fix["x"], fix["y"])
            self.btn_del.config(state=tk.NORMAL)
            self.lbl_status.config(text=self._fix_status(hit))
            # Shift held: vertical drift correction; Z held: horizontal drift correction
            self._drag_all = bool(event.state & 0x1)
            self._drag_horiz = self._z_held and not self._drag_all
            if self._drag_all or self._drag_horiz:
                self._drag_all_origins = [
                    (f["x"], f["y"]) for f in self.trials[self.trial_idx]["fixations"]
                ]
            else:
                self._drag_all_origins = []
        else:
            self._deselect()
            self._box_start = (event.x, event.y)
        self._draw_fixations(self._effective_scale())

    def _on_drag(self, event):
        if self._box_start is not None:
            bx, by = self._box_start
            self.canvas.delete("selbox")
            self.canvas.create_rectangle(bx, by, event.x, event.y, outline="#ff44ff",
                                         width=1, dash=(4, 2), tags="selbox")
            return
        if self.selected is None or self._drag_screen_start is None:
            return
        s = self._effective_scale()
        sx0, sy0 = self._drag_screen_start
        dx = (event.x - sx0) / s
        dy = (event.y - sy0) / s
        fixations = self.trials[self.trial_idx]["fixations"]
        who = f"{len(self.group)} box-selected fixations" if self.group else "all fixations"
        if self._drag_all:
            for i, fix in enumerate(fixations):
                if fix["deleted"] or (self.group and i not in self.group):
                    continue
                ox, oy = self._drag_all_origins[i]
                fix["x"] = ox
                fix["y"] = oy + dy
                fix["modified"] = True
            self.lbl_status.config(
                text=f"SHIFT-drag: {who} moved by ({dy:+.1f}) px vertically"
            )
        elif self._drag_horiz:
            for i, fix in enumerate(fixations):
                if fix["deleted"] or (self.group and i not in self.group):
                    continue
                ox, oy = self._drag_all_origins[i]
                fix["x"] = ox + dx
                fix["y"] = oy
                fix["modified"] = True
            self.lbl_status.config(
                text=f"Z-drag: {who} moved by ({dx:+.1f}) px horizontally"
            )
        else:
            fix_sel = fixations[self.selected]
            fix_sel["x"] = self._drag_fix_start[0] + dx
            fix_sel["y"] = self._drag_fix_start[1] + dy
            fix_sel["modified"] = True
            self.lbl_status.config(
                text=self._fix_status(self.selected, suffix="(dragging)")
            )
        self._draw_fixations(s)

    def _on_release(self, event):
        if self._box_start is not None:
            self._finish_box(event)
            return
        if self.selected is None or self._drag_screen_start is None:
            return
        fixations = self.trials[self.trial_idx]["fixations"]
        iw, ih = self._orig_pil.size
        if self._drag_all or self._drag_horiz:
            # The grabbed fixation may lie outside the box selection, so check them all
            if any(abs(f["x"] - ox) > 0.5 or abs(f["y"] - oy) > 0.5
                   for f, (ox, oy) in zip(fixations, self._drag_all_origins)):
                self._undo_stack.append(("move_all", None, self._drag_all_origins, None))
            for fix in fixations:
                if not fix["deleted"]:
                    fix["x"] = max(0.0, min(iw, fix["x"]))
                    fix["y"] = max(0.0, min(ih, fix["y"]))
        else:
            fix = fixations[self.selected]
            ox, oy = self._drag_fix_start
            if abs(fix["x"] - ox) > 0.5 or abs(fix["y"] - oy) > 0.5:
                self._undo_stack.append(("move", self.selected, ox, oy))
            fix["x"] = max(0.0, min(iw, fix["x"]))
            fix["y"] = max(0.0, min(ih, fix["y"]))
        self._drag_screen_start = None
        self._drag_all = False
        self._drag_horiz = False
        self._drag_all_origins = []
        self._update_title()
        self._draw_fixations(self._effective_scale())

    def _finish_box(self, event):
        """Box-select the fixations inside the dragged rectangle."""
        bx, by = self._box_start
        self._box_start = None
        self.canvas.delete("selbox")
        if abs(event.x - bx) < 4 and abs(event.y - by) < 4:
            # Plain click on empty space: clear the box selection
            if self.group:
                self.group.clear()
                self._status_note = ""
                self._draw_fixations(self._effective_scale())
            return
        x1, y1 = self._to_asc(min(bx, event.x), min(by, event.y))
        x2, y2 = self._to_asc(max(bx, event.x), max(by, event.y))
        fixations = self.trials[self.trial_idx]["fixations"]
        self.group = {i for i, f in enumerate(fixations)
                      if not f["deleted"] and x1 <= f["x"] <= x2 and y1 <= f["y"] <= y2}
        if self.group:
            self._status_note = (f"{len(self.group)} fixations box-selected: Shift+drag moves them "
                                 f"vertically, Z+drag horizontally, Esc clears the selection")
        else:
            self._status_note = "Box selection: no fixations in the box"
        self.lbl_status.config(text=self._status_note)
        self._draw_fixations(self._effective_scale())

    def _escape(self):
        self._deselect()
        if self.group:
            self.group.clear()
            self._status_note = ""
        self._draw_fixations(self._effective_scale())

    def _fix_status(self, idx: int, suffix: str = "") -> str:
        fix = self.trials[self.trial_idx]["fixations"][idx]
        flags = []
        if fix.get("blink"):
            flags.append("BLINK")
        if fix["modified"]:
            flags.append("moved")
        s = (f"Fix #{idx + 1}   x={fix['x']:.1f}   y={fix['y']:.1f}   "
             f"dur={fix['dur']} ms   pupil={fix['pupil']:.0f}")
        s += self._box_info(fix["x"], fix["y"])
        if flags:
            s += "   [" + ", ".join(flags) + "]"
        if suffix:
            s += "   " + suffix
        return s

    def _on_hover(self, event):
        if self._drag_screen_start is not None:
            return
        if self.selected is not None:
            return  # keep showing the selected fixation's info
        hit = self._hit_test(event.x, event.y)
        if hit is not None:
            self.lbl_status.config(text=self._fix_status(hit))
        else:
            self.lbl_status.config(text=self._status_note)

    def _on_scroll(self, event):
        # Determine direction
        if event.num == 4 or event.delta > 0:
            factor = 1.15
        else:
            factor = 1 / 1.15
        self.zoom = max(0.25, min(8.0, self.zoom * factor))
        self._redraw()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _toggle_words(self):
        self.show_words = not self.show_words
        if self.show_words:
            self.btn_words.config(text="Word AOIs: ON  [B]", bg="#225566")
        else:
            self.btn_words.config(text="Word AOIs: OFF  [B]", bg="#444444")
        self._draw_fixations(self._effective_scale())

    def _deselect(self):
        self.selected = None
        self._drag_screen_start = None
        self._drag_fix_start = None
        self.btn_del.config(state=tk.DISABLED)
        self.lbl_status.config(text="")

    def _delete_selected(self):
        if self.selected is None:
            return
        fix = self.trials[self.trial_idx]["fixations"][self.selected]
        self._undo_stack.append(("delete", self.selected, fix["x"], fix["y"]))
        fix["deleted"] = True
        fix["modified"] = False
        self._deselect()
        self._update_title()
        self._draw_fixations(self._effective_scale())

    def _undo(self):
        if not self._undo_stack:
            self.lbl_status.config(text="Nothing to undo.")
            return
        action, idx, ox, oy = self._undo_stack.pop()
        self._status_note = ""
        fixations = self.trials[self.trial_idx]["fixations"]
        if action == "move_all":
            # ox holds the list of original (x, y) for every fixation
            for i, fix in enumerate(fixations):
                fix["x"], fix["y"] = ox[i]
                fix["modified"] = (fix["x"] != fix["x0"] or fix["y"] != fix["y0"])
        elif action == "move":
            fix = fixations[idx]
            fix["x"], fix["y"] = ox, oy
            fix["modified"] = (fix["x"] != fix["x0"] or fix["y"] != fix["y0"])
        elif action == "delete":
            fix = fixations[idx]
            fix["deleted"] = False
            fix["x"], fix["y"] = ox, oy
            fix["modified"] = (ox != fix["x0"] or oy != fix["y0"])
        self._update_title()
        if self.selected is not None:
            self.lbl_status.config(text=self._fix_status(self.selected))
        self._draw_fixations(self._effective_scale())

    def _auto_align(self):
        """Shift the fixations by the (dx, dy) that puts the most of them on text lines.

        Works on the box-selected fixations if there are any, otherwise on all.
        The shift is fitted on those made while the text was shown (from the
        first DISPLAY ON to the question screen or trialend), then applied to
        all of them, like a Shift/Z-drag. It is a suggestion: Ctrl+Z undoes it,
        saving keeps it.
        """
        trial = self.trials[self.trial_idx]
        lines, pitch = text_lines(trial.get("words", []))
        if not lines:
            self.lbl_status.config(
                text="Auto-align: no OBT word boxes for this trial (is the OBT folder set?)")
            return
        fixations = trial["fixations"]
        targets = [i for i, f in enumerate(fixations)
                   if not f["deleted"] and (not self.group or i in self.group)]
        who = f"{len(targets)} box-selected fixations" if self.group else "all fixations"
        onset, end = trial.get("onset"), trial.get("end")
        points = [(fixations[i]["x"], fixations[i]["y"]) for i in targets
                  if (onset is None or fixations[i]["start"] >= onset)
                  and (end is None or fixations[i]["end"] <= end)]
        if len(points) < ALIGN_MIN_FIX:
            self.lbl_status.config(
                text=f"Auto-align: only {len(points)} fixations to fit, too few "
                     f"(need {ALIGN_MIN_FIX})")
            return
        fit = best_shift(lines, pitch, points)
        dy, scale, y0 = fit["dy"], fit["scale"], fit["y0"]
        dx = fit["dx"] if fit["apply_dx"] else 0
        share = fit["share_h"] if fit["apply_dx"] else fit["share_v"]
        note_h = ""
        if fit["dx"] and not fit["apply_dx"]:
            note_h = (f"  (horizontal dx={fit['dx']:+d} would add "
                      f"{fit['share_h'] - fit['share_v']:.0%}; not applied)")
        if (dx, dy, scale) == (0, 0, 1.0):
            self._status_note = (f"Auto-align: no vertical shift improves the fit "
                                 f"({fit['share0']:.0%} of fixations on lines){note_h}")
            self.lbl_status.config(text=self._status_note)
            return

        origins = [(f["x"], f["y"]) for f in fixations]
        iw, ih = self._orig_pil.size
        for i in targets:
            f = fixations[i]
            f["x"] = max(0.0, min(iw, f["x"] + dx))
            f["y"] = max(0.0, min(ih, y0 + (f["y"] - y0) * scale + dy))
            f["modified"] = (f["x"] != f["x0"] or f["y"] != f["y0"])
        self._undo_stack.append(("move_all", None, origins, None))
        self._update_title()
        self._draw_fixations(self._effective_scale())
        scaled = f"scaled vertically x{scale:.2f} and " if scale != 1.0 else ""
        self._status_note = (f"Auto-align: {who} {scaled}shifted by dx={dx:+d}, dy={dy:+d} px; "
                             f"fixations on lines {fit['share0']:.0%} -> {share:.0%}.  "
                             f"Ctrl+Z to undo, Ctrl+S to save.{note_h}")
        self.lbl_status.config(text=self._status_note)

    def _reset_trial(self):
        self._status_note = ""
        trial = self.trials[self.trial_idx]
        for fix in trial["fixations"]:
            fix["x"] = fix["x0"]
            fix["y"] = fix["y0"]
            fix["deleted"] = False
            fix["modified"] = False
        self._undo_stack.clear()
        self._deselect()
        self.group.clear()
        self._update_title()
        self._draw_fixations(self._effective_scale())

    def _nav_fixation(self, delta: int):
        """Move selection to the next/previous non-deleted fixation."""
        trial = self.trials[self.trial_idx]
        fixations = trial["fixations"]
        visible = [i for i, f in enumerate(fixations) if not f["deleted"]]
        if not visible:
            return
        if self.selected is None or self.selected not in visible:
            # Nothing selected: go to first or last depending on direction
            new_idx = visible[0] if delta > 0 else visible[-1]
        else:
            pos = visible.index(self.selected)
            pos = max(0, min(len(visible) - 1, pos + delta))
            new_idx = visible[pos]
        self.selected = new_idx
        self.btn_del.config(state=tk.NORMAL)
        self.lbl_status.config(text=self._fix_status(new_idx))
        self._draw_fixations(self._effective_scale())

    def _nav_left(self):
        if self.selected is not None:
            self._nav_fixation(-1)
        else:
            self._prev()

    def _nav_right(self):
        if self.selected is not None:
            self._nav_fixation(1)
        else:
            self._next()

    def _prev(self):
        if self.trial_idx > 0:
            self.trial_idx -= 1
            self._load_trial()

    def _next(self):
        if self.trial_idx < len(self.trials) - 1:
            self.trial_idx += 1
            self._load_trial()

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------

    def _show_help(self):
        win = tk.Toplevel(self.root)
        win.title("Keyboard & Mouse Bindings")
        win.resizable(False, False)
        win.transient(self.root)

        text = (
            "MOUSE\n"
            "─────────────────────────────────────────────\n"
            "  Click fixation          Select it\n"
            "  Drag fixation           Move it\n"
            "  Drag on empty space     Box-select the fixations inside the box (magenta)\n"
            "  Shift + drag fixation   Move ALL fixations vertically\n"
            "                          (only the box-selected ones, if any)\n"
            "  Z + drag fixation       Move ALL fixations horizontally\n"
            "                          (only the box-selected ones, if any)\n"
            "  Mouse wheel             Zoom in / out\n"
            "\n"
            "NAVIGATION\n"
            "─────────────────────────────────────────────\n"
            "  Left / Right            Previous / next fixation (when one is selected)\n"
            "                          Previous / next trial (when nothing is selected)\n"
            "  Ctrl + Left / Right     Previous / next trial (always)\n"
            "  Escape                  Deselect fixation and clear box selection\n"
            "\n"
            "EDITING\n"
            "─────────────────────────────────────────────\n"
            "  Delete                  Remove selected fixation\n"
            "  A                       Auto-align: shift (or scale) fixations onto text lines\n"
            "                          (only the box-selected ones, if any)\n"
            "  Ctrl + Z                Undo last move, delete or auto-align (per trial)\n"
            "  R                       Reset current trial to original positions\n"
            "\n"
            "DISPLAY\n"
            "─────────────────────────────────────────────\n"
            "  B                       Toggle word AOI boxes on / off\n"
            "\n"
            "FILE\n"
            "─────────────────────────────────────────────\n"
            "  Ctrl + S                Save corrected ASC file\n"
            "\n"
            "  F1                      Show this help\n"
        )

        frm = tk.Frame(win, padx=16, pady=12)
        frm.pack(fill=tk.BOTH, expand=True)

        lbl = tk.Label(frm, text=text, font=("Courier", 10), justify=tk.LEFT, anchor="w")
        lbl.pack(anchor="w")

        tk.Button(frm, text="Close", width=10, command=win.destroy,
                  default=tk.ACTIVE).pack(pady=(10, 0))
        win.bind("<Return>", lambda e: win.destroy())
        win.bind("<Escape>", lambda e: win.destroy())
        win.bind("<F1>", lambda e: win.destroy())

    # ------------------------------------------------------------------

    def _save(self):
        lines = list(self.raw_lines)

        n_moved = n_deleted = n_sacc_updated = 0
        for trial in self.trials:
            for fix in trial["fixations"]:
                if fix["deleted"]:
                    # Comment out the original EFIX line so no data is lost
                    lines[fix["line_no"]] = (
                        f"## DELETED  EFIX R  {fix['start']}\t{fix['end']}\t{fix['dur']}\t"
                        f"{fix['x0']:.1f}\t{fix['y0']:.1f}\t{fix['pupil']:.0f}\n"
                    )
                    n_deleted += 1
                elif fix["modified"]:
                    lines[fix["line_no"]] = (
                        f"EFIX R   {fix['start']}\t{fix['end']}\t{fix['dur']}\t"
                        f"  {fix['x']:.1f}\t  {fix['y']:.1f}\t   {fix['pupil']:.0f}\n"
                    )
                    n_moved += 1

            # Update saccade endpoints for any moved fixations.
            # Match by file position (line_no) rather than timestamp to avoid off-by-one
            # issues in EyeLink timing: the fixation immediately before a saccade in the
            # file corresponds to x1/y1, and the one immediately after to x2/y2.
            sorted_fixes = sorted(
                [(f["line_no"], f) for f in trial["fixations"] if not f["deleted"]],
                key=lambda x: x[0],
            )
            for sacc in trial.get("saccades", []):
                sacc_ln = sacc["line_no"]
                prev_fix = next(
                    (f for ln, f in reversed(sorted_fixes) if ln < sacc_ln), None
                )
                next_fix = next(
                    (f for ln, f in sorted_fixes if ln > sacc_ln), None
                )
                new_x1 = prev_fix["x"] if (prev_fix and prev_fix["modified"]) else sacc["x1"]
                new_y1 = prev_fix["y"] if (prev_fix and prev_fix["modified"]) else sacc["y1"]
                new_x2 = next_fix["x"] if (next_fix and next_fix["modified"]) else sacc["x2"]
                new_y2 = next_fix["y"] if (next_fix and next_fix["modified"]) else sacc["y2"]
                if new_x1 != sacc["x1"] or new_y1 != sacc["y1"] or new_x2 != sacc["x2"] or new_y2 != sacc["y2"]:
                    lines[sacc["line_no"]] = (
                        f"ESACC R   {sacc['start']}\t{sacc['end']}\t{sacc['dur']}\t"
                        f"  {new_x1:.1f}\t  {new_y1:.1f}\t  {new_x2:.1f}\t  {new_y2:.1f}\t"
                        f"   {sacc['amplitude']}\t   {sacc['peak_vel']}\n"
                    )
                    n_sacc_updated += 1

        with self.out_path.open("w", encoding="utf-8") as f:
            f.writelines(lines)

        msg = (f"Saved  –  {n_moved} moved, {n_deleted} deleted, "
               f"{n_sacc_updated} saccades updated  →  {self.out_path.name}")
        # Short confirmation in the toolbar; the full summary stays in the status bar
        self.lbl_saved.config(text="Saved ✓")
        self.root.after(5000, lambda: self.lbl_saved.config(text=""))
        self._status_note = msg
        self.lbl_status.config(text=msg)


# ---------------------------------------------------------------------------
# Startup dialog
# ---------------------------------------------------------------------------

class StartupDialog:
    """Small dialog to pick the ASC file, screenshots folder, and output path."""

    def __init__(self, root: tk.Tk, defaults: dict):
        self.root = root
        self.result: dict | None = None  # set to dict of paths on OK
        self._updating = False           # guard against trace recursion

        root.title("Fixation Editor – Open files")
        root.resizable(False, False)

        pad = dict(padx=10, pady=6)

        # ---- ASC file row ----
        tk.Label(root, text="ASC file:", anchor="w").grid(row=0, column=0, sticky="w", **pad)
        self.var_asc = tk.StringVar(value=defaults.get("asc", ""))
        tk.Entry(root, textvariable=self.var_asc, width=55).grid(row=0, column=1, **pad)
        tk.Button(root, text="Browse…", command=self._browse_asc).grid(row=0, column=2, **pad)

        # ---- Screenshots folder row ----
        tk.Label(root, text="Screenshots folder:", anchor="w").grid(row=1, column=0, sticky="w", **pad)
        self.var_shots = tk.StringVar(value=defaults.get("screenshots", ""))
        tk.Entry(root, textvariable=self.var_shots, width=55).grid(row=1, column=1, **pad)
        tk.Button(root, text="Browse…", command=self._browse_shots).grid(row=1, column=2, **pad)

        # ---- OBT folder row ----
        tk.Label(root, text="OBT folder:", anchor="w").grid(row=2, column=0, sticky="w", **pad)
        self.var_obt = tk.StringVar(value=defaults.get("obt", ""))
        tk.Entry(root, textvariable=self.var_obt, width=55).grid(row=2, column=1, **pad)
        tk.Button(root, text="Browse…", command=self._browse_obt).grid(row=2, column=2, **pad)

        # ---- Output file row ----
        tk.Label(root, text="Output ASC file:", anchor="w").grid(row=3, column=0, sticky="w", **pad)
        self.var_out = tk.StringVar(value=defaults.get("out", ""))
        tk.Entry(root, textvariable=self.var_out, width=55).grid(row=3, column=1, **pad)
        tk.Button(root, text="Browse…", command=self._browse_out).grid(row=3, column=2, **pad)

        # ---- Info label (shown when a corrected file is detected) ----
        self.lbl_info = tk.Label(root, text="", fg="#ffaa00", anchor="w")
        self.lbl_info.grid(row=4, column=0, columnspan=3, sticky="w", padx=10)

        # Auto-update output path whenever ASC path changes
        self.var_asc.trace_add("write", self._auto_out)

        # ---- Buttons ----
        btn_frame = tk.Frame(root)
        btn_frame.grid(row=5, column=0, columnspan=3, pady=10)
        tk.Button(btn_frame, text="Open", width=12, default=tk.ACTIVE,
                  command=self._ok).pack(side=tk.LEFT, padx=8)
        tk.Button(btn_frame, text="Cancel", width=12,
                  command=root.destroy).pack(side=tk.LEFT, padx=8)

        root.bind("<Return>", lambda e: self._ok())
        root.bind("<Escape>", lambda e: root.destroy())

        # Trigger auto-fill if a default ASC was supplied
        if defaults.get("asc"):
            self._auto_out()

    def _auto_out(self, *_):
        if self._updating:
            return
        asc = self.var_asc.get().strip()
        if not asc:
            return
        p = Path(asc)

        # Auto-fill sibling folders if not already set
        if not self.var_shots.get().strip():
            guess = p.parent / "../img"
            if guess.is_dir():
                self.var_shots.set(str(guess.resolve()))
        if not self.var_obt.get().strip():
            guess = p.parent / "../obt"
            if guess.is_dir():
                self.var_obt.set(str(guess.resolve()))

        corrected = p.with_name(p.stem + "_corrected" + p.suffix)
        self.var_out.set(str(corrected))

        # If a corrected version already exists, load that instead of the original
        if corrected.exists() and p.resolve() != corrected.resolve():
            self._updating = True
            self.var_asc.set(str(corrected))
            self._updating = False
            self.lbl_info.config(
                text=f"Corrected version found – loading {corrected.name} as input."
            )
        else:
            self.lbl_info.config(text="")

    def _browse_asc(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="Select ASC file",
            filetypes=[("ASC files", "*.asc"), ("All files", "*.*")],
        )
        if path:
            self.var_asc.set(path)

    def _browse_shots(self):
        from tkinter import filedialog
        path = filedialog.askdirectory(title="Select screenshots folder")
        if path:
            self.var_shots.set(path)

    def _browse_obt(self):
        from tkinter import filedialog
        path = filedialog.askdirectory(title="Select OBT folder")
        if path:
            self.var_obt.set(path)

    def _browse_out(self):
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(
            title="Save corrected ASC as",
            defaultextension=".asc",
            filetypes=[("ASC files", "*.asc"), ("All files", "*.*")],
        )
        if path:
            self.var_out.set(path)

    def _ok(self):
        asc = self.var_asc.get().strip()
        shots = self.var_shots.get().strip()
        obt = self.var_obt.get().strip()
        out = self.var_out.get().strip()
        if not asc or not shots or not out:
            messagebox.showwarning("Missing fields",
                                   "Please fill in ASC file, screenshots folder, and output file.")
            return
        if not Path(asc).exists():
            messagebox.showerror("File not found", f"ASC file not found:\n{asc}")
            return
        if not Path(shots).is_dir():
            messagebox.showerror("Folder not found", f"Screenshots folder not found:\n{shots}")
            return
        if obt and not Path(obt).is_dir():
            messagebox.showerror("Folder not found", f"OBT folder not found:\n{obt}")
            return
        self.result = {
            "asc": Path(asc),
            "screenshots": Path(shots),
            "obt": Path(obt) if obt else None,
            "out": Path(out),
        }
        self.root.destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Interactive EyeLink fixation editor")
    ap.add_argument("--asc", default=None, help="Input ASC file")
    ap.add_argument("--screenshots", default=None,
                    help="Directory containing {stim_id}.png screenshots")
    ap.add_argument("--obt", default=None, help="Directory containing {stim_id}.obt files")
    ap.add_argument("--out", default=None, help="Output corrected ASC file")
    args = ap.parse_args()

    # If all required args supplied on the command line, skip the dialog
    if args.asc and args.screenshots and args.out:
        asc = Path(args.asc)
        screenshots = Path(args.screenshots)
        obt = Path(args.obt) if args.obt else None
        out = Path(args.out)
    else:
        # Build sensible defaults for the dialog from whatever was supplied
        asc_default = args.asc or ""
        shots_default = args.screenshots or ""
        obt_default = args.obt or ""
        if asc_default:
            asc_p = Path(asc_default)
            if not shots_default:
                shots_guess = asc_p.parent / "../img"
                if shots_guess.is_dir():
                    shots_default = str(shots_guess.resolve())
            if not obt_default:
                obt_guess = asc_p.parent / "../obt"
                if obt_guess.is_dir():
                    obt_default = str(obt_guess.resolve())
        out_default = args.out or ""
        if asc_default and not out_default:
            p = Path(asc_default)
            out_default = str(p.with_name(p.stem + "_corrected" + p.suffix))

        dlg_root = tk.Tk()
        dlg = StartupDialog(dlg_root, {
            "asc": asc_default,
            "screenshots": shots_default,
            "obt": obt_default,
            "out": out_default,
        })
        dlg_root.mainloop()

        if dlg.result is None:
            return  # user cancelled
        asc = dlg.result["asc"]
        screenshots = dlg.result["screenshots"]
        obt = dlg.result["obt"]
        out = dlg.result["out"]

    root = tk.Tk()
    FixationEditor(root, asc, screenshots, out, obt_dir=obt)
    root.mainloop()


if __name__ == "__main__":
    main()
