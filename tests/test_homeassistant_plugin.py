import json
import sqlite3
import textwrap

import yaml

from restore_probe.plugins.homeassistant import (count_automations_yaml, disable_config_entries, load_ha_yaml,
                                                 neutralize_configuration, recorder_stats)

CONFIG = textwrap.dedent("""
    homeassistant:
      internal_url: http://192.0.2.10:8123
    default_config:
    frontend:
      themes: !include_dir_merge_named themes
    automation: !include automations.yaml
    shell_command:
      reboot_pc: ssh pc reboot
    wake_on_lan:
    sonos:
      media_player:
        hosts: [192.0.2.59]
    api_password: !secret pw
""")


def test_neutralize_keeps_tags_and_removes_dangerous_domains():
    out = neutralize_configuration(CONFIG, "172.30.99.0/24")
    assert "shell_command" not in out and "wake_on_lan" not in out
    # PyYAML quotes tagged scalars; HA reads both forms
    assert "!include_dir_merge_named 'themes'" in out or "!include_dir_merge_named themes" in out
    assert "!include 'automations.yaml'" in out or "!include automations.yaml" in out
    assert "!secret 'pw'" in out or "!secret pw" in out
    data = load_ha_yaml(out)
    providers = data["homeassistant"]["auth_providers"]
    assert providers[0]["type"] == "trusted_networks"
    assert providers[0]["trusted_networks"] == ["172.30.99.0/24"]
    assert providers[0]["allow_bypass_login"] is True
    assert data["homeassistant"]["internal_url"].startswith("http://")   # Bestehendes bleibt
    assert "sonos" in data                                               # unknown domains stay (isolation via network + config_entries)


def test_disable_config_entries(tmp_path):
    f = tmp_path / "core.config_entries"
    entries = [{"domain": "zha", "disabled_by": None}, {"domain": "sun", "disabled_by": None},
               {"domain": "tplink", "disabled_by": "user"}, {"domain": "mqtt", "disabled_by": None}]
    f.write_text(json.dumps({"version": 1, "data": {"entries": entries}}))
    counts = disable_config_entries(f, ["sun"])
    assert counts == {"disabled": 2, "kept": 1, "total": 4}
    after = json.loads(f.read_text())["data"]["entries"]
    assert {e["domain"]: e["disabled_by"] for e in after} == {"zha": "user", "sun": None, "tplink": "user", "mqtt": "user"}


def test_count_automations():
    assert count_automations_yaml("- id: '1'\n  alias: a\n- id: '2'\n  alias: b\n") == 2
    assert count_automations_yaml("") == 0


def test_recorder_stats(tmp_path):
    db = tmp_path / "home-assistant_v2.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE states (state_id INTEGER PRIMARY KEY, state TEXT);
        CREATE TABLE events (event_id INTEGER PRIMARY KEY);
        CREATE TABLE schema_changes (change_id INTEGER PRIMARY KEY, schema_version INTEGER);
        INSERT INTO states (state) VALUES ('a'), ('b'), ('c');
        INSERT INTO events DEFAULT VALUES;
        INSERT INTO schema_changes (schema_version) VALUES (48);
    """)
    con.commit()
    con.close()
    st = recorder_stats(db)
    assert st == {"quick_check": "ok", "max_state_id": 3, "max_event_id": 1, "schema_version": 48, "newest_state_ts": None}
    # Damaged file → quick_check reports an error instead of ok
    raw = bytearray(db.read_bytes())
    raw[4096:8192] = b"\xff" * 4096      # destroy page 2 (table states, page size 4096)
    db.write_bytes(raw)
    try:
        st = recorder_stats(db)
        assert st["quick_check"] != "ok"
    except sqlite3.DatabaseError:
        pass  # ebenfalls ein erkannter Schaden
