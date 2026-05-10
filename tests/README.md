# Tests

136 тестов на критические части `bot.py`. Запускаются за ~0.1 сек, работают
без реальной БД и без подключения к Telegram (моки в `conftest.py`).

## Как запустить

Из корня репозитория:

```bash
pip install -r requirements-dev.txt
pytest
```

Чтобы видеть только результат, без подробного вывода:
```bash
pytest -q
```

Запустить только один файл:
```bash
pytest tests/test_helpers.py
```

Один тест:
```bash
pytest tests/test_helpers.py::TestParseNumber::test_zero_is_valid_number -v
```

## Что покрыто

| Файл | Тестов | Что проверяет |
|------|--------|---------------|
| `test_helpers.py` | 62 | `parse_number`, `parse_hhmm`, `is_working_day`, `period_range` — все edge cases |
| `test_aggregate.py` | 18 | `aggregate_reports` — суммы, серии по датам, комментарии, текстовые поля, многодневные данные |
| `test_admin_api.py` | 40 | Аутентификация (token), CRUD метрик, CRUD сотрудников (вкл. cascade rename), settings, send-reminder |
| `test_scenarios.py` | 16 | Реалистичные сценарии: уровни плана, переименование без потери истории, скрытие/восстановление, онбординг, изменение времени |

## Зачем это всё

Тесты — страховка перед рефакторингом. Если после рефакторинга все 136
зелёные → поведение не сломалось.

## Что НЕ покрыто (намеренно)

- **Telegram-обработчики** (`start`, `handle_answer`, `cancel`, `reminder_job`) —
  они async и зависят от типов из python-telegram-bot. Проверены вручную в проде.
- **Реальная БД** — тесты используют моки `psycopg2.connect`. Для тестов с
  настоящим Postgres нужен docker-compose / testcontainers (Phase 4).
- **Браузерная работа дашборда** — нужен Selenium / Playwright. Проверять
  визуально через `dashboard_preview.html`.

## Как добавить свой тест

```python
# tests/test_my_feature.py
import bot

def test_my_thing():
    assert bot.parse_number("42") == 42.0
```

Тесты запускаются автоматически — `pytest` собирает всё, что начинается с `test_`.
