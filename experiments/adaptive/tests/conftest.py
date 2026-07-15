from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


@pytest.fixture
def run_directory(tmp_path: Path) -> Path:
    path = tmp_path / "adaptive-run"
    path.mkdir()
    return path


@pytest.fixture(scope="session")
def deterministic_seed() -> int:
    return 1729
