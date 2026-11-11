# edit_fixations.py

Interactive editor for correcting fixation positions in EyeLink ASC files.
It shows the fixations of each trial on top of the stimulus image, lets you
move or delete them, and saves the result to a new ASC file. The original
file is never changed.

## Starting

Double click `edit_fixations.exe`, or run the Python version:

```bash
python3 edit_fixations.py
```

A dialog asks for four things:

| Field | What to select | Required |
|---|---|---|
| ASC file | The participant's `.asc` file, e.g. `dat/op2_11_001.asc` | yes |
| Screenshots folder | The folder with the stimulus images, `img/` | yes |
| OBT folder | The folder with the word position files, `obt/` | no, but needed for word boxes and auto-align |
| Output ASC file | Where to save the corrected file | yes |

Once you select the ASC file, the rest is filled in automatically where
possible:

- **Screenshots and OBT folders:** if `img/` and `obt/` sit next to the
  folder of the ASC file (as in `dat/`, `img/`, `obt/`), they are found
  automatically.

- **Output file:** the ASC name with `_corrected` added, in the same folder
  (e.g. `op2_11_001_corrected.asc`).

- **Continuing earlier work:** if a `_corrected` file already exists, it is
  opened instead of the original, so you continue where you left off. The
  dialog says so in orange.

The images and OBT files are matched to trials by the `plafile` message in
the ASC file: `plafile MA_MI027.bmp` uses `img/MA_MI027.bmp` (or `.png`) and
`obt/MA_MI027.obt`. Trials without an image or without fixations (e.g. the
filler trials) are skipped. If an experimental trial has no image, a warning
lists it.

You can also skip the dialog by giving all paths on the command line:

```bash
python3 edit_fixations.py --asc ../dat/op2_11_001.asc --screenshots ../img --obt ../obt --out ../dat/op2_11_001_corrected.asc
```

## What you see

- **Fixations:** circles numbered in reading order, coloured from blue
  (first) to red (last) and connected by the scanpath.

- **Colours:** yellow is the selected fixation, magenta the box-selected
  ones, orange the moved ones.

- **Word boxes:** dashed cyan rectangles from the OBT file.

- **Status bar:** position, duration, pupil size and the word under the
  fixation you hover over or select. Fixations next to a blink are marked
  `BLINK`.

## Controls

### Navigation

| Action | Control |
|---|---|
| Previous / next fixation (when one is selected) | Left / Right arrow |
| Previous / next trial (when nothing is selected) | Left / Right arrow |
| Previous / next trial (always) | Ctrl+Left / Ctrl+Right, or Prev / Next buttons |
| Zoom in / out | Mouse wheel |
| Deselect fixation and clear box selection | Escape |
| Show all controls | F1 or Help button |

### Editing

| Action | Control |
|---|---|
| Select a fixation | Click it |
| Move a fixation | Drag it |
| Box-select fixations | Drag on empty space |
| Move all fixations vertically | Shift+drag any fixation |
| Move all fixations horizontally | Z+drag any fixation |
| Automatic drift correction | `A` or Auto-align button |
| Delete the selected fixation | `Delete` or Delete button |
| Undo | Ctrl+Z or Undo button (per trial) |
| Reset the trial to the original positions | `R` or Reset button |
| Show / hide word boxes | `B` or Word AOIs button |
| Save | Ctrl+S or Save button |

**Box selection:** when some fixations are box-selected, Shift+drag,
Z+drag and auto-align act only on them. This helps when only part of the
page has drifted. Escape or a click on empty space clears the selection.

**Undo** works per trial and is cleared when you move to another trial.
Reset cannot be undone.

## Auto-align

Auto-align looks for the vertical shift that puts the most fixations on the
text lines and applies it. A fixation counts as on a line when it is within
a quarter of the line spacing of the line's middle and horizontally within
the line (a few characters before the first word are allowed, since
undershoot at the start of a line is normal).

- **Vertical shift:** searched up to one line spacing up or down. Larger
  drift has to be corrected by hand with Shift+drag.

- **Vertical scaling:** on texts with at least 4 lines, it also tries
  stretching or compressing the fixations vertically (x0.70 to x1.30). This
  is used only when it puts at least 10% more fixations on lines than a
  plain shift.

- **Horizontal shift:** applied only when clearly needed (at least 50 px
  and at least 20% more fixations on lines). A smaller one is mentioned in
  the status bar but not applied.

- **Fixations used:** only those made while the text was on screen, from
  the first `DISPLAY ON` message to the question screen or the end of the
  trial.

The status bar reports the result, e.g.
`dy=+40 px; fixations on lines 17% -> 97%`. It is a suggestion: check it on
screen, then save it or undo it with Ctrl+Z. Auto-align needs the OBT
folder, since the line positions come from the OBT files.

## Saving

Saving writes the whole ASC file to the output file:

- **Moved fixations:** the `EFIX` line gets the new x and y. Times,
  duration and pupil size are unchanged.

- **Deleted fixations:** the `EFIX` line is commented out as
  `## DELETED EFIX ...`, keeping the original values.

- **Saccades:** the start and end points of an `ESACC` line are updated
  when the fixation before or after it was moved. Amplitude and peak
  velocity are kept as recorded.

- **Everything else** (messages, blinks, raw samples) is unchanged. The raw
  samples are therefore not corrected: use the `EFIX` lines for analysis.

A short "Saved ✓" appears next to the Save button and a summary of the
changes in the status bar.

## Requirements for the Python version

- Python 3.7 or newer

- Pillow (`pip install pillow`)

- tkinter (included with standard Python on most systems)
