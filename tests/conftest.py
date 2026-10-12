import shutil
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from misecre.config import load_config

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 12, 10, 0)  # lunes


@pytest.fixture
def cfg(tmp_path):
    raw = yaml.safe_load((ROOT / "config/config.example.yaml").read_text(encoding="utf-8"))
    raw["data_dir"] = str(tmp_path / "data")
    cdir = tmp_path / "config"
    cdir.mkdir()
    shutil.copy(ROOT / "config/rules.yaml", cdir / "rules.yaml")
    (cdir / "config.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return load_config(cdir / "config.yaml")


@pytest.fixture
def now():
    return NOW
