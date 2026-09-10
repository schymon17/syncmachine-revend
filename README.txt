
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
- MySQL user with CREATE, ALTER and TRIGGER permissions for event-driven sync.
  Without them the agent automatically uses the 20-second cursor fallback.

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
    "sync": { "intervalSecondsTrans": 20, "enabledTrans": true },
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

Immediate transaction delivery
------------------------------
The daemon installs two MySQL triggers on user_transaction. Completion state
2, 4 or 5 writes a durable transaction_finished event to sync_outbox. The
daemon normally sends it within one second. API/network failures are retried,
and the regular cursor scan remains enabled as a safety net.

Files
-----
- src\*.php              (core classes)
- bin\sync.php           (CLI entry)
- data\app.config.json   (config)
- data\app.logs.ndjson   (logs)
- data\offline-queue.jsonl
- data\snapshot.json
