"""
Checks: memory_dir_exists, memory_dir_writable

Verifies that the configured memory directory is present on disk and that
the running user can create files inside it.
Severity: critical — without it nothing works.
"""
from __future__ import annotations

import os
from pathlib import Path

from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext


@register(tags=("fast",))
def memory_dir_exists(ctx: DoctorContext) -> CheckResult:
    """Verify that config.memory_dir exists on disk."""
    memory_dir = Path(ctx.config.memory_dir).expanduser().resolve()

    if memory_dir.exists() and memory_dir.is_dir():
        return CheckResult(
            name="memory_dir_exists",
            severity="critical",
            passed=True,
            message=f"Memory directory exists: {memory_dir}",
            remediation=None,
        )

    return CheckResult(
        name="memory_dir_exists",
        severity="critical",
        passed=False,
        message=f"Memory directory not found: {memory_dir}",
        remediation=(
            f"Create the directory or set PALINODE_DIR to an existing path.\n"
            f"  mkdir -p {memory_dir}"
        ),
    )


@register(tags=("fast",))
def memory_dir_writable(ctx: DoctorContext) -> CheckResult:
    """Verify that the running user can create files in config.memory_dir.

    A memory directory that exists but is not writable passes
    ``memory_dir_exists`` and then fails every save.  Writing a file needs
    search permission on the directory as well as write permission, so both
    bits are required here.

    Uses ``os.access`` rather than a write probe, matching
    ``audit_log_writable``.  It is advisory and can disagree with a real
    write, but doctor should not leave files behind in the user's memory
    directory to answer a question.
    """
    memory_dir = Path(ctx.config.memory_dir).expanduser().resolve()

    if not (memory_dir.exists() and memory_dir.is_dir()):
        # memory_dir_exists already reports this. One cause, one failure.
        return CheckResult(
            name="memory_dir_writable",
            severity="critical",
            passed=True,
            message=(
                f"Memory directory is absent, so writability does not apply: "
                f"{memory_dir}"
            ),
            remediation=None,
        )

    if os.access(str(memory_dir), os.W_OK | os.X_OK):
        return CheckResult(
            name="memory_dir_writable",
            severity="critical",
            passed=True,
            message=f"Memory directory is writable: {memory_dir}",
            remediation=None,
        )

    return CheckResult(
        name="memory_dir_writable",
        severity="critical",
        passed=False,
        message=(
            f"Memory directory exists but is not writable: {memory_dir}  "
            "(Every save will fail.)"
        ),
        remediation=(
            f"Check ownership, whether the path is a read-only mount, and the\n"
            f"directory mode. To fix a mode that was tightened by hand:\n"
            f"  chmod u+wx {memory_dir}\n"
            f"To fix an ownership mismatch:\n"
            f"  chown $(id -u):$(id -g) {memory_dir}\n"
            f"Or set PALINODE_DIR to a writable path."
        ),
    )
