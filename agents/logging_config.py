import logging
import logging.handlers
import os
import json
from datetime import datetime, timezone


class MagnusLiveHandler(logging.Handler):
    """Writes formatted lines to magnus_live.log (backward-compatible with tail_magnus.py)."""

    def __init__(self, path: str = "magnus_live.log"):
        super().__init__()
        self.path = path

    def emit(self, record: logging.LogRecord):
        try:
            ts = datetime.now().strftime("%H:%M:%S")
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {record.getMessage()}\n")
                f.flush()
        except Exception:
            self.handleError(record)


class JsonFormatter(logging.Formatter):
    """Structured JSON log lines for machine parsing."""

    def format(self, record: logging.LogRecord):
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(json_log_path: str = "magnus_structured.log"):
    """Configure root logger with console, live-log and JSON handlers."""
    root = logging.getLogger("magnus")
    if root.handlers:
        return root
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(console)

    live = MagnusLiveHandler()
    live.setLevel(logging.INFO)
    root.addHandler(live)

    os.makedirs(os.path.dirname(json_log_path) or ".", exist_ok=True)
    json_handler = logging.handlers.RotatingFileHandler(
        json_log_path, maxBytes=10_000_000, backupCount=3, encoding="utf-8"
    )
    json_handler.setLevel(logging.DEBUG)
    json_handler.setFormatter(JsonFormatter())
    root.addHandler(json_handler)

    return root
