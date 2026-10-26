import json
import os
import tempfile
import time
from pathlib import Path


def atomic_json(path, value):
    """Each writer owns its temporary file; Windows sharing retries are bounded."""
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=path.stem+"-", suffix=".tmp", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
        for attempt in range(4):
            try:
                os.replace(temporary, path); return
            except PermissionError:
                if attempt == 3: raise
                time.sleep(0.025 * 2**attempt)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
