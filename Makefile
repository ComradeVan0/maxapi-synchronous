SHELL = /bin/bash


.PHONY: upgrade
upgrade:
	uv lock --upgrade


.PHONY: run-test
run-test:
	@echo "Running linters and tests in parallel (uv run)..."
	@status=0; \
	uv run --locked -- ruff check . & p1=$$!; \
	uv run --locked -- ruff format . --check & p2=$$!; \
	uv run --locked -- mypy maxapi & p3=$$!; \
	uv run --locked -- pytest -q & p4=$$!; \
	for p in $$p1 $$p2 $$p3 $$p4; do \
		wait $$p || status=1; \
	done; \
	exit $$status


# Проверки CI, которых нет в run-test: сборка пакета, workflow.
# GH_TOKEN включает онлайн-аудиты zizmor (как в CI), если gh авторизован.
.PHONY: check-ci
check-ci: export GH_TOKEN ?= $(shell gh auth token 2>/dev/null)
check-ci:
	rm -rf dist
	uv build
	uv run --locked --group ci twine check dist/*
	uv run --locked --group ci check-wheel-contents dist/*.whl
	uv run --locked --group ci actionlint
	uv run --locked --group ci zizmor --config zizmor.yml .github/workflows/


.PHONY: format
format:
	@echo "Running ruff formatter..."
	uv run ruff format .
