#!/usr/bin/env php
<?php
require __DIR__ . '/../src/Config.php';
require __DIR__ . '/../src/Logger.php';
require __DIR__ . '/../src/Db.php';
require __DIR__ . '/../src/DbDiff.php';
require __DIR__ . '/../src/Http.php';
require __DIR__ . '/../src/Sync.php';
require __DIR__ . '/../src/Bootstrap.php';

$ROOT = realpath(__DIR__ . '/..') ?: (__DIR__ . '/..');
$CFG = $ROOT . '/data/app.config.json';
$CFGB = $ROOT . '/data/basic.config.json';
$EMERGENCY_LOG = $ROOT . '/data/emergency.logs.ndjson';
$emergencyLog = new Logger($EMERGENCY_LOG);

register_shutdown_function(function () use ($emergencyLog) {
    $fatal = error_get_last();
    if (!$fatal) return;

    $fatalTypes = [E_ERROR, E_PARSE, E_CORE_ERROR, E_COMPILE_ERROR, E_USER_ERROR];
    if (!in_array((int)$fatal['type'], $fatalTypes, true)) return;

    $emergencyLog->log('ERROR', 'Fatal shutdown', [
        'type' => $fatal['type'],
        'message' => $fatal['message'] ?? '',
        'file' => $fatal['file'] ?? '',
        'line' => $fatal['line'] ?? 0,
    ]);
});

set_exception_handler(function (Throwable $e) use ($emergencyLog) {
    $emergencyLog->log('ERROR', 'Uncaught exception', [
        'error' => $e->getMessage(),
        'file' => $e->getFile(),
        'line' => $e->getLine(),
    ]);
    fwrite(STDERR, "FATAL: ".$e->getMessage().PHP_EOL);
    exit(1);
});

function usage(): void {
    echo "Usage:\n";
    echo "  php bin/sync.php bootstrap        Fetch remote config and save\n";
    echo "  php bin/sync.php run-once         Run one sync cycle (auto-bootstrap if needed)\n";
    echo "  php bin/sync.php daemon           Run continuous sync loop (auto-bootstrap if needed)\n";
    echo "  php bin/sync.php flush-queue      Try to flush queued payloads\n";
    echo "  php bin/sync.php test-db          Test DB connection\n";
    echo "\n";
    exit(1);
}

function loadConfigOrFail(Config $config, Logger $log, string $label): array {
    try {
        return $config->load();
    } catch (Throwable $e) {
        $log->log('ERROR', 'Config load failed', ['config' => $label, 'error' => $e->getMessage()]);
        fwrite(STDERR, "CONFIG ERROR ($label): ".$e->getMessage().PHP_EOL);
        exit(1);
    }
}

function parseMemoryToBytes(string $memory): int {
    $memory = trim($memory);
    if ($memory === '' || $memory === '-1') return -1;
    $unit = strtolower(substr($memory, -1));
    $num = (float)$memory;
    return match ($unit) {
        'g' => (int)($num * 1024 * 1024 * 1024),
        'm' => (int)($num * 1024 * 1024),
        'k' => (int)($num * 1024),
        default => (int)$num,
    };
}

function ensureMinimumMemoryLimit(string $minLimit, Logger $log): void {
    $currentRaw = (string)ini_get('memory_limit');
    $current = parseMemoryToBytes($currentRaw);
    $min = parseMemoryToBytes($minLimit);
    if ($min <= 0) return;
    if ($current === -1 || $current >= $min) return;

    $ok = @ini_set('memory_limit', $minLimit);
    $log->log($ok !== false ? 'INFO' : 'WARN', 'Adjusted PHP memory_limit', [
        'from' => $currentRaw,
        'to' => $minLimit,
        'success' => $ok !== false,
    ]);
}

$cmd = $argv[1] ?? null;
if (!$cmd) usage();

$config = new Config($CFG);
$configBasic = new Config($CFGB);
$cfgArr = loadConfigOrFail($config, $emergencyLog, 'data/app.config.json');
loadConfigOrFail($configBasic, $emergencyLog, 'data/basic.config.json');

$log = new Logger($ROOT . '/' . ($cfgArr['paths']['log'] ?? 'data/app.logs.ndjson'));
$log->log('INFO', 'CLI command start', ['cmd' => $cmd]);
ensureMinimumMemoryLimit((string)($cfgArr['runtime']['memoryLimit'] ?? '256M'), $log);

function ensure_bootstrap(Config $config, Config $configBasic, Logger $log): void {
    $cfg = $config->load();
    if (!isset($cfg['configDone']) || $cfg['configDone'] !== true) {
        echo "Running first-time bootstrap...\n";
        $boot = new Bootstrap($config, $configBasic, $log);
        $boot->run();
    }
}

try {
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
                try {
                    $sync->runOnce();
                } catch (Throwable $e) {
                    $log->log('ERROR', 'Daemon iteration failed (continuing)', [
                        'error' => $e->getMessage(),
                        'file' => $e->getFile(),
                        'line' => $e->getLine(),
                    ]);
                }
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
                    $log->log('WARN', 'Still offline (manual flush)', ['error' => $e->getMessage()]);
                }
            }
            file_put_contents($queuePath, implode(PHP_EOL, $remain).(count($remain) ? PHP_EOL : ''), LOCK_EX);
            echo "Flush complete.\n";
            break;

        case 'test-db':
            try {
                $pdo = Db::pdo($cfgArr['db']);
                $pdo->query('SELECT 1');
                $log->log('INFO', 'DB test OK');
                echo "DB OK\n";
            } catch (Throwable $e) {
                $log->log('ERROR', 'DB test failed', ['error' => $e->getMessage()]);
                echo "DB ERROR: " . $e->getMessage() . "\n";
            }
            break;

        default:
            usage();
    }
} catch (Throwable $e) {
    $log->log('ERROR', 'Command failed', [
        'cmd' => $cmd,
        'error' => $e->getMessage(),
        'file' => $e->getFile(),
        'line' => $e->getLine(),
    ]);
    $emergencyLog->log('ERROR', 'Command failed (emergency)', [
        'cmd' => $cmd,
        'error' => $e->getMessage(),
    ]);
    fwrite(STDERR, "ERROR: ".$e->getMessage().PHP_EOL);
    exit(1);
}
