"""Runtime settings resolved from the environment and config.

Separate from constants.py because these read live config, and separate from
__init__.py because printing needs get_port() to build QR URLs -- importing the
package root from a submodule that the root imports would be circular.
"""
import os

from . import constants, state
from .logsetup import log


def get_host():
    """Bind address: TASKHOME_HOST > config['host'] > 0.0.0.0."""
    return (os.environ.get('TASKHOME_HOST')
            or state.config.get('host')
            or constants.DEFAULT_HOST)


def get_port():
    """Listen port: TASKHOME_PORT > config['port'] > 5000.

    Port 5000 is claimed by AirPlay Receiver on macOS, so an override is
    needed there. Anything unparseable falls back rather than refusing to
    start.
    """
    raw = (os.environ.get('TASKHOME_PORT')
           or state.config.get('port')
           or constants.DEFAULT_PORT)
    try:
        port = int(raw)
    except (TypeError, ValueError):
        log.warning(f"Invalid port {raw!r}, falling back to {constants.DEFAULT_PORT}")
        return constants.DEFAULT_PORT
    if not 1 <= port <= 65535:
        log.warning(f"Port {port} out of range, falling back to {constants.DEFAULT_PORT}")
        return constants.DEFAULT_PORT
    return port
