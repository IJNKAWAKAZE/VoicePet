from concurrent.futures import Future, ThreadPoolExecutor

from PySide6.QtCore import QObject
from PySide6.QtTest import QSignalSpy
from test_settings_interactions import page
from test_settings_viewmodel import Runtime, Store, completed

from core.config import AppConfig
from core.wake_models import WakeModelState
from ui.viewmodels.settings import SettingsViewModel


def test_missing_notice_is_persistent_and_never_downloads_automatically(qapp):
    runtime = Runtime()
    settings = SettingsViewModel(AppConfig(), Store(), runtime=runtime)
    settings.refresh_wake_model()

    assert settings.wakeModelStatus == "missing"
    assert "尚未下载" in settings.wakeModelNotice
    assert "暂不可用" in settings.wakeModelNotice
    settings.set_status_context("ai")
    settings._status_timer.timeout.emit()
    assert "尚未下载" in settings.wakeModelNotice
    assert runtime.wake_downloads == 0
    assert not settings.operationBusy


def test_enabling_checks_model_and_disabling_hides_notice(qapp):
    runtime = Runtime()
    settings = SettingsViewModel(AppConfig(), Store(), runtime=runtime)
    settings.set_field("wake_word", "enabled", False)
    assert settings.wakeModelNotice == ""
    settings.set_field("wake_word", "enabled", True)
    assert "尚未下载" in settings.wakeModelNotice
    settings.set_field("wake_word", "enabled", False)
    assert settings.wakeModelNotice == ""
    assert runtime.wake_downloads == 0


def test_model_check_failure_is_not_reported_as_missing_and_can_retry(qapp):
    runtime = Runtime()
    future = Future()
    future.set_exception(RuntimeError("private backend detail"))
    runtime.wake_model_state = lambda: future
    settings = SettingsViewModel(AppConfig(), Store(), runtime=runtime)
    settings.refresh_wake_model()
    assert settings.wakeModelStatus == "error"
    assert "检查失败" in settings.wakeModelNotice
    assert "private" not in settings.wakeModelNotice

    runtime.wake_model_state = lambda: completed(WakeModelState.READY)
    settings.refresh_wake_model()
    assert settings.wakeModelStatus == "ready"
    assert settings.wakeModelNotice == ""


def test_download_refreshes_state_and_discards_earlier_check(qapp):
    runtime = Runtime()
    stale = Future()
    runtime.wake_model_state = lambda: stale
    settings = SettingsViewModel(AppConfig(), Store(), runtime=runtime)
    settings.refresh_wake_model()
    runtime.wake_model_state = lambda: completed(runtime.wake_state)
    download = Future()
    runtime.download_wake_model = lambda progress: download
    settings.download_wake_model()
    assert settings.wakeModelStatus == "downloading"
    stale.set_result(WakeModelState.MISSING)
    assert settings.wakeModelStatus == "downloading"
    runtime.wake_state = WakeModelState.READY
    download.set_result(None)
    assert settings.wakeModelStatus == "ready"
    assert settings.wakeModelNotice == ""


def test_failed_download_keeps_retry_available(qapp):
    runtime = Runtime()
    download = Future()
    runtime.download_wake_model = lambda progress: download
    settings = SettingsViewModel(AppConfig(), Store(), runtime=runtime)
    settings.download_wake_model()
    download.set_exception(RuntimeError("network"))
    assert settings.wakeModelStatus == "error"
    assert settings.wakeModelNotice
    assert not settings.operationBusy


def test_background_model_check_updates_on_qt_thread(qapp):
    runtime = Runtime()
    future = Future()
    runtime.wake_model_state = lambda: future
    settings = SettingsViewModel(AppConfig(), Store(), runtime=runtime)
    settings.refresh_wake_model()
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(future.set_result, WakeModelState.MISSING).result()
    assert settings.wakeModelStatus == "checking"
    changed = QSignalSpy(settings.wakeModelStatusChanged)
    assert changed.wait(1000)
    assert settings.wakeModelStatus == "missing"


def test_page_checks_when_visible_and_shows_notice_below_toggle(qapp):
    runtime = Runtime()
    settings = SettingsViewModel(AppConfig(), Store(), runtime=runtime)
    with page(qapp, "VoiceSettings", settings, visible=False) as root:
        assert settings.wakeModelStatus == "unknown"
        root.setVisible(True)
        qapp.processEvents()
        notice = root.findChild(QObject, "wakeModelNotice")
        toggle = root.findChild(QObject, "wakeEnabledToggle")
        assert notice is not None
        assert notice.property("visible")
        assert "尚未下载" in notice.property("text")
        assert notice.property("y") >= toggle.property("y") + toggle.property("height")
        assert runtime.wake_downloads == 0
        settings.set_field("wake_word", "enabled", False)
        qapp.processEvents()
        assert not notice.property("visible")
        runtime.wake_state = WakeModelState.READY
        settings.set_field("wake_word", "enabled", True)
        qapp.processEvents()
        assert not notice.property("visible")


def test_cancelled_model_check_and_submit_failure_allow_retry(qapp):
    runtime = Runtime()
    pending = Future()
    runtime.wake_model_state = lambda: pending
    settings = SettingsViewModel(AppConfig(), Store(), runtime=runtime)
    settings.refresh_wake_model()
    pending.cancel()
    assert settings.wakeModelStatus == "error"

    def fail():
        raise RuntimeError("unavailable")

    runtime.wake_model_state = fail
    settings.download_wake_model()
    assert settings.wakeModelStatus == "error"
    assert not settings.operationBusy
    runtime.wake_model_state = lambda: completed(WakeModelState.CACHED)
    settings.refresh_wake_model()
    assert settings.wakeModelStatus == "cached"
    assert "已下载" in settings.wakeModelNotice
