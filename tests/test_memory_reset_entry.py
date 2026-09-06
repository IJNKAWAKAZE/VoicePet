import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

import app


def test_reset_mode_dispatches_without_ui_or_worker():
    calls = []
    assert app.main(["--reset-test-memory"], reset_entry=lambda: calls.append("reset") or 2,
                    ui_entry=lambda smoke: calls.append("ui"),
                    worker_entry=lambda: calls.append("worker")) == 2
    assert calls == ["reset"]


@pytest.mark.parametrize("other", ["--tool-worker", "--smoke-test"])
def test_reset_mode_is_exclusive(other):
    with pytest.raises(SystemExit):
        app.main(["--reset-test-memory", other], reset_entry=lambda: 0)


def make_database(root):
    database = root / "data" / "assistant.db"
    database.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE memories(id TEXT,content TEXT)")
        connection.execute("INSERT INTO memories VALUES('test','synthetic content')")
        connection.execute("CREATE TABLE audit_probe(value TEXT)")
        connection.execute("INSERT INTO audit_probe VALUES('keep')")
    return database


def test_explicit_reset_only_rebuilds_memory_tables_and_reports_target(tmp_path):
    from core.memory_reset import reset_memory_entry

    database = make_database(tmp_path)
    prompts, notices = [], []
    result = reset_memory_entry(config_path=tmp_path / "config.json",
                                confirm=lambda text: prompts.append(text) or True,
                                notify=lambda title, text: notices.append((title, text)))
    assert result == 0
    assert str(database.resolve()) in prompts[0]
    assert "不可恢复" in prompts[0]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM audit_probe").fetchall() == [("keep",)]
        assert connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert notices


def test_cancel_reset_leaves_database_untouched(tmp_path):
    from core.memory_reset import reset_memory_entry

    database = make_database(tmp_path)
    before = database.read_bytes()
    before_paths = set(tmp_path.rglob("*"))
    assert reset_memory_entry(config_path=tmp_path / "config.json", confirm=lambda text: False,
                               notify=lambda title, text: None) == 2
    assert database.read_bytes() == before
    assert set(tmp_path.rglob("*")) == before_paths
    assert not (tmp_path / ".voicepet-instance.lock").exists()


def test_reset_rejects_database_path_redirected_during_confirmation(tmp_path, monkeypatch):
    from core.memory_reset import reset_memory_entry

    database = make_database(tmp_path)
    outside = tmp_path / "outside.db"
    before = database.read_bytes()
    original_resolve = Path.resolve
    redirected = False

    def confirm(text):
        nonlocal redirected
        assert str(database.resolve()) in text
        redirected = True
        return True

    def resolve(path, *args, **kwargs):
        if redirected and path == database:
            return outside
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    notices = []
    assert reset_memory_entry(config_path=tmp_path / "config.json", confirm=confirm,
                              notify=lambda title, text: notices.append(text)) == 1
    assert database.read_bytes() == before
    assert "已拒绝" in notices[0]


def test_reset_missing_database_does_not_create_it(tmp_path):
    from core.memory_reset import reset_memory_entry

    prompts = []
    assert reset_memory_entry(config_path=tmp_path / "config.json",
                               confirm=lambda text: prompts.append(text) or True,
                               notify=lambda title, text: None) == 0
    assert prompts == []
    assert not (tmp_path / "data" / "assistant.db").exists()


def test_running_instance_blocks_reset_without_changing_data(tmp_path):
    from core.memory_reset import InstanceGuard, reset_memory_entry

    database = make_database(tmp_path)
    before = database.read_bytes()
    notices = []
    with InstanceGuard(tmp_path):
        result = reset_memory_entry(config_path=tmp_path / "config.json", confirm=lambda text: True,
                                    notify=lambda title, text: notices.append(text))
    assert result == 1
    assert database.read_bytes() == before
    assert "退出" in notices[0]


def test_main_ui_wrapper_holds_same_guard_until_ui_exit(tmp_path, monkeypatch):
    from core.memory_reset import InstanceBusyError, InstanceGuard

    monkeypatch.setattr("core.config.default_config_path", lambda: tmp_path / "config.json")

    def ui(*, smoke_test):
        with pytest.raises(InstanceBusyError), InstanceGuard(tmp_path):
            pass
        return 7

    monkeypatch.setattr("ui.application.run_ui", ui)
    assert app._run_ui(False) == 7
    with InstanceGuard(tmp_path):
        pass


def test_windowed_reset_can_report_without_stdout_or_stderr(tmp_path, monkeypatch):
    from core.memory_reset import reset_memory_entry

    notices = []
    monkeypatch.setattr("sys.stdout", None)
    monkeypatch.setattr("sys.stderr", None)
    result = reset_memory_entry(config_path=tmp_path / "config.json", confirm=lambda text: True,
                                notify=lambda title, text: notices.append(text))
    assert result == 0
    assert notices


def test_reset_default_path_comes_from_config_directory(tmp_path, monkeypatch):
    import core.memory_reset as module

    database = make_database(tmp_path)
    monkeypatch.setattr(module, "default_config_path", lambda: tmp_path / "config.json")
    prompts = []
    assert module.reset_memory_entry(confirm=lambda text: prompts.append(text) or False,
                                      notify=lambda title, text: None) == 2
    assert str(Path(database).resolve()) in prompts[0]


def test_guard_blocks_other_process_and_releases_after_exit(tmp_path):
    from core.memory_reset import InstanceGuard

    script = (
        "import sys\nfrom core.memory_reset import InstanceGuard, InstanceBusyError\n"
        "try:\n with InstanceGuard(sys.argv[1]): pass\n"
        "except InstanceBusyError:\n sys.exit(9)\n"
    )
    command = [sys.executable, "-c", script, str(tmp_path)]
    with InstanceGuard(tmp_path):
        assert subprocess.run(command, capture_output=True, timeout=10, check=False).returncode == 9
    assert subprocess.run(command, capture_output=True, timeout=10, check=False).returncode == 0


def test_reset_rejects_resolved_database_outside_application_data(tmp_path, monkeypatch):
    from core.memory_reset import reset_memory_entry

    database = make_database(tmp_path)
    before = database.read_bytes()
    resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda path, *args, **kwargs:
                        tmp_path / "outside.db" if path == database else resolve(path, *args, **kwargs))
    notices, prompts = [], []
    assert reset_memory_entry(config_path=tmp_path / "config.json",
                              confirm=lambda text: prompts.append(text) or True,
                              notify=lambda title, text: notices.append(text)) == 1
    assert prompts == []
    assert database.read_bytes() == before
    assert "已拒绝" in notices[0]


def test_corrupt_database_reports_safe_failure_and_releases_guard(tmp_path):
    from core.memory_reset import InstanceGuard, reset_memory_entry

    database = tmp_path / "data" / "assistant.db"
    database.parent.mkdir()
    database.write_bytes(b"synthetic invalid database")
    notices = []
    assert reset_memory_entry(config_path=tmp_path / "config.json", confirm=lambda text: True,
                              notify=lambda title, text: notices.append(text)) == 1
    assert database.read_bytes() == b"synthetic invalid database"
    assert "synthetic" not in notices[0]
    with InstanceGuard(tmp_path):
        pass


def test_database_removed_during_confirmation_is_not_recreated(tmp_path):
    from core.memory_reset import reset_memory_entry

    database = make_database(tmp_path)

    def confirm(text):
        database.unlink()
        return True

    notices = []
    assert reset_memory_entry(config_path=tmp_path / "config.json", confirm=confirm,
                              notify=lambda title, text: notices.append(text)) == 1
    assert not database.exists()
    assert notices


def test_normal_start_with_old_schema_shows_explicit_reset_without_erasing(tmp_path, monkeypatch):
    from core.memory import MemoryStore

    database = make_database(tmp_path)
    notices = []
    monkeypatch.setattr("core.config.default_config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr("core.memory_reset.show_startup_notice", notices.append)
    monkeypatch.setattr("ui.application.run_ui", lambda **kwargs: MemoryStore(database))
    assert app._run_ui(False) == 1
    assert "--reset-test-memory" in notices[0]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM memories").fetchall() == [("test", "synthetic content")]
        assert connection.execute("SELECT * FROM audit_probe").fetchall() == [("keep",)]
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='voicepet_components'").fetchone() is None


def test_unwritable_instance_directory_shows_safe_startup_notice(tmp_path, monkeypatch):
    root = tmp_path / "not_a_directory"
    root.write_text("preserve", encoding="utf-8")
    notices = []
    monkeypatch.setattr("core.config.default_config_path", lambda: root / "config.json")
    monkeypatch.setattr("core.memory_reset.show_startup_notice", notices.append)
    assert app._run_ui(False) == 1
    assert notices and "目录" in notices[0]
    assert root.read_text(encoding="utf-8") == "preserve"
