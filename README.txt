
PHP CLI Sync Agent (MySQL)
==========================

What this is
------------
A portable PHP command-line tool that:
  - Loads config from data/app.config.json
  - Diffs selected MySQL tables (by pk + chosen columns)
  - Sends only changes to your API (POST /sync/changes)
  - Queues when offline and retries later
  - Writes NDJSON logs to data/app.logs.ndjson

Requirements
------------
- Windows with PHP 8.x (CLI) available on PATH (php.exe)
  (If you use PHP Desktop's php.exe, you can call it directly: php\php.exe bin\sync.php ...)

Configure
---------
Edit: data\app.config.json
  {
    "machineId": "RVM_3000_ABC",
    "db": { "driver":"mysql","host":"127.0.0.1","port":3306,"database":"db","username":"user","password":"pass" },
    "tables": [
      {"name":"orders","pk":"id","columns":["id","status","updated_at"]}
    ],
    "api": { "baseUrl": "https://api.example.com", "token": "YOUR_TOKEN" },
    "sync": { "intervalSeconds": 60, "enabled": true },
    "paths": {
      "snapshot": "data/snapshot.json",
      "queue": "data/offline-queue.jsonl",
      "log": "data/app.logs.ndjson"
    }
  }

Commands
--------
- Run once:          php bin\sync.php run-once
- Daemon loop:       php bin\sync.php daemon
- Flush queue now:   php bin\sync.php flush-queue
- Test DB:           php bin\sync.php test-db
- Print version:     php bin\sync.php version
- Check update:      php bin\sync.php check-update
- Check + download:  php bin\sync.php check-update --download
- Apply pending:     php bin\sync.php apply-update

Versioning
----------
- App version is stored in: VERSION (SemVer recommended, e.g. 1.4.2)
- Every release package should update VERSION.

Auto-update (check/download/apply)
----------------------------------
Config block in data\app.config.json:
  "update": {
    "enabled": false,
    "channel": "stable",
    "checkPath": "/update/check",
    "checkIntervalSeconds": 3600,
    "autoDownload": true,
    "restartAfterPrepare": true,
    "checkOnRunOnce": false,
    "maxPackageBytes": 104857600,
    "stateFile": "data/update.state.json",
    "pendingFile": "data/update.pending.json"
  }

Flow:
1) Daemon checks endpoint for a newer version.
2) When available, it downloads package and writes pending metadata.
3) Daemon exits with code 20.
4) daemon.bat runs apply-update and restarts process.

Expected update package format (ZIP):
- update.manifest.json
- files/... (new files to deploy)

Manifest example:
  {
    "version": "1.4.2",
    "minSupportedVersion": "1.3.0",
    "files": [
      {"from":"files/bin/sync.php","to":"bin/sync.php","sha256":"..."},
      {"from":"files/src/Sync.php","to":"src/Sync.php","sha256":"..."},
      {"from":"files/VERSION","to":"VERSION","sha256":"..."}
    ]
  }

Windows helpers
---------------
- run-once.bat   (runs one cycle and pauses)
- daemon.bat     (runs continuous loop + applies pending updates)

Files
-----
- src\*.php              (core classes)
- bin\sync.php           (CLI entry)
- data\app.config.json   (config)
- data\app.logs.ndjson   (logs)
- data\offline-queue.jsonl
- data\snapshot.json
