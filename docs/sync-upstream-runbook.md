# Sync форка с upstream — пошаговое руководство (runbook)

**Форк:** https://github.com/ComradeVan0/maxapi-synchronous (ветка `main`, sync)
**Upstream:** https://github.com/love-apples/maxapi (ветка `main`, async)

> Здесь — **как выполнять** синхронизацию: шпаргалка конверсии, шаги, верификация, откат.
> (Дизайн-обоснование и спецификация codemod — во внутренней спеке; она в репозиторий не входит.)

---

## 1. Шпаргалка ручной конверсии (Tier-2)

| Async-паттерн | Sync-эквивалент / действие |
|---|---|
| `asyncio.Lock()` / `Semaphore` | удалить (no-op в однопоточном sync); убрать `async with self._lock:` |
| `asyncio.create_task(coro(...))` | вызвать `coro(...)` последовательно; если fire-and-forget — просто вызвать |
| `await asyncio.gather(*xs)` | последовательные вызовы `for x in xs: x()` |
| `await asyncio.wait_for(f, t)` | обычный вызов `f()` (таймаут — через `requests` timeout или убрать) |
| `asyncio.Event` / `.wait()` / `add_done_callback` | убрать координацию; пересмотреть логику (часто не нужна в sync) |
| `async with session.post(url, data=form) as r:` | `r = requests.post(url, data=form, ...)` |
| `async with aiofiles.open(p, "rb") as f:` | `with open(p, "rb") as f:` |
| `async for chunk in resp.content.iter_chunked(n):` | `for chunk in resp.iter_content(n):` (requests) |
| async-генератор `async def ... yield` | `def ... yield` (правило codemod уже снимает `async`) |
| `ClientSession` lifecycle (`ensure_session` + bg close) | `requests.Session()` (переиспользование) или module-level вызовы |

---

## 2. Одноразовая настройка (1 раз)

1. **Upstream remote** (если ещё нет):
   ```bash
   git remote add upstream https://github.com/love-apples/maxapi.git
   git fetch upstream
   ```
   *(Используем `tools/`, а не `scripts/` — `scripts/` в `.gitignore`.)*

2. **Codemod и списки:** реализовать `tools/async_to_sync.py` (см. спеку §4),
   добавить `libcst` в dev-зависимости. Создать:
   - `tools/tier2.txt` — манифест Tier-2 (спека §5.1)
   - `tools/excluded.txt` — исключённые пути (спека §5.2)

3. **Валидация codemod** (доказательство идемпотентности):
   ```bash
   python tools/async_to_sync.py maxapi/
   # ожидание: codemod ничего не меняет (дерево уже sync)
   git diff --exit-code   # должно быть пусто (доказательство идемпотентности)
   uv run ruff check . && uv run mypy maxapi && uv run pytest -m "not integration"
   ```
   `git diff` не пуст — баг в правилах codemod (меняет уже-sync код), чинить до использования.
   `flagged > 0` — либо ложное срабатывание детекции, либо остаток async в коде; разобрать и устранить.

---

## 3. Каждая синхронизация (повторяемо)

**Шаг 0. Подготовка.**
```bash
git checkout main
git pull origin main                          # актуальный main форка
git fetch upstream
git log --oneline HEAD..upstream/main         # что приезжает (контроль)
git checkout -b sync/upstream-<YYYY-MM-DD>    # работа в ветке
```

**Шаг 1. Merge без коммита.**
```bash
git merge --no-ff --no-commit upstream/main
```
Появятся конфликты (ожидаемо). Чистые слияния (файлы, которые форк не трогал)
автомерджатся в async-версию upstream — нормально, codemod их переконвертит.

**Шаг 2. Удалить Excluded-пути (async-специфичные, не переносим).**
Покрывает и delete/modify конфликты upstream в этих областях, и новые файлы upstream
(напр. `context/manager.py`).
```bash
git rm -r --ignore-unmatch maxapi/context maxapi/webhook
git rm --ignore-unmatch maxapi/exceptions/dispatcher.py \
                   maxapi/filters/handler.py \
                   maxapi/filters/middleware.py \
                   maxapi/utils/commands.py
```

**Шаг 3. Триаж оставшихся конфликтов.**
```bash
git diff --name-only --diff-filter=U          # конфликтные файлы (после шага 2)
```
Каждый отнести к категории:
- **delete/modify (форк удалил, upstream изменил)** → `git rm <file>` (оставить удалённым). Покрывает модули, убранные из форка и не входящие в excluded-список Шага 2.
- **Tier-1** (нет в `tools/tier2.txt`) → Шаг 4.
- **Tier-2** (есть в манифесте) → Шаг 5.
- **fork-tweaked Tier-1** (форк вносил свою правку в механический файл,
  напр. `send_message.py` с проверкой пустой клавиатуры) → Шаг 5 (вручную).

**Шаг 4. Tier-1 конфликты — взять upstream.**
Берём async-версию upstream; codemod потом переконвертит.
```bash
git checkout --theirs -- <tier1-file>
git add <tier1-file>
```

**Шаг 5. Tier-2 конфликты — вручную (3-way).**
Открыть, слить логику upstream в sync-версию, сложные места отметить
`# TODO(async2sync): ...`. Дублирующиеся правки (напр. #130 «пустая клавиатура» уже в форке) —
оставить одно решение, `git add`.

**Шаг 6. Завершить состояние merge.**
```bash
git add -A
# merge ещё не закоммичен (--no-commit); переходим к конверсии
```

**Шаг 7. Запуск codemod по всему дереву.**
```bash
python tools/async_to_sync.py maxapi/
```
Конвертирует механику (Tier-1 + чистые слияния), ставит TODO на сложные функции (Tier-2).
Прочитать отчёт: `auto-converted: N; flagged: M`.

**Шаг 8. Ручная работа по TODO (Tier-2).**
```bash
git grep -n "TODO(async2sync)"                # список ручной работы
```
Для каждой функции — конверсия по шпаргалке (§1), проверить новую логику upstream,
**удалить** строку-баннер после конверсии.
Проверить, что новые upstream-импорты не тянут исключённые модули (`context`, `webhook` и др.).
Проверить формы `asyncio`, которые codemod НЕ ловит (он рассчитывает на точечную запись `asyncio.X`; формы `from asyncio import X` и `import asyncio as aio` пропускают и конверсию, и детекцию):
```bash
git grep -nE "from asyncio import|import asyncio as" -- maxapi/ || echo "clean"
```
При находке — `gather`/`Lock`/`Event`/и т.п. из таких импортов убрать вручную, `sleep` → `time.sleep`.

**Шаг 9. Верификация.**
```bash
uv run ruff format .
uv run ruff check . --fix
uv run mypy maxapi
uv run pytest -m "not integration"
git grep -n "TODO(async2sync)"                # должно быть пусто (или сознательно отложено + описано)
```
Опционально — интеграционные тесты (`MAX_BOT_TOKEN`).

**Шаг 10. Коммит и публикация.**
```bash
git add -A
git commit                                    # завершает merge-коммит (+ правки codemod/Tier-2 внутри)
git push origin sync/upstream-<YYYY-MM-DD>    # PR в main форка (или прямой push — на усмотрение)
```
Логика коммитов: один merge-коммит приемлем; при желании codemod/Tier-2 правки вынести отдельными коммитами поверх merge.

**Пост-синхронизация.**
- Зафиксировать новый «upstream synced-through» HEAD для будущего контроля (`git log HEAD..upstream/main` само корректируется).
- Поправить `CLAUDE.md`, если описание устарело.
- Удалить merged-ветку: `git branch -d sync/upstream-<YYYY-MM-DD>`.

---

## 4. Критерии «готово»

- `ruff check`, `mypy maxapi`, `pytest -m "not integration"` — зелёные.
- `git grep "TODO(async2sync)"` — пуст (или отложенные явно описаны).
- Все новые файлы upstream в `maxapi/` присутствуют и сконвертированы; excluded-пути отсутствуют.
- Tier-2 файлы прошли ручной review (особенно `connection/base.py`: SSL #168 + `download_bytes` #112;
  `bot.py`: session lifecycle).
- Smoke-проверка: простой echo-бот из `examples/` запускается и отвечает (examples при этом в репо не меняются).

---

## 5. Откат при сбое

- До коммита merge: `git merge --abort`.
- После неудачного коммита: `git reset --hard ORIG_HEAD`.
