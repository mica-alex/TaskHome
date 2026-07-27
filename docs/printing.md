# Printing

## Printer identity

| Constant | Value | Where |
| --- | --- | --- |
| Vendor ID | `0x04b8` (Epson) | `app.py:21` |
| Product ID | `0x0e27` (TM-T20III) | `app.py:22` |
| escpos profile | `'TM-T20II'` | `app.py:180,235` |

The physical printer is a TM-T20**III** on 80 mm paper; python-escpos (3.1) is
given the TM-T20**II** capability profile because it is the closest available
and works in practice. A commented-out line (`app.py:181`) shows an aborted
attempt to set `media_width_mm = 80` explicitly.

## Connection lifecycle

- `is_printer_connected()` is a pure presence probe:
  `usb.core.find(idVendor, idProduct) is not None`. It does not open the device.
- Each print goes through the `open_printer()` context manager, which
  constructs a fresh `Usb(VID, PID, profile='TM-T20II')` and **always** closes
  it, success or failure. Previously `close()` sat on the success path only, so
  any mid-receipt exception leaked the claimed interface — enough of those and
  the device stops opening until it is physically replugged (`P0-11`).
  An error raised while closing is logged but never masks the original error.
- A TOCTOU window remains between probe and open: the printer can vanish in
  between. That is handled by treating the print as failed rather than by
  trying to eliminate the race.

### Print functions return whether paper came out

`print_task()` and `print_scf_issue()` both return `True` only if a receipt was
genuinely produced, and `False` otherwise — disconnected printer, USB error,
malformed payload. This return value is load-bearing in three places:

- the scheduler advances a task's schedule **only** on `True` (`P0-4`);
- the SCF listener marks an issue as seen only on `True`, so an unprinted issue
  is retried next cycle;
- the test-print routes report the real outcome instead of always claiming
  success (`P0-10`).

History is written only after a receipt is out and the handle is closed.
History is the record of paper that exists — reprint-from-history depends on
that being true.

## Measured column widths

Determined by printing a ruler on the actual hardware
(`./scripts/calibrate_printer.py --confirm`) rather than trusting published
figures, because the published figures disagree:

| Font | Columns | Source |
| --- | --- | --- |
| `a` (default) | **48** | Measured. Confirms escpos's TM-T20II profile; the widely quoted 42 is wrong for this model |
| `b` (small) | **≥ 64** | Measured — did not wrap at 64. Re-run with `--width 96` to pin down the exact value |

This matters beyond trivia: MASTER_PLAN `P3-3`'s live receipt preview is only
honest if the browser models the same column count the printer uses. A preview
built on 42 columns would wrap text the printer would not, and vice versa.

Note the connected unit reports itself as **TM-T20III*L*** (the "L" variant),
while `PRINTER_MODEL` says `TM-T20III`. The escpos profile in use is
`TM-T20II`. All three disagree and it works anyway — ESC/POS is
forgiving here — but the measured numbers above are the ones to trust.

## `p.set(...)` cheat sheet

`p.set()` is python-escpos's character-formatting call; parameters used here:

| Param | Meaning |
| --- | --- |
| `align` | Horizontal alignment (`center` everywhere in this app) |
| `font` | `'a'` = 48 columns, `'b'` = 64+ columns on 80 mm — **measured on the actual unit**, see below |
| `bold` | Emphasis on/off |
| `custom_size=True, width=N, height=N` | Character-cell multipliers 1–8; `width=3, height=3` prints triple-size characters |
| `density` | Print density (darkness), 0–8; `4` used for QR/title blocks |

Note: `p.set()` is stateful — settings persist until the next `p.set()`. The
comments in `app.py` saying "italic" are aspirational; no italics are ever set
(ESC/POS has no italic in this profile).

## Receipt layouts

Layouts are **data**, not code: `layouts.py` returns a list of blocks, and
`receipt.py` renders that list either to the printer (`render_escpos`) or to
text (`render_text`). One definition drives both, so a preview cannot describe
something different from what prints — the property MASTER_PLAN `P3-4`'s live
preview depends on.

```python
import layouts, receipt
blocks = layouts.task_receipt(task, qr_url)
print(receipt.preview(blocks))          # ASCII, with a height in mm
receipt.render_escpos(blocks, printer)  # the same blocks, on paper
```

Block types: `text` (font, width/height multipliers, bold, align, optional
density and leading), `qr`, `barcode`, `rule`, `blank`, `gap` (sub-line space).

### Current defaults

```
task reminder                       SCF issue
  [QR, size 4]                        [QR, size 4]
  Title      font a, 2x, bold         Category   font a, 2x, bold
  (6-dot gap)                         (6-dot gap)
  Extra      font a, 1x               Address    font b
  Daily - Printed <time> - <id8>      Status - Reported - Photo
                                      ----
                                      Description   font b, left
                                      ----
                                      #id - Printed <time>
```

Roughly 40% shorter than the originals with no field removed. The reasoning
for each choice is in `layouts.py`; the previous designs are kept as
`legacy_*` so any change can be justified by a measured height difference.

### Line spacing — the part that is not obvious

`render_escpos` sets line spacing per block rather than using the printer
default, for two reasons found on paper:

- The default feed (~34 dots) is **shorter than a double-height font a cell**
  (48 dots), so a 2x title had the next line printed into its descenders.
- The printer **floors line spacing at the character height and silently
  clamps anything smaller**. Six different leading values printed identically
  until one exceeded that floor. `MIN_LINE_DOTS` exists for exactly this:
  below it, requests have no effect at all.

Also note `ESC 3 n` sets *n × the vertical motion unit*, which is 1/203 inch
here — so one unit is one dot, despite python-escpos naming the parameter
`divisor=180`.

Text is wrapped by `receipt.wrap()` before being sent. Letting the printer wrap
splits words mid-way (`"...5 cars g"` / `"o by at a time"`) because it
hard-wraps at the column limit, which made the preview disagree with the paper.

## Test prints

`/test_print` and `/test_scf_print` build hardcoded sample payloads and run the
same print functions — so **they emit real paper and real history records**.

They report the real outcome: `200` on success, `500` when the print failed,
`503` when the printer is absent. The front end trusts the status code. It used
to match the response body against the substring `"successful"`, which reported
success unconditionally because the print functions swallowed their own
exceptions (`P0-10`). The buttons also disable while a request is in flight, so
a double click cannot emit two receipts.

## Rules for agents

- Never call print paths or the test routes without an explicit user request —
  every call produces physical output.
- History is the only record of what printed; do not clear or truncate it
  while testing.

## One receipt at a time (P5-2 #9)

`printing.PRINT_LOCK` serialises `print_blocks` across threads. Several can
print now: the scheduler (due tasks and the queue drain), a web request (test
print, reprint, "print now", a checklist), and a push listener's own network
thread — MQTT delivers on paho's loop thread.

Two of them opening the same USB device at once produces interleaved bytes —
half of one receipt inside another — or a claim failure that leaks the
interface (`P0-11`). The lock is held for a **whole receipt**, because a
receipt is the atomic unit here: waiting a few seconds behind another print is
fine, sharing paper with it is not.

## Images (P4-7)

`taskhome/images.py` fetches and prepares photos for the printer. Off by
default: a photo roughly doubles the paper for an SCF issue (59 mm → 108 mm).

A thermal printer has one ink level, so a photograph must become pure black and
white, and **how** that reduction is done is the whole difference between a
recognisable picture and a black smear:

- **Floyd–Steinberg error diffusion**, not thresholding. It trades spatial
  resolution for apparent grey and keeps a scene legible at 203 dpi.
- **EXIF orientation is applied** — a phone photo is usually stored rotated
  with a tag saying which way is up, and ignoring it prints the picture
  sideways.
- Scaled to fit, never cropped: a cropped photo of a pothole may not contain
  the pothole.

Everything on that path is defensive, because it runs against a URL from a
third-party API: capped at 2 MB and 5 seconds, streamed and counted rather
than trusting `Content-Length`, throttled so a catch-up burst does not fire
twenty downloads at someone else's CDN, cached under `data/cache/media/`
because the queue may retry a receipt, and pruned so a year of potholes cannot
fill a disk. **Every failure returns None** and prints `[Photo unavailable]` —
a receipt missing its photo is a good outcome; a receipt that failed to print
because a CDN was down is not.

`image` is a template block type, so placement is editable in the Studio.
`{media_url}` is empty both when an issue has no photo and when photos are
switched off, and `fill()` drops the block, so neither case leaves a gap.

## Column alignment

`receipt.wrap()` preserves runs of spaces and leading indentation. It used to
join words with a single space and strip the result, which silently collapsed
both — and since text is pre-wrapped before reaching the device, that meant the
printer could not receive an aligned column layout at all. A departure board is
columns; so is any `label     value` line.
