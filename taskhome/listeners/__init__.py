"""Listeners poll an external source and print new items as receipts.

`scf` predates the plugin interface and still runs through its own function;
`nws` is built on `base.Listener`, which provides interval gating, watermarks,
dedup, backoff, per-poll caps and the settings schema (P5-1).

`webhook` is a **push** listener: nothing polls it, and items arrive through
base.deliver() from an inbound HTTP request. It shares the whole tail of the
pipeline -- dedup, caps, filtering, templates, history, queue-on-failure -- so
a pushed receipt behaves exactly like a polled one.
"""
from . import (base, binday, brief, calendar, feeds, github, mqtt, nws,
               scf, transit, webhook)

__all__ = ['base', 'binday', 'brief', 'calendar', 'feeds', 'github', 'mqtt',
           'nws', 'scf', 'transit', 'webhook']
