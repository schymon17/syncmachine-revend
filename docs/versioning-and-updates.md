# Versioning + Auto-Update

## 1. Wersjonowanie (SemVer)

- Format: `MAJOR.MINOR.PATCH` (np. `1.4.2`).
- Źródło prawdy: plik `VERSION` w root projektu.
Zasady:
- `PATCH`: poprawki bez zmiany kontraktów.
- `MINOR`: nowe funkcje kompatybilne wstecz.
- `MAJOR`: zmiany niekompatybilne.

## 2. Kontrakt check-update (backend)

Agent wysyła `POST {baseUrl}/update/check` (lub `update.checkUrl`):

```json
{
  "machineId": "RVM_500_123456",
  "integration": "polka",
  "currentVersion": "1.4.1",
  "channel": "stable",
  "platform": "Windows",
  "runtime": "php-cli",
  "timestamp": "2026-03-22T12:00:00Z"
}
```

Przykładowa odpowiedź:

```json
{
  "updateAvailable": true,
  "version": "1.4.2",
  "packageUrl": "https://cdn.example.com/revend/1.4.2/update.zip",
  "sha256": "HEX_SHA256",
  "notes": "hotfix",
  "minSupportedVersion": "1.4.0"
}
```

## 3. Struktura paczki update (ZIP)

Wymagane:
- `update.manifest.json`
- pliki aktualizacji (np. pod `files/`)

Manifest:

```json
{
  "version": "1.4.2",
  "minSupportedVersion": "1.4.0",
  "files": [
    {"from":"files/bin/sync.php","to":"bin/sync.php","sha256":"..."},
    {"from":"files/src/Sync.php","to":"src/Sync.php","sha256":"..."},
    {"from":"files/VERSION","to":"VERSION","sha256":"..."}
  ]
}
```

## 4. Runtime flow

1. `daemon` wykonuje sync.
2. `Updater` sprawdza endpoint aktualizacji.
3. Jeśli jest nowa wersja: pobiera ZIP + zapisuje `data/update.pending.json`.
4. Proces kończy się kodem `20`.
5. `daemon.bat` uruchamia `apply-update` i restartuje daemon.

## 5. Co jest celowo blokowane przy podmianie plików

`apply-update` nie pozwala pisać do:
- `data/`
- `php/`
- `.git/`

To zabezpiecza lokalną konfigurację i bundlowany runtime przed przypadkowym nadpisaniem.
