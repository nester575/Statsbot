"""Regression tests for the boss-forwarding code in tg_bot.py.

Bug history: ключи метрик содержат "_" (например, объекты_работа,
план_завтра, клиенты_работа). При parse_mode="Markdown" с нечётным
числом подчёркиваний в сообщении Telegram возвращал 400 BAD_REQUEST
("can't parse entities"), и сообщение боссу не доходило. При этом
БД-запись успешно сохранялась, поэтому отчёт был виден в дашборде,
но не в ленте босса. Это маскировало баг — он был замечен только
визуально.

Эти тесты проверяют структуру кода форвардинга, чтобы баг не вернулся.
"""
from pathlib import Path

import pytest


TG_BOT_SRC = Path(__file__).resolve().parents[1] / "tg_bot.py"


@pytest.fixture(scope="module")
def src():
    return TG_BOT_SRC.read_text(encoding="utf-8")


def test_boss_forward_uses_html_not_markdown(src):
    """Форвардинг боссу должен использовать parse_mode="HTML".

    Markdown V1 ломается на непарных подчёркиваниях в ключах метрик.
    """
    # Найдём блок с send_message боссу
    boss_block_start = src.find("if config.BOSS_ID")
    assert boss_block_start != -1, "boss-forward block must exist"
    boss_block = src[boss_block_start:boss_block_start + 1500]

    assert 'parse_mode="HTML"' in boss_block, (
        "boss-forward must use HTML mode (Markdown breaks on '_' in metric keys)"
    )
    assert 'parse_mode="Markdown"' not in boss_block, (
        "boss-forward must NOT use Markdown — it crashed on metric keys with '_'"
    )


def test_boss_forward_escapes_user_input(src):
    """Имя, ключи и значения должны проходить через html.escape().

    Иначе сотрудник или метрика с символом '<' / '&' сломает HTML-парсер
    тем же образом, что подчёркивания ломали Markdown.
    """
    boss_block_start = src.find("if config.BOSS_ID")
    boss_block = src[boss_block_start:boss_block_start + 1500]

    # html_lib is the alias for html module (avoids collision with helpers)
    assert "html_lib.escape" in boss_block, (
        "boss-forward must escape name/keys/values before HTML send"
    )

    # Считаем количество escape-вызовов — должно быть минимум 3
    # (name + ключ + значение в цикле). Достаточно проверить ≥3.
    escape_count = boss_block.count("html_lib.escape")
    assert escape_count >= 3, (
        f"expected ≥3 html_lib.escape calls (name, key, value), got {escape_count}"
    )


def test_html_lib_imported_with_alias(src):
    """`html` стандартной библиотеки конфликтует с переменными HTML —
    импортируется как html_lib для ясности."""
    assert "import html as html_lib" in src, (
        "must import: `import html as html_lib`"
    )
