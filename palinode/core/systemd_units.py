"""Shared systemd unit-name resolution for the watcher.

The watcher unit does not have one true name. ``deploy/systemd/install.sh``
installs it as ``palinode-watcher`` by default but honours a
``WATCHER_UNIT_NAME`` override, and real deployments use it (a deployment
hand-installed before these templates existed may run
``palinode-indexer.service``). Anything that talks to systemd about
the watcher therefore has to *resolve* the unit rather than assume it.

This module is the one place that resolution lives. ``palinode doctor``'s
``watcher_alive`` check and ``palinode stop`` both use it, so a host cannot be
reported healthy under one name and left running under another.
"""
from __future__ import annotations

import os
import subprocess

# Unit names we accept, in probe order. "palinode-watcher.service" is what
# deploy/systemd/ ships; "palinode-indexer.service" is the name real
# deployments installed via the installer's WATCHER_UNIT_NAME override.
WATCHER_UNIT_NAMES = ("palinode-watcher.service", "palinode-indexer.service")
WATCHER_UNIT_ENV = "WATCHER_UNIT_NAME"
SYSTEMCTL_TIMEOUT = 5

# Probe order for the two systemd managers: the system manager first, because
# that is where the shipped templates install to.
MANAGERS: tuple[tuple[str, bool], ...] = (("system", False), ("user", True))


def watcher_unit_candidates() -> tuple[str, ...]:
    """Return the watcher unit names to probe, in order.

    ``WATCHER_UNIT_NAME`` is the installer's existing knob for renaming the
    watcher unit (see deploy/systemd/install.sh); when it is exported, that
    name is probed first. The ``.service`` suffix is optional.
    """
    override = os.environ.get(WATCHER_UNIT_ENV, "").strip()
    if not override:
        return WATCHER_UNIT_NAMES
    if not override.endswith(".service"):
        override = f"{override}.service"
    return (override, *(u for u in WATCHER_UNIT_NAMES if u != override))


def active_unit(units: tuple[str, ...], *, user: bool) -> str | None:
    """Return the first unit of *units* the given manager reports as active.

    One ``systemctl is-active`` invocation covers every candidate name:
    systemd prints one state per unit, in argument order. Returns None when
    none are active, or when systemctl is missing, errors, or times out.
    """
    cmd = ["systemctl"]
    if user:
        cmd.append("--user")
    cmd += ["is-active", *units]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SYSTEMCTL_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    # strict=False on purpose: a manager that errored prints fewer states
    # (or none) than units, and that just means "not active".
    for unit, state in zip(units, result.stdout.split(), strict=False):
        if state == "active":
            return unit
    return None


def resolve_active_watcher_unit() -> tuple[str, bool] | None:
    """Return ``(unit, user)`` for the running watcher unit, or None.

    Probes every candidate name under the system manager first and the user
    manager second. ``user`` is True when the unit answered under
    ``systemctl --user``, which callers need in order to address it (a user
    unit is invisible to ``sudo systemctl``, and vice versa).
    """
    units = watcher_unit_candidates()
    for _manager, user in MANAGERS:
        found = active_unit(units, user=user)
        if found is not None:
            return found, user
    return None
