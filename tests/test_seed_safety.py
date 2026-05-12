"""Tests for seed.py — focused on the SEED_OK safety guard.

The guard prevents accidental data wipes if seed.py is ever invoked without
explicit intent (e.g., misconfigured pre-deploy step).
"""
import os
import sys
import types
import importlib

import pytest


@pytest.fixture
def fake_psycopg2(monkeypatch):
    """Replace psycopg2 with a recording mock; collect SQL calls."""
    mock = types.ModuleType("psycopg2")
    calls = []

    class FakeCursor:
        def execute(self, q, params=None):
            calls.append((q, params))
        def fetchone(self): return (0,)
        def close(self): pass

    class FakeConn:
        def cursor(self): return FakeCursor()
        def commit(self): pass
        def close(self): pass

    mock.connect = lambda *a, **kw: FakeConn()
    monkeypatch.setitem(sys.modules, "psycopg2", mock)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    return calls


def _run_seed(argv, env=None):
    """Reload seed.py and run main() with given argv + env."""
    if env:
        for k, v in env.items():
            os.environ[k] = v
    sys.argv = argv

    # seed.py lives at project root — make sure import path includes it
    proj_root = os.path.join(os.path.dirname(__file__), "..")
    if proj_root not in sys.path:
        sys.path.insert(0, proj_root)

    if "seed" in sys.modules:
        importlib.reload(sys.modules["seed"])
    else:
        import seed  # noqa: F401
    sys.modules["seed"].main()


class TestSeedSafetyGuard:
    def test_refuses_without_seed_ok_or_force(self, fake_psycopg2, capsys, monkeypatch):
        """Default behavior — refuses to run, doesn't touch DB."""
        monkeypatch.delenv("SEED_OK", raising=False)
        _run_seed(["seed.py", "--wipe-today"])
        out = capsys.readouterr().out
        assert "ЗАБЛОКИРОВАН" in out
        # No DB calls at all — connect() should NOT have been called.
        # If it had, fake_psycopg2 list would contain SQL queries.
        assert fake_psycopg2 == []

    def test_seed_ok_env_unlocks(self, fake_psycopg2, capsys, monkeypatch):
        """SEED_OK=1 — bypasses the guard, normal flow runs."""
        monkeypatch.setenv("SEED_OK", "1")
        _run_seed(["seed.py", "--wipe-today"])
        out = capsys.readouterr().out
        assert "ЗАБЛОКИРОВАН" not in out
        # DB was touched — at least the CREATE TABLE call ran.
        assert len(fake_psycopg2) > 0

    def test_force_flag_unlocks(self, fake_psycopg2, capsys, monkeypatch):
        """--force flag — bypasses guard for explicit CLI usage."""
        monkeypatch.delenv("SEED_OK", raising=False)
        _run_seed(["seed.py", "--wipe-today", "--force"])
        out = capsys.readouterr().out
        assert "ЗАБЛОКИРОВАН" not in out
        assert len(fake_psycopg2) > 0

    def test_arbitrary_seed_ok_value_does_not_unlock(self, fake_psycopg2, capsys, monkeypatch):
        """Only SEED_OK=1 unlocks — random values should not."""
        monkeypatch.setenv("SEED_OK", "yes")  # truthy in Python but not "1"
        _run_seed(["seed.py", "--wipe-today"])
        out = capsys.readouterr().out
        assert "ЗАБЛОКИРОВАН" in out

    def test_guard_message_mentions_both_unlock_paths(self, fake_psycopg2, capsys, monkeypatch):
        """The user-facing error should tell admin how to proceed legitimately."""
        monkeypatch.delenv("SEED_OK", raising=False)
        _run_seed(["seed.py", "--wipe-today"])
        out = capsys.readouterr().out
        assert "SEED_OK=1" in out
        assert "--force" in out
