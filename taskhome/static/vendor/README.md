# Vendored front-end assets

These are committed on purpose. TaskHome is a LAN appliance: the machine it
runs on often has no internet connection, and loading the UI from CDNs meant
the interface broke exactly when it was most needed (MASTER_PLAN `P0-15`).
Nothing here is fetched at runtime.

| File | Source | Version |
| --- | --- | --- |
| `materialize.min.css` / `.js` | cdnjs.cloudflare.com/ajax/libs/materialize | 1.0.0 |
| `flatpickr.min.css` / `.js` | cdn.jsdelivr.net/npm/flatpickr | latest at vendoring time |
| `material-icons.css` | fonts.googleapis.com/icon?family=Material+Icons | v145 |
| `material-icons.ttf` | fonts.gstatic.com (referenced by the CSS above) | v145 |

`material-icons.css` was edited after download: its `@font-face` `src` pointed
at `fonts.gstatic.com` and now points at the local `.ttf`. That is the only
modification to any file here — do not reformat or re-minify them, so that
diffs against upstream stay readable.

## Planned removal

MASTER_PLAN `P2A` replaces Materialize with the Mica Technologies design
tokens and hand-written CSS, and `P2B-6` replaces flatpickr with native
`datetime-local` inputs. When that lands, most of this directory goes away.
It exists now because a broken-offline UI is a bug today, and the redesign is
several phases out.

## Updating

Re-download from the same URLs, then re-apply the `@font-face` rewrite in
`material-icons.css`. Verify afterwards that nothing external remains:

```sh
grep -rE 'https?://' static/vendor/*.css     # only license comments should match
grep -rn 'cdnjs|jsdelivr|googleapis|gstatic' templates/   # should be empty
```
