"""Listeners poll an external source and print new items as receipts.

`scf` predates the plugin interface and still runs through its own function;
`nws` is built on `base.Listener`, which provides interval gating, watermarks,
dedup, backoff, per-poll caps and the settings schema (P5-1).
"""
from . import base, nws, scf

__all__ = ['base', 'nws', 'scf']
