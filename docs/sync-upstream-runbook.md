# Sync форка с upstream — runbook

**Форк:** https://github.com/ComradeVan0/maxapi-synchronous (ветка `main`, **синхронная** версия)
**Upstream:** https://github.com/love-apples/maxapi (ветка `main`, **асинхронная** версия)

---

## О чём это

Этот форк — **синхронная** (sync) копия **асинхронной** (async) библиотеки maxapi. Оригинал (upstream) развивается как async, а нам нужно переносить его свежие коммиты в наш sync-форк, превращая async-код обратно в sync.

**Проблема:** ручной перевод async→sync порождает баги (легко забыть `await`, перепутать `gather`, оставить `asyncio.Lock` и т.п.).

**Решение:** мы **не переводим руками**. Мы втягиваем async-код upstream как есть, а затем скармливаем его скрипту `tools/async_to_sync.py` (**codemod**), который делает механическую конверсию детерминированно и без багов. Отсюда главное правило (см. ниже): **Tier-1 файлы берём у upstream как async и НЕ трогаем руками — codemod сам переведёт**.

**Кто что делает:** человек — git-механику (merge, `checkout`, `rm`, `commit`); codemod — механическую конверсию; агент — сложную семантическую конверсию и проверку качества.

---

## Глоссарий

| Термин | Что значит |
|---|---|
| **Tier-1** | «Простые» файлы: их async→sync конверсия чисто механическая (убрать `async`/`await`). Codemod делает её сам. Пример: `maxapi/methods/*.py`. |
| **Tier-2** | «Сложные» файлы: в форке переписаны вручную (aiohttp→requests и т.п.), codemod их полностью не осилит — нужна ручная/агентская работа. Список: `tools/tier2.txt`. |
| **Excluded** | Файлы, которых в форке быть **не должно** (async-специфичные: `dispatcher`, `context/`, `webhook/`, …). Удаляются при merge. Список: `tools/excluded.txt`. |
| **`--theirs`** | При конфликте merge — взять версию **upstream** (ту, что вливается). |
| **`--ours`** | При конфликте merge — взять версию **нашего форка** (текущую). |
| **codemod** | Скрипт `tools/async_to_sync.py` — механически превращает async в sync и помечает сложные места `# TODO(async2sync)`. |

---

## Легенда: кто что делает

| Метка | Делает |
|---|---|
| 🧑 **Человек** | Механику git: `merge`, `checkout --theirs/--ours`, `git rm`, `commit`, `push` |
| 🤖 **Агент** | codemod, конверсию async→sync, QA-проверки, правки mypy/test |

**Точка передачи:** человек делает Шаги 1–5 (доводит merge до committable-состояния) → передаёт агенту Шаги 6–8 (codemod + проверки + конверсия) → вместе Шаг 9 (верификация) → человек Шаг 10 (коммит).

---

## 0. Одноразовая настройка (1 раз)

1. **Upstream remote:**
   ```bash
   git remote add upstream https://github.com/love-apples/maxapi.git
   git fetch upstream
   ```
2. **Codemod и списки** уже в репо: `tools/async_to_sync.py`, `tools/tier2.txt`, `tools/excluded.txt`.
3. **Проверка codemod** (на sync-дереве он ничего не должен менять):
   ```bash
   uv run python -m tools.async_to_sync maxapi/   # ожидание: auto-converted: 0, flagged: 0
   git diff --exit-code                            # пусто = всё ок
   ```

---

## 1. Шпаргалка конверсии (🤖 для Tier-2)

| Async-паттерн | Sync-эквивалент / действие |
|---|---|
| `asyncio.Lock()` / `Semaphore` | удалить (no-op в однопоточном sync); убрать `async with self._lock:` |
| `asyncio.create_task(coro(...))` | вызвать `coro(...)` последовательно; fire-and-forget — просто вызвать |
| `await asyncio.gather(*xs)` | последовательные вызовы `for x in xs: x()` |
| `await asyncio.wait_for(f, t)` | обычный вызов `f()` (таймаут — через `requests` timeout или убрать) |
| `asyncio.Event` / `.wait()` / `add_done_callback` | убрать координацию; пересмотреть логику |
| `async with session.post(url, data=form) as r:` | `r = requests.post(url, data=form, ...)` |
| `async with aiofiles.open(p, "rb") as f:` | `with open(p, "rb") as f:` |
| `async for chunk in resp.content.iter_chunked(n):` | `for chunk in resp.iter_content(n):` (requests) |
| async-генератор `async def ... yield` | `def ... yield` (codemod уже снял `async`) |
| `ClientSession` lifecycle | `requests.Session()` или module-level вызовы |

---

## 2. Списки (источник истины для автоматики)

- **`tools/excluded.txt`** — что удалять одной командой после merge (удалённые из форка файлы + тесты удалённых подсистем + новые несовместимые файлы upstream). Править при появлении новых несовместимостей.
- **`tools/tier2.txt`** — файлы, поддерживаемые вручную (Tier-2): `connection/base.py`, `bot.py`, `types/shortcuts.py`, `types/chats.py`, `types/fetchable.py`.

---

## 3. Синхронизация — пошагово

### 🧑 Шаг 1. Подготовка
```bash
git checkout main && git pull origin main
git fetch upstream
git log --oneline HEAD..upstream/main          # посмотреть, что приезжает
git checkout -b sync/upstream-<YYYY-MM-DD>     # дата = сегодня
```

### 🧑 Шаг 2. Merge без коммита
```bash
git merge --no-ff --no-commit upstream/main
```
Конфликты ожидаемы: многие Tier-1 файлы (`methods/*` и т.п.) сольются **сами без конфликта** (наши sync-правки и новые 
фичи upstream обычно на разных строках), поэтому реальных конфликтов меньше, чем кажется.

### 🧑 Шаг 3. Удалить excluded-пути (одной командой)
```bash
grep -vE '^#|^$' tools/excluded.txt | xargs git rm -r --ignore-unmatch
```
Эта команда убирает все async-специфичные файлы, которых в форке быть не должно (dispatcher, context/, webhook/, и их тесты).

### 🧑 Шаг 4. Разрешить оставшиеся конфликты — ГЛАВНОЕ ПРАВИЛО

> ⚠️ **ГЛАВНОЕ ПРАВИЛО: при конфликте в Tier-1 файле бери версию upstream (`--theirs`), даже если она async. НЕ сливай и не переводи в sync руками — это сделает codemod на Шаге 6. Ручной перевод = баги.**

Пример (конфликт в Tier-1 файле):
```bash
# НЕ открывай редактор, НЕ разбирай <<<< markers. Просто:
git checkout --theirs -- maxapi/methods/get_me.py
git add maxapi/methods/get_me.py
# Готово — позже codemod превратит эту async-версию в sync.
```

Как определить категорию конфликтного файла (список: `git diff --name-only --diff-filter=U`):

| Файл | Категория | Что делать |
|---|---|---|
| `maxapi/__init__.py` | спец | `git checkout --ours` (там наши экспорты `Bot, F`; upstream тащит `Dispatcher`/`Router`) |
| путь есть в `tools/tier2.txt` | **Tier-2** | `git checkout --ours` (🤖 агент перенесёт новые фичи upstream в нашу sync-версию на Шаге 8) |
| `tests/*` | тесты | `git checkout --ours` (наши sync-тесты; новые async-тесты upstream — 🤖 конвертирует/удалит на Шаге 8) |
| остальное в `maxapi/` (`methods/`, `enums/`, `types/`, `utils/`, …) | **Tier-1** | `git checkout --theirs` + `git add` (🤖 codemod переведёт) |
| файл, где у нас своя правка в «простом» модуле (напр. `methods/send_message.py` — пустая клавиатура) | спец | вручную (3-way), чтобы не потерять нашу правку |
| `docs/`, `examples/`, `README` | контент | на усмотрение (обычно версию upstream); `examples/` не трогать |

> Не уверен, к какой категории файл? Если это `maxapi/*.py` и его нет в `tier2.txt` — это Tier-1 (`--theirs`). Если сомневаешься — спроси агента.

### 🧑 Шаг 5. Завершить merge-состояние
```bash
git add -A
```
Дерево готово к коммиту, но async-код в Tier-1 ещё не переведён (это сделает codemod). **Не коммитить.** → Передать агенту.

---

### 🤖 Шаг 6. Codemod
```bash
uv run python -m tools.async_to_sync maxapi/
```
Скрипт превратит механический async в sync (Tier-1 + авто-слияния) и пометит сложные функции `# TODO(async2sync)`. Прочитать отчёт: `auto-converted: N; flagged: M`.

### 🤖 Шаг 7. QA-проверки (ловля багов, которые codemod не видит)
Codemod синтаксический — он не понимает семантику импортов и не знает про async-библиотеки. Поэтому прогнать эти sweepe (если строка `clean` — порядок; если есть пути — чинить):

```bash
# 1. Импорты удалённых модулей (ломают всю импорт-цепочку):
git grep -nE "from \.\.?context|from \.\.?webhook|from \.dispatcher|StateFilter|ErrorEvent|ExceptionTypeFilter" -- maxapi/ || echo "clean"
# → нашёл: файл ссылается на удалённую подсистему. Удали файл (если он от удалённой фичи) и добавь путь в tools/excluded.txt.

# 2. Остатки async-библиотек (aiohttp/aiofiles/asyncio/ClientSession):
git grep -nE "aiohttp|aiofiles|TCPConnector|ClientSession|import asyncio|from asyncio" -- maxapi/ || echo "clean"
# → нашёл: остаток async. Переведи (requests/time) или удали.

# 3. Формы `from asyncio import X` / `import asyncio as aio` (codemod их пропускает):
git grep -nE "from asyncio import|import asyncio as" -- maxapi/ || echo "clean"
# → нашёл: перепиши на точечную форму или замени вручную.

# 4. Codemod должен быть no-op на результате (повторный запуск ничего не меняет):
uv run python -m tools.async_to_sync maxapi/   # ожидание: 0/0, git diff пуст
```

### 🤖 Шаг 8. Tier-2 конверсия + async-тесты
- **TODO-список:** `git grep -n "TODO(async2sync)" -- maxapi/` → конверсия по шпаргалке (§1), удалить баннер после.
- **Tier-2 порты:** перенести новые фичи upstream в sync-версии Tier-2 файлов (напр. SSL → `requests verify=`, проверить `download_bytes`, добавить deprecation-предупреждения).
- **Async-тесты:** новые тест-файлы upstream (`async def test_`/`AsyncMock`) → конвертировать в sync (`def test_`, `MagicMock`, мок через `patch.object(BaseConnection, "request", new=MagicMock(...))`). Утверждения **не ослаблять**. Если тест проверяет удалённую фичу — удалить и добавить путь в `tools/excluded.txt`.

---

### 🤖 + 🧑 Шаг 9. Верификация
```bash
uv run ruff format .
uv run ruff check . --fix          # авто-фиксы; non-fixable baseline в maxapi/ допустимы (см. §4)
uv run mypy maxapi                 # 0 ошибок
uv run pytest -m "not integration"
git grep -n "TODO(async2sync)" -- maxapi/ || echo "clean"
```
Опционально — интеграционные тесты (`MAX_BOT_TOKEN`) и smoke (echo-бот из `examples/`).

### 🧑 Шаг 10. Коммит и публикация
```bash
git add -A
git commit                        # завершает merge-коммит
git push origin sync/upstream-<YYYY-MM-DD>
```
Открыть PR `sync/upstream-<YYYY-MM-DD>` → `main`.

**Пост-синхронизация:** следующий sync отслеживается сам через `git log HEAD..upstream/main`; удалить merged-ветку.

---

## 4. Известные грабли

- **`maxapi/__init__.py`**: upstream экспортирует `Dispatcher`/`Router`/`ErrorEvent`/`ExceptionTypeFilter` (всё удалено в sync). Брать `--ours` (экспорты `Bot, F`).
- **Новые upstream-файлы, импортирующие удалённое ядро**: upstream добавляет `filters/state.py`, `types/error_event.py`, `filters/exception_type.py` — они зависят от удалённого `context`/`dispatcher`. Ловятся Шагом 7 (grep); удалить и добавить в `tools/excluded.txt`.
- **`ruff`/`mypy` baseline-долг** в `maxapi/` (особенно `connection/base.py`, `send_message.py` — для них `C90` ignore в `pyproject.toml`): это pre-existing, не блокирует. Чистыми должны быть только изменённые файлы.
- **Тесты upstream async** (`aresponses`/`AsyncMock`/`async def test_`): codemod `aresponses`→`responses` НЕ делает — конверсия вручную/агентом.
- **`client/ssl.py`** теперь sync (requests `verify=`) — поддерживается вручную; если upstream меняет его async-версию, это Tier-2 конфликт (НЕ excluded).

---

## 5. Критерии «готово»

- `mypy maxapi` — 0 ошибок; `pytest -m "not integration"` — зелёные.
- `git grep "TODO(async2sync)" -- maxapi/` — пуст.
- Все sweepe Шага 7 — `clean`.
- Codemod no-op на результате (дерево полностью sync).
- Изменённые файлы — ruff-чистые.

---

## 6. Откат

- До коммита merge: `git merge --abort`.
- После неудачного коммита: `git reset --hard ORIG_HEAD`.
