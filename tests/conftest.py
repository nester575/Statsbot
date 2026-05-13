"""Shared pytest fixtures.

Mocks heavy dependencies (telegram SDK, psycopg2.connect) so tests run
without a real DB or Telegram credentials.
"""
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 1. Ensure the project root is on sys.path so `import bot` works
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 2. Set required env BEFORE importing bot
os.environ.setdefault("BOT_TOKEN", "testbot")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("ADMIN_TOKEN", "testtoken")
os.environ.setdefault("BOSS_ID", "")

# 3. Stub the telegram SDK (we don't run the bot in tests)
for mod_name in ("telegram", "telegram.ext"):
    if mod_name not in sys.modules:
        sys.modules[mod_name] = types.ModuleType(mod_name)
sys.modules["telegram"].Update = type("Update", (), {})
sys.modules["telegram"].ReplyKeyboardRemove = type("ReplyKeyboardRemove", (), {})
sys.modules["telegram"].InlineKeyboardButton = type("InlineKeyboardButton", (), {})
sys.modules["telegram"].InlineKeyboardMarkup = type("InlineKeyboardMarkup", (), {})
te = sys.modules["telegram.ext"]
te.Application = type("Application", (), {})
te.CommandHandler = type("CommandHandler", (), {})
te.MessageHandler = type("MessageHandler", (), {})
te.CallbackQueryHandler = type("CallbackQueryHandler", (), {})
te.filters = types.SimpleNamespace(TEXT=1, COMMAND=2)
te.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=type)
te.ConversationHandler = type("ConversationHandler", (), {"END": -1})

# 4. Now we can import bot safely (and the modules it composes)
import bot  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import helpers  # noqa: E402
import tg_bot  # noqa: E402


# ---------- DB mocking helpers ----------

class RecordingConn:
    """A psycopg2-conn-shaped mock that records execute() calls and
    returns scripted values from fetchone()/fetchall().

    Usage:
        conn = RecordingConn()
        conn.fetchone_returns = [(0,), ('09:00',)]   # popped FIFO
        conn.fetchall_returns = [[('row1',), ('row2',)]]
    """
    def __init__(self):
        self.queries = []
        self.fetchone_returns = []
        self.fetchall_returns = []
        self.rowcount_default = 1
        self.committed = 0
        self.rolled_back = 0

    # cursor protocol
    def cursor(self, cursor_factory=None):
        self._cursor_factory = cursor_factory
        return self

    def execute(self, q, params=None):
        self.queries.append((q, params))

    def fetchone(self):
        if self.fetchone_returns:
            return self.fetchone_returns.pop(0)
        return (0,)  # default: COUNT(*) returns 0 → triggers seed paths

    def fetchall(self):
        if self.fetchall_returns:
            return self.fetchall_returns.pop(0)
        return []

    @property
    def rowcount(self):
        return self.rowcount_default

    # connection protocol
    def commit(self): self.committed += 1
    def rollback(self): self.rolled_back += 1
    def close(self): pass

    # context manager — returns self for both `with conn:` and `with cur:`
    def __enter__(self): return self
    def __exit__(self, exc_type, *a): return False


@pytest.fixture
def fake_conn(monkeypatch):
    """Replace psycopg2.connect with a RecordingConn; force pool fallback."""
    conn = RecordingConn()
    monkeypatch.setattr("psycopg2.connect", lambda *a, **kw: conn)
    monkeypatch.setattr(db, "_db_pool", None)  # force fallback path in get_conn
    return conn


# ---------- Flask test client ----------

@pytest.fixture
def app():
    return bot.app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth_headers():
    return {"X-Admin-Token": "testtoken"}


@pytest.fixture
def auth_query():
    return "?token=testtoken"


# ---------- Telegram HTTP mocking (for send-reminder tests) ----------

@pytest.fixture
def mock_telegram_http(monkeypatch):
    """Capture all httpx.post calls made by _send_telegram_message."""
    import httpx
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json})
        r = MagicMock()
        r.raise_for_status = lambda: None
        r.json = lambda: {"ok": True, "result": {"message_id": 1}}
        return r

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


# ---------- Convenience: patch any helper for a single test ----------

@pytest.fixture
def patch_helper(monkeypatch):
    """Returns a function that patches bot.<name> with a callable returning a value."""
    def _patch(name, return_value):
        if callable(return_value):
            monkeypatch.setattr(f"bot.{name}", return_value)
        else:
            monkeypatch.setattr(f"bot.{name}", lambda *a, **kw: return_value)
    return _patch
