"""Keep automatically published Engine test sessions outside the real registry."""
import functools
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(autouse=True)
def isolate_engine_observer_registry(monkeypatch, tmp_path):
    import stage2_run_observer
    from stage2_run_state import RunPublisher
    monkeypatch.setattr(stage2_run_observer, "RunPublisher", functools.partial(
        RunPublisher, registry_root=tmp_path / "engine-run-registry"))
