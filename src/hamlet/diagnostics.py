import json
import logging
from pathlib import Path
import traceback


LOGGER = logging.getLogger("hamlet")


def event(name, *, level=logging.INFO, **fields):
    LOGGER.log(level, json.dumps({"event": name, **fields}, sort_keys=True))


def failure_fields(error):
    # Never format exception text, source lines, or locals: they may contain mail or secrets.
    return {
        "error_type": type(error).__name__,
        "frames": [
            {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(error.__traceback__)
        ],
    }
