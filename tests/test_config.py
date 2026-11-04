import textwrap

import pytest

from restore_probe.config import ConfigError, load_config, load_env


def test_env_parsing(tmp_path):
    env = tmp_path / ".env"
    env.write_text('A=1\n# comment\nB="with spaces"\nC=\'x\'\nEMPTY=\n')
    assert load_env(env) == {"A": "1", "B": "with spaces", "C": "x", "EMPTY": ""}


def test_env_reference_resolved_and_missing(tmp_path):
    (tmp_path / ".env").write_text("HA_TOKEN=secret\n")
    conf = tmp_path / "config.yaml"
    conf.write_text(textwrap.dedent(f"""
        env_file: {tmp_path / '.env'}
        general: {{state_dir: {tmp_path}}}
        home_assistant:
          token: ${{HA_TOKEN}}
        services:
          - name: a
            plugin: files
            source: {{type: path, glob: "/x/*"}}
    """))
    cfg = load_config(conf)
    assert cfg.home_assistant.token == "secret"
    assert cfg.services[0].timeout_min == 15
    conf.write_text(conf.read_text().replace("HA_TOKEN}", "MISSING_VAR}"))
    with pytest.raises(ConfigError, match="MISSING_VAR"):
        load_config(conf)


def test_duplicate_and_bad_names(tmp_path):
    conf = tmp_path / "config.yaml"
    conf.write_text(textwrap.dedent(f"""
        env_file: {tmp_path / '.env'}
        services:
          - {{name: a, plugin: files, source: {{type: path}}}}
          - {{name: a, plugin: files, source: {{type: path}}}}
    """))
    with pytest.raises(ConfigError, match="Duplicate"):
        load_config(conf)
    conf.write_text(f"env_file: {tmp_path / '.env'}\nservices:\n  - {{name: 'Bad-Name', plugin: files, source: {{type: path}}}}\n")
    with pytest.raises(ConfigError, match="a-z"):
        load_config(conf)


def test_env_permission_warning(tmp_path):
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    env.chmod(0o644)
    conf = tmp_path / "config.yaml"
    conf.write_text(f"env_file: {env}\nservices:\n  - {{name: a, plugin: files, source: {{type: path}}}}\n")
    cfg = load_config(conf)
    assert any("600" in w for w in cfg.warnings)


def test_example_config_loads(tmp_path):
    """The shipped example must stay valid (with dummy secrets)."""
    from pathlib import Path
    example = Path(__file__).resolve().parents[1] / "config" / "config.example.yaml"
    env = tmp_path / ".env"
    env.write_text("HA_TOKEN=x\nHA_BACKUP_KEY=x\nRESTIC_REPOSITORY=/tmp/r\nRESTIC_PASSWORD=x\n")
    cfg = load_config(example, env_path=env)
    assert {s.name for s in cfg.services} >= {"homeassistant", "grafana", "prometheus", "nginx", "influxdb"}
