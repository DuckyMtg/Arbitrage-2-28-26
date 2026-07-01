"""
Gate 1: Offline composition tests.

Marked @pytest.mark.gate1 — deselected by default via pytest.ini addopts.
Run explicitly with:
    pytest -m gate1
    pytest -m gate1 -v

Snapshot selection (in priority order):
    1. GATE1_SNAPSHOT env var — path to a JSON file
    2. tools/out/gate1_snapshot.json (default, produced by gate1_collect.py)
    3. tests/fixtures/mini_snapshot.json — fallback for CI / early development

If no snapshot file exists, tests are skipped with a message directing the
user to run gate1_collect.py.

If the snapshot is more than 30 days old, a warning is emitted but tests
do not fail.
"""
from __future__ import annotations

import json
import os
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest
import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_SNAPSHOT = _REPO_ROOT / "tools" / "out" / "gate1_snapshot.json"
_MINI_SNAPSHOT = _REPO_ROOT / "tests" / "fixtures" / "mini_snapshot.json"
_EXPECTATIONS_PATH = _REPO_ROOT / "app" / "data" / "pool_expectations.yaml"
_KNOWN_FAILURES_PATH = _REPO_ROOT / "tests" / "gate1_known_failures.yaml"

# ---------------------------------------------------------------------------
# Snapshot fixture
# ---------------------------------------------------------------------------

def _resolve_snapshot_path() -> Optional[Path]:
    env = os.environ.get("GATE1_SNAPSHOT")
    if env:
        return Path(env)
    if _DEFAULT_SNAPSHOT.exists():
        return _DEFAULT_SNAPSHOT
    if _MINI_SNAPSHOT.exists():
        return _MINI_SNAPSHOT
    return None


@pytest.fixture(scope="session")
def snapshot() -> dict:
    path = _resolve_snapshot_path()
    if path is None:
        pytest.skip(
            "No Gate 1 snapshot found. Run `python -m tools.gate1_collect` to generate "
            f"{_DEFAULT_SNAPSHOT}, or set GATE1_SNAPSHOT to a file path."
        )
    with open(path) as f:
        data = json.load(f)

    generated_at = data.get("generated_at", "")
    try:
        ts = datetime.fromisoformat(generated_at)
        age_days = (datetime.now(timezone.utc) - ts).days
        if age_days > 30:
            warnings.warn(
                f"Gate 1 snapshot is {age_days} days old ({generated_at}). "
                "Consider re-running gate1_collect.py.",
                stacklevel=2,
            )
    except Exception:
        pass

    return data


@pytest.fixture(scope="session")
def pools_by_key(snapshot: dict) -> dict[str, dict]:
    """Index snapshot pools by 'SET|kind|pool_label'."""
    return {
        f"{p['set']}|{p['kind']}|{p['pool_label']}": p
        for p in snapshot.get("pools", [])
    }


# ---------------------------------------------------------------------------
# Expectations loader
# ---------------------------------------------------------------------------

def _load_expectations() -> dict[str, dict]:
    if not _EXPECTATIONS_PATH.exists():
        return {}
    with open(_EXPECTATIONS_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


# ---------------------------------------------------------------------------
# Test 1: Regression guard — parametrized over pool_expectations.yaml
# ---------------------------------------------------------------------------

def _expectation_params() -> list[pytest.param]:
    expectations = _load_expectations()
    params = []
    for key, entry in sorted(expectations.items()):
        expected = entry.get("expected")
        # unique is optional in the yaml; None means "use pool's own unique mode"
        unique = entry.get("unique")  # may be None
        if expected is None:
            continue
        params.append(pytest.param(key, expected, unique, id=key.replace("|", "-")))
    return params


@pytest.mark.gate1
@pytest.mark.parametrize("exp_key,expected_count,unique_mode", _expectation_params())
def test_expectation_regression(
    pools_by_key: dict[str, dict],
    exp_key: str,
    expected_count: int,
    unique_mode: Optional[str],
) -> None:
    """Each pool_expectations.yaml entry must match the snapshot's count at that unique mode.

    If the pool is not present in the snapshot (e.g. running against mini_snapshot.json),
    the test is skipped — the regression guard only fires when the pool is actually fetched.

    unique_mode None (no 'unique:' key in yaml) → compare against primary_count, which
    is always fetched at the pool's own unique setting.
    """
    pool = pools_by_key.get(exp_key)
    if pool is None:
        pytest.skip(
            f"{exp_key}: pool not in this snapshot "
            "(expected when using mini_snapshot.json)"
        )

    if unique_mode is None:
        actual = pool["primary_count"]
        mode_label = f"primary (pool.unique={pool['unique']})"
    elif unique_mode == "prints":
        actual = pool["count_prints"]
        mode_label = "prints"
    elif unique_mode == "cards":
        actual = pool["count_cards"]
        mode_label = "cards"
    else:
        actual = pool["primary_count"]
        mode_label = unique_mode

    assert actual == expected_count, (
        f"{exp_key}: expected {expected_count} (unique={mode_label}) "
        f"but snapshot has count_prints={pool['count_prints']}, "
        f"count_cards={pool['count_cards']}, primary_count={pool['primary_count']}"
    )


# ---------------------------------------------------------------------------
# Test 2: FAIL_ZERO ratchet
# ---------------------------------------------------------------------------

def _is_fail_zero(pool: dict, expected_count: Optional[int]) -> bool:
    """Return True iff this pool classifies as FAIL_ZERO under Gate 1 rules."""
    primary = pool["primary_count"]
    if primary == -1:
        return False  # FETCH_ERROR
    if primary != 0:
        return False
    # primary == 0
    if expected_count == 0:
        return False  # expected 0 → PASS, not FAIL_ZERO
    fb = pool.get("fallback_count")
    return fb is None or fb == 0


def _load_known_failures() -> set[str]:
    if not _KNOWN_FAILURES_PATH.exists():
        return set()
    with open(_KNOWN_FAILURES_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f) or []
    return set(data)


@pytest.mark.gate1
def test_fail_zero_ratchet(snapshot: dict) -> None:
    """
    No new FAIL_ZERO pools may appear without being quarantined.
    Every quarantined pool must still be FAIL_ZERO (ratchet only shrinks).

    A pool with expected_count == 0 in pool_expectations.yaml is classified
    as PASS (not FAIL_ZERO) and is excluded from this ratchet.
    """
    known_failures = _load_known_failures()
    expectations = _load_expectations()

    actual_fail_zero: set[str] = set()
    for pool in snapshot.get("pools", []):
        key = f"{pool['set']}|{pool['kind']}|{pool['pool_label']}"
        exp_entry = expectations.get(key)
        expected_count: Optional[int] = exp_entry.get("expected") if exp_entry else None
        if _is_fail_zero(pool, expected_count):
            actual_fail_zero.add(key)

    new_failures = actual_fail_zero - known_failures
    stale_quarantine = known_failures - actual_fail_zero

    messages: list[str] = []
    if new_failures:
        messages.append(
            "New FAIL_ZERO pool(s) detected — fix the query or add to "
            "tests/gate1_known_failures.yaml:\n"
            + "\n".join(f"  - {k}" for k in sorted(new_failures))
        )
    if stale_quarantine:
        messages.append(
            "Pool(s) in gate1_known_failures.yaml are no longer FAIL_ZERO — "
            "remove them from the quarantine file (the ratchet only shrinks):\n"
            + "\n".join(f"  - {k}" for k in sorted(stale_quarantine))
        )

    if messages:
        pytest.fail("\n\n".join(messages))
