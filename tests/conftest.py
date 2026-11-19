from __future__ import annotations

from pathlib import Path

import pytest

from estatequery.ingest.build_db import build

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A small database built from synthetic fixture data."""
    path = tmp_path_factory.mktemp("db") / "estatequery.db"
    build(FIXTURES / "raw", path, snapshot="2026-11-19")
    return path


@pytest.fixture(autouse=True)
def no_real_fallback_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never reach a real model: fallbacks are off unless a test passes its own."""
    from estatequery.config import get_settings

    monkeypatch.setattr(get_settings(), "fallback_models", "")
