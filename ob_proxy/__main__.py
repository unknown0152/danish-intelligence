"""Entrypoint: run the OB-NZB proxy."""

from __future__ import annotations

import logging

from aiohttp import web

from .config import Config
from .server import create_app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = Config.from_env()
    app = create_app(config)
    logging.getLogger("ob_proxy").info(
        "starting OB-NZB proxy on %s:%s -> %s", config.host, config.port, config.ob_base_url
    )
    web.run_app(app, host=config.host, port=config.port, print=None)


if __name__ == "__main__":
    main()
