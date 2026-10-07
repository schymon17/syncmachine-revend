# ReVend Sync – agent synchronizacji maszyny (v3)

Pakiet `revend_sync`. Czyta lokalną bazę MySQL automatu i synchronizuje ją z ReVend
przez Machine API v2: transakcje, worki, status, heartbeat i logi wychodzą, katalog EAN,
kupony i aktualizacje agenta przychodzą.

## Jak działa

- **Transakcje w czasie rzeczywistym.** Co 2 s agent czyta `user_transaction` po kluczu
  głównym, od najstarszego niedokończonego wiersza. Transakcja (wszystkie wiersze kuponu
  z `transactiondone` 2/4/5) trafia do kolejki po 2–3 s od zakończenia. Raz na 5 minut
  przegląd ostatnich 20 000 wierszy łapie to, co zakończyło się później. Agent nie instaluje
  triggerów – zepsuty trigger blokowałby zapisy samej maszyny.
- **Tryb offline.** Wszystko, co wychodzi, najpierw trafia do kolejki w SQLite
  (`agent.db`, WAL, `synchronous=FULL`) razem z kursorem, w jednej transakcji.
  Awaria, brak prądu albo tydzień bez internetu niczego nie gubią ani nie dublują.
  Po powrocie łącza kolejka wysyła się sama, od najstarszych.
- **Ponawianie.** Brak połączenia → pauza z rosnącym odstępem (do 60 s). 408/429/5xx
  i zegar → ponowienie tej jednej wiadomości. Inne 4xx → wiadomość odrzucona
  („dead letter”), nie blokuje reszty, błąd idzie do logów panelu. Po 5xx nowy klucz
  idempotencji (serwer zapamiętałby błąd na 24 h).
- **Zegar.** Każda odpowiedź koryguje przesunięcie zegara (nagłówek `Date`), podpisy
  używają czasu serwera. Przy odchyłce > 60 s ostrzeżenie w logach.
- **Worki** – każda plomba osobno (konflikt jednej plomby nie odrzuca innych),
  `left` → `pet`, `right` → `can`.
- **Status** – przy zmianie albo co 10 min, w kolejce tylko najnowszy.
- **Heartbeat** co 5 min z blokiem `agent` (kolejka, ostatnia wysyłka, baza maszyny) –
  widoczny w karcie maszyny w panelu.
- **Logi** – plik `logs/agent.log` (JSON, rotacja 5 × 5 MB) oraz ostrzeżenia i błędy
  do panelu (`/v2/logs`), także zebrane offline. Sekrety są maskowane.
- **Katalog EAN** co 6 h: ładowanie do tabeli tymczasowej i atomowe `RENAME TABLE` –
  maszyna nigdy nie zostaje z pustą tabelą `barcode`. Odpowiedź dużo mniejsza niż obecny
  katalog nie jest importowana.
- **Kupony** – pula `printer_barcode` uzupełniana do 50, bez numerów użytych lub drukowanych.
- **Aktualizacje** co 6 h: pobranie i weryfikacja SHA-256; instalację wykonuje usługa (etap 3).
- Jedna instancja na katalog danych (blokada pliku), zadania niezależne – błąd jednego nie
  zatrzymuje innych.

Dane: `%PROGRAMDATA%\ReVend\Sync` (Windows) albo `~/.revend-sync`; `REVEND_SYNC_HOME`
lub `--home` zmienia katalog. Sekret API i hasło bazy są w `secrets.bin`, na Windows
zaszyfrowane DPAPI (zakres komputera).

## Polecenia

```
python -m revend_sync enroll --file enrollment.json [--db-password ...]   # instalator
python -m revend_sync run            # usługa
python -m revend_sync check          # baza maszyny + połączenie z API
python -m revend_sync status         # kolejka i odrzucone wiadomości
python -m revend_sync requeue-dead   # wyślij odrzucone ponownie (po poprawce na serwerze)
```

`enrollment.json` to odpowiedź `POST /api/revend/agent/enroll` (kod instalacyjny z karty
maszyny w panelu). Domyślnie wysyłane są tylko nowe transakcje i worki;
`--transactions-from-id` / `--bins-from-id` pozwalają dosłać starsze.

## Testy

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest                      # testy jednostkowe + atrapa API v2
docker run -d --name revend-sync-test-mysql57 --platform linux/amd64 -e MYSQL_ROOT_PASSWORD=test -p 33057:3306 mysql:5.7
docker run -d --name revend-sync-test-mysql80 -e MYSQL_ROOT_PASSWORD=test -p 33080:3306 mysql:8.0
REVEND_TEST_MYSQL=127.0.0.1:33057,127.0.0.1:33080 .venv/bin/python -m pytest   # + prawdziwy MySQL
.venv/bin/ruff check && .venv/bin/ruff format --check
```

Atrapa API (`tests/fake_api.py`) weryfikuje podpis HMAC, okno czasu, nonce, idempotencję
i reguły `/trans` tak jak serwer. Wektor podpisu w `tests/test_api.py` jest policzony w PHP.

## Etap 3 (jeszcze nie ma)

Paczka Windows (PyInstaller) z `install.ps1` w katalogu głównym (kontrakt:
`docs/integrations/MACHINE_API.md` w repo API), usługa Windows z restartem po awarii,
instalacja pobranej aktualizacji z cofnięciem, przejęcie maszyny od agenta PHP
(kursor z jego `snapshot.json`), reklamy (`/adverts`).

---

# Prototyp (`sync_new_machine`) – do wycofania

Poniżej opis prototypu z lipca 2026. Zostanie usunięty, gdy agent v3 go zastąpi.


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
