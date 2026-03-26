
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

Windows helpers
---------------
- run-once.bat   (runs one cycle and pauses)
- daemon.bat     (watchdog loop, restarts daemon on every exit)
- daemon-autostart.bat (starts daemon.bat minimized; preferred for Windows Startup)

Files
-----
- src\*.php              (core classes)
- bin\sync.php           (CLI entry)
- data\app.config.json   (config)
- data\app.logs.ndjson   (logs)
- data\offline-queue.jsonl
- data\snapshot.json
