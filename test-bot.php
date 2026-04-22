#!/usr/bin/env php
<?php
declare(strict_types=1);

/**
 * test-bot.php — end-to-end test harness for the C# Revend.EventService.
 *
 * Seeds MySQL with N transactions, fires webhooks against the C# service,
 * verifies each event was forwarded to a mock (local) or production API,
 * and prints a pass/fail summary.
 *
 * Run:   php test-bot.php
 * Flip:  edit $cfg['mode'] to 'prod' to target panel.revend.pl
 */

$cfg = [
    'mode' => 'local',

    'csharp_url'  => 'http://localhost:21011',
    'machine_id'  => 'RVM_TEST_001',

    'mock_host' => '127.0.0.1',
    'mock_port' => 8089,

    'prod_base_url' => 'https://panel.revend.pl/api/revend/machine/v1',

    'transactions' => 30,
    'bags'         => 5,
    'errors'       => 1,
    'resets'       => 1,
    'bottles_min'  => 1,
    'bottles_max'  => 7,

    'db' => [
        'host' => '127.0.0.1',
        'port' => 3306,
        'db'   => 'qcs',
        'user' => 'root',
        'pass' => 'chushengfeng123',
    ],

    'appsettings_path' => __DIR__ . '/revend-event-service/Revend.EventService/appsettings.json',
    'mock_log_path'    => __DIR__ . '/test-bot.mock.log',
    'mock_router_path' => sys_get_temp_dir() . '/revend-test-bot-mock-router.php',
    'mock_server_log'  => __DIR__ . '/test-bot.mock.server.log',

    'poll_timeout_sec' => 30,
    'poll_interval_ms' => 500,
    'webhook_pause_ms' => 50,
];

// ---------- ENTRY ----------

exit(main($cfg));

function main(array $cfg): int
{
    banner($cfg);

    $state = [
        'appsettings_backup' => null,
        'mock_proc'          => null,
    ];

    register_shutdown_function(function () use (&$state, $cfg) {
        cleanup($cfg, $state);
    });
    if (function_exists('pcntl_signal')) {
        pcntl_async_signals(true);
        pcntl_signal(SIGINT, function () {
            fwrite(STDERR, "\n[test-bot] SIGINT — cleaning up\n");
            exit(130);
        });
        pcntl_signal(SIGTERM, function () {
            fwrite(STDERR, "\n[test-bot] SIGTERM — cleaning up\n");
            exit(143);
        });
    }

    try {
        ensureMachineIdSet($cfg);

        if ($cfg['mode'] === 'local') {
            @unlink($cfg['mock_log_path']);
            $state['mock_proc'] = startMockServer($cfg);
            info('mock server listening at http://' . $cfg['mock_host'] . ':' . $cfg['mock_port']);
        } else {
            info('mode=prod — mock server skipped; C# will forward to real BaseUrl in appsettings.json');
        }

        $state['appsettings_backup'] = patchAppsettings($cfg);
        info('appsettings.json patched (backup at ' . $state['appsettings_backup'] . ')');

        $printBarcodes = seedTransactions($cfg);
        info('seeded ' . count($printBarcodes) . ' transactions into qcs.user_transaction');

        $fired = fireAllWebhooks($cfg, $printBarcodes);
        info('fired ' . count($fired) . ' webhooks total');

        $results = pollMonitor($cfg, $fired);

        $pass = printSummary($results, $cfg);
        return $pass ? 0 : 1;
    } catch (Throwable $e) {
        fwrite(STDERR, "\n[test-bot] FATAL: " . $e->getMessage() . "\n");
        fwrite(STDERR, $e->getTraceAsString() . "\n");
        return 2;
    }
}

function cleanup(array $cfg, array $state): void
{
    if (!empty($state['appsettings_backup']) && is_file($state['appsettings_backup'])) {
        @copy($state['appsettings_backup'], $cfg['appsettings_path']);
        @unlink($state['appsettings_backup']);
        info('appsettings.json restored');
    }
    if (!empty($state['mock_proc'])) {
        stopMockServer($state['mock_proc']);
        info('mock server stopped');
    }
    if (is_file($cfg['mock_router_path'])) {
        @unlink($cfg['mock_router_path']);
    }
}

// ---------- MACHINE ID ----------

function ensureMachineIdSet(array $cfg): void
{
    $resp = httpJson('GET', $cfg['csharp_url'] . '/api/setup/machine');
    if ($resp['status'] < 200 || $resp['status'] >= 300) {
        throw new RuntimeException('C# service unreachable at ' . $cfg['csharp_url'] . ' (GET /api/setup/machine returned ' . $resp['status'] . '). Is `dotnet run` running?');
    }
    $body = json_decode($resp['body'], true) ?: [];
    $current = $body['machineNumber'] ?? '';
    if (is_string($current) && $current !== '') {
        info('machine id already set: ' . $current);
        return;
    }
    info('machine id not set — POSTing ' . $cfg['machine_id']);
    $set = httpJson('POST', $cfg['csharp_url'] . '/api/setup/machine', ['machineNumber' => $cfg['machine_id']]);
    if ($set['status'] < 200 || $set['status'] >= 300) {
        throw new RuntimeException('failed to set machine id: HTTP ' . $set['status'] . ' ' . $set['body']);
    }
}

// ---------- MOCK SERVER ----------

function startMockServer(array $cfg): array
{
    writeMockRouter($cfg['mock_router_path']);
    $cmd = sprintf(
        '%s -S %s:%d -t %s %s',
        escapeshellarg(PHP_BINARY),
        escapeshellarg($cfg['mock_host']),
        (int) $cfg['mock_port'],
        escapeshellarg(sys_get_temp_dir()),
        escapeshellarg($cfg['mock_router_path'])
    );
    $env = array_merge($_ENV, [
        'MOCK_LOG' => $cfg['mock_log_path'],
    ]);
    $descriptors = [
        0 => ['pipe', 'r'],
        1 => ['file', $cfg['mock_server_log'], 'a'],
        2 => ['file', $cfg['mock_server_log'], 'a'],
    ];
    $proc = proc_open($cmd, $descriptors, $pipes, null, $env);
    if (!is_resource($proc)) {
        throw new RuntimeException('failed to start mock server');
    }
    if (isset($pipes[0])) {
        fclose($pipes[0]);
    }
    waitForPort($cfg['mock_host'], (int) $cfg['mock_port'], 5.0);
    return ['proc' => $proc];
}

function stopMockServer(array $handle): void
{
    if (!isset($handle['proc']) || !is_resource($handle['proc'])) {
        return;
    }
    proc_terminate($handle['proc']);
    for ($i = 0; $i < 20; $i++) {
        $status = proc_get_status($handle['proc']);
        if (!$status['running']) {
            break;
        }
        usleep(100_000);
    }
    proc_close($handle['proc']);
}

function waitForPort(string $host, int $port, float $timeoutSec): void
{
    $deadline = microtime(true) + $timeoutSec;
    while (microtime(true) < $deadline) {
        $sock = @fsockopen($host, $port, $errno, $errstr, 0.2);
        if ($sock) {
            fclose($sock);
            return;
        }
        usleep(100_000);
    }
    throw new RuntimeException("mock server did not open {$host}:{$port} within {$timeoutSec}s");
}

function writeMockRouter(string $path): void
{
    $router = <<<'PHP'
<?php
$logFile = getenv('MOCK_LOG') ?: (sys_get_temp_dir() . '/revend-mock.log');
$method  = $_SERVER['REQUEST_METHOD'] ?? 'GET';
$uri     = $_SERVER['REQUEST_URI'] ?? '/';
$path    = parse_url($uri, PHP_URL_PATH) ?: '/';
$body    = file_get_contents('php://input') ?: '';
$parsed  = json_decode($body, true);

$entry = [
    'ts'       => date('c'),
    'method'   => $method,
    'path'     => $path,
    'api_key'  => $_SERVER['HTTP_X_API_KEY_MACHINE'] ?? null,
    'body_len' => strlen($body),
    'body'     => $parsed === null ? $body : $parsed,
];
@file_put_contents($logFile, json_encode($entry, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE) . "\n", FILE_APPEND | LOCK_EX);

$postRoutes = ['/trans', '/bins', '/status', '/reset', '/heartbeat'];
if ($method === 'POST' && in_array($path, $postRoutes, true)) {
    http_response_code(204);
    return true;
}
if ($method === 'GET' && $path === '/config') {
    header('Content-Type: application/json');
    echo json_encode(['ok' => true, 'config' => new stdClass()]);
    return true;
}
http_response_code(404);
header('Content-Type: application/json');
echo json_encode(['error' => 'not found in mock', 'method' => $method, 'path' => $path]);
return true;
PHP;
    file_put_contents($path, $router);
}

// ---------- APPSETTINGS PATCHER ----------

function patchAppsettings(array $cfg): string
{
    if (!is_file($cfg['appsettings_path'])) {
        throw new RuntimeException('appsettings.json not found at ' . $cfg['appsettings_path']);
    }
    $raw = file_get_contents($cfg['appsettings_path']);
    $backup = $cfg['appsettings_path'] . '.testbot.bak';
    file_put_contents($backup, $raw);

    $json = json_decode($raw, true);
    if (!is_array($json)) {
        throw new RuntimeException('appsettings.json is not valid JSON');
    }

    $json['Service'] = $json['Service'] ?? [];
    $json['Service']['Forwarding'] = $json['Service']['Forwarding'] ?? [];

    if ($cfg['mode'] === 'local') {
        $base = 'http://' . $cfg['mock_host'] . ':' . $cfg['mock_port'];
        $json['Service']['Forwarding']['TransactionUrl'] = $base . '/trans';
        $json['Service']['Forwarding']['BagUrl']         = $base . '/bins';
        $json['Service']['Forwarding']['ErrorUrl']       = $base . '/status';
        $json['Service']['Forwarding']['ResetUrl']       = $base . '/reset';
    } else {
        $json['Service']['Forwarding']['TransactionUrl'] = '';
        $json['Service']['Forwarding']['BagUrl']         = '';
        $json['Service']['Forwarding']['ErrorUrl']       = '';
        $json['Service']['Forwarding']['ResetUrl']       = '';
    }

    $out = json_encode($json, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    file_put_contents($cfg['appsettings_path'], $out);
    return $backup;
}

// ---------- SEEDER ----------

function seedTransactions(array $cfg): array
{
    $d = $cfg['db'];
    $dsn = sprintf('mysql:host=%s;port=%d;dbname=%s;charset=utf8mb4', $d['host'], (int) $d['port'], $d['db']);
    $pdo = new PDO($dsn, $d['user'], $d['pass'], [
        PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
    ]);

    $sql = 'INSERT INTO `user_transaction` (
        `transactionid`,`user`,`print_barcode`,`dateline`,`statecode`,`barcode`,`bors`,`weight`,`diam`,`metal`,
        `recognitionstatus`,`rebateordonate`,`payplatform`,`bottlevalue`,`charityid`,`charityname`,`octreceipt`,`transactiondone`,`uploaddone`
    ) VALUES (
        :transactionid,:user,:print_barcode,:dateline,:statecode,:barcode,:bors,:weight,:diam,:metal,
        :recognitionstatus,:rebateordonate,:payplatform,:bottlevalue,:charityid,:charityname,:octreceipt,:transactiondone,:uploaddone
    )';
    $stmt = $pdo->prepare($sql);

    $printBarcodes = [];
    $count = (int) $cfg['transactions'];
    $min = (int) $cfg['bottles_min'];
    $max = (int) $cfg['bottles_max'];

    for ($i = 1; $i <= $count; $i++) {
        $dateline = time();
        $printBarcode = randomBarcode13();
        $transactionId = 'm001' . $dateline . str_pad((string) $i, 2, '0', STR_PAD_LEFT);
        $bottles = random_int($min, $max);
        $seen = [];

        $pdo->beginTransaction();
        try {
            for ($n = 1; $n <= $bottles; $n++) {
                do {
                    $barcode = randomBarcode13();
                } while (isset($seen[$barcode]));
                $seen[$barcode] = true;

                $stmt->execute([
                    'transactionid'     => $transactionId,
                    'user'              => 'unknown',
                    'print_barcode'     => $printBarcode,
                    'dateline'          => (string) $dateline,
                    'statecode'         => '0',
                    'barcode'           => $barcode,
                    'bors'              => '',
                    'weight'            => '15',
                    'diam'              => '0',
                    'metal'             => '0',
                    'recognitionstatus' => '1',
                    'rebateordonate'    => '0',
                    'payplatform'       => 'RVM',
                    'bottlevalue'       => '5',
                    'charityid'         => 0,
                    'charityname'       => '',
                    'octreceipt'        => '0',
                    'transactiondone'   => '2',
                    'uploaddone'        => '1',
                ]);
            }
            $pdo->commit();
            $printBarcodes[] = $printBarcode;
        } catch (Throwable $e) {
            $pdo->rollBack();
            throw $e;
        }
    }
    return $printBarcodes;
}

function randomBarcode13(): string
{
    $s = (string) random_int(1, 9);
    for ($i = 0; $i < 12; $i++) {
        $s .= (string) random_int(0, 9);
    }
    return $s;
}

// ---------- WEBHOOK FIRER ----------

function fireAllWebhooks(array $cfg, array $printBarcodes): array
{
    $fired = [];
    $pauseUs = (int) $cfg['webhook_pause_ms'] * 1000;

    foreach ($printBarcodes as $pb) {
        $fired[] = fireWebhook($cfg, 'transaction', '/webhooks/transaction', [
            'print_barcode' => $pb,
            'datetime'      => date('Y-m-d H:i:s'),
        ]);
        usleep($pauseUs);
    }

    for ($i = 0; $i < (int) $cfg['bags']; $i++) {
        $fired[] = fireWebhook($cfg, 'bag', '/webhooks/bag', [
            'id'       => (int) (microtime(true) * 1000) + $i,
            'mid'      => $cfg['machine_id'],
            'bin_type' => $i % 2 === 0 ? 'PET' : 'CAN',
            'barcode'  => 'SEAL-' . randomBarcode13(),
            'dateline' => time(),
        ]);
        usleep($pauseUs);
    }

    for ($i = 0; $i < (int) $cfg['errors']; $i++) {
        $fired[] = fireWebhook($cfg, 'error', '/webhooks/error', [
            'mid'            => $cfg['machine_id'],
            'storage'        => 20,
            'storageplastic' => 20,
            'storagecan'     => 30,
            'errorcode'      => '0',
        ]);
        usleep($pauseUs);
    }

    for ($i = 0; $i < (int) $cfg['resets']; $i++) {
        $fired[] = fireWebhook($cfg, 'reset', '/webhooks/reset', [
            'mid'      => $cfg['machine_id'],
            'dateline' => time(),
        ]);
        usleep($pauseUs);
    }

    return array_values(array_filter($fired));
}

function fireWebhook(array $cfg, string $eventType, string $path, array $payload): ?array
{
    $resp = httpJson('POST', $cfg['csharp_url'] . $path, $payload);
    $body = json_decode($resp['body'], true);
    $id = is_array($body) && isset($body['id']) ? (int) $body['id'] : null;

    if ($resp['status'] < 200 || $resp['status'] >= 300) {
        fwrite(STDERR, "[fire] {$eventType} {$path} -> HTTP {$resp['status']} : {$resp['body']}\n");
    }
    if ($id === null) {
        fwrite(STDERR, "[fire] {$eventType} {$path} -> no id in response body\n");
        return null;
    }
    return [
        'id'         => $id,
        'event_type' => $eventType,
        'http'       => $resp['status'],
    ];
}

// ---------- VERIFIER ----------

function pollMonitor(array $cfg, array $fired): array
{
    if (empty($fired)) {
        return [];
    }
    $firedIds = array_column($fired, 'id');
    $wantedIds = array_fill_keys($firedIds, true);
    $typeById = [];
    foreach ($fired as $f) {
        $typeById[$f['id']] = $f['event_type'];
    }

    $deadline = microtime(true) + (int) $cfg['poll_timeout_sec'];
    $intervalUs = (int) $cfg['poll_interval_ms'] * 1000;
    $results = [];

    while (microtime(true) < $deadline) {
        $resp = httpJson('GET', $cfg['csharp_url'] . '/api/monitor/events?take=500');
        if ($resp['status'] >= 200 && $resp['status'] < 300) {
            $events = json_decode($resp['body'], true) ?: [];
            if (isset($events['items']) && is_array($events['items'])) {
                $events = $events['items'];
            }
            foreach ($events as $ev) {
                $id = isset($ev['id']) ? (int) $ev['id'] : null;
                if ($id === null || !isset($wantedIds[$id])) {
                    continue;
                }
                $status = $ev['status'] ?? 'unknown';
                if (!in_array($status, ['received'], true)) {
                    $results[$id] = [
                        'id'            => $id,
                        'event_type'    => $typeById[$id] ?? ($ev['eventType'] ?? '?'),
                        'status'        => $status,
                        'response_code' => $ev['responseCode'] ?? null,
                        'response_body' => $ev['responseBody'] ?? null,
                    ];
                    unset($wantedIds[$id]);
                }
            }
        }
        if (empty($wantedIds)) {
            break;
        }
        usleep($intervalUs);
    }

    foreach ($wantedIds as $id => $_) {
        $results[$id] = [
            'id'            => $id,
            'event_type'    => $typeById[$id] ?? '?',
            'status'        => 'timeout',
            'response_code' => null,
            'response_body' => 'no terminal status within ' . $cfg['poll_timeout_sec'] . 's',
        ];
    }

    return $results;
}

// ---------- REPORTER ----------

function printSummary(array $results, array $cfg): bool
{
    $byType = [];
    $failures = [];

    foreach ($results as $r) {
        $t = $r['event_type'];
        $byType[$t] = $byType[$t] ?? [
            'fired' => 0, 'forwarded' => 0, 'forward_failed' => 0,
            'skipped' => 0, 'exception' => 0, 'timeout' => 0, 'other' => 0,
        ];
        $byType[$t]['fired']++;

        $status = $r['status'];
        $code = $r['response_code'];
        $ok2xx = is_int($code) && $code >= 200 && $code < 300;

        if ($status === 'forwarded' && $ok2xx) {
            $byType[$t]['forwarded']++;
        } elseif ($status === 'forward_failed') {
            $byType[$t]['forward_failed']++;
            $failures[] = $r;
        } elseif ($status === 'skipped') {
            $byType[$t]['skipped']++;
            $failures[] = $r;
        } elseif ($status === 'exception') {
            $byType[$t]['exception']++;
            $failures[] = $r;
        } elseif ($status === 'timeout') {
            $byType[$t]['timeout']++;
            $failures[] = $r;
        } else {
            $byType[$t]['other']++;
            $failures[] = $r;
        }
    }

    echo "\n====== TEST BOT SUMMARY (mode={$cfg['mode']}) ======\n";
    printf("%-12s | %5s | %9s | %10s | %7s | %9s | %7s\n",
        'event_type', 'fired', 'forwarded', 'fwd_fail', 'skipped', 'exception', 'timeout');
    echo str_repeat('-', 76) . "\n";
    foreach ($byType as $t => $counts) {
        printf("%-12s | %5d | %9d | %10d | %7d | %9d | %7d\n",
            $t, $counts['fired'], $counts['forwarded'], $counts['forward_failed'],
            $counts['skipped'], $counts['exception'], $counts['timeout']);
    }

    if (!empty($failures)) {
        echo "\nFirst " . min(3, count($failures)) . " failure(s):\n";
        foreach (array_slice($failures, 0, 3) as $f) {
            $body = is_string($f['response_body']) ? substr($f['response_body'], 0, 400) : json_encode($f['response_body']);
            echo "  id={$f['id']} type={$f['event_type']} status={$f['status']} code=" . ($f['response_code'] ?? 'null') . "\n";
            echo "  body: {$body}\n";
        }
    }

    $pass = empty($failures);
    echo "\nRESULT: " . ($pass ? 'PASS' : 'FAIL') . "\n";
    if ($cfg['mode'] === 'local') {
        echo "Mock request log: {$cfg['mock_log_path']}\n";
    }
    return $pass;
}

// ---------- HTTP HELPER ----------

function httpJson(string $method, string $url, ?array $body = null): array
{
    $ch = curl_init();
    curl_setopt_array($ch, [
        CURLOPT_URL            => $url,
        CURLOPT_CUSTOMREQUEST  => $method,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => 30,
        CURLOPT_CONNECTTIMEOUT => 5,
        CURLOPT_HTTPHEADER     => ['Content-Type: application/json', 'Accept: application/json'],
    ]);
    if ($body !== null) {
        curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($body, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE));
    }
    $respBody = curl_exec($ch);
    if ($respBody === false) {
        $err = curl_error($ch);
        curl_close($ch);
        throw new RuntimeException("HTTP {$method} {$url} failed: {$err}");
    }
    $status = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    return ['status' => $status, 'body' => (string) $respBody];
}

// ---------- LOGGING ----------

function info(string $msg): void
{
    fwrite(STDOUT, '[test-bot] ' . $msg . "\n");
}

function banner(array $cfg): void
{
    echo "\n==== Revend test-bot ====\n";
    echo "mode         : {$cfg['mode']}\n";
    echo "csharp_url   : {$cfg['csharp_url']}\n";
    echo "machine_id   : {$cfg['machine_id']}\n";
    if ($cfg['mode'] === 'local') {
        echo "mock_target  : http://{$cfg['mock_host']}:{$cfg['mock_port']}\n";
    } else {
        echo "prod_target  : {$cfg['prod_base_url']} (from appsettings.json)\n";
    }
    echo "plan         : {$cfg['transactions']} trans, {$cfg['bags']} bags, {$cfg['errors']} errors, {$cfg['resets']} resets\n";
    echo "=========================\n\n";
}
