import logging
from logging.handlers import RotatingFileHandler


def setup_logging() -> None:
    """
    Base logging for Magnus.

    - Sets root logger to INFO.
    - Logs to both stdout and `magnus_structured.log` (rotating).
    - Run once at start of `Trade` via `setup_logging()`.
    """
    root = logging.getLogger()
    if root.handlers:
        # Already configured – avoid double logging.
        return

    root.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(name)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    try:
        file_handler = RotatingFileHandler(
            "magnus_structured.log",
            maxBytes=5_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except Exception:
        # Logging must never crash the app.
        pass

    # Tone down spam from third party (httpx/py_clob_client etc.).
    for noisy in ("httpx", "py_clob_client", "urllib3"):
        try:
            logging.getLogger(noisy).setLevel(logging.WARNING)
        except Exception:
            continue

