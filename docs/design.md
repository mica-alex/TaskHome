# Design system

TaskHome uses the Mica Technologies design language, so the appliance looks
like it belongs to the same family as the website.

## Where the tokens come from

`taskhome/static/vendor/mica-tokens.css`, transcribed from
`website-micatechnologies-com/src/components/shared-theme/themePrimitives.ts`.

**The code, not the design page.** The two disagree — most visibly on the
typeface: the design page prose says Roboto while the theme sets
`fontFamily: 'Inter, sans-serif'`. The theme is what ships, so Inter won
(decision `D-4`).

Worth knowing: the site declares Inter but never actually loads it, so it
currently renders in the system fallback. TaskHome loads it properly, from
`vendor/fonts/inter-variable.woff2` — self-hosted, never a CDN, because the UI
has to work with no internet (`P0-15`). Google serves Inter as a variable font,
so one 48 KB file covers 400/500/600 rather than three static cuts.

| Scale | Values |
| --- | --- |
| brand | `hsl(210, 98–100%, L)`, 50→900 |
| gray | `hsl(220, 20–35%, L)`, 50→900 |
| radius | 8px, 16px (cards, appbar), pill |
| spacing | 4/8/12/16/24/32 — MUI's 8px base |

## The appbar

A floating translucent pill, transcribed from `MicaAppBar.tsx`:

| Property | Value | Source |
| --- | --- | --- |
| radius | `shape.borderRadius + 8` = 16px | `ThemedToolbar` |
| blur | `backdropFilter: blur(24px)` | same |
| background | `background.default` at 40% | same |
| border | 1px `palette.divider` | same |
| shadow | `shadows[1]` | same |
| padding | 8px 12px | same |
| offset | `mt: 28px` | `AppBar` root |
| collapse | 900px | site breakpoint |

Below 900px the links wrap under the brand and gain larger tap targets rather
than overflowing the pill.

`color-mix()` produces the translucency; where it is unsupported the bar falls
back to a solid background rather than an unreadable transparent one.

## Component conventions

Taken from how the site actually uses MUI: **cards are outlined by default**,
with elevation reserved for genuinely floating surfaces; chips carry status and
categories; the Rounded icon family is preferred.

## Coexistence with Materialize

`mica.css` is additive — it styles TaskHome's own class names rather than
overriding Materialize, so pages can migrate one at a time. `P2A-4` retires
Materialize once nothing depends on it.

One trap: `styles.css` hides native `<select>` elements (a Materialize
workaround), so Mica field styles re-assert `display: block !important`.
Without that, every dropdown vanishes when Materialize fails to load — which is
exactly the offline case the vendoring exists to support.

## Keeping in sync

These are a transcription, not a live import: the site expresses them in MUI
TypeScript, which cannot be vendored into a Flask app. If the site's palette
changes, update `mica-tokens.css`. `tests/test_offline.py` pins the values that
must match, and checks both themes define every semantic token — one defined
for a single theme renders as an invalid value in the other, which fails
silently and merely looks like a glitch.
