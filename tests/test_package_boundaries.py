from pathlib import Path

import pytest

from hermes_discord_skill_audit import reaction_audit


PANEL_HELPER_EXPORTS = {
    "format_berlin_time",
    "get_hermes_bin",
    "format_cron_datetime",
    "get_cron_status",
    "get_vps_stats",
}


def test_reaction_audit_package_does_not_export_panel_helpers():
    for name in PANEL_HELPER_EXPORTS:
        assert name not in reaction_audit.__all__
        with pytest.raises(AttributeError):
            getattr(reaction_audit, name)


def test_reaction_audit_package_has_no_system_info_module():
    package_dir = Path(__file__).resolve().parents[1] / "hermes_discord_skill_audit"
    assert not (package_dir / "system_info.py").exists()
