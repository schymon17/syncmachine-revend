# Plan: Sync New Machine

## Cel

Zastapic stary terminalowy synchronizator PHP aplikacja Python z oknem, ktora mozna zbudowac do pliku `.exe` dla Windows. Synchronizacja ma reagowac na zdarzenia w bazie maszyny, a nie odpalac pelny cykl co minute.

## Architektura

1. Aplikacja desktopowa w Pythonie/Tkinter:
   - ekran rejestracji maszyny,
   - test polaczenia z baza i API,
   - start/stop synchronizacji,
   - widok statusu, kolejki i logow.

2. Lokalny watcher bazy:
   - tabela `sync_outbox`,
   - triggery MySQL dla `user_transaction`, `empty_record`, `command`,
   - szybki polling tylko tabeli outbox, domyslnie co 2 sekundy,
   - fallback bez triggerow: lekki skan cursorow, jesli instalacja triggerow sie nie powiedzie.

3. Synchronizacja:
   - transakcje tylko zakonczone: `transactiondone IN (2,4,5)`,
   - wysylka binow z `empty_record`,
   - wysylka statusu z ostatniego rekordu `command`,
   - heartbeat okresowy,
   - EAN/kupony/reklamy jako zadania okresowe z cache/hash, bo nie sa lokalnymi zdarzeniami transakcji.

4. Stabilnosc:
   - lokalna kolejka offline dla transakcji,
   - limity wielkosci payloadow,
   - retry z oznaczaniem bledow w outbox,
   - snapshot cursorow w pliku JSON,
   - logi NDJSON.

5. API v2:
   - nowy prefix `/api/revend/machine/v2`,
   - endpoint `config` zwraca konfiguracje dla nowego synca z `baseUrl` v2,
   - pozostale endpointy v2 sa kompatybilne z v1 i przyjmuja payloady nowej aplikacji.

## Kolejne kroki po wdrozeniu

1. Uruchomic `python -m sync_new_machine` z folderu `sync-new-machine`.
2. W GUI wpisac `machineId`, adres API config oraz token.
3. Kliknac test bazy/API.
4. Kliknac instalacje watcherow.
5. Kliknac start.
6. Po weryfikacji zbudowac `.exe`: `powershell -ExecutionPolicy Bypass -File build-windows.ps1`.

## Dev database

Projekt zawiera `docker-compose.yml` z MySQL 8.4 i schematem minimalnej bazy maszyny:

- `user_transaction`,
- `empty_record`,
- `command`,
- `barcode`,
- `printer_barcode`,
- `machineinformation`.

Generator danych testowych jest dostepny w GUI w zakladce `Dev baza` oraz jako CLI:

```bash
python -m sync_new_machine.dev_seed --dev-config --transactions 3 --bins 2
```
