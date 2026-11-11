# YT Downloader

Aplikacja desktopowa dla Windows 10/11 x64. Interfejs PySide6 obsługuje podgląd i kolejkę, a osobne procesy wywołują bibliotekę `yt_dlp`. SQLite zapisuje stan kolejki i historię w `%LOCALAPPDATA%\YTDownloader`.

## Instalacja i pierwsze pobranie

1. Pobierz `YTDownloader-Setup.exe` z [wydań GitHub](https://github.com/pr3ston1989/YT_Downloader/releases). Instalator tworzy skrót w menu Start i opcjonalny skrót na pulpicie, domyślnie zaznaczony.
2. Wklej adres filmu, playlisty albo kanału. Dla kanału wybierz **Filmy**, **Shorty** lub **Transmisje**. Link z parametrem `list` otwiera playlistę.
3. Kliknij **Pokaż podgląd**, odznacz zbędne pozycje i dodaj zaznaczone do kolejki. Jakość, folder i format ustawisz w zakładce **Ustawienia**. Każde zadanie zachowuje ustawienia obowiązujące w chwili dodania.
4. Jeśli YouTube prosi o zalogowanie, aplikacja otworzy link do materiału w przeglądarce. W **Ustawieniach** wybierz przeglądarkę i ewentualnie profil, w którym logujesz się do YouTube. Zapisz ustawienia i kliknij **Zalogowano · ponów**. Aplikacja ponownie odczyta podgląd lub uruchomi zadania czekające na logowanie.

Prywatna playlista wymaga konta, które może ją otworzyć w przeglądarce. Film 18+ wymaga konta dopuszczonego przez YouTube do oglądania treści z ograniczeniem wiekowym. Jeśli YouTube prosi o potwierdzenie wieku, wykonaj je w przeglądarce. [Dokumentacja YouTube](https://support.google.com/youtube/answer/10070779?hl=pl)

Jeśli przeglądarka blokuje odczyt ciasteczek, wybierz zalogowany profil Firefoksa lub zaimportuj plik Netscape. Z importowanego pliku aplikacja zachowuje tylko ciasteczka YouTube. Szyfruje je przez Windows DPAPI dla bieżącego użytkownika i przekazuje do yt-dlp w pamięci.

Długie playlisty i zakładki kanałów pojawiają się w podglądzie w miarę odczytu kolejnych stron. Po zakończeniu możesz wybrać pozycje do pobrania. Aplikacja ponawia odczyt bieżącej strony przy błędach połączenia obsługiwanych przez yt-dlp. Jeśli odczyt się nie powiedzie lub go anulujesz, podgląd zachowa odczytane pozycje i oznaczy listę jako niepełną.

Pole **Filtruj tytuły w podglądzie** ogranicza zaznaczanie i dodawanie do kolejki do widocznych wierszy. Po wyczyszczeniu filtra zobaczysz ponownie całą listę.

## YouTube Music

Wklej link do utworu, playlisty albo albumu YouTube Music. Album możesz wkleić jako link do strony `browse/MPRE…` albo do listy `OLAK5uy_…`. Link do wykonawcy otwiera podgląd zakładek jego kanału YouTube. Jeśli kanał typu Topic nie ma zakładki Filmy, aplikacja spróbuje odczytać jego listę przesłanych materiałów. Link do utworu z automatycznym miksem w tle otwiera sam utwór.

Dla YouTube Music podgląd domyślnie wybiera **Tylko audio**. Zmień tryb przy podglądzie, jeśli chcesz dodać wideo. Format MP3, M4A lub Opus i bitrate ustawisz w **Ustawieniach**. Przy włączonych metadanych aplikacja zapisuje album i numer utworu, gdy są dostępne. Historia rozpoznaje ten sam identyfikator niezależnie od tego, z którego serwisu pochodzi link.

Kolejkę lub wybrane zadania możesz wstrzymać. Po ponownym uruchomieniu zadania w toku wracają do kolejki, a wstrzymane pozostają wstrzymane. Wznawianie korzysta z plików `.part` i zależy od obsługi wznawiania przez serwer oraz dostępności tego samego strumienia. Gdy silnik wybierze inny strumień, aplikacja rozpoczyna pobieranie od początku. Błędy sieci mają trzy ponowienia z rosnącym opóźnieniem. W zakładce **Historia pobrań** możesz przejrzeć albo wyczyścić pamięć ID.

Domyślny szablon to `{channel}/{playlist}/{index} - {title} [{id}].{ext}`. Ścieżka jest względna wobec folderu docelowego. Dozwolone pola to `{channel}`, `{playlist}`, `{index}`, `{title}`, `{id}` i końcowe `{ext}`. Opcja ponownego pobrania dopisuje numer zadania do nazwy pliku. Tryb audio zapisuje napisy jako osobne pliki SRT. Napisy w MP4 są osadzane jako strumień tekstowy, a MKV obsługuje osadzone SRT.

Domyślnie aplikacja osadza metadane i miniaturę w pliku multimedialnym. MKV może też przechować pełny plik `.info.json` jako załącznik wewnątrz kontenera. Aby zapisywać osobne kopie w folderze pobrań, włącz **Zachowuj osobne pliki z miniaturą i metadanymi**. Ustawienie dotyczy nowych zadań. Pliki z wcześniejszych pobrań pozostają na dysku.

Subskrypcje sprawdzają wybrane zakładki kanału, gdy aplikacja działa, także w zasobniku. Pierwsze sprawdzenie uwzględnia materiały dostępne w tych zakładkach i pomija ID z historii. Autostart Windows i praca w zasobniku są domyślnie wyłączone.

Subskrypcja wykonawcy z YouTube Music dodaje zadania w trybie audio. Gdy kanał wymaga logowania, jego subskrypcja czeka na **Zalogowano · ponów**, a pozostałe są sprawdzane zgodnie z harmonogramem. Usunięcie subskrypcji przerywa jej bieżący odczyt.

## Aktualizacje

Przy starcie aplikacja testuje yt-dlp wraz z EJS, Deno, FFmpeg i FFprobe. Gdy lokalna kopia ulegnie uszkodzeniu, aplikacja sprawdza poprzednią wersję i kopię z instalatora. Brakujące komponenty pobiera z oficjalnych wydań i sprawdza ich SHA‑256.

Sprawdzanie wydań działa przy starcie i co sześć godzin. Rozpoznany błąd ekstraktora uruchamia dodatkowe sprawdzenie stable, a gdy ponowienie nadal zawodzi, aplikacja sprawdza nightly. Po automatycznym przejściu na nightly następna nowsza wersja stable przywróci ten kanał. Każde pobranie korzysta z wersji dostępnych w momencie startu zadania.

Zakładka **Aktualizacje** pokazuje wynik i szczegóły, a przycisk **Sprawdź teraz** pomija limit czasowy. Ustawienia pozwalają wyłączyć aktualizacje automatyczne lub wybrać nightly. Zainstalowana aplikacja pobiera nowsze stabilne wydanie z tego repozytorium, sprawdza SHA‑256 i uruchamia cichą instalację po zakończeniu aktywnych zadań.

Aktualizator korzysta z [oficjalnych wydań yt-dlp](https://github.com/yt-dlp/yt-dlp/releases) i [nightly](https://github.com/yt-dlp/yt-dlp-nightly-builds/releases), [wydań Deno](https://github.com/denoland/deno/releases) oraz [mirroru buildów FFmpeg dla Windows](https://github.com/GyanD/codexffmpeg/releases). Każde wydanie FFmpeg wskazuje odpowiadającą wersję kodu źródłowego. Szczegóły wymagań YouTube są opisane w [dokumentacji EJS](https://github.com/yt-dlp/yt-dlp/wiki/EJS).

## Struktura projektu

- `yt_downloader/model.py`, `store.py` — linki, ustawienia, ścieżki i trwały stan.
- `yt_downloader/worker.py`, `controller.py` — odczyt metadanych, pobieranie i sterowanie kolejką.
- `yt_downloader/updates/` — sprawdzanie wydań, sumy kontrolne, diagnostyka i aktywacja wersji.
- `yt_downloader/ui.py` — polski interfejs, ustawienia, historia i zasobnik systemowy.
- `packaging/`, `tests/` — kompilacja Windows, instalator, publikacja i testy.

Dane użytkownika obejmują `data.sqlite3`, `engine/`, `cookies.dpapi` i rotowane `app.log` w `%LOCALAPPDATA%\YTDownloader`. Domyślny folder pobrań to `%USERPROFILE%\Videos\YT Downloader`.

## Uruchomienie ze źródeł

Na Windows x64 z Pythonem 3.14 uruchom PowerShell w katalogu repozytorium.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m yt_downloader
```

Przy pierwszym starcie wersji źródłowej aplikacja pobiera zweryfikowane komponenty. Wersja instalowana zawiera sprawdzone kopie startowe i może uruchomić się bez internetu.

## Budowanie i testy

Zainstaluj [Inno Setup](https://jrsoftware.org/isdl.php) w wersji 6.3 lub nowszej.

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller pillow
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\packaging\build.ps1
```

`dist\YTDownloader\YTDownloader.exe` wymaga całego folderu onedir. `dist\YTDownloader-Setup.exe` jest instalatorem, a `dist\YTDownloader-Setup.exe.sha256` zawiera jego sumę kontrolną. Parametr `-OnedirOnly` pomija kompilację instalatora. Możesz przekazać własną ścieżkę kompilatora przez `-ISCC`.

Testy aktualizatora obejmują poprawną wymianę, zachowanie kopii poprzedniej, odrzucenie błędnej sumy, nieudany test uruchomieniowy i rollback po uszkodzeniu aktywnej wersji. GitHub Actions buduje instalator i uruchamia test dymny zapakowanej aplikacji.

Testy podglądu sprawdzają kolejność wierszy z wielu stron, zachowanie częściowej listy po błędzie lub anulowaniu oraz dodawanie tysięcy pozycji do kolejki.

Testy transferu zatrzymują osobny proces podczas pobierania z lokalnego serwera i porównują gotowy plik bajt po bajcie. Sprawdzają wznowienie oraz zmianę formatu w trakcie ponowienia. Test aktualizacji Windows sprawdza także zachowanie wstrzymanej kolejki po instalacji nowej wersji.

## Publikowanie nowej wersji

Zmień `APP_VERSION` w `yt_downloader/__init__.py` i zatwierdź zmiany w Git. Jedno polecenie tworzy oraz wysyła tag. GitHub Actions buduje i publikuje instalator oraz jego sumę.

```powershell
.\packaging\release.ps1
```
