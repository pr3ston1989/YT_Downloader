from collections import Counter
from dataclasses import replace
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import subprocess
import winreg

from PySide6.QtCore import QAbstractTableModel, QLockFile, QModelIndex, QSortFilterProxyModel, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSpinBox,
    QStyle, QStyleOptionProgressBar, QStyledItemDelegate, QSystemTrayIcon, QTabWidget,
    QTableView, QVBoxLayout, QWidget,
)

from . import APP_NAME, APP_VERSION
from .auth import crypt, youtube_cookie_file
from .controller import Controller, LABELS
from .model import APP_DIR, CHANNEL_TABS, Options, TERMINAL, parse_source
from .runtime import RESOURCES, atomic_bytes, command
from .updates.application import restart_for


LOG = logging.getLogger(__name__)
TAB_NAMES = {"videos": "Filmy", "shorts": "Shorty", "streams": "Transmisje"}
SOURCE_NAMES = {"video": "Film", "playlist": "Playlista", "channel": "Kanał", "album": "Album"}
MUSIC_SOURCE_NAMES = {**SOURCE_NAMES, "video": "Utwór", "channel": "Kanał wykonawcy"}
DISPLAY = Qt.ItemDataRole.DisplayRole
CHECK = Qt.ItemDataRole.CheckStateRole
USER = Qt.ItemDataRole.UserRole


def row(*widgets):
    wrapper = QWidget()
    layout = QHBoxLayout(wrapper)
    layout.setContentsMargins(0, 0, 0, 0)
    for widget in widgets:
        if widget is None:
            layout.addStretch()
        else:
            layout.addWidget(widget)
    return wrapper


def button(title, action, *, primary=False):
    widget = QPushButton(title)
    widget.clicked.connect(action)
    if primary:
        widget.setProperty("primary", True)
    return widget


def combo(items, selected):
    widget = QComboBox()
    for label, value in items:
        widget.addItem(label, value)
    widget.setCurrentIndex(max(0, widget.findData(selected)))
    return widget


def checkbox(label, checked=False):
    widget = QCheckBox(label)
    widget.setChecked(checked)
    return widget


def stamp(epoch):
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


def duration(seconds):
    if seconds is None:
        return "—"
    hours, remainder = divmod(int(seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


class TableModel(QAbstractTableModel):
    def __init__(self, headers):
        super().__init__()
        self.headers = headers
        self.rows = []

    def replace(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=DISPLAY):
        if orientation == Qt.Orientation.Horizontal and role == DISPLAY:
            return self.headers[section]
        return None

    def data(self, index, role=DISPLAY):
        if index.isValid() and role in (DISPLAY, Qt.ItemDataRole.ToolTipRole):
            return self.rows[index.row()][index.column()]
        return None


class PreviewModel(TableModel):
    def __init__(self):
        super().__init__(("Pobierz", "Nr", "Tytuł", "Kanał", "Czas", "Zakładka"))
        self.selected = set()
        self.entries = []
        self.checkable = True

    def set_entries(self, entries):
        self.beginResetModel()
        self.entries = []
        self.selected.clear()
        self.rows = []
        self.endResetModel()
        self.append_entries(entries)

    def append_entries(self, entries):
        if not entries:
            return
        first = len(self.entries)
        end = first + len(entries)
        self.beginInsertRows(QModelIndex(), first, end - 1)
        self.entries.extend(entries)
        self.selected.update(range(first, end))
        self.rows.extend(("", str(e.index), e.title, e.channel, duration(e.duration), TAB_NAMES.get(e.tab, ""))
                         for e in entries)
        self.endInsertRows()

    def set_checkable(self, checkable):
        self.checkable = checkable
        if self.rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, 0), [CHECK])

    def flags(self, index):
        flags = super().flags(index)
        return flags | Qt.ItemFlag.ItemIsUserCheckable if self.checkable and index.column() == 0 else flags

    def data(self, index, role=DISPLAY):
        if index.isValid() and index.column() == 0 and role == CHECK:
            return Qt.CheckState.Checked if index.row() in self.selected else Qt.CheckState.Unchecked
        return super().data(index, role)

    def setData(self, index, value, role=CHECK):
        if not self.checkable or not index.isValid() or index.column() != 0 or role != CHECK:
            return False
        if value in (Qt.CheckState.Checked, Qt.CheckState.Checked.value):
            self.selected.add(index.row())
        else:
            self.selected.discard(index.row())
        self.dataChanged.emit(index, index, [CHECK])
        return True

    def select_rows(self, rows, selected):
        if selected:
            self.selected.update(rows)
        else:
            self.selected.difference_update(rows)
        if self.rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, 0), [CHECK])


class QueueModel(TableModel):
    def __init__(self):
        super().__init__(("Tytuł", "Stan", "Postęp", "Prędkość", "ETA", "Informacja"))
        self.jobs = []
        self.positions = {}
        self.events = {}

    def update_jobs(self, jobs):
        same_ids = len(jobs) == len(self.jobs) and all(a.id == b.id for a, b in zip(jobs, self.jobs))
        self.jobs = jobs
        self.positions = {job.id: i for i, job in enumerate(jobs)}
        self.events = {job.id: self.events[job.id] for job in jobs
                       if job.state == "running" and job.id in self.events}
        rows = [self.cells(job) for job in jobs]
        if same_ids:
            self.rows = rows
            if rows:
                self.dataChanged.emit(self.index(0, 0), self.index(len(rows) - 1, 5))
        else:
            self.replace(rows)

    def cells(self, job):
        event = self.events.get(job.id, {})
        progress = 100 if job.state in ("done", "skipped") else event.get("percent", 0)
        speed = event.get("speed")
        status = "Przetwarzanie" if event.get("kind") == "processing" else LABELS[job.state]
        return job.entry.title, status, int(progress), (f"{speed / 1048576:.1f} MB/s" if speed else "—"), \
            duration(event.get("eta")), job.reason or job.path

    def update_progress(self, job_id, event):
        if job_id not in self.positions:
            return
        position = self.positions[job_id]
        self.events[job_id] = {**self.events.get(job_id, {}), **event}
        self.rows[position] = self.cells(self.jobs[position])
        self.dataChanged.emit(self.index(position, 1), self.index(position, 4))

    def data(self, index, role=DISPLAY):
        if index.isValid() and index.column() == 2 and role == USER:
            return self.rows[index.row()][2]
        return super().data(index, role)


class ProgressDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        bar = QStyleOptionProgressBar()
        bar.rect = option.rect.adjusted(6, 6, -6, -6)
        bar.minimum, bar.maximum = 0, 100
        bar.progress = index.data(USER)
        bar.text = f"{bar.progress}%"
        bar.textVisible = True
        bar.textAlignment = Qt.AlignmentFlag.AlignCenter
        bar.palette = option.palette
        QApplication.style().drawControl(QStyle.ControlElement.CE_ProgressBar, bar, painter)


def table(model, stretch):
    view = QTableView()
    view.setModel(model)
    view.setAlternatingRowColors(True)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.verticalHeader().hide()
    view.verticalHeader().setDefaultSectionSize(33)
    header = view.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
    return view


def set_theme(app, theme):
    system_dark = app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    dark = theme == "dark" or theme == "system" and system_dark
    bg, panel, alternate, fg, muted, border = (
        ("#101822", "#162230", "#1b2a3a", "#edf3fa", "#a5b6ca", "#34465b") if dark else
        ("#f0f4f9", "#ffffff", "#f4f7fc", "#182840", "#50647d", "#c9d6e4"))
    palette = QPalette()
    for role, color in {
        "Window": bg, "Base": panel, "AlternateBase": alternate, "WindowText": fg,
        "Text": fg, "Button": panel, "ButtonText": fg, "Highlight": "#1864c2",
        "HighlightedText": "#ffffff", "PlaceholderText": muted, "ToolTipBase": panel,
        "ToolTipText": fg,
    }.items():
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(color))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(muted))
    app.setPalette(palette)
    app.setStyleSheet(f"""
        QWidget {{ font-family: "Segoe UI"; font-size: 13px; }}
        QLabel#brand {{ font-size: 25px; font-weight: 700; }}
        QLabel#muted {{ color: {muted}; }}
        QLineEdit, QComboBox, QSpinBox, QPushButton {{
            min-height: 24px; padding: 4px 9px; border: 1px solid {border}; border-radius: 5px;
            background: {panel};
        }}
        QPushButton:hover {{ border-color: #3985d7; }}
        QPushButton[primary="true"] {{ background: #1864c2; color: white; border-color: #1864c2;
                                       font-weight: 600; padding: 4px 14px; }}
        QPushButton[primary="true"]:disabled {{ background: {alternate}; color: {muted}; }}
        QTableView, QGroupBox {{ border: 1px solid {border}; border-radius: 5px; }}
        QHeaderView::section {{ background: {alternate}; color: {fg}; padding: 6px; border: 0; }}
        QGroupBox {{ margin-top: 12px; padding: 12px 8px 4px 8px; font-weight: 600; }}
        QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; }}
        QTabBar::tab {{ padding: 8px 15px; margin-right: 3px; }}
        QTabBar::tab:selected {{ background: {panel}; border-bottom: 2px solid #3985d7; }}
    """)


class DownloadOptionsForm(QGroupBox):
    def __init__(self, options):
        super().__init__("Pliki i jakość")
        form = QFormLayout(self)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.folder = QLineEdit(options.folder)
        self.template = QLineEdit(options.template)
        self.resolution = combo([("Najlepsza", 0), *((f"{p}p", p) for p in (2160, 1440, 1080, 720, 480))],
                                options.resolution)
        self.container = combo([(s.upper(), s) for s in ("mp4", "mkv")], options.container)
        self.audio_only = checkbox("Tylko audio", options.audio_only)
        self.audio_format = combo([(s.upper(), s) for s in ("mp3", "m4a", "opus")], options.audio_format)
        self.bitrate = combo([(f"{n} kb/s", n) for n in (64, 96, 128, 160, 192, 256, 320)], options.bitrate)
        self.audio_lang = QLineEdit(options.audio_language)
        self.audio_lang.setPlaceholderText("np. pl lub en")
        self.subs = checkbox("Pobieraj napisy", options.subtitles)
        self.auto_subs = checkbox("Uwzględniaj automatyczne", options.auto_subtitles)
        self.embed_subs = checkbox("Osadzaj w filmie", options.embed_subtitles)
        self.sub_lang = QLineEdit(options.subtitle_languages)
        self.metadata = checkbox("Osadzaj metadane", options.metadata)
        self.thumbnail = checkbox("Osadzaj miniaturę", options.thumbnail)
        self.keep_sidecars = checkbox("Zachowuj osobne pliki z miniaturą i metadanymi", options.keep_sidecars)
        self.force = checkbox("Pobieraj ponownie filmy z historii", options.force)
        folder_row = row(self.folder, button("Przeglądaj…", self.choose_folder))
        folder_row.layout().setStretch(0, 1)
        form.addRow("Folder docelowy", folder_row)
        form.addRow("Szablon ścieżki", self.template)
        form.addRow("Rozdzielczość maks. / kontener", row(self.resolution, self.container, None))
        form.addRow("Audio / format / bitrate", row(self.audio_only, self.audio_format, self.bitrate, None))
        form.addRow("Preferowany język audio", self.audio_lang)
        form.addRow("Napisy", row(self.subs, self.auto_subs, self.embed_subs, None))
        form.addRow("Języki napisów", self.sub_lang)
        self.sub_lang.setPlaceholderText("np. pl,en")
        form.addRow(row(self.metadata, self.thumbnail, None))
        form.addRow(row(self.keep_sidecars, None))
        form.addRow(row(self.force, None))

    def choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Wybierz folder", self.folder.text())
        if path:
            self.folder.setText(path)

    def options(self):
        return Options(
            folder=self.folder.text().strip(), template=self.template.text().strip(),
            resolution=self.resolution.currentData(), container=self.container.currentData(),
            audio_only=self.audio_only.isChecked(), audio_format=self.audio_format.currentData(),
            bitrate=self.bitrate.currentData(), audio_language=self.audio_lang.text().strip(),
            subtitles=self.subs.isChecked(), auto_subtitles=self.auto_subs.isChecked(),
            subtitle_languages=self.sub_lang.text().strip(), embed_subtitles=self.embed_subs.isChecked(),
            thumbnail=self.thumbnail.isChecked(), metadata=self.metadata.isChecked(), force=self.force.isChecked(),
            keep_sidecars=self.keep_sidecars.isChecked(),
        )


class PreviewOptionsDialog(QDialog):
    def __init__(self, options, defaults, parent):
        super().__init__(parent)
        self.setWindowTitle("Parametry bieżącego podglądu")
        self.resize(670, 440)
        self.options = None
        self.defaults = defaults
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.form = DownloadOptionsForm(options)
        self.scroll.setWidget(self.form)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.hide()
        buttons = QDialogButtonBox()
        buttons.addButton("Przywróć domyślne", QDialogButtonBox.ButtonRole.ResetRole).clicked.connect(self.reset)
        buttons.addButton("Anuluj", QDialogButtonBox.ButtonRole.RejectRole)
        apply = buttons.addButton("Zastosuj do podglądu", QDialogButtonBox.ButtonRole.AcceptRole)
        apply.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.scroll, 1)
        layout.addWidget(self.error)
        layout.addWidget(buttons)

    def reset(self):
        self.form = DownloadOptionsForm(self.defaults)
        self.scroll.setWidget(self.form)
        self.error.hide()

    def accept(self):
        options = self.form.options()
        try:
            options.validate()
        except ValueError as exc:
            self.error.setText(str(exc))
            self.error.show()
            return
        self.options = options
        super().accept()


class Window(QMainWindow):
    def __init__(self, app, controller):
        super().__init__()
        self.app = app
        self.c = controller
        self.login_prompted = False
        self.retry_preview = None
        self.preview_options = None
        self.preview_music = False
        self.login_url = ""
        self.quitting = False
        self.setWindowTitle(APP_NAME)
        screen = self.screen().availableGeometry()
        self.resize(min(1140, screen.width() - 32), min(790, screen.height() - 64))
        self.setMinimumSize(740, 460)
        self.tabs = QTabWidget()
        self.notice = QLabel("Przygotowywanie zależności…")
        self.notice.setObjectName("muted")
        self.notice.setWordWrap(True)
        self.login = button("Zaloguj w przeglądarce", self.open_login)
        self.retry_login = button("Zalogowano · ponów", self.retry_after_login)
        self.login.hide()
        self.retry_login.hide()
        self.engine_label = QLabel()
        self.engine_label.setObjectName("muted")
        brand = QLabel(APP_NAME)
        brand.setObjectName("brand")
        center = QWidget()
        self.setCentralWidget(center)
        layout = QVBoxLayout(center)
        layout.setContentsMargins(20, 14, 20, 14)
        layout.setSpacing(11)
        layout.addWidget(row(brand, None, self.engine_label))
        layout.addWidget(self.tabs, 1)
        layout.addWidget(row(self.notice, None, self.login, self.retry_login))
        self.preview_tab = self.preview_page()
        self.queue_tab = self.queue_page()
        self.settings_tab = self.settings_page()
        self.history_tab = self.history_page()
        self.updates_tab = self.updates_page()
        self.subscriptions_tab = self.subscriptions_page()
        self.tabs.addTab(self.preview_tab, "Podgląd")
        self.tabs.addTab(self.queue_tab, "Kolejka")
        self.tabs.addTab(self.settings_tab, "Ustawienia")
        self.tabs.addTab(self.history_tab, "Historia pobrań")
        self.tabs.addTab(self.updates_tab, "Aktualizacje")
        self.tabs.addTab(self.subscriptions_tab, "Subskrypcje")
        self.tabs.currentChanged.connect(lambda _: self.refresh())
        self.tray = QSystemTrayIcon(app.windowIcon(), self)
        menu = QMenu(self)
        menu.addAction("Pokaż YT Downloader", self.showNormal)
        menu.addAction("Wstrzymaj / wznów kolejkę", lambda: self.c.pause_all(not self.c.paused))
        menu.addAction("Zakończ", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.showNormal()
                                    if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.setToolTip(APP_NAME)
        self.tray.setVisible(self.c.prefs.tray and QSystemTrayIcon.isSystemTrayAvailable())
        self.c.changed.connect(self.refresh)
        self.c.progress.connect(self.on_progress)
        self.c.message.connect(self.notify)
        self.c.scanned.connect(self.on_scan)
        self.c.scan_entries.connect(self.on_scan_entries)
        self.c.login_needed.connect(self.login_needed)
        self.c.update_ready.connect(self.apply_app_update)
        self.refresh()

    def preview_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.source = QLineEdit()
        self.source.setPlaceholderText("Wklej link z YouTube lub YouTube Music")
        self.source_kind = QLabel()
        self.source_kind.setObjectName("muted")
        self.source.textChanged.connect(self.detect)
        self.source.returnPressed.connect(self.preview)
        self.preview_button = button("Pokaż podgląd", self.preview, primary=True)
        self.cancel_button = button("Anuluj odczyt", self.c.cancel_scan)
        self.cancel_button.hide()
        source_row = row(self.source, self.source_kind, self.preview_button, self.cancel_button)
        source_row.layout().setStretch(0, 1)
        layout.addWidget(source_row)
        self.channel_tabs = {key: checkbox(name, True) for key, name in TAB_NAMES.items()}
        self.channel_row = row(*self.channel_tabs.values(), None)
        self.channel_row.hide()
        layout.addWidget(self.channel_row)
        self.preview_model = PreviewModel()
        self.preview_filter = QSortFilterProxyModel(self)
        self.preview_filter.setSourceModel(self.preview_model)
        self.preview_filter.setFilterKeyColumn(2)
        self.preview_filter.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.preview_table = table(self.preview_filter, 2)
        self.preview_table.setColumnWidth(0, 72)
        self.preview_table.setColumnWidth(1, 44)
        self.preview_table.setColumnWidth(4, 64)
        self.preview_info = QLabel("Wklej link, aby odczytać listę.")
        self.preview_info.setWordWrap(True)
        self.select_all_button = button("Zaznacz wszystkie", lambda: self.select_visible(True))
        self.select_none_button = button("Odznacz wszystkie", lambda: self.select_visible(False))
        self.add_button = button("Dodaj zaznaczone do kolejki", self.enqueue, primary=True)
        self.preview_search = QLineEdit()
        self.preview_search.setPlaceholderText("Filtruj tytuły w podglądzie")
        self.preview_search.textChanged.connect(self.filter_preview)
        self.preview_mode = combo([("Tryb z ustawień", None), ("Tylko audio", True), ("Wideo", False)], None)
        self.preview_mode.setToolTip("Tryb pobierania pozycji dodawanych z tego podglądu.")
        self.preview_parameters = button("Parametry pobierania…", self.edit_preview_options)
        self.preview_parameters.setEnabled(False)
        self.preview_mode.setEnabled(False)
        self.set_preview_selectable(False)
        filter_row = row(self.preview_search, self.preview_mode, self.preview_parameters)
        filter_row.layout().setStretch(0, 1)
        layout.addWidget(filter_row)
        layout.addWidget(row(self.preview_info, None, self.select_all_button, self.select_none_button, self.add_button))
        layout.addWidget(self.preview_table, 1)
        return page

    def queue_page(self):
        page = QWidget()
        queue_layout = QVBoxLayout(page)
        self.queue_model = QueueModel()
        self.queue_table = table(self.queue_model, 0)
        self.queue_table.setColumnWidth(1, 139)
        self.queue_table.setColumnWidth(2, 116)
        self.queue_table.setColumnWidth(3, 92)
        self.queue_table.setColumnWidth(4, 65)
        self.queue_table.setColumnWidth(5, 230)
        self.queue_table.setItemDelegateForColumn(2, ProgressDelegate(self.queue_table))
        self.pause_all = button("Wstrzymaj kolejkę", lambda: self.c.pause_all(not self.c.paused))
        self.pause_selected = button("Wstrzymaj", lambda: self.selected_action("pause"))
        self.resume_selected = button("Wznów", lambda: self.selected_action("resume"))
        self.retry_failed = button("Ponów nieudane", self.retry_failures)
        self.queue_table.selectionModel().selectionChanged.connect(lambda *_: self.selection_state())
        queue_layout.addWidget(row(self.pause_selected, self.resume_selected,
                                   self.pause_all, self.retry_failed,
                                   button("Wyczyść zakończone", self.clear_queue), None))
        queue_layout.addWidget(self.queue_table, 1)
        self.summary = QLabel()
        self.total = QProgressBar()
        self.total.setFormat("%p% · łączny postęp")
        queue_layout.addWidget(self.summary)
        queue_layout.addWidget(self.total)
        return page

    def settings_page(self):
        settings = self.c.prefs
        area = QScrollArea()
        area.setWidgetResizable(True)
        page = QWidget()
        layout = QVBoxLayout(page)
        self.defaults_form = DownloadOptionsForm(settings.options)
        layout.addWidget(self.defaults_form)
        behavior = QGroupBox("Aplikacja i logowanie")
        form = QFormLayout(behavior)
        self.parallel = QSpinBox()
        self.parallel.setRange(1, 8)
        self.parallel.setValue(settings.parallel)
        self.theme = combo([("Systemowy", "system"), ("Jasny", "light"), ("Ciemny", "dark")], settings.theme)
        self.auto_update = checkbox("Aktualizuj automatycznie", settings.auto_update)
        self.update_channel = combo([("Stable", "stable"), ("Nightly", "nightly")], settings.channel)
        self.run_at_login = checkbox("Uruchamiaj z Windows", settings.autostart)
        self.use_tray = checkbox("Pracuj w zasobniku po zamknięciu okna", settings.tray)
        self.browser = combo([("Plik zaimportowany lub bez logowania", ""),
                              ("Firefox", "firefox"), ("Edge", "edge"), ("Chrome", "chrome"),
                              ("Brave", "brave"), ("Opera", "opera"), ("Vivaldi", "vivaldi")], settings.browser)
        self.browser_profile = QLineEdit(settings.browser_profile)
        self.browser_profile.setPlaceholderText("Opcjonalnie · nazwa lub ścieżka profilu")
        form.addRow("Limit równoległych pobrań / motyw", row(self.parallel, self.theme, None))
        form.addRow("Aktualizacje i kanał yt-dlp", row(self.auto_update, self.update_channel, None))
        form.addRow(row(self.run_at_login, self.use_tray, None))
        form.addRow("Źródło ciasteczek", self.browser)
        form.addRow("Profil przeglądarki", self.browser_profile)
        form.addRow(row(button("Importuj ciasteczka Netscape…", self.import_cookies),
                        button("Usuń zaimportowane ciasteczka", self.remove_cookies), None))
        layout.addWidget(behavior)
        layout.addWidget(row(button("Otwórz folder danych i logów", lambda: self.open_path(self.c.root)),
                             None, button("Zapisz ustawienia", self.save_settings, primary=True)))
        layout.addStretch()
        area.setWidget(page)
        return area

    def history_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.history = TableModel(("Tytuł / źródło", "ID YouTube", "Data zapisu", "Plik"))
        view = table(self.history, 0)
        view.setColumnWidth(3, 420)
        view.doubleClicked.connect(self.open_history_folder)
        layout.addWidget(row(button("Importuj archiwum yt-dlp…", self.import_history), None,
                             button("Wyczyść historię", self.clear_history)))
        layout.addWidget(view)
        return page

    def updates_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.updates = TableModel(("Data", "Komponent", "Wersja", "Wynik", "Szczegóły"))
        view = table(self.updates, 4)
        view.setColumnWidth(0, 145)
        layout.addWidget(row(QLabel(f"Wersja aplikacji {APP_VERSION}"), None,
                             button("Sprawdź teraz", self.c.check_now, primary=True)))
        layout.addWidget(view)
        return page

    def subscriptions_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.sub_url = QLineEdit()
        self.sub_url.setPlaceholderText("Adres kanału YouTube")
        self.hours = QSpinBox()
        self.hours.setRange(1, 168)
        self.hours.setValue(24)
        self.hours.setSuffix(" godz.")
        self.sub_checks = {key: checkbox(name, key == "videos") for key, name in TAB_NAMES.items()}
        top = row(self.sub_url, *self.sub_checks.values(), self.hours,
                  button("Dodaj subskrypcję", self.add_subscription, primary=True))
        top.layout().setStretch(0, 1)
        layout.addWidget(top)
        self.subscriptions = TableModel(("Kanał", "Zakładki", "Co ile godzin", "Następne sprawdzenie"))
        self.sub_view = table(self.subscriptions, 0)
        layout.addWidget(self.sub_view)
        layout.addWidget(row(None, button("Usuń zaznaczone", self.delete_subscriptions)))
        return page

    def detect(self):
        try:
            source = parse_source(self.source.text())
            label = MUSIC_SOURCE_NAMES[source.kind] if source.music else SOURCE_NAMES[source.kind]
            self.source_kind.setText(f"{label} · YouTube Music" if source.music else label)
            self.channel_row.setVisible(source.kind == "channel")
            if source.tab:
                for tab, check in self.channel_tabs.items():
                    check.setChecked(tab == source.tab)
        except ValueError:
            self.source_kind.clear()
            self.channel_row.hide()

    def preview(self):
        try:
            source = parse_source(self.source.text())
        except ValueError as exc:
            self.notify(str(exc))
            return
        tabs = [key for key, check in self.channel_tabs.items() if check.isChecked()]
        if source.kind == "channel" and not tabs:
            self.notify("Wybierz przynajmniej jedną zakładkę kanału.")
            return
        self.start_preview(source, tabs)

    def start_preview(self, source, tabs, *, preserve_options=False):
        try:
            self.c.preview(source, tabs)
        except ValueError as exc:
            self.notify(str(exc))
            return
        if not self.c.scan:
            return
        self.retry_preview = None
        self.refresh_login_actions()
        self.preview_button.setEnabled(False)
        self.cancel_button.show()
        self.preview_info.setText("Odczytywanie listy…")
        self.preview_info.setToolTip("")
        self.preview_model.set_entries([])
        self.preview_search.clear()
        self.preview_music = source.music
        if not preserve_options:
            self.preview_options = None
            self.preview_mode.setCurrentIndex(1 if source.music else 0)
            self.preview_parameters.setText("Parametry pobierania…")
        self.preview_mode.setEnabled(True)
        self.preview_parameters.setEnabled(True)
        self.set_preview_selectable(False)
        self.tabs.setCurrentWidget(self.preview_tab)

    def set_preview_selectable(self, enabled):
        self.preview_model.set_checkable(enabled)
        self.refresh_preview_actions()

    def filter_preview(self, text):
        active = bool(text.strip())
        self.preview_filter.setFilterFixedString(text.strip())
        self.select_all_button.setText("Zaznacz widoczne" if active else "Zaznacz wszystkie")
        self.select_none_button.setText("Odznacz widoczne" if active else "Odznacz wszystkie")
        self.add_button.setText("Dodaj widoczne zaznaczone" if active else "Dodaj zaznaczone do kolejki")
        self.refresh_preview_actions()

    def refresh_preview_actions(self):
        enabled = self.preview_model.checkable and self.preview_filter.rowCount() > 0
        self.select_all_button.setEnabled(enabled)
        self.select_none_button.setEnabled(enabled)
        self.add_button.setEnabled(enabled)

    def visible_rows(self):
        if not self.preview_search.text().strip():
            return range(len(self.preview_model.entries))
        return [self.preview_filter.mapToSource(self.preview_filter.index(row, 0)).row()
                for row in range(self.preview_filter.rowCount())]

    def select_visible(self, selected):
        self.preview_model.select_rows(self.visible_rows(), selected)

    def on_scan_entries(self, offset, entries):
        if offset == 0 and self.preview_model.entries:
            self.preview_model.set_entries([])
        self.preview_model.append_entries(entries)
        self.preview_info.setText(f"Odczytano pozycji: {offset + len(entries)}…")

    def on_scan(self, result):
        self.preview_button.setEnabled(True)
        self.cancel_button.hide()
        entries = result["entries"]
        if self.preview_model.entries != entries:
            self.preview_model.set_entries(entries)
        self.set_preview_selectable(bool(entries))
        warnings = "\n".join(f"{TAB_NAMES.get(item['tab'], item['tab'])} — {item['message']}"
                             for item in result["warnings"])
        self.preview_info.setToolTip(warnings)
        if result["complete"]:
            missing = ", ".join(TAB_NAMES.get(item["tab"], item["tab"]) for item in result["warnings"])
            self.preview_info.setText(f"Pozycje: {len(entries)}" + (f" · brak zakładek: {missing}" if missing else ""))
        elif entries:
            reason = result.get("message", "Nie udało się odczytać wszystkich zakładek.")
            self.preview_info.setText(f"Niepełna lista · {len(entries)} pozycji. {reason}")
        else:
            self.preview_info.setText(result.get("message", "Nie udało się odczytać listy."))
        self.retry_preview = result["login_retry"]

    def options_for_preview(self):
        defaults = self.c.prefs.options
        options = self.preview_options or defaults
        mode = self.preview_mode.currentData()
        return replace(options, audio_only=defaults.audio_only if mode is None else mode)

    def edit_preview_options(self):
        defaults = self.c.prefs.options
        if self.preview_music:
            defaults = replace(defaults, audio_only=True)
        dialog = PreviewOptionsDialog(self.options_for_preview(), defaults, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.preview_options = dialog.options if dialog.options != defaults else None
            if self.preview_options is None:
                self.preview_mode.setCurrentIndex(1 if self.preview_music else 0)
                self.preview_parameters.setText("Parametry pobierania…")
            else:
                self.preview_mode.setCurrentIndex(self.preview_mode.findData(dialog.options.audio_only))
                self.preview_parameters.setText("Zmienione parametry…")
        dialog.deleteLater()

    def enqueue(self):
        options = self.options_for_preview()
        try:
            options.validate()
        except ValueError as exc:
            self.notify(str(exc))
            return
        selected = [index for index in self.visible_rows() if index in self.preview_model.selected]
        if selected:
            count = self.c.store.enqueue((self.preview_model.entries[i] for i in selected), options)
            self.c.store.skip_downloaded()
            self.preview_model.select_rows(selected, False)
            self.notify(f"Dodano pozycji: {count}.")
            self.refresh()

    def selected_jobs(self):
        return [self.queue_model.jobs[index.row()] for index in self.queue_table.selectionModel().selectedRows()]

    def selection_state(self):
        jobs = self.selected_jobs()
        self.pause_selected.setEnabled(any(job.state in ("queued", "running", "repair") for job in jobs))
        self.resume_selected.setEnabled(any(job.state == "paused" for job in jobs))

    def selected_action(self, action):
        for job in self.selected_jobs():
            if action == "pause" and job.state in ("queued", "running", "repair"):
                self.c.pause_job(job.id)
            elif action == "resume" and job.state == "paused":
                self.c.resume_job(job.id)

    def retry_failures(self):
        self.c.store.retry_failed()
        self.refresh()

    def clear_queue(self):
        self.c.store.clear_jobs()
        self.refresh()

    def clear_history(self):
        if QMessageBox.question(self, "Historia pobrań", "Usunąć wszystkie wpisy z historii pobrań?") == (
                QMessageBox.StandardButton.Yes):
            self.c.store.clear_history()
            self.refresh()

    def open_history_folder(self, index):
        path = self.history.rows[index.row()][3]
        if path:
            self.open_path(Path(path).parent)

    def import_history(self):
        path, _ = QFileDialog.getOpenFileName(self, "Importuj archiwum yt-dlp", "",
                                              "Pliki tekstowe (*.txt);;Wszystkie pliki (*)")
        if not path:
            return
        try:
            with Path(path).open(encoding="utf-8-sig") as archive:
                added, known, ignored = self.c.store.import_archive(archive)
        except (OSError, UnicodeError) as exc:
            LOG.info("Import archiwum pobrań: %s", exc)
            self.notify("Nie można odczytać archiwum. Wybierz plik tekstowy yt-dlp w kodowaniu UTF-8.")
            return
        self.c.store.skip_downloaded()
        self.refresh()
        self.notify(f"Zaimportowano {added} identyfikatorów · już w historii: {known} · nierozpoznane wiersze: {ignored}.")

    def save_settings(self):
        options = self.defaults_form.options()
        try:
            options.validate()
            if self.run_at_login.isChecked() != self.c.prefs.autostart or (
                    self.run_at_login.isChecked() and self.use_tray.isChecked() != self.c.prefs.tray):
                autostart(self.run_at_login.isChecked(), self.use_tray.isChecked())
        except (ValueError, OSError) as exc:
            self.notify(str(exc))
            return
        self.c.prefs = replace(
            self.c.prefs, options=options, parallel=self.parallel.value(), theme=self.theme.currentData(),
            auto_update=self.auto_update.isChecked(), channel=self.update_channel.currentData(),
            browser=self.browser.currentData(), browser_profile=self.browser_profile.text().strip(),
            tray=self.use_tray.isChecked(), autostart=self.run_at_login.isChecked(),
        )
        self.c.save()
        set_theme(self.app, self.c.prefs.theme)
        self.tray.setVisible(self.c.prefs.tray and QSystemTrayIcon.isSystemTrayAvailable())
        self.notify("Zapisano ustawienia.")

    def import_cookies(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Ciasteczka YouTube w formacie Netscape",
                                                  "", "Pliki tekstowe (*.txt);;Wszystkie pliki (*)")
        if not filename:
            return
        try:
            path = Path(filename)
            if path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("Plik ciasteczek jest zbyt duży.")
            atomic_bytes(self.c.root / "cookies.dpapi", crypt(youtube_cookie_file(path.read_bytes())))
            self.c.prefs.browser = ""
            self.browser.setCurrentIndex(0)
            self.c.save()
            self.notify("Zapisano zaszyfrowane ciasteczka YouTube.")
        except (OSError, ValueError) as exc:
            self.notify(str(exc))

    def remove_cookies(self):
        (self.c.root / "cookies.dpapi").unlink(missing_ok=True)
        self.notify("Usunięto zaimportowane ciasteczka.")

    def login_needed(self, url, message, open_browser):
        self.login_url = url
        self.login.show()
        self.retry_login.show()
        self.notify(message)
        if open_browser and not self.login_prompted:
            self.login_prompted = True
            self.open_login()

    def open_login(self):
        url = self.retry_preview[0].url if self.retry_preview else self.login_url
        if not url:
            return
        exe_name = {"chrome": "chrome", "edge": "msedge", "firefox": "firefox",
                    "brave": "brave", "opera": "opera", "vivaldi": "vivaldi"}.get(self.c.prefs.browser)
        if exe_name:
            exe = browser_exe(exe_name)
            if exe:
                try:
                    subprocess.Popen([exe, url], close_fds=True)
                    return
                except OSError:
                    LOG.exception("Przeglądarka %s", exe_name)
        QDesktopServices.openUrl(QUrl(url))

    def retry_after_login(self):
        if not self.c.prefs.browser and not (self.c.root / "cookies.dpapi").is_file():
            self.tabs.setCurrentWidget(self.settings_tab)
            self.notify("Wybierz przeglądarkę w ustawieniach i zapisz wybór albo zaimportuj ciasteczka.")
            return
        if self.retry_preview:
            source, tabs = self.retry_preview
            self.source.setText(source.url)
            for tab, check in self.channel_tabs.items():
                check.setChecked(tab in tabs)
            self.start_preview(source, tabs, preserve_options=True)
        else:
            self.tabs.setCurrentWidget(self.queue_tab)
        self.c.retry_auth()

    def refresh_login_actions(self):
        waiting = next((job for job in self.queue_model.jobs if job.state == "auth"), None)
        subscription = self.c.store.subscription_needing_login()
        if waiting:
            self.login_url = waiting.entry.url
        elif subscription:
            self.login_url = subscription["url"]
        visible = bool(waiting or subscription or self.retry_preview)
        self.login.setVisible(visible)
        self.retry_login.setVisible(visible)

    def add_subscription(self):
        try:
            source = parse_source(self.sub_url.text())
            if source.kind != "channel":
                raise ValueError("Subskrypcja wymaga adresu kanału.")
            tabs = [key for key in CHANNEL_TABS if self.sub_checks[key].isChecked()]
            if not tabs:
                raise ValueError("Wybierz zakładki subskrypcji.")
            self.c.prefs.options.validate()
            self.c.store.add_subscription(source.url, tabs, self.hours.value(), music=source.music)
        except ValueError as exc:
            self.notify(str(exc))
            return
        self.sub_url.clear()
        self.refresh()

    def delete_subscriptions(self):
        rows = self.c.store.subscriptions()
        ids = [rows[index.row()]["id"] for index in self.sub_view.selectionModel().selectedRows()]
        self.c.remove_subscriptions(ids)

    def refresh(self):
        runtime = self.c.manager.snapshot()
        self.engine_label.setText(f"yt-dlp {runtime.version} · {runtime.channel}" if runtime.ready
                                  else "Przygotowywanie zależności")
        self.queue_model.update_jobs(self.c.store.jobs())
        self.refresh_login_actions()
        self.pause_all.setText("Wznów kolejkę" if self.c.paused else "Wstrzymaj kolejkę")
        self.retry_failed.setEnabled(any(job.state == "failed" for job in self.queue_model.jobs))
        self.selection_state()
        self.update_summary()
        page = self.tabs.currentWidget()
        if page is self.history_tab:
            self.history.replace([(r["title"] or "Z archiwum yt-dlp", r["id"], stamp(r["time"]), r["path"])
                                  for r in self.c.store.history()])
        elif page is self.updates_tab:
            self.updates.replace([(stamp(r["time"]), r["component"], r["version"], r["status"], r["detail"])
                                  for r in self.c.store.update_history()])
        elif page is self.subscriptions_tab:
            self.subscriptions.replace([(r["url"] + (" · YouTube Music" if r["music"] else ""),
                                         ", ".join(TAB_NAMES[t] for t in json.loads(r["tabs"])), str(r["hours"]),
                                         "Wymaga logowania" if r["needs_login"]
                                         else stamp(r["due"]) if r["due"] else "Wkrótce")
                                        for r in self.c.store.subscriptions()])

    def on_progress(self, job_id, event):
        self.queue_model.update_progress(job_id, event)
        self.update_total_progress()

    def update_summary(self):
        jobs = self.queue_model.jobs
        counts = Counter(job.state for job in jobs)
        self.summary.setText(f"Gotowe: {counts['done']}   ·   Pominięte: {counts['skipped']}"
                             f"   ·   Nieudane: {counts['failed']}   ·   Logowanie: {counts['auth']}"
                             f"   ·   Razem: {len(jobs)}")
        self.finished_progress = 100 * sum(counts[state] for state in TERMINAL)
        self.update_total_progress()

    def update_total_progress(self):
        jobs = self.queue_model.jobs
        progress = self.finished_progress
        progress += sum(values.get("percent", 0) for values in self.queue_model.events.values())
        self.total.setValue(int(progress / len(jobs)) if jobs else 0)

    def notify(self, message):
        self.notice.setText(message)
        if self.tray.isVisible() and not self.isVisible():
            self.tray.showMessage(APP_NAME, message)

    def open_path(self, path):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def apply_app_update(self, update):
        try:
            restart_for(update, tray=not self.isVisible())
        except OSError as exc:
            self.c.record("aplikacja", update.version, "błąd restartu", str(exc))
            self.notify("Nie udało się uruchomić instalatora aktualizacji.")
            return
        self.quit()

    def closeEvent(self, event):
        if not self.quitting and self.tray.isVisible():
            event.ignore()
            self.hide()
        else:
            event.accept()
            self.app.quit()

    def quit(self):
        self.quitting = True
        self.app.quit()


def browser_exe(name):
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{name}.exe") as key:
                return winreg.QueryValue(key, None)
        except OSError:
            continue
    return shutil.which(name + ".exe")


def autostart(enabled, tray):
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        if enabled:
            args = command(*(["--tray"] if tray else []))
            winreg.SetValueEx(key, "YTDownloader", 0, winreg.REG_SZ, subprocess.list2cmdline(args))
        else:
            try:
                winreg.DeleteValue(key, "YTDownloader")
            except FileNotFoundError:
                pass


def launch(args):
    smoke = "--smoke-test" in args
    root = Path(args[args.index("--smoke-test") + 1]).resolve() if smoke else APP_DIR
    root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[
        RotatingFileHandler(root / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    ])
    app = QApplication([APP_NAME])
    app.setStyle("Fusion")
    app.setApplicationName(APP_NAME)
    lock = QLockFile(str(root / "instance.lock"))
    if not lock.tryLock(0):
        return 0
    icon = QIcon(str(RESOURCES / "app.ico"))
    if icon.isNull():
        icon = app.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown)
    app.setWindowIcon(icon)
    controller = Controller(root, smoke=smoke)
    app.aboutToQuit.connect(controller.close)
    set_theme(app, controller.prefs.theme)
    window = Window(app, controller)
    app.styleHints().colorSchemeChanged.connect(lambda _: set_theme(app, controller.prefs.theme))
    app.setQuitOnLastWindowClosed(False)
    if "--tray" not in args or not window.tray.isVisible():
        window.show()
    if smoke:
        def smoke_result():
            if controller.future is None:
                app.exit(0 if controller.manager.snapshot().ready else 1)
            else:
                QTimer.singleShot(200, smoke_result)

        QTimer.singleShot(300, smoke_result)
        QTimer.singleShot(45_000, lambda: app.exit(2))
    result = app.exec()
    lock.unlock()
    return result
