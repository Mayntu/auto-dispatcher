"""Small async runtime helpers shared by the service entrypoints."""

from __future__ import annotations

import asyncio
import signal


async def run_forever() -> None:
    """Block until SIGINT/SIGTERM (Docker stop) so `finally` blocks can shut down cleanly."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # e.g. Windows
            pass
    try:
        await stop.wait()
    except asyncio.CancelledError:
        raise
