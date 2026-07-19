import sys

from tools.async_to_sync import convert_source


def test_smoke_import():
    convert_source("")


def test_desugars_async_keywords():
    src = (
        "async def f():\n"
        "    async for x in s:\n"
        "        pass\n"
        "    async with c() as r:\n"
        "        pass\n"
        "    return [i async for i in s]\n"
    )
    out, _ = convert_source(src)
    assert "async def f():" not in out
    assert "def f():" in out
    assert "for x in s:" in out
    assert "with c() as r:" in out
    assert "[i for i in s]" in out


def test_strips_await():
    src = (
        "async def f():\n"
        "    a = await g()\n"
        "    b = await h(1, 2)\n"
        "    return a + b\n"
    )
    out, _ = convert_source(src)
    assert "await g()" not in out
    assert "g()" in out
    assert "await h(1, 2)" not in out
    assert "h(1, 2)" in out


def test_asyncio_sleep_to_time_sleep_adds_import():
    src = "import asyncio\n\nasync def f():\n    await asyncio.sleep(1)\n"
    out, _ = convert_source(src)
    assert "asyncio.sleep" not in out
    assert "time.sleep(1)" in out
    assert "import time" in out


def test_asyncio_sleep_no_duplicate_time_import():
    src = (
        "import asyncio\n"
        "import time\n"
        "\n"
        "async def f():\n"
        "    await asyncio.sleep(1)\n"
    )
    out, _ = convert_source(src)
    assert out.count("import time") == 1


def test_flags_complex_function():
    src = "async def f():\n    await asyncio.gather(a(), b())\n"
    out, t = convert_source(src)
    assert "# TODO(async2sync)[f]:" in out
    assert "asyncio.gather" in out
    assert t.flagged == 1


def test_does_not_flag_plain_function():
    src = "async def f():\n    return await g()\n"
    out, t = convert_source(src)
    assert "TODO(async2sync)" not in out
    assert t.flagged == 0


def test_flags_async_with_and_for():
    src = (
        "async def f():\n"
        "    async with s() as r:\n"
        "        async for c in r:\n"
        "            pass\n"
    )
    out, t = convert_source(src)
    assert "# TODO(async2sync)[f]:" in out
    assert t.flagged == 1


def test_idempotent_mixed_reasons():
    src = (
        "async def f():\n"
        "    async with c() as r:\n"
        "        await asyncio.gather(a(), b())\n"
        "    return [i async for i in s]\n"
    )
    once, t1 = convert_source(src)
    twice, t2 = convert_source(once)
    assert once == twice
    assert t1.flagged == 1
    assert t2.flagged == 0
    assert once.count("TODO(async2sync)") == 1


def test_idempotent_on_sync_output():
    src = "async def f():\n    await asyncio.gather(a(), b())\n"
    once, _ = convert_source(src)
    twice, _ = convert_source(once)
    assert once == twice


def test_idempotent_on_plain_sync_code():
    src = "def f():\n    return g()\n"
    out, _ = convert_source(src)
    assert out == src


def test_main_rewrites_file_and_reports(tmp_path, capsys, monkeypatch):
    f = tmp_path / "m.py"
    f.write_text(
        "import asyncio\n\nasync def f():\n    await asyncio.sleep(1)\n"
    )
    monkeypatch.setattr(sys, "argv", ["async_to_sync.py", str(f)])
    from tools.async_to_sync import main

    rc = main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "import time" in f.read_text()
    assert "auto-converted: 1" in out
