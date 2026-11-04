import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from restore_probe.config import Config, HAConfig, ResticConfig, SandboxConfig, ServiceConfig  # noqa: E402


@pytest.fixture
def cfg(tmp_path) -> Config:
    """Minimale Konfiguration ohne Secrets für Unit-Tests."""
    return Config(
        config_path=tmp_path / "config.yaml", work_dir=tmp_path / "work", state_dir=tmp_path / "state",
        www_dir=tmp_path / "www", min_free_gb=0, default_timeout_min=1, backup_max_age_hours=26,
        tolerance_percent=5, data_drop_alert_percent=20, history_keep=5, report_title="Test",
        prometheus_textfile=tmp_path / "node" / "restore_probe.prom", sandbox=SandboxConfig(),
        home_assistant=HAConfig(token=""), restic=ResticConfig(),
        services=[ServiceConfig(name="demo", plugin="files", title="Demo", source={"type": "path", "glob": "x"},
                                options={}, timeout_min=1, tolerance_percent=5, backup_max_age_hours=26)],
        env={}, warnings=[],
    )
