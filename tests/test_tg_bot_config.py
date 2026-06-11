"""Tests for telegram bot setup invariants.

These are static checks against tg_bot.py source — they don't run the
bot itself but verify critical configuration that's hard to test by
running the bot in isolation.
"""
from pathlib import Path


SRC = Path(__file__).parent.parent / "tg_bot.py"


class TestConversationHandlerConfig:
    @classmethod
    def setup_class(cls):
        cls.source = SRC.read_text(encoding="utf-8")

    def test_allow_reentry_is_true(self):
        """CRITICAL: /start must always re-enter the conversation, even if
        the user is currently in ASKING state.

        Without allow_reentry=True, users who started a survey but didn't
        finish get stuck — every subsequent /start is silently ignored,
        and only /cancel can unstick them.

        This was discovered when Эльдана's /start went unanswered for days.
        """
        assert "allow_reentry=True" in self.source, (
            "ConversationHandler must have allow_reentry=True to prevent "
            "stuck-state bug. See git blame for context."
        )

    def test_cancel_fallback_present(self):
        """/cancel must remain in fallbacks so users have a safety hatch
        even if allow_reentry breaks somehow."""
        assert 'CommandHandler("cancel", cancel)' in self.source

    def test_start_entry_point_present(self):
        assert 'CommandHandler("start", start)' in self.source

    def test_post_init_captures_event_loop(self):
        """For cross-thread reschedule from Flask to work, the event loop
        must be captured at startup."""
        assert "post_init(_post_init)" in self.source
        assert "TG_LOOP = asyncio.get_running_loop()" in self.source
