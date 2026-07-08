# Sync New Machine

Nowy synchronizator Revend w Pythonie z oknem desktopowym.

## Start developerski

### Baza maszyny w Dockerze

```bash
cd sync-new-machine
docker compose up -d
```

Statyczne parametry DB maszyny:

- host: `127.0.0.1`
- port: `3306`
- database: `qcs`
- username: `root`
- password: `chushengfeng123`
- machine ID: `DEV_MACHINE_001`

W aplikacji wejdz w zakladke `Dev baza`, kliknij `Ustaw config Docker DB`, potem `Test bazy dev` i `Instaluj watcher`.

### Aplikacja

```powershell
cd sync-new-machine
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m sync_new_machine
```

Jesli Tkinter na macOS pokazuje puste okno, uruchom panel webowy:

```bash
python -m sync_new_machine.web_app
```

Panel otworzy sie pod `http://127.0.0.1:8787`.

Pierwszy start moze miec pusty `Machine ID`. W panelu webowym wejdz w `Akcje`,
podaj PIN serwisowy `210189`, ustaw `URL rejestracji` oraz osobny `URL pobierania konfiguracji`.
Najpierw uzyj `Sprawdz rejestracje`, a dopiero potem `Pobierz konfiguracje`.

Na macOS/Linux:

```bash
cd sync-new-machine
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m sync_new_machine
```

### Sztuczne transakcje i zdarzenia

Z GUI: zakladka `Dev baza` -> `Dodaj transakcje`, `Dodaj bin`, `Dodaj status` albo `Dodaj wszystko`.

Z CLI:

```bash
python -m sync_new_machine.dev_seed --dev-config --transactions 3 --bins 2
```

## Build EXE

```powershell
powershell -ExecutionPolicy Bypass -File build-windows.ps1
```

Wynik bedzie w `dist/RevendSyncNew/RevendSyncNew.exe`.

## Jak dziala watcher

Aplikacja instaluje w lokalnej bazie MySQL tabele `sync_outbox` oraz triggery:

- `user_transaction` -> event `transaction_finished`, gdy transakcja jest zakonczona,
- `empty_record` -> event `bin_record`,
- `command` -> event `status_changed`.

Program odpytuje tylko `sync_outbox`, domyslnie co 2 sekundy. Nie wykonuje pelnego synca co minute. Gdy API jest niedostepne, transakcje trafiaja do kolejki offline i sa ponawiane.
