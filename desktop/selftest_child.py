"""Stand-in for a dataset import in the self-test: proves `Copilot.exe --run-import <module>` works."""
import os
from pathlib import Path

if __name__ == "__main__":
    Path(os.environ["COPILOT_SELFTEST_MARKER"]).write_text("ok", encoding="utf-8")
