"""Fixed queue scenario for the packaged application's Windows smoke test."""

import logging
from pathlib import Path

from PySide6.QtCore import QTimer


PLAYLIST = "https://www.youtube.com/playlist?list=PLWindowsSmoke"
ARCHIVED = "v0000000001"
NORMAL = "v0000000002"
URGENT = "v0000000003"
PROFILE = "Testowy profil MKV"
PLAYLIST_TITLE = "Playlista testowa"
URGENT_CHANNEL = "Kanał pilnego filmu"


def prepare_queue(window):
    controller = window.c
    assert controller.paused, "The test queue must start paused"
    window.build_profiles_menu()
    next(action for action in window.profiles_menu.actions() if action.text() == PROFILE).trigger()
    window.source.setText(PLAYLIST)
    window.preview_button.click()
    while controller.scan:
        yield
    assert [entry.id for entry in window.preview_model.entries] == [ARCHIVED, NORMAL], window.preview_info.text()
    window.add_button.click()

    window.source.setText(f"https://www.youtube.com/watch?v={URGENT}")
    window.preview_button.click()
    while controller.scan:
        yield
    assert [entry.id for entry in window.preview_model.entries] == [URGENT], window.preview_info.text()
    window.add_first.click()
    assert window.tabs.currentWidget() == window.queue_tab
    assert_saved_queue(window)
    position = window.queue_model.package_rows[controller.store.jobs()[1].package_id][0]
    window.queue_table.selectRow(position)
    window.pause_selected.click()


def assert_saved_queue(window):
    jobs = window.c.store.jobs()
    actual = [(job.entry.id, job.state) for job in jobs]
    assert actual == [(URGENT, "queued"), (ARCHIVED, "skipped"), (NORMAL, "queued")], actual
    assert all(job.options == window.download_profiles[PROFILE] for job in jobs)
    assert jobs[0].package_id is None
    assert jobs[1].package_id == jobs[2].package_id
    assert window.c.store.packages()[jobs[1].package_id].name == PLAYLIST_TITLE


def interrupt_queue(window):
    controller = window.c
    assert controller.paused, "The paused queue was not restored"
    assert_saved_queue(window)
    window.tabs.setCurrentWidget(window.queue_tab)
    package_id = controller.store.jobs()[1].package_id
    assert controller.store.packages()[package_id].paused, "The playlist package pause was not restored"
    position = window.queue_model.package_rows[package_id][0]
    window.queue_table.selectRow(position)
    window.resume_selected.click()
    assert not controller.store.packages()[package_id].paused
    window.pause_all.click()
    folder = Path(window.download_profiles[PROFILE].folder) / URGENT_CHANNEL
    while True:
        jobs = window.c.store.jobs()
        assert all(job.state not in ("failed", "auth", "done") for job in jobs), [(job.id, job.reason) for job in jobs]
        if any(path.stat().st_size >= 65536 for path in folder.glob("*.part")):
            assert jobs[0].entry.id == URGENT and jobs[0].state == "running"
            return
        yield


def resume_queue(window):
    assert not window.c.paused, "The active queue was not restored"
    while True:
        jobs = window.c.store.jobs()
        assert all(job.state not in ("failed", "auth") for job in jobs), [(job.id, job.reason) for job in jobs]
        if [job.state for job in jobs] == ["done", "skipped", "done"]:
            break
        yield
    assert len(window.c.store.history()) == 3, "Expected the imported item and two completed downloads"


def run_queue_smoke(app, window, phase):
    scenario = {"prepare": prepare_queue, "interrupt": interrupt_queue, "resume": resume_queue}[phase](window)

    def advance():
        try:
            if window.c.future is None:
                assert window.c.manager.snapshot().ready, "Bundled dependencies did not start"
                next(scenario)
        except StopIteration:
            app.exit(0)
            return
        except Exception:
            logging.exception("Queue smoke test (%s)", phase)
            app.exit(1)
            return
        QTimer.singleShot(100, advance)

    QTimer.singleShot(100, advance)
    QTimer.singleShot(75_000, lambda: app.exit(2))
