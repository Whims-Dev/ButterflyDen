import uuid
import shutil
import os
from pathlib import Path

TEMP_ROOT = Path(os.getenv("TEMP_ROOT", "./temp"))

class Job:
    def __init__(self):
        self.id = uuid.uuid4().hex[:8]
        self.path = TEMP_ROOT / f"job_{self.id}"
        self.path.mkdir(parents=True, exist_ok=True)

    def cleanup(self):
        if self.path.exists():
            shutil.rmtree(self.path)