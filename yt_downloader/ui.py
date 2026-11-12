from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import subprocess
import winreg

from PySide6.QtCore import (
    QAbstractTableModel, QItemSelection, QItemSelectionModel, QLockFile, QModelIndex, QSortFilterProxyModel,
    Qt, QTimer, QUrl,
)
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSpinBox,
    QStyle, QStyleOptionProgressBar, QStyledItemDelegate, QSystemTrayIcon, QTabWidget,
    QTableView, QVBoxLayout, QWidget,
)

from . import APP_NAME, APP_VERSION
from .auth import crypt, youtube_cookie_file
from .controller import Controller, LABELS
from .model import (
    APP_DIR, CHANNEL_TABS, EDITABLE_STATES, LANGUAGE_CODE, Options, TERMINAL, WAITING_STATES,
    detect_episode_numbers, parse_source,
)
from .runtime import RESOURCES, atomic_bytes, command
from .organize import apply_plan, make_plan
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
        self.positions = {}
        self.number_labels = {}
        self.episode_numbers = {}
        self.checkable = True

    def set_entries(self, entries):
        self.beginResetModel()
        self.entries = []
        self.selected.clear()
        self.positions.clear()
        self.number_labels.clear()
        self.episode_numbers.clear()
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

    def set_order(self, order):
        rows = range(len(self.entries))
        if order == "reverse":
            rows = reversed(rows)
        elif order in ("oldest", "newest"):
            direction = 1 if order == "oldest" else -1

            def date_key(row):
                value = self.entries[row].published_at
                return value is None, direction * (value or 0)

            rows = sorted(rows, key=date_key)
        elif order == "episode" and self.episode_numbers:
            rows = sorted(rows, key=lambda row: (row not in self.episode_numbers,
                                                  self.episode_numbers.get(row, (0, 0))))
        self.positions = {row: position for position, row in enumerate(rows, 1)} if order != "source" else {}
        self.number_labels = {}
        if order == "episode" and self.episode_numbers:
            width = max(2, len(str(max(number for _, number in self.episode_numbers.values()))))
            unnumbered = 0
            for row in self.positions:
                if row in self.episode_numbers:
                    season, number = self.episode_numbers[row]
                    self.number_labels[row] = f"S{season:02d}E{number:0{width}d}" if season else f"{number:0{width}d}"
                else:
                    unnumbered += 1
                    self.number_labels[row] = f"Bez numeru {unnumbered:0{width}d}"
        if self.rows:
            self.dataChanged.emit(self.index(0, 1), self.index(len(self.rows) - 1, 1), [DISPLAY, USER])

    def entry_at(self, row):
        entry = self.entries[row]
        if row in self.positions:
            position = self.positions[row]
            width = max(2, len(str(len(self.entries))))
            return replace(entry, index=position, index_label=self.number_labels.get(row, f"{position:0{width}d}"))
        return entry

    def flags(self, index):
        flags = super().flags(index)
        return flags | Qt.ItemFlag.ItemIsUserCheckable if self.checkable and index.column() == 0 else flags

    def data(self, index, role=DISPLAY):
        if index.isValid() and index.column() == 0 and role == CHECK:
            return Qt.CheckState.Checked if index.row() in self.selected else Qt.CheckState.Unchecked
        if index.isValid() and index.column() == 1 and index.row() in self.positions:
            position = self.positions[index.row()]
            if role == USER:
                return position
            if role in (DISPLAY, Qt.ItemDataRole.ToolTipRole):
                return self.number_labels.get(index.row(), str(position))
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
        return not same_ids

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


class QueueFilter(QSortFilterProxyModel):
    def __init__(self, model, parent):
        super().__init__(parent)
        self.title = ""
        self.state = None
        self.setSourceModel(model)

    def set_filters(self, title, state):
        self.beginFilterChange()
        self.title = title.strip().casefold()
        self.state = state
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(self, source_row, source_parent):
        jobs = self.sourceModel().jobs
        if source_row >= len(jobs):
            return False
        job = jobs[source_row]
        return (self.state is None or job.state == self.state) and self.title in job.entry.title.casefold()


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
        self.template.setToolTip("{source} wstawia nazwę playlisty, albumu lub kanału otwartego w podglądzie.")
        heights = sorted({2160, 1440, 1080, 720, 480} | ({options.resolution} - {0}), reverse=True)
        self.resolution = combo([("Najlepsza", 0), *((f"{p}p", p) for p in heights)],
                                options.resolution)
        self.container = combo([(s.upper(), s) for s in ("mp4", "mkv")], options.container)
        self.audio_only = checkbox("Tylko audio", options.audio_only)
        self.audio_format = combo([(s.upper(), s) for s in ("mp3", "m4a", "opus")], options.audio_format)
        self.bitrate = combo([(f"{n} kb/s", n) for n in (64, 96, 128, 160, 192, 256, 320)], options.bitrate)
        self.audio_lang = QLineEdit(options.audio_language)
        self.audio_lang.setPlaceholderText("np. pl lub en")
        self.audio_exact = checkbox("Wymagaj tego języka", options.audio_language_exact)
        self.subs = checkbox("Pobieraj napisy", options.subtitles)
        self.subtitle_type = combo([("Ręczne", "manual"), ("Ręczne i automatyczne", "both"),
                                    ("Tylko automatyczne", "auto")],
                                   "auto" if options.auto_subtitles_only else "both" if options.auto_subtitles else "manual")
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
        form.addRow("Preferowany język audio", row(self.audio_lang, self.audio_exact))
        form.addRow("Napisy", row(self.subs, self.subtitle_type, self.embed_subs, None))
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
            subtitles=self.subs.isChecked(), auto_subtitles=self.subtitle_type.currentData() == "both",
            subtitle_languages=self.sub_lang.text().strip(), embed_subtitles=self.embed_subs.isChecked(),
            thumbnail=self.thumbnail.isChecked(), metadata=self.metadata.isChecked(), force=self.force.isChecked(),
            keep_sidecars=self.keep_sidecars.isChecked(),
            audio_language_exact=self.audio_exact.isChecked(), auto_subtitles_only=self.subtitle_type.currentData() == "auto",
        )


class PreviewOptionsDialog(QDialog):
    def __init__(self, options, defaults, parent, *, queue=False, batch=False):
        super().__init__(parent)
        self.setWindowTitle("Parametry zadań w kolejce" if queue else "Parametry bieżącego podglądu")
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
        if defaults is not None:
            buttons.addButton("Przywróć domyślne", QDialogButtonBox.ButtonRole.ResetRole).clicked.connect(self.reset)
        buttons.addButton("Anuluj", QDialogButtonBox.ButtonRole.RejectRole)
        apply = buttons.addButton("Zapisz parametry" if queue else "Zastosuj do podglądu", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        if batch:
            hint = QLabel("Do zaznaczonych zadań zostaną zastosowane pola zmienione w tym oknie.")
            hint.setWordWrap(True)
            layout.addWidget(hint)
        layout.addWidget(self.scroll, 1)
        layout.addWidget(self.error)
        layout.addWidget(buttons)
        apply.setDefault(True)

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


class MediaDetailsDialog(QDialog):
    def __init__(self, entry, parent, choices=None):
        super().__init__(parent)
        self.setWindowTitle("Dostępne formaty i napisy")
        self.resize(680, 440)
        title = QLabel(entry.title)
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        self.status = QLabel("Odczytywanie dostępnych formatów…")
        self.status.setWordWrap(True)
        self.choices = choices or {}
        self.resolution = QComboBox()
        self.language = QComboBox()
        self.captions = QComboBox()
        self.tabs = QTabWidget()
        self.models = (
            TableModel(("Rozdzielczość", "Klatki/s", "Kodek", "Format źródłowy")),
            TableModel(("Język", "Bitrate (kb/s)", "Kodek", "Format źródłowy")),
            TableModel(("Kod języka", "Język", "Rodzaj")),
        )
        for i, label in enumerate(("Wideo", "Audio", "Napisy")):
            self.tabs.addTab(table(self.models[i], 2 if i < 2 else 1), label)
        self.tabs.hide()
        self.selection = QWidget()
        form = QFormLayout(self.selection)
        form.addRow("Maks. rozdzielczość", self.resolution)
        form.addRow("Język audio", self.language)
        form.addRow("Napisy", self.captions)
        self.selection.hide()
        self.apply = button("Zastosuj do tego filmu", self.accept, primary=True)
        self.apply.setEnabled(False)
        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(self.status)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.selection)
        layout.addStretch()
        layout.addWidget(row(None, button("Zamknij", self.reject), self.apply))

    def set_result(self, result):
        if result["kind"] != "done":
            self.status.setText(result["message"])
            return
        self.models[0].replace([(f"{height}p", f"{fps:g}" if fps else "—", codec, ext)
                                for height, fps, codec, ext in result["video"]])
        self.models[1].replace([(language or "—", f"{bitrate:g}" if bitrate else "—", codec, ext)
                                for language, codec, bitrate, ext in result["audio"]])
        self.models[2].replace([(language, name, "Automatyczne" if auto else "Ręczne")
                                for language, name, auto in result["subtitles"]])
        for i, label in enumerate(("Wideo", "Audio", "Napisy")):
            self.tabs.setTabText(i, f"{label} ({self.models[i].rowCount()})")
        default = "Z parametrów podglądu"
        heights = sorted({row[0] for row in result["video"]}, reverse=True)
        languages = sorted({row[0] for row in result["audio"] if LANGUAGE_CODE.fullmatch(row[0])})
        self.resolution.addItem(default, None)
        self.language.addItem(default, None)
        self.captions.addItem(default, None)
        self.captions.addItem("Bez napisów", False)
        if self.choices.get("subtitles") is False:
            self.captions.setCurrentIndex(1)
        for height in heights:
            self.resolution.addItem(f"{height}p", height)
            if height == self.choices.get("resolution"):
                self.resolution.setCurrentIndex(self.resolution.count() - 1)
        for language in languages:
            self.language.addItem(language, language)
            if language == self.choices.get("audio_language"):
                self.language.setCurrentIndex(self.language.count() - 1)
        for language, name, auto in result["subtitles"]:
            kind = "automatyczne" if auto else "ręczne"
            label = f"{name} ({language})" if name else language
            self.captions.addItem(f"{label} · {kind}", (language, auto))
            if (self.choices.get("subtitles") and language == self.choices.get("subtitle_languages")
                    and auto == self.choices.get("auto_subtitles_only")):
                self.captions.setCurrentIndex(self.captions.count() - 1)
        self.status.hide()
        self.tabs.show()
        self.selection.show()
        self.apply.setEnabled(True)

    def accept(self):
        if not self.apply.isEnabled():
            return
        choices = {}
        if (height := self.resolution.currentData()) is not None:
            choices["resolution"] = height
        if language := self.language.currentData():
            choices.update(audio_language=language, audio_language_exact=True)
        captions = self.captions.currentData()
        if captions is False:
            choices["subtitles"] = False
        elif captions is not None:
            language, auto = captions
            choices.update(subtitles=True, subtitle_languages=language, auto_subtitles=False, auto_subtitles_only=auto)
        self.choices = choices
        super().accept()


class OrganizeDialog(QDialog):
    def __init__(self, root, groups, parent):
        super().__init__(parent)
        self.setWindowTitle("Plan uporządkowania plików")
        self.resize(850, 460)
        paths = TableModel(("Obecna ścieżka", "Ścieżka po przeniesieniu", "Stan"))
        paths.replace([(str(path.relative_to(root)), str((group.target / path.name).relative_to(root)),
                        group.status or "Do przeniesienia") for group in groups for path in group.files])
        view = table(paths, 0)
        view.setColumnWidth(1, 300)
        view.setColumnWidth(2, 145)
        count = sum(len(group.files) for group in groups if not group.status)
        destination = QLabel(str(groups[0].target))
        destination.setTextFormat(Qt.TextFormat.PlainText)
        destination.setWordWrap(True)
        move = button(f"Przenieś pliki ({count})", self.accept, primary=True)
        move.setEnabled(count > 0)
        layout = QVBoxLayout(self)
        layout.addWidget(destination)
        layout.addWidget(view, 1)
        layout.addWidget(row(None, button("Zamknij", self.reject), move))


class Window(QMainWindow):
    def __init__(self, app, controller):
        super().__init__()
        self.app = app
        self.c = controller
        self.login_prompted = False
        self.retry_preview = None
        self.preview_options = None
        self.media_choices = {}
        self.download_profiles = {name: Options(**values) for name, values in self.c.store.get("download_profiles", {}).items()}
        self.active_profile = None
        self.preview_music = False
        self.preview_source = None
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
        self.profiles_button = QPushButton("Profile…")
        self.profiles_menu = QMenu(self.profiles_button)
        self.profiles_menu.aboutToShow.connect(self.build_profiles_menu)
        self.profiles_button.setMenu(self.profiles_menu)
        source_row = row(self.source, self.source_kind, self.profiles_button, self.preview_button, self.cancel_button)
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
        self.preview_filter.setSortRole(USER)
        self.preview_table = table(self.preview_filter, 2)
        self.preview_table.setColumnWidth(0, 72)
        self.preview_table.setColumnWidth(1, 44)
        self.preview_table.setColumnWidth(4, 64)
        self.preview_info = QLabel("Wklej link, aby odczytać listę.")
        self.preview_info.setWordWrap(True)
        self.select_all_button = button("Zaznacz wszystkie", lambda: self.select_visible(True))
        self.select_none_button = button("Odznacz wszystkie", lambda: self.select_visible(False))
        self.add_button = button("Dodaj do kolejki", self.enqueue, primary=True)
        self.add_button.setToolTip("Dodaj zaznaczone pozycje widoczne w podglądzie.")
        self.add_first = button("Pobierz następne", lambda: self.enqueue(first=True))
        self.add_first.setToolTip("Dodaj zaznaczone widoczne pozycje na początek kolejki. Ruszą w pierwszym wolnym miejscu.")
        self.organize_button = button("Uporządkuj pliki…", self.organize_downloads)
        self.organize_button.setToolTip("Dopasuj istniejące pliki do pozycji z playlisty i zobacz plan przeniesienia.")
        self.preview_search = QLineEdit()
        self.preview_search.setPlaceholderText("Filtruj tytuły w podglądzie")
        self.preview_search.textChanged.connect(self.filter_preview)
        self.preview_order = combo([("Kolejność źródła", "source"), ("Odwróć listę", "reverse"),
                                    ("Od najstarszych", "oldest"), ("Od najnowszych", "newest"),
                                    ("Według numeru odcinka", "episode")], "source")
        self.preview_order.setToolTip("Kolejność w kolejce i numeracja plików. Daty z podglądu mogą być przybliżone.")
        self.episode_order = self.preview_order.model().item(self.preview_order.findData("episode"))
        self.episode_order.setEnabled(False)
        self.preview_order.currentIndexChanged.connect(self.order_preview)
        self.preview_mode = combo([("Tryb z ustawień", None), ("Tylko audio", True), ("Wideo", False)], None)
        self.preview_mode.setToolTip("Tryb pobierania pozycji dodawanych z tego podglądu.")
        self.preview_parameters = button("Parametry pobierania…", self.edit_preview_options)
        self.media_details = button("Formaty i napisy…", self.show_media_details)
        self.preview_parameters.setEnabled(False)
        self.preview_mode.setEnabled(False)
        self.preview_model.dataChanged.connect(lambda *_: self.refresh_preview_actions())
        self.preview_table.selectionModel().currentChanged.connect(lambda *_: self.refresh_preview_actions())
        self.set_preview_selectable(False)
        filter_row = row(self.preview_search, self.preview_order, self.preview_mode, self.preview_parameters,
                         self.media_details)
        filter_row.layout().setStretch(0, 1)
        layout.addWidget(filter_row)
        layout.addWidget(row(self.preview_info, None, self.select_all_button, self.select_none_button,
                             self.organize_button, self.add_first, self.add_button))
        layout.addWidget(self.preview_table, 1)
        return page

    def queue_page(self):
        page = QWidget()
        queue_layout = QVBoxLayout(page)
        self.queue_model = QueueModel()
        self.queue_filter = QueueFilter(self.queue_model, self)
        self.queue_table = table(self.queue_filter, 0)
        self.queue_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.queue_table.setColumnWidth(1, 139)
        self.queue_table.setColumnWidth(2, 116)
        self.queue_table.setColumnWidth(3, 92)
        self.queue_table.setColumnWidth(4, 65)
        self.queue_table.setColumnWidth(5, 230)
        self.queue_table.setItemDelegateForColumn(2, ProgressDelegate(self.queue_table))
        self.pause_all = button("Wstrzymaj kolejkę", lambda: self.c.pause_all(not self.c.paused))
        self.pause_selected = button("Wstrzymaj", lambda: self.selected_action("pause"))
        self.resume_selected = button("Wznów", lambda: self.selected_action("resume"))
        self.retry_failed = button("Ponów wszystkie nieudane", self.retry_failures)
        self.move_first = button("Na początek", lambda: self.move_queue("first"))
        self.move_up = button("Wyżej", lambda: self.move_queue("up"))
        self.move_down = button("Niżej", lambda: self.move_queue("down"))
        self.move_last = button("Na koniec", lambda: self.move_queue("last"))
        self.remove_selected = button("Usuń z kolejki", self.remove_selected_jobs)
        self.remove_selected.setToolTip("Usuń zaznaczone zadania. Aktywne pobrania zostaną zatrzymane, a pliki pozostaną na dysku.")
        self.edit_selected = button("Parametry…", self.edit_selected_jobs)
        self.retry_selected = button("Ponów wybrane", self.retry_selected_jobs)
        for control in (self.move_first, self.move_up, self.move_down, self.move_last):
            control.setToolTip("Zmień pozycję zaznaczonych zadań w całej kolejce.")
        self.queue_table.selectionModel().selectionChanged.connect(lambda *_: self.selection_state())
        self.queue_search = QLineEdit()
        self.queue_search.setPlaceholderText("Szukaj po tytule w kolejce")
        self.queue_search.setClearButtonEnabled(True)
        self.queue_status = combo([("Wszystkie stany", None), *((label, state) for state, label in LABELS.items())], None)
        search_row = row(self.queue_search, self.queue_status)
        search_row.layout().setStretch(0, 1)
        queue_layout.addWidget(search_row)
        queue_layout.addWidget(row(self.pause_selected, self.resume_selected,
                                   self.pause_all, self.retry_failed,
                                   button("Wyczyść zakończone", self.clear_queue), None))
        queue_layout.addWidget(row(self.move_first, self.move_up, self.move_down, self.move_last, None,
                                   self.edit_selected, self.retry_selected, self.remove_selected))
        queue_layout.addWidget(self.queue_table, 1)
        self.summary = QLabel()
        self.total = QProgressBar()
        self.total.setFormat("%p% · łączny postęp")
        queue_layout.addWidget(self.summary)
        queue_layout.addWidget(self.total)
        self.queue_search.textChanged.connect(self.filter_queue)
        self.queue_status.currentIndexChanged.connect(self.filter_queue)
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
        self.preview_order.setCurrentIndex(0)
        self.preview_model.set_entries([])
        self.episode_order.setEnabled(False)
        self.preview_search.clear()
        self.preview_music = source.music
        self.preview_source = source
        if not preserve_options:
            self.media_choices.clear()
            self.reset_preview_options()
        self.preview_mode.setEnabled(True)
        self.preview_parameters.setEnabled(True)
        self.set_preview_selectable(False)
        self.tabs.setCurrentWidget(self.preview_tab)

    def set_preview_selectable(self, enabled):
        self.preview_model.set_checkable(enabled)
        multiple = len(self.preview_model.entries) > 1
        for order in ("reverse", "oldest", "newest"):
            self.preview_order.model().item(self.preview_order.findData(order)).setEnabled(multiple)
        self.preview_order.setEnabled(enabled and (multiple or bool(self.preview_model.episode_numbers)))
        self.refresh_preview_actions()

    def order_preview(self):
        order = self.preview_order.currentData()
        self.preview_model.set_order(order)
        self.preview_filter.sort(1 if self.preview_model.positions else -1)
        number_header = self.preview_table.horizontalHeader()
        number_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents
                                           if order == "episode" else QHeaderView.ResizeMode.Interactive)
        if order != "episode":
            self.preview_table.setColumnWidth(1, 44)
        if order in ("oldest", "newest"):
            entries = self.preview_model.entries
            missing = sum(entry.published_at is None for entry in entries)
            if missing == len(entries):
                self.notify("Brak dat publikacji. Jeśli lista zaczyna się od najnowszych, wybierz „Odwróć listę”.")
            elif missing:
                self.notify(f"Posortowano według dostępnych dat. Pozycje bez daty ({missing}) są na końcu.")
            else:
                self.notify("Posortowano według dat publikacji. Daty w podglądzie mogą być przybliżone.")
        elif order == "episode":
            self.notify(f"Wykryto numery w tytułach {len(self.preview_model.episode_numbers)} pozycji.")

    def filter_preview(self, text):
        active = bool(text.strip())
        self.preview_filter.setFilterFixedString(text.strip())
        self.select_all_button.setText("Zaznacz widoczne" if active else "Zaznacz wszystkie")
        self.select_none_button.setText("Odznacz widoczne" if active else "Odznacz wszystkie")
        self.add_button.setText("Dodaj widoczne" if active else "Dodaj do kolejki")
        self.refresh_preview_actions()

    def refresh_preview_actions(self):
        enabled = self.preview_model.checkable and self.preview_filter.rowCount() > 0
        self.select_all_button.setEnabled(enabled)
        self.select_none_button.setEnabled(enabled)
        can_add = enabled and any(row in self.preview_model.selected for row in self.visible_rows())
        self.add_button.setEnabled(can_add)
        self.add_first.setEnabled(can_add)
        playlist = self.preview_source is not None and self.preview_source.kind in ("playlist", "album")
        self.organize_button.setVisible(playlist)
        self.organize_button.setEnabled(playlist and self.preview_model.checkable and bool(self.preview_model.entries))
        entry = self.selected_media()
        self.media_details.setEnabled(entry is not None)
        self.media_details.setText("Wybrane formaty…" if entry and entry.id in self.media_choices else "Formaty i napisy…")

    def selected_media(self):
        if not self.preview_model.checkable:
            return None
        index = self.preview_table.currentIndex()
        if self.preview_filter.rowCount() == 1:
            index = self.preview_filter.index(0, 0)
        source = self.preview_filter.mapToSource(index)
        if source.isValid() and source.row() < len(self.preview_model.entries):
            return self.preview_model.entries[source.row()]
        return None

    def organize_downloads(self):
        folder = QFileDialog.getExistingDirectory(self, "Folder ze starszymi pobraniami", self.options_for_preview().folder)
        if not folder:
            return
        try:
            root = Path(folder).resolve(strict=True)
            groups = make_plan(root, self.preview_model.entries, self.c.store.history())
        except (OSError, ValueError) as exc:
            LOG.info("Plan porządkowania pobrań: %s", exc)
            self.notify(str(exc) if isinstance(exc, ValueError) else "Nie można odczytać folderu z pobraniami.")
            return
        if not groups:
            self.notify("W wybranym folderze nie znaleziono plików z tej playlisty.")
            return
        active = {item.job.entry.id for item in self.c.active.values()}
        groups = [replace(group, status="Trwa pobieranie") if not group.status and group.video_id in active else group
                  for group in groups]
        dialog = OrganizeDialog(root, groups, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            active = {item.job.entry.id for item in self.c.active.values()}
            moved, errors, busy = apply_plan(self.c.store, root, groups, active)
            message = f"Przeniesiono plików: {moved}."
            if errors:
                message += f" Grupy z błędem: {errors}. Szczegóły są w logu."
            if busy:
                message += f" Pliki pominięte podczas pobierania: {busy}."
            self.notify(message)
            self.refresh()
        dialog.deleteLater()

    def show_media_details(self):
        entry = self.selected_media()
        if entry is None:
            return
        try:
            self.c.inspect_media(entry)
        except ValueError as exc:
            self.notify(str(exc))
            return
        dialog = MediaDetailsDialog(entry, self, self.media_choices.get(entry.id))
        self.c.media_inspected.connect(dialog.set_result)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                if dialog.choices:
                    self.media_choices[entry.id] = dialog.choices
                else:
                    self.media_choices.pop(entry.id, None)
                self.refresh_preview_actions()
        finally:
            self.c.cancel_media_inspection()
            self.c.media_inspected.disconnect(dialog.set_result)
            dialog.deleteLater()

    def visible_rows(self):
        if not self.preview_search.text().strip() and not self.preview_model.positions:
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
        self.preview_model.episode_numbers = detect_episode_numbers(entries)
        detected = bool(self.preview_model.episode_numbers)
        self.episode_order.setEnabled(detected)
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
        if detected:
            self.preview_order.setCurrentIndex(self.preview_order.findData("episode"))

    def preview_defaults(self):
        if self.active_profile is not None:
            return self.download_profiles[self.active_profile]
        defaults = self.c.prefs.options
        return replace(defaults, audio_only=True) if self.preview_music else defaults

    def reset_preview_options(self):
        self.preview_options = None
        self.preview_mode.setCurrentIndex(1 if self.preview_music and self.active_profile is None else 0)
        self.preview_parameters.setText("Parametry pobierania…")

    def options_for_preview(self):
        defaults = self.preview_defaults()
        options = self.preview_options or defaults
        mode = self.preview_mode.currentData()
        return replace(options, audio_only=defaults.audio_only if mode is None else mode)

    def build_profiles_menu(self):
        self.profiles_menu.clear()
        profiles = [("Ustawienia domyślne", None)]
        profiles.extend((name, name) for name in sorted(self.download_profiles, key=str.casefold))
        for label, name in profiles:
            action = self.profiles_menu.addAction(label.replace("&", "&&"))
            action.setCheckable(True)
            action.setChecked(self.active_profile == name)
            action.triggered.connect(lambda checked=False, value=name: self.select_profile(value))
        self.profiles_menu.addSeparator()
        self.profiles_menu.addAction("Zapisz parametry podglądu…", self.save_download_profile)
        remove = self.profiles_menu.addAction("Usuń aktywny profil…", self.delete_download_profile)
        remove.setEnabled(self.active_profile is not None)

    def select_profile(self, name):
        self.active_profile = name
        self.reset_preview_options()
        label = self.profiles_button.fontMetrics().elidedText(name or "Profile…", Qt.TextElideMode.ElideRight, 115)
        self.profiles_button.setText(label.replace("&", "&&"))
        self.profiles_button.setToolTip(f"Profil pobierania · {name}" if name else "Profile pobierania")

    def write_profiles(self, profiles):
        self.c.store.set("download_profiles", {name: asdict(options) for name, options in profiles.items()})
        self.download_profiles = profiles

    def save_download_profile(self):
        options = self.options_for_preview()
        try:
            options.validate()
        except ValueError as exc:
            self.notify(str(exc))
            return
        name, accepted = QInputDialog.getText(self, "Zapisz profil pobierania", "Nazwa profilu",
                                              text=self.active_profile or "")
        name = name.strip()
        if not accepted or not name:
            return
        existing = next((key for key in self.download_profiles if key.casefold() == name.casefold()), None)
        profiles = dict(self.download_profiles)
        if existing is not None:
            if QMessageBox.question(self, "Zastąp profil", f"Zapisać nowe parametry profilu „{existing}”?") != QMessageBox.StandardButton.Yes:
                return
            del profiles[existing]
        profiles[name] = options
        self.write_profiles(profiles)
        self.select_profile(name)
        self.notify(f"Zapisano profil „{name}”.")

    def delete_download_profile(self):
        name = self.active_profile
        if name is None:
            return
        if QMessageBox.question(self, "Usuń profil", f"Usunąć profil „{name}”?") == QMessageBox.StandardButton.Yes:
            self.write_profiles({key: value for key, value in self.download_profiles.items() if key != name})
            self.select_profile(None)
            self.notify(f"Usunięto profil „{name}”.")

    def edit_preview_options(self):
        defaults = self.preview_defaults()
        dialog = PreviewOptionsDialog(self.options_for_preview(), defaults, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.preview_options = dialog.options if dialog.options != defaults else None
            if self.preview_options is None:
                self.reset_preview_options()
            else:
                self.preview_mode.setCurrentIndex(self.preview_mode.findData(dialog.options.audio_only))
                self.preview_parameters.setText("Zmienione parametry…")
        dialog.deleteLater()

    def enqueue(self, *, first=False):
        options = self.options_for_preview()
        selected = [index for index in self.visible_rows() if index in self.preview_model.selected]
        entries = [self.preview_model.entry_at(i) for i in selected]
        overrides = {entry.id: replace(options, **self.media_choices[entry.id])
                     for entry in entries if entry.id in self.media_choices}
        try:
            options.validate()
            for value in overrides.values():
                value.validate()
        except ValueError as exc:
            self.notify(str(exc))
            return
        if selected:
            count = self.c.store.enqueue(entries, options, first=first, overrides=overrides)
            self.c.store.skip_downloaded()
            self.preview_model.select_rows(selected, False)
            message = f"Dodano {count} pozycji na początek kolejki." if first else f"Dodano pozycji: {count}."
            if first and self.c.paused:
                message += " Kolejka jest wstrzymana."
            self.notify(message)
            self.refresh()
            if first:
                self.tabs.setCurrentWidget(self.queue_tab)
                self.queue_table.scrollToTop()

    def selected_jobs(self):
        jobs = self.queue_model.jobs
        return [jobs[source.row()] for index in self.queue_table.selectionModel().selectedRows()
                if (source := self.queue_filter.mapToSource(index)).isValid() and source.row() < len(jobs)]

    def filter_queue(self):
        self.queue_filter.set_filters(self.queue_search.text(), self.queue_status.currentData())
        self.selection_state()
        self.update_summary()

    def selection_state(self):
        jobs = self.selected_jobs()
        self.remove_selected.setEnabled(bool(jobs))
        self.edit_selected.setEnabled(any(job.state in EDITABLE_STATES and job.id not in self.c.active for job in jobs))
        self.retry_selected.setEnabled(any(job.state == "failed" for job in jobs))
        self.pause_selected.setEnabled(any(job.state in ("queued", "running", "repair") for job in jobs))
        self.resume_selected.setEnabled(any(job.state == "paused" for job in jobs))
        selected = {job.id for job in jobs}
        waiting = [job.id for job in self.queue_model.jobs if job.state in WAITING_STATES]
        positions = [i for i, job_id in enumerate(waiting) if job_id in selected]
        before = bool(positions) and positions[-1] >= len(positions)
        after = bool(positions) and positions[0] < len(waiting) - len(positions)
        self.move_first.setEnabled(before)
        self.move_up.setEnabled(before)
        self.move_down.setEnabled(after)
        self.move_last.setEnabled(after)

    def move_queue(self, direction):
        selected = [job.id for job in self.selected_jobs() if job.state in WAITING_STATES]
        if self.c.store.move_jobs(selected, direction):
            self.refresh()
            rows = [self.queue_model.positions[job_id] for job_id in selected]
            edge = min(rows) if direction in ("first", "up") else max(rows)
            self.queue_table.scrollTo(self.queue_filter.mapFromSource(self.queue_model.index(edge, 0)))

    def selected_action(self, action):
        for job in self.selected_jobs():
            if action == "pause" and job.state in ("queued", "running", "repair"):
                self.c.pause_job(job.id)
            elif action == "resume" and job.state == "paused":
                self.c.resume_job(job.id)

    def retry_failures(self):
        self.c.store.retry_failed()
        self.refresh()

    def retry_selected_jobs(self):
        if self.c.store.retry_failed([job.id for job in self.selected_jobs()]):
            self.refresh()

    def edit_selected_jobs(self):
        jobs = [job for job in self.selected_jobs() if job.state in EDITABLE_STATES and job.id not in self.c.active]
        if not jobs:
            return
        ids = {job.id for job in jobs}
        batch = len(jobs) > 1
        original = jobs[0].options
        dialog = PreviewOptionsDialog(original, None if batch else self.c.prefs.options, self, queue=True, batch=batch)
        self.c.editing_jobs.update(ids)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                baseline = asdict(original)
                changes = {key: value for key, value in asdict(dialog.options).items() if value != baseline[key]}
                count = self.c.store.edit_job_options(jobs, changes)
                if count:
                    self.notify(f"Zapisano parametry dla {count} zadań.")
        except ValueError as exc:
            self.notify(str(exc))
        finally:
            self.c.editing_jobs.difference_update(ids)
            dialog.deleteLater()
            self.refresh()

    def remove_selected_jobs(self):
        count = self.c.remove_jobs([job.id for job in self.selected_jobs()])
        if count:
            self.notify(f"Usunięto {count} zadań z kolejki.")

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
        selected = [job.id for job in self.selected_jobs()]
        if self.queue_model.update_jobs(self.c.store.jobs()) and selected:
            selection = QItemSelection()
            for job_id in selected:
                if job_id in self.queue_model.positions:
                    index = self.queue_filter.mapFromSource(self.queue_model.index(self.queue_model.positions[job_id], 0))
                    if index.isValid():
                        selection.select(index, index)
            flags = QItemSelectionModel.SelectionFlag
            self.queue_table.selectionModel().select(selection, flags.ClearAndSelect | flags.Rows)
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
                             f"   ·   Widoczne: {self.queue_filter.rowCount()} z {len(jobs)}")
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
    if smoke and "--queue-test" in args:
        from .smoke import run_queue_smoke
        run_queue_smoke(app, window, args[args.index("--queue-test") + 1])
    elif smoke:
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
