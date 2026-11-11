# YT Downloader

Aplikacja desktopowa dla Windows 10/11 x64. Interfejs PySide6 obsługuje podgląd i kolejkę, a osobne procesy wywołują bibliotekę `yt_dlp`. SQLite zapisuje stan kolejki i historię w `%LOCALAPPDATA%\YTDownloader`.

- `yt_downloader/model.py`, `store.py` — linki, ustawienia, ścieżki i trwały stan.
- `yt_downloader/worker.py`, `controller.py` — odczyt metadanych, pobieranie i sterowanie kolejką.
- `yt_downloader/updates/` — sprawdzanie wydań, sumy kontrolne, diagnostyka i aktywacja wersji.
- `yt_downloader/ui.py` — polski interfejs, ustawienia, historia i zasobnik systemowy.
- `packaging/`, `tests/` — kompilacja Windows, instalator, publikacja i testy.
