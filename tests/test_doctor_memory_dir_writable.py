"""
Tests for the memory_dir_writable doctor check.

Covers:
  - writable directory passes
  - existing directory with write permission removed fails, with remediation
  - directory with write but no search permission fails
  - absent directory returns the informational pass, not a second failure
  - a file where the directory should be takes the same informational pass

Real directories under tmp_path, real mode bits. No mocking of os.access.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from palinode.core.config import Config
from palinode.diagnostics.runner import run_one
from palinode.diagnostics.types import DoctorContext


def _ctx(memory_dir: Path) -> DoctorContext:
    """Build a DoctorContext pointed at *memory_dir*."""
    cfg = Config(
        memory_dir=str(memory_dir),
        db_path=str(memory_dir / ".palinode.db"),
    )
    cfg.doctor.search_roots = [str(memory_dir)]
    return DoctorContext(config=cfg)


def test_writable_directory_passes(tmp_path: Path) -> None:
    memory_dir = tmp_path / "palinode"
    memory_dir.mkdir()

    result = run_one(_ctx(memory_dir), "memory_dir_writable")

    assert result.passed is True
    assert result.severity == "critical"
    assert result.remediation is None


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX mode bits do not govern directory writability on Windows",
)
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="os.access reports success for root regardless of mode",
)
def test_unwritable_directory_fails_with_remediation(tmp_path: Path) -> None:
    memory_dir = tmp_path / "palinode"
    memory_dir.mkdir()
    original = stat.S_IMODE(memory_dir.stat().st_mode)
    memory_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        result = run_one(_ctx(memory_dir), "memory_dir_writable")
    finally:
        # Without this, tmp_path cleanup fails on some platforms.
        memory_dir.chmod(original)

    assert result.passed is False
    assert result.severity == "critical"
    assert result.remediation is not None
    assert str(memory_dir) in result.message


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX mode bits do not govern directory writability on Windows",
)
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="os.access reports success for root regardless of mode",
)
def test_directory_without_search_permission_fails(tmp_path: Path) -> None:
    """Write permission alone is not enough to create a file inside."""
    memory_dir = tmp_path / "palinode"
    memory_dir.mkdir()
    original = stat.S_IMODE(memory_dir.stat().st_mode)
    memory_dir.chmod(stat.S_IRUSR | stat.S_IWUSR)
    try:
        result = run_one(_ctx(memory_dir), "memory_dir_writable")
    finally:
        memory_dir.chmod(original)

    assert result.passed is False


def test_absent_directory_reports_an_informational_pass(tmp_path: Path) -> None:
    """memory_dir_exists owns the absent case, so this one stays quiet."""
    memory_dir = tmp_path / "does-not-exist"

    result = run_one(_ctx(memory_dir), "memory_dir_writable")

    assert result.passed is True
    assert result.remediation is None
    assert "does not apply" in result.message


def test_file_where_the_directory_should_be_is_not_a_second_failure(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "palinode"
    memory_dir.write_text("not a directory")

    result = run_one(_ctx(memory_dir), "memory_dir_writable")

    assert result.passed is True
