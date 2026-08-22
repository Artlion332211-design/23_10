from __future__ import annotations

import logging

from src.config import Config
from src.monitor import Monitor

CONFIG_PATH = "config.yaml"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    config = Config.load(CONFIG_PATH)
    logging.getLogger().addHandler(logging.FileHandler(config.log_file, encoding="utf-8"))
    Monitor(config).run()


if __name__ == "__main__":
    main()
