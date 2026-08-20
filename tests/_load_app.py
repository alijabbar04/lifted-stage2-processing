"""Load the Stage 2 `.pyw` module without opening its Tk application."""

import importlib.util
import sys
from pathlib import Path


APP_PATH = Path(__file__).resolve().parent.parent / "src" / "Stage2_Processing.pyw"


def load_app():
    if "stage2_app" in sys.modules:
        return sys.modules["stage2_app"]
    spec = importlib.util.spec_from_file_location("stage2_app", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["stage2_app"] = module
    spec.loader.exec_module(module)
    return module
