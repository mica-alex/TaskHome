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

- `is_printer_connected()` (`app.py:140-146`) is a pure presence probe:
  `usb.core.find(idVendor, idProduct) is not None`. It does not open the device.
- Each print call then constructs a **fresh** `Usb(VID, PID, profile='TM-T20II')`
  handle. There is a TOCTOU window between probe and open, and — more
  importantly — `p.close()` is only reached on the success path. Any exception
  after the open (bad payload field, USB hiccup) is caught by the outer
  `except`, logged, and the handle **leaks**; the partially-printed receipt is
  left uncut in the printer (MASTER_PLAN `P0-11`).
- If the probe fails, print functions log a warning and return — the caller does
  not know, and the scheduler advances the schedule anyway (MASTER_PLAN `P0-4`).

## `p.set(...)` cheat sheet

`p.set()` is python-escpos's character-formatting call; parameters used here:

| Param | Meaning |
| --- | --- |
| `align` | Horizontal alignment (`center` everywhere in this app) |
| `font` | `'a'` = 12×24 dots (48 cols on 80 mm) or `'b'` = 9×17 dots (64 cols); all text in this app is centered so column math rarely bites |
| `bold` | Emphasis on/off |
| `custom_size=True, width=N, height=N` | Character-cell multipliers 1–8; `width=3, height=3` prints triple-size characters |
| `density` | Print density (darkness), 0–8; `4` used for QR/title blocks |

Note: `p.set()` is stateful — settings persist until the next `p.set()`. The
comments in `app.py` saying "italic" are aspirational; no italics are ever set
(ESC/POS has no italic in this profile).

## Task receipt layout (`print_task`, `app.py:175-227`)

```
        [ QR code ]              <- size 5, model 2; URL = task.url if set,
                                    else http://<hostname>:5000/task_page#<id>
        TITLE                    <- font a, bold, 3x3, density 4, centered
                                 <- blank line (only if extra present)
        Extra text               <- font b, 2x2, centered (only if task.extra)
                                 <- blank line
        Printed at 09:36 PM, 08/26/2025   <- font b, 1x1
                                 <- blank line
        Task Type: Recurring (Daily)      <- font b, 1x1
        Task ID: 6ef1b365-...             <- same style
        [ cut ]
```

- Task type string: `Non-recurring` for `none`, else
  `Recurring (<Mode capitalized>)` — note `every_weekday` renders as
  `Recurring (Every_weekday)` (cosmetic, MASTER_PLAN `P2-10`).
- On success: record appended to history (front), truncated to `max_history`,
  `save_history()`.

## SCF issue receipt layout (`print_scf_issue`, `app.py:230-300`)

```
        [ QR code ]              <- issue.html_url, size 5, model 2
        CATEGORY                 <- request_type.title or "Unknown Category";
                                    font a, bold, 3x3, density 4
                                 <- blank line
        Location: 123 Main St    <- font b, 1x1 (this style through the end)
        Reported: 06:53 PM, 07/23/2026
        Status: Open
        Has Media: Yes|No
        Description:             <- only if description present and truthy
        <description text>
                                 <- blank line
        Printed at 09:36 AM, 08/26/2025
        [ CODE39 barcode of issue id, pos below ]   <- falls back to
                                                       "Issue ID: NNN" text on error
        [ cut ]
```

Payload-shape hazards (verified against the live API):

- `issue['html_url']` (`app.py:238`) and `issue['media']['image_full']`
  (`app.py:258`) are indexed without guards. The live API today always includes
  both (`media` is an object whose `image_full` may be null), but a missing
  `media.image_full` key or `media: null` would raise mid-print — after the QR
  and title are already on paper — leaving an uncut partial receipt and no
  history record, while `last_check` still advances so the issue is never
  retried (MASTER_PLAN `P0-8`).
- `reported_at` parsing handles both `Z` and offset forms via
  `.replace('Z', '+00:00')` + `fromisoformat` — but then **formats the time in
  the issue's own offset**, not local time (live API returns the place's local
  offset, so in practice this reads correctly for nearby places).
- The history record is a projection, not the raw issue — see
  [data-model.md](data-model.md#type-scf--a-printed-seeclickfix-issue).

## Test prints

`/test_print` and `/test_scf_print` (`app.py:438-485`) build hardcoded sample
payloads and run the same print functions — so **they emit real paper and real
history records**. Their success responses are misleading: `print_task` /
`print_scf_issue` swallow their own exceptions, so the routes report
"successful" even when the print failed (MASTER_PLAN `P0-10`).

## Rules for agents

- Never call print paths or the test routes without an explicit user request —
  every call produces physical output.
- History is the only record of what printed; do not clear or truncate it
  while testing.
