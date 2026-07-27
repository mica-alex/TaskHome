#!/usr/bin/env python
"""Entry point: `python app.py`.

The application itself lives in the taskhome package. This file only assembles
it and starts the server, which is the point of the split -- importing the
package no longer reads files, starts threads or touches the printer, so
scripts and tests can import it freely.

Kept at the repo root because that is what existing installs, the IDE run
configurations and the service units all invoke.
"""
import taskhome

# scheduler=True here and nowhere else: this is the one place where starting a
# background thread that prints is unambiguously intended (P0-12).
app = taskhome.create_app(with_scheduler=True)

if __name__ == '__main__':
    taskhome.log.debug('Running directly via python app.py')
    app.run(host=taskhome.get_host(), port=taskhome.get_port())
