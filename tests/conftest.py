import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every test gets its own data/reports/payments dirs and a fixed fingerprint secret; never the network."""
    monkeypatch.setenv("PAYEEGUARD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PAYEEGUARD_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("PAYEEGUARD_PAYMENTS_DIR", str(tmp_path / "payments"))
    monkeypatch.setenv("PAYEEGUARD_FP_SECRET", "0" * 64)
    monkeypatch.setenv("PAYEEGUARD_OFFLINE", "1")
    monkeypatch.delenv("PAYEEGUARD_CACHE_DIR", raising=False)
    monkeypatch.delenv("FAKE_API_DOWN", raising=False)
    return tmp_path


@pytest.fixture
def graph():
    from payeeguard.wiring import MemoryGraph
    g = MemoryGraph()
    g.load_seed(str(ROOT / "samples" / "seed_history.json"))
    return g

