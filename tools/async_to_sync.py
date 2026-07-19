"""Детерминированный codemod async->sync. См. docs/sync-upstream-runbook.md."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import libcst as cst

_BANNER_RE = re.compile(r"^# TODO\(async2sync\)\[(?P<name>[^\]]+)\]")

ASYNCIO_COMPLEX = {
    "gather",
    "create_task",
    "ensure_future",
    "wait",
    "wait_for",
    "shield",
    "Lock",
    "Semaphore",
    "Queue",
    "Event",
    "Condition",
    "get_event_loop",
    "get_running_loop",
    "run_in_executor",
}
ATTR_COMPLEX = {"iter_chunked", "iter_any", "add_done_callback"}
COMPLEX_NAMES = {"ClientSession", "asynccontextmanager"}


def _dotted(node: cst.BaseExpression) -> str | None:
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr.value}" if base else node.attr.value
    return None


def _collect_flagged_names(module: cst.Module) -> set[str]:
    """Возвращает множество имён функций, уже помеченных предыдущим проходом.

    Идемпотентность строится на ключе по *имени* функции (оно стабильно между
    проходами — десахаринг не переименовывает функции), а не по полному тексту
    баннера (тот нестабилен: причины-ключевые слова вроде ``async with``
    исчезают после первого прохода, а причины-вызовы вроде ``asyncio.gather``
    остаются, поэтому та же функция на повторном проходе получает другой текст
    баннера).

    libcst цепляет комментарий непосредственно перед первым оператором
    верхнего уровня к ``module.header``, а не к ``leading_lines`` оператора —
    поэтому проверки только по ``leading_lines`` функции недостаточно. Обход
    всего дерева находит баннеры независимо от того, куда libcst их пристроил.
    """
    names: set[str] = set()

    def walk(node: cst.CSTNode) -> None:
        if isinstance(node, cst.EmptyLine) and node.comment is not None:
            match = _BANNER_RE.match(node.comment.value)
            if match is not None:
                names.add(match.group("name"))
        for child in node.children:
            walk(child)

    walk(module)
    return names


class _AsyncToSync(cst.CSTTransformer):
    def __init__(self) -> None:
        self.needs_time_import = False
        self.auto_converted = 0
        self.flagged = 0
        self._reasons: list[list[str]] = []
        self._existing_flagged_names: set[str] = set()

    # --- детекция (visit) ---
    def visit_Module(self, node: cst.Module) -> None:
        # Снимок имён функций, уже помеченных ранее, чтобы не помечать их
        # повторно при повторном запуске codemod на собственном выводе
        # (идемпотентность). Ключ по имени функции (а не по тексту баннера) —
        # именно это стабильно для функций со смешанными причинами, у которых
        # набор причин меняется между проходами (десахаренные ключевые слова
        # исчезают, сохранённые вызовы остаются).
        self._existing_flagged_names = _collect_flagged_names(node)

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        self._reasons.append([])

    def _record(self, reason: str) -> None:
        if self._reasons:
            self._reasons[-1].append(reason)

    def visit_With(self, node: cst.With) -> None:
        if node.asynchronous is not None:
            self._record("async with")

    def visit_For(self, node: cst.For) -> None:
        if node.asynchronous is not None:
            self._record("async for")

    def visit_CompFor(self, node: cst.CompFor) -> None:
        if node.asynchronous is not None:
            self._record("async comprehension")

    def visit_Call(self, node: cst.Call) -> None:
        name = _dotted(node.func)
        if name and name.startswith("asyncio."):
            attr = name.split(".", 1)[1]
            if attr in ASYNCIO_COMPLEX:
                self._record(f"asyncio.{attr}")
        elif name and name.split(".")[-1] in ATTR_COMPLEX:
            self._record(name.split(".")[-1])

    def visit_Name(self, node: cst.Name) -> None:
        if node.value in COMPLEX_NAMES:
            self._record(node.value)

    # --- трансформации (leave) ---
    def leave_Await(
        self, original_node: cst.Await, updated_node: cst.Await
    ) -> cst.BaseExpression:
        return updated_node.expression

    def leave_With(
        self, original_node: cst.With, updated_node: cst.With
    ) -> cst.BaseStatement:
        if updated_node.asynchronous is not None:
            return updated_node.with_changes(asynchronous=None)
        return updated_node

    def leave_For(
        self, original_node: cst.For, updated_node: cst.For
    ) -> cst.BaseStatement:
        if updated_node.asynchronous is not None:
            return updated_node.with_changes(asynchronous=None)
        return updated_node

    def leave_CompFor(
        self, original_node: cst.CompFor, updated_node: cst.CompFor
    ) -> cst.CompFor:
        if updated_node.asynchronous is not None:
            return updated_node.with_changes(asynchronous=None)
        return updated_node

    def leave_Call(
        self, original_node: cst.Call, updated_node: cst.Call
    ) -> cst.BaseExpression:
        f = updated_node.func
        if (
            isinstance(f, cst.Attribute)
            and isinstance(f.value, cst.Name)
            and f.value.value == "asyncio"
            and f.attr.value == "sleep"
        ):
            self.needs_time_import = True
            return updated_node.with_changes(
                func=f.with_changes(value=cst.Name("time"))
            )
        return updated_node

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        if updated_node.asynchronous is not None:
            updated_node = updated_node.with_changes(asynchronous=None)
            self.auto_converted += 1
        return self._maybe_flag(updated_node)

    def _maybe_flag(self, fn: cst.FunctionDef) -> cst.FunctionDef:
        reasons = self._reasons.pop() if self._reasons else []
        if not reasons:
            return fn
        name = fn.name.value
        if name in self._existing_flagged_names:
            # Уже помечено предыдущим проходом (напр. повторный запуск на
            # сконвертированном выводе). Сложные вызовы намеренно оставлены —
            # поэтому функция снова выводит непустой набор причин, — но
            # помечать её повторно нельзя. Ключ по стабильному имени функции
            # (а не по тексту баннера, который у смешанных функций меняется,
            # т.к. десахаренные ключевые слова исчезают между проходами) —
            # именно это обеспечивает идемпотентность codemod.
            return fn
        self.flagged += 1
        banner_text = (
            f"# TODO(async2sync)[{name}]: "
            + " / ".join(sorted(set(reasons)))
            + " — manual review."
        )
        banner = cst.EmptyLine(comment=cst.Comment(value=banner_text))
        return fn.with_changes(leading_lines=(*fn.leading_lines, banner))


def _has_plain_time_import(module: cst.Module) -> bool:
    for stmt in module.body:
        if isinstance(stmt, cst.SimpleStatementLine):
            for small in stmt.body:
                if isinstance(small, cst.Import):
                    for alias in small.names:
                        if alias.name.value == "time" and alias.asname is None:
                            return True
    return False


def _prepend_time_import(module: cst.Module) -> cst.Module:
    line = cst.SimpleStatementLine(
        body=[cst.Import(names=[cst.ImportAlias(name=cst.Name("time"))])]
    )
    return module.with_changes(body=(line, *module.body))


def convert_source(code: str) -> tuple[str, _AsyncToSync]:
    module = cst.parse_module(code)
    transformer = _AsyncToSync()
    new_module = module.visit(transformer)
    if transformer.needs_time_import and not _has_plain_time_import(
        new_module
    ):
        new_module = _prepend_time_import(new_module)
    return new_module.code, transformer


def _convert_path(path: Path) -> _AsyncToSync:
    code = path.read_text(encoding="utf-8")
    new_code, transformer = convert_source(code)
    if new_code != code:
        path.write_text(new_code, encoding="utf-8")
    return transformer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Детерминированный codemod async->sync."
    )
    parser.add_argument("path", help="файл или директория, напр. maxapi/")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    root = Path(args.path)
    files = sorted(root.rglob("*.py")) if root.is_dir() else [root]

    total_auto = total_flagged = 0
    flagged_files: list[str] = []
    for f in files:
        t = _convert_path(f)
        total_auto += t.auto_converted
        total_flagged += t.flagged
        if t.flagged:
            flagged_files.append(str(f))

    print(f"auto-converted: {total_auto} functions")
    print(f"flagged: {total_flagged} functions")
    for ff in flagged_files:
        print(f"  TODO(async2sync): {ff}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
