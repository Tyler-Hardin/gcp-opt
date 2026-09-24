"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

from gcp_opt.catalog import Catalog  # noqa: E402
from gcp_opt.dataset import Dataset  # noqa: E402


@pytest.fixture(scope="session")
def catalog() -> Catalog:
    """A catalog backed by the committed, hash-verified snapshots."""
    return Catalog(Dataset.load_bundled())


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return REPO_ROOT / "tests" / "fixtures"
