#!/usr/bin/env php
<?php
require __DIR__ . '/../src/Config.php';
require __DIR__ . '/../src/Logger.php';
require __DIR__ . '/../src/Db.php';
require __DIR__ . '/../src/DbDiff.php';
require __DIR__ . '/../src/Http.php';
require __DIR__ . '/../src/Sync.php';
require __DIR__ . '/../src/Bootstrap.php';

$ROOT = realpath(__DIR__ . '/..');
$CFG = $ROOT . '/data/app.config.json';
$CFGB = $ROOT . '/data/basic.config.json';

function usage() {
    echo "Usage:\n";
    echo "  php bin/sync.php bootstrap        Fetch remote config and save\n";
    echo "  php bin/sync.php run-once         Run one sync cycle (auto-bootstrap if needed)\n";
    echo "  php bin/sync.php daemon           Run continuous sync loop (auto-bootstrap if needed)\n";
    echo "  php bin/sync.php flush-queue      Try to flush queued payloads\n";
    echo "  php bin/sync.php test-db          Test DB connection\n";
    echo "\n";
    exit(1);
}

$cmd = $argv[1] ?? null;
if (!$cmd) usage();

$config = new Config($CFG);
$cfgArr = $config->load();

$configBasic = new Config($CFGB);
$cfgArrBasic = $configBasic->load();

$log = new Logger($ROOT . '/' . ($cfgArr['paths']['log'] ?? 'data/app.logs.ndjson'));

function ensure_bootstrap(Config $config, Config $configBasic, Logger $log) {
    $cfg = $config->load();
    if (!isset($cfg['configDone']) || $cfg['configDone'] !== true) {
        echo "Running first-time bootstrap...\n";
        $boot = new Bootstrap($config, $configBasic, $log);
        $boot->run();
    }
}

switch ($cmd) {
    case 'bootstrap':
        $boot = new Bootstrap($config, $configBasic, $log);
        $boot->run();
        break;

    case 'run-once':
        ensure_bootstrap($config, $configBasic, $log);
        $cfgArr = $config->load();
        $sync = new Sync($cfgArr, $log);
        $sync->runOnce();
        echo "OK\n";
        break;

    case 'daemon':
        ensure_bootstrap($config, $configBasic, $log);
        $cfgArr = $config->load();
        $interval = max(10, (int)($cfgArr['sync']['intervalSeconds'] ?? 60));
        $sync = new Sync($cfgArr, $log);
        $log->log('INFO', 'Daemon started', ['interval' => $interval]);
        while (true) {
            $sync->runOnce();
            sleep($interval);
        }
        break;

    case 'flush-queue':
        $http = new Http($cfgArr['api']['baseUrl'] ?? '', $cfgArr['api']['token'] ?? null);
        $queueFile = $cfgArr['paths']['queue'] ?? 'data/offline-queue.jsonl';
        $queuePath = $ROOT . '/' . $queueFile;
        if (!file_exists($queuePath)) { echo "No queue file.\n"; exit(0); }
        $lines = file($queuePath, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) ?: [];
        $remain = [];
        foreach ($lines as $line) {
            $entry = json_decode($line, true);
            if (!is_array($entry)) { $remain[] = $line; continue; }

            $payload = $entry;
            $endpoint = '/sync/changes';
            if (isset($entry['endpoint']) && is_string($entry['endpoint']) && isset($entry['payload']) && is_array($entry['payload'])) {
                $endpoint = $entry['endpoint'];
                $payload = $entry['payload'];
            } else {
                $kind = (string)($entry['kind'] ?? '');
                $endpoint = match ($kind) {
                    'transactions' => '/trans',
                    'status' => '/status',
                    'heartbeat' => '/heartbeat',
                    'sync_bins' => '/bins',
                    default => '/sync/changes',
                };
            }
            try {
                $res = $http->postJson($endpoint, $payload);
                if ($res['status'] < 200 || $res['status'] >= 300) throw new RuntimeException('Bad status '.$res['status']);
                $log->log('INFO', 'Flushed queued item (manual)', [
                    'endpoint' => $endpoint,
                    'kind' => $payload['kind'] ?? 'unknown',
                ]);
            } catch (Throwable $e) {
                $remain[] = $line;
                $log->log('WARN', 'Still offline (manual flush)', ['error'=>$e->getMessage()]);
            }
        }
        file_put_contents($queuePath, implode(PHP_EOL, $remain).(count($remain)?PHP_EOL:''));
        echo "Flush complete.\n";
        break;

    case 'test-db':
        try {
            $pdo = Db::pdo($cfgArr['db']);
            $pdo->query('SELECT 1');
            echo "DB OK\n";
        } catch (Throwable $e) {
            echo "DB ERROR: " . $e->getMessage() . "\n";
        }
        break;

    default:
        usage();
}
