from collections import defaultdict, deque
from dataclasses import dataclass, replace
import re

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QPlainTextEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .model import CHANNEL_TABS, Source, detect_episode_numbers, episode_order, parse_source


def parse_links(text):
    sources = {}
    invalid = repeated = 0
    for token in re.split(r"[\s,;]+", text.strip()):
        if not token:
            continue
        try:
            source = parse_source(token.strip("<>[]()\"'"))
        except ValueError:
            invalid += 1
        else:
            repeated += source in sources
            sources[source] = None
    return list(sources), invalid, repeated


@dataclass
class CollectedSource:
    source: Source
    result: dict | None = None
    profile: str | None = None
    folder: str = ""
    status: str = "Oczekuje"

    @property
    def entries(self):
        return self.result["entries"] if self.result else ()

    @property
    def title(self):
        if self.entries:
            entry = self.entries[0]
            return entry.title if self.source.kind == "video" else entry.source_name or entry.playlist or entry.channel
        return self.source.url


class LinkCollector(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.c = window.c
        self.items = []
        self.owners = defaultdict(set)
        self.pending = deque()
        self.active = None
        self.input = QPlainTextEdit()
        self.input.setPlaceholderText("Wklej linki do filmów, playlist, kanałów lub albumów, po jednym w wierszu")
        self.input.setMaximumHeight(74)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(("Źródło", "Pozycje", "Duplikaty", "Profil", "Folder", "Stan"))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for index, width in enumerate((65, 76, 108, 126, 154), 1):
            self.table.setColumnWidth(index, width)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.hide()
        self.read = QPushButton("Odczytaj linki")
        self.read.clicked.connect(self.submit)
        self.stop = QPushButton("Anuluj odczyt")
        self.stop.clicked.connect(self.cancel)
        self.preview = QPushButton("Otwórz podgląd")
        self.preview.clicked.connect(self.open_selected)
        self.profile = QPushButton("Profil…")
        self.profile.clicked.connect(self.choose_profile)
        self.folder = QPushButton("Folder…")
        self.folder.clicked.connect(self.choose_folder)
        self.add = QPushButton("Dodaj zaznaczone do kolejki")
        self.add.clicked.connect(self.add_selected)
        self.add.setProperty("primary", True)
        self.table.itemSelectionChanged.connect(self.update_actions)
        self.table.cellDoubleClicked.connect(lambda *_: self.open_selected())
        self.input.textChanged.connect(self.update_actions)
        self.c.collected.connect(self.finished)
        layout = QVBoxLayout(self)
        layout.addWidget(self.input)
        scan_actions = QHBoxLayout()
        for control in (self.read, self.stop):
            scan_actions.addWidget(control)
        scan_actions.addStretch()
        layout.addLayout(scan_actions)
        layout.addWidget(self.notice)
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        for control in (self.preview, self.profile, self.folder):
            actions.addWidget(control)
        actions.addStretch()
        actions.addWidget(self.add)
        layout.addLayout(actions)
        self.timer = QTimer(self)
        self.timer.setInterval(300)
        self.timer.timeout.connect(self.start_next)
        self.update_actions()

    def selection(self):
        return [index.row() for index in self.table.selectionModel().selectedRows()]

    def update_actions(self):
        selection = self.selection()
        item = self.items[selection[0]] if len(selection) == 1 else None
        self.read.setEnabled(bool(self.input.toPlainText().strip()))
        self.stop.setEnabled(self.active is not None or bool(self.pending))
        self.preview.setEnabled(bool(item and item.result and (item.entries or item.result["login_retry"])))
        self.profile.setEnabled(bool(selection))
        self.folder.setEnabled(bool(selection))
        self.add.setEnabled(any(self.items[index].entries and not self.items[index].status.startswith("Dodano")
                                for index in selection))

    def submit(self):
        sources, invalid, repeated = parse_links(self.input.toPlainText())
        existing = {item.source: index for index, item in enumerate(self.items)}
        first = len(self.items)
        count = 0
        for source in sources:
            index = existing.get(source)
            if index is None:
                self.pending.append(len(self.items))
                self.items.append(CollectedSource(source))
                count += 1
            elif index not in self.pending and index != self.active and not self.items[index].entries:
                self.items[index].status = "Oczekuje"
                self.items[index].result = None
                self.pending.append(index)
                self.show_item(index)
                count += 1
        self.table.setRowCount(len(self.items))
        for index in range(first, len(self.items)):
            self.show_item(index)
        self.notice.setText(f"Linki do odczytu: {count} · Powtórzone: {len(sources) - count + repeated} · Nieprawidłowe: {invalid}")
        self.notice.show()
        if count:
            self.input.clear()
            self.timer.start()
            self.start_next()
        self.update_actions()

    def start_next(self):
        if not self.pending:
            self.timer.stop()
        elif self.active is None and not self.c.scan and not self.c.install_pending and self.c.manager.snapshot().ready:
            self.active = self.pending.popleft()
            item = self.items[self.active]
            item.status = "Odczytywanie…"
            self.show_item(self.active)
            source = item.source
            tabs = (source.tab,) if source.tab else CHANNEL_TABS
            self.c.preview(source, tabs, collector=True)
        self.update_actions()

    def finished(self, result):
        index, self.active = self.active, None
        if index is None:
            return
        item = self.items[index]
        item.result = result
        if result["complete"]:
            skipped = result.get("skipped_members", 0)
            item.status = f"Pominięto dla wspierających: {skipped}" if skipped else ("Gotowe" if item.entries else "Pusta lista")
        elif result["login_retry"]:
            item.status = "Wymaga logowania"
        else:
            item.status = "Niepełna lista" if item.entries else result.get("message", "Błąd odczytu")
        affected = {index}
        for video_id in {entry.id for entry in item.entries}:
            owners = self.owners[video_id]
            affected.update(owners)
            owners.add(index)
        for row in affected:
            self.show_item(row)
        self.update_actions()

    def cancel(self):
        while self.pending:
            index = self.pending.popleft()
            self.items[index].status = "Odczyt anulowany"
            self.show_item(index)
        if self.active is not None:
            self.c.cancel_scan()
        self.update_actions()

    def show_item(self, index):
        item = self.items[index]
        ids = {entry.id for entry in item.entries}
        queued, history = self.c.store.duplicate_ids(ids) if ids else (set(), set())
        shared = {video_id for video_id in ids if len(self.owners[video_id]) > 1}
        overlap = len(queued | history | shared) + len(item.entries) - len(ids)
        values = (item.title or item.source.url, str(len(item.entries)), str(overlap), item.profile or "Domyślny",
                  item.folder or "Z profilu", item.status)
        for column, value in enumerate(values):
            cell = QTableWidgetItem(value)
            tooltip = {0: item.source.url, 2: "Powtórzenia w źródłach, kolejce lub historii"}
            cell.setToolTip(tooltip.get(column, value))
            self.table.setItem(index, column, cell)

    def options(self, item):
        options = self.window.download_profiles.get(item.profile)
        if options is None:
            options = self.c.prefs.options
            if item.source.music:
                options = replace(options, audio_only=True)
        return replace(options, folder=item.folder) if item.folder else options

    def choose_profile(self):
        names = sorted(self.window.download_profiles, key=str.casefold)
        value, accepted = QInputDialog.getItem(self, "Profil pakietu", "Profil pobierania",
                                               ["(Domyślny)", *names], editable=False)
        if accepted:
            for index in self.selection():
                self.items[index].profile = value if value in names else None
                self.show_item(index)

    def choose_folder(self):
        selection = self.selection()
        if not selection:
            return
        folder = QFileDialog.getExistingDirectory(self, "Folder pakietu", self.options(self.items[selection[0]]).folder)
        if folder:
            for index in selection:
                self.items[index].folder = folder
                self.show_item(index)

    def open_selected(self):
        if self.preview.isEnabled():
            item = self.items[self.selection()[0]]
            self.window.open_collected_preview(item.source, item.result, self.options(item))

    def add_selected(self):
        batch = []
        for index in sorted(self.selection()):
            item = self.items[index]
            if not item.entries or item.status.startswith("Dodano"):
                continue
            options = self.options(item)
            try:
                options.validate()
            except ValueError as exc:
                self.notice.setText(str(exc))
                return
            name = (item.title or item.source.url) if item.source.kind != "video" else None
            batch.append((index, item, options, name))
        total = 0
        for index, item, options, name in batch:
            entries = item.entries
            positions, labels = episode_order(detect_episode_numbers(entries), len(entries))
            if positions:
                entries = [replace(entries[row], index=position, index_label=labels[row])
                           for row, position in positions.items()]
            count, _ = self.c.store.enqueue_batch(entries, options, package_name=name, omit_queued_duplicates=True)
            item.status = f"Dodano: {count} · Powtórzenia: {len(item.entries) - count}"
            self.show_item(index)
            total += count
        self.c.store.skip_downloaded(self.c.editing_jobs)
        self.notice.setText(f"Dodano do kolejki: {total}")
        self.update_actions()
        self.window.refresh()
