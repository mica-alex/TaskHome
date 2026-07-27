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

Payload handling:

- **Every field is resolved before the printer is opened.** `html_url`,
  `media.image_full`, `request_type.title` and `created_at` were previously
  indexed inline, so an unexpected shape raised *after* the QR and title were
  already on paper — an uncut partial receipt, no history record, and (before
  `P0-7`) a watermark that advanced anyway so the issue was never retried.
  Resolving first means a malformed payload fails having printed nothing.
  Helpers: `scf_category()`, `scf_has_media()`, `scf_reported_at()`.
- Tolerated shapes: `media` absent, null, or not a dict; `request_type` absent,
  null, or missing `title` (→ `"Unknown Category"`); `created_at` absent or
  unparseable (→ the raw value, or `"Unknown"`); `html_url` absent (the QR is
  simply omitted).
- `reported_at` is formatted in **the issue's own offset**, not local time. The
  live API returns the place's local offset, so this reads correctly for nearby
  places.
- The history record is a projection, not the raw issue — see
  [data-model.md](data-model.md#type-scf--a-printed-seeclickfix-issue).

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
