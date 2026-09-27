"""Uvicorn entry point — mirrors apps/api/src/server.ts."""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from .config import load_config
from .main import setup_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


async def main() -> None:
    config = load_config(dict(os.environ))
    app = await setup_app(config)

    import uvicorn
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.API_HOST,
            port=config.API_PORT,
            loop="asyncio",
            log_level="info",
        )
    )

    shutting_down = False

    def handle_signal(signum: int) -> None:
        nonlocal shutting_down
        if shutting_down:
            return
        shutting_down = True
        logger.info("Received signal %s, shutting down...", signum)
        server.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: handle_signal(s))

    await server.serve()
    logger.info("Server exited")


if __name__ == "__main__":
    asyncio.run(main())
