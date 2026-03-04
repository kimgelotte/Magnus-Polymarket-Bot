import logging
from logging.handlers import RotatingFileHandler


def setup_logging() -> None:
    """
    Basloggning för Magnus.

    - Sätter root‑logger till INFO.
    - Loggar både till stdout och till `magnus_structured.log` (roterande).
    - Körs en gång i början av `Trade` via `setup_logging()`.
    """
    root = logging.getLogger()
    if root.handlers:
        # Redan konfigurerad – undvik dubbel logging.
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
        # Loggning får aldrig krascha botten.
        pass

    # Tona ned spam från tredjepart (httpx/py_clob_client etc.).
    for noisy in ("httpx", "py_clob_client", "urllib3"):
        try:
            logging.getLogger(noisy).setLevel(logging.WARNING)
        except Exception:
            continue

