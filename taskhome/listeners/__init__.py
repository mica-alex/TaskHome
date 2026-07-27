"""Listeners poll an external source and print new items as receipts.

Only SeeClickFix exists so far and the scheduler calls it directly. The plugin
interface that makes adding one a matter of dropping in a class -- with an
auto-generated settings page -- is MASTER_PLAN P5-1.
"""
from . import scf

__all__ = ['scf']
