# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

maxapi is a **synchronous** Python client library for the MAX Messenger API (Russian messaging platform). It uses **requests** for HTTP, Pydantic v2 for data models, and targets Python 3.10+. The public API is `from maxapi import Bot, F` — a synchronous `Bot` HTTP client wrapping all API methods, plus the `F` magic filter. This repository is the **synchronous port** of the upstream async library (`love-apples/maxapi`); the async dispatch/polling/webhook/FSM layers are not present here (see *Syncing with upstream* below).

## Commands

Uses **uv** for package management. Dev dependencies are included by default.

```bash
# Install dependencies
uv sync

# Run all checks (lint + format check + mypy + tests) in parallel
make run-test

# Individual commands
uv run ruff check .              # Lint
uv run ruff check --fix .        # Lint with auto-fix
uv run ruff format .             # Format
uv run ruff format . --check     # Check formatting
uv run mypy maxapi               # Type check
uv run pytest                    # Run all tests
uv run pytest tests/test_bot.py  # Run a single test file
uv run pytest -k "test_name"     # Run tests matching a name
uv run pytest -m "not integration"  # Skip integration tests (require MAX_BOT_TOKEN env var)

# Format only
make format
```

## Code Style

- **Line length**: 79 chars, **indent**: 4 spaces, **quotes**: double, **line ending**: LF
- **Ruff** with `select = ["ALL"]` and many ignores (see pyproject.toml for full list)
- **MyPy** with `ignore_missing_imports = true`, `check_untyped_defs = true`
- Pre-commit hooks: ruff check --fix, ruff format, mypy maxapi

## Architecture

### Core Entry Points

`from maxapi import Bot, F` — the public API.

- **Bot** (`maxapi/bot.py`) — synchronous HTTP client wrapping all MAX API methods (`send_message`, `get_updates`, `get_my_info`, etc.). Inherits from `BaseConnection` for retry/backoff. Token is read from the `MAX_BOT_TOKEN` env var.
- **F** (`maxapi/filters/`) — `MagicFilter` object for declarative condition matching in user code.

> Note: unlike upstream, this sync fork has **no `Dispatcher`/`Router`** — there is no built-in event loop, polling runner, or handler-registration framework. You call `Bot` methods directly.

### Usage / Call Flow

There is no framework event loop. The `Bot` is a direct synchronous client:

1. Instantiate: `bot = Bot()` (reads `MAX_BOT_TOKEN`).
2. Call API methods directly — each performs a synchronous HTTP request and returns a typed Pydantic model, e.g. `bot.send_message(user_id=..., text=...)`, `bot.get_my_info()`.
3. To receive updates, call `bot.get_updates(...)` and poll in your own loop, dispatching with your own logic; `F` / `BaseFilter` can express match conditions.
4. Errors surface as synchronous exceptions (e.g. `MaxApiError`, `MaxConnection`).

### Key Subsystems

- **Filters** (`maxapi/filters/`) — `MagicFilter` (via `F`) for declarative matching like `F.message.body.text == "hello"`; `BaseFilter` for custom filters. Operator precedence is validated for safety.
- **Methods** (`maxapi/methods/`) — Each API endpoint is a class (`SendMessage`, `EditMessage`, `GetUpdates`, etc.) with typed request/response models, surfaced as methods on `Bot`.
- **Types** (`maxapi/types/`) — Pydantic models for all API entities (messages, chats, users, attachments, updates). Shortcut mixins add convenience helpers.
- **Utils** (`maxapi/utils/`) — formatting (`as_html`/`as_markdown`), inline-keyboard builder, message-link helpers, upload helpers.
- **Connection** (`maxapi/connection/base.py`) — HTTP layer (requests) with configurable retries and backoff for 5xx errors.

> Removed in this sync fork (still present in upstream): `Dispatcher`/`Router`, polling/webhook runners (`maxapi/webhook/`), and FSM/context (`maxapi/context/`).

### Pydantic Patterns

- All API models inherit from `pydantic.BaseModel`
- `flake8-type-checking.runtime-evaluated-base-classes` is configured for Pydantic, so use `TYPE_CHECKING` imports where possible
- Empty payload segments are converted to `None` for Optional fields

### Testing

- **pytest** (synchronous) — test functions are plain `def`; no asyncio plugin.
- **responses** for mocking `requests` HTTP calls.
- Integration tests require the `MAX_BOT_TOKEN` secret and are marked with `@pytest.mark.integration`.
- Tests live in `tests/` mirroring the source structure.

## Syncing with upstream (this is the sync fork)

This repo is the **synchronous port** of maxapi. Upstream `love-apples/maxapi` (git remote `upstream`, branch `main`) is **async** (aiohttp). When porting upstream changes, async code must be converted to sync. **Full step-by-step: `docs/sync-upstream-runbook.md`.**

Repeatable sync process:
1. `git merge --no-commit upstream/main`.
2. Prune **excluded paths** (deleted from fork as async-specific, must NOT come back): `maxapi/context/`, `maxapi/webhook/`, `maxapi/exceptions/dispatcher.py`, `maxapi/filters/handler.py`, `maxapi/filters/middleware.py`, `maxapi/utils/commands.py`. Any **delete/modify** conflict (fork deleted, upstream modified) → `git rm` (keep deleted).
3. Resolve **Tier-1** conflicts with `git checkout --theirs` (take upstream's async version — the codemod reconverts it).
4. Resolve **Tier-2** conflicts manually (3-way merge): `maxapi/connection/base.py`, `maxapi/bot.py`, `maxapi/types/shortcuts.py`, `maxapi/types/chats.py`, `maxapi/types/fetchable.py`.
5. Run **codemod**: `python tools/async_to_sync.py maxapi/` — converts mechanical async→sync and flags complex functions with `# TODO(async2sync): ...`.
6. Work the TODO list (`git grep -n "TODO(async2sync)"`), then verify: `uv run ruff check . && uv run mypy maxapi && uv run pytest -m "not integration"`.

**Codemod** (`tools/async_to_sync.py`, idempotent, uses `libcst`): mechanical rules — drop `async`/`await`, `asyncio.sleep`→`time.sleep`. Patterns it **flags** for manual review (does NOT auto-convert): `create_task`/`gather`/`Lock`/`Event`/`wait_for`/`async with`/`async for`/aiohttp streaming.

Notes: scope is `maxapi/` only — do **not** touch `examples/`. `scripts/` is gitignored, so sync tooling lives in `tools/`.