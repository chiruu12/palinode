"""Tests for `palinode stop`.

Two concerns:
  - the original contract: systemctl must exist, a failed unit doesn't abort
    the rest, a failure exits non-zero
  - the watcher unit is *resolved* (system manager then --user,
    honouring WATCHER_UNIT_NAME) rather than hardcoded, so a host running
    palinode-indexer.service actually gets its watcher stopped

The subprocess seam is monkeypatched the way the doctor tests do it: a stub
that answers `systemctl is-active` from a fixture and records every argv.
"""

import subprocess

from click.testing import CliRunner

from palinode.cli import main
from palinode.core.systemd_units import WATCHER_UNIT_NAMES


def _states(units, active_unit):
    """Render one `systemctl is-active` state line per probed unit."""
    return "".join(
        f"{'active' if unit == active_unit else 'inactive'}\n" for unit in units
    )


def _fake_systemctl(
    *,
    system_active=None,
    user_active=None,
    calls=None,
    fail_unit=None,
):
    """Build a subprocess.run stub covering both is-active probes and stops.

    *system_active* / *user_active* name the unit each manager reports as
    active (None = nothing active). *fail_unit* makes `stop` raise
    CalledProcessError for that unit.
    """

    def _run(cmd, **kwargs):
        cmd = list(cmd)
        if calls is not None:
            calls.append(cmd)
        if "is-active" in cmd:
            probed = tuple(cmd[cmd.index("is-active") + 1 :])
            active = user_active if "--user" in cmd else system_active

            class _Result:
                returncode = 0 if active is not None else 3
                stdout = _states(probed, active)
                stderr = ""

            return _Result()
        if fail_unit is not None and cmd[-1] == fail_unit:
            raise subprocess.CalledProcessError(1, cmd)
        return None

    return _run


def _stop_calls(calls):
    return [c for c in calls if "stop" in c]


def test_stop_without_systemctl_exits_nonzero(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)

    runner = CliRunner()
    result = runner.invoke(main, ["stop"])

    assert result.exit_code != 0
    assert "systemctl" in result.output


def test_stop_success_exits_zero(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/systemctl")
    monkeypatch.delenv("WATCHER_UNIT_NAME", raising=False)
    monkeypatch.setattr("subprocess.run", _fake_systemctl())

    runner = CliRunner()
    result = runner.invoke(main, ["stop"])

    assert result.exit_code == 0
    assert "stopped" in result.output


def test_stop_systemctl_failure_exits_nonzero(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/systemctl")
    monkeypatch.delenv("WATCHER_UNIT_NAME", raising=False)
    monkeypatch.setattr(
        "subprocess.run", _fake_systemctl(fail_unit="palinode-api.service")
    )

    runner = CliRunner()
    result = runner.invoke(main, ["stop"])

    assert result.exit_code != 0
    assert "Failed to stop" in result.output


def test_stop_continues_with_remaining_services_after_failure(monkeypatch):
    """A failed service must not prevent the remaining services from being stopped."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/systemctl")
    monkeypatch.delenv("WATCHER_UNIT_NAME", raising=False)
    monkeypatch.setattr(
        "subprocess.run", _fake_systemctl(fail_unit="palinode-api.service")
    )

    runner = CliRunner()
    result = runner.invoke(main, ["stop"])

    assert result.exit_code != 0
    assert "Failed to stop palinode-api.service" in result.output
    assert "✓ palinode-watcher.service stopped" in result.output


# ---------------------------------------------------------------------------
# The watcher unit is resolved, not hardcoded
# ---------------------------------------------------------------------------


class TestStopResolvesWatcherUnit:
    @staticmethod
    def _systemd(monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/systemctl")
        monkeypatch.delenv("WATCHER_UNIT_NAME", raising=False)

    def test_active_system_indexer_unit_is_the_one_stopped(self, monkeypatch):
        """The renamed-unit case: watcher installed as palinode-indexer.service."""
        self._systemd(monkeypatch)
        calls: list[list[str]] = []
        monkeypatch.setattr(
            "subprocess.run",
            _fake_systemctl(system_active="palinode-indexer.service", calls=calls),
        )

        runner = CliRunner()
        result = runner.invoke(main, ["stop"])

        assert result.exit_code == 0
        assert ["sudo", "systemctl", "stop", "palinode-indexer.service"] in calls
        assert ["sudo", "systemctl", "stop", "palinode-watcher.service"] not in calls
        assert "✓ palinode-indexer.service stopped" in result.output

    def test_watcher_unit_name_override_is_honoured(self, monkeypatch):
        """WATCHER_UNIT_NAME names a unit outside the shipped list; suffix optional."""
        self._systemd(monkeypatch)
        monkeypatch.setenv("WATCHER_UNIT_NAME", "palinode-memory-watcher")
        calls: list[list[str]] = []
        monkeypatch.setattr(
            "subprocess.run",
            _fake_systemctl(
                system_active="palinode-memory-watcher.service", calls=calls
            ),
        )

        runner = CliRunner()
        result = runner.invoke(main, ["stop"])

        assert result.exit_code == 0
        assert [
            "sudo",
            "systemctl",
            "stop",
            "palinode-memory-watcher.service",
        ] in calls

    def test_user_unit_is_stopped_through_the_user_manager(self, monkeypatch):
        """A --user unit is invisible to `sudo systemctl` — address it as --user."""
        self._systemd(monkeypatch)
        calls: list[list[str]] = []
        monkeypatch.setattr(
            "subprocess.run",
            _fake_systemctl(user_active="palinode-watcher.service", calls=calls),
        )

        runner = CliRunner()
        result = runner.invoke(main, ["stop"])

        assert result.exit_code == 0
        assert [
            "systemctl",
            "--user",
            "stop",
            "palinode-watcher.service",
        ] in calls
        assert "user manager" in result.output

    def test_probes_system_manager_before_user(self, monkeypatch):
        self._systemd(monkeypatch)
        calls: list[list[str]] = []
        monkeypatch.setattr("subprocess.run", _fake_systemctl(calls=calls))

        runner = CliRunner()
        runner.invoke(main, ["stop"])

        probes = [c for c in calls if "is-active" in c]
        assert len(probes) == 2
        assert "--user" not in probes[0]
        assert "--user" in probes[1]

    def test_falls_back_to_the_default_unit_when_none_is_active(self, monkeypatch):
        """Nothing running: stop the shipped default, as before the fix."""
        self._systemd(monkeypatch)
        calls: list[list[str]] = []
        monkeypatch.setattr("subprocess.run", _fake_systemctl(calls=calls))

        runner = CliRunner()
        result = runner.invoke(main, ["stop"])

        assert result.exit_code == 0
        assert ["sudo", "systemctl", "stop", WATCHER_UNIT_NAMES[0]] in calls

    def test_no_watcher_skips_the_probe_entirely(self, monkeypatch):
        self._systemd(monkeypatch)
        calls: list[list[str]] = []
        monkeypatch.setattr(
            "subprocess.run",
            _fake_systemctl(system_active="palinode-indexer.service", calls=calls),
        )

        runner = CliRunner()
        result = runner.invoke(main, ["stop", "--no-watcher"])

        assert result.exit_code == 0
        assert not [c for c in calls if "is-active" in c]
        assert _stop_calls(calls) == [
            ["sudo", "systemctl", "stop", "palinode-api.service"]
        ]
