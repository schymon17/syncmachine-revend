#!/usr/bin/env php
<?php
declare(strict_types=1);

/**
 * test-bot.php — prosty bot testowy dla C# Revend.EventService.
 *
 * Wstawia N fake transakcji do MySQL, odpala webhooki na localhost,
 * usługa C# sama forwarduje do Revendu (cokolwiek ma w appsettings.json).
 * Raport = kod HTTP zwrócony przez C# na każdy webhook.
 *
 *   php test-bot.php              # default counts z configu niżej
 *   php test-bot.php 50           # 50 transakcji (reszta bez zmian)
 *   php test-bot.php 50 3 1 0     # 50 trans, 3 bags, 1 error, 0 resets
 */

$cfg = [
    'csharp_url' => 'http://localhost:21011',

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

    'pause_ms' => 50,
];

// CLI override: php test-bot.php [trans] [bags] [errors] [resets]
if ($argc > 1) $cfg['transactions'] = (int) $argv[1];
if ($argc > 2) $cfg['bags']         = (int) $argv[2];
if ($argc > 3) $cfg['errors']       = (int) $argv[3];
if ($argc > 4) $cfg['resets']       = (int) $argv[4];

exit(main($cfg));

function main(array $cfg): int
{
    echo "==== Revend test-bot ====\n";
    echo "target : {$cfg['csharp_url']}\n";
    echo "plan   : {$cfg['transactions']} trans, {$cfg['bags']} bags, {$cfg['errors']} errors, {$cfg['resets']} resets\n";
    echo "=========================\n\n";

    try {
        checkCsharpReachable($cfg['csharp_url']);

        $machineId = ensureMachineIdSet($cfg['csharp_url']);
        info("machine id: {$machineId}");

        $printBarcodes = seedTransactions($cfg);
        info('seeded ' . count($printBarcodes) . ' transakcji do qcs.user_transaction');

        $results = fireAllWebhooks($cfg, $printBarcodes, $machineId);

        return printSummary($results) ? 0 : 1;
    } catch (Throwable $e) {
        fwrite(STDERR, "\n[FATAL] " . $e->getMessage() . "\n");
        return 2;
    }
}

// ---------- SETUP ----------

function checkCsharpReachable(string $csharpUrl): void
{
    $resp = httpJson('GET', $csharpUrl . '/api/monitor/modes');
    if ($resp['status'] < 200 || $resp['status'] >= 300) {
        throw new RuntimeException("C# EventService nie odpowiada pod {$csharpUrl} (HTTP {$resp['status']}). Odpal `dotnet run`.");
    }
}

function ensureMachineIdSet(string $csharpUrl): string
{
    $resp = httpJson('GET', $csharpUrl . '/api/setup/machine');
    $body = json_decode($resp['body'], true) ?: [];
    $current = $body['machineNumber'] ?? '';
    if (is_string($current) && $current !== '') {
        return $current;
    }
    throw new RuntimeException("Numer seryjny nie ustawiony w C# service. Ustaw go najpierw: curl -X POST {$csharpUrl}/api/setup/machine -H 'Content-Type: application/json' -d '{\"machineNumber\":\"TWOJ_MID\"}'");
}

// ---------- SEEDER ----------

function seedTransactions(array $cfg): array
{
    $d = $cfg['db'];
    $dsn = sprintf('mysql:host=%s;port=%d;dbname=%s;charset=utf8mb4', $d['host'], (int) $d['port'], $d['db']);
    $pdo = new PDO($dsn, $d['user'], $d['pass'], [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);

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

function fireAllWebhooks(array $cfg, array $printBarcodes, string $machineId): array
{
    $pauseUs = (int) $cfg['pause_ms'] * 1000;
    $results = [];

    echo "\n[firing " . count($printBarcodes) . " transaction webhooks]\n";
    foreach ($printBarcodes as $i => $pb) {
        $results[] = fire($cfg['csharp_url'], 'transaction', '/webhooks/transaction', [
            'print_barcode' => $pb,
            'datetime'      => date('Y-m-d H:i:s'),
        ], $i + 1);
        usleep($pauseUs);
    }

    if ($cfg['bags'] > 0) echo "\n[firing {$cfg['bags']} bag webhooks]\n";
    for ($i = 0; $i < (int) $cfg['bags']; $i++) {
        $results[] = fire($cfg['csharp_url'], 'bag', '/webhooks/bag', [
            'id'       => (int) (microtime(true) * 1000) + $i,
            'mid'      => $machineId,
            'bin_type' => $i % 2 === 0 ? 'PET' : 'CAN',
            'barcode'  => 'SEAL-' . randomBarcode13(),
            'dateline' => time(),
        ], $i + 1);
        usleep($pauseUs);
    }

    if ($cfg['errors'] > 0) echo "\n[firing {$cfg['errors']} error webhooks]\n";
    for ($i = 0; $i < (int) $cfg['errors']; $i++) {
        $results[] = fire($cfg['csharp_url'], 'error', '/webhooks/error', [
            'mid'            => $machineId,
            'storage'        => 20,
            'storageplastic' => 20,
            'storagecan'     => 30,
            'errorcode'      => '0',
        ], $i + 1);
        usleep($pauseUs);
    }

    if ($cfg['resets'] > 0) echo "\n[firing {$cfg['resets']} reset webhooks]\n";
    for ($i = 0; $i < (int) $cfg['resets']; $i++) {
        $results[] = fire($cfg['csharp_url'], 'reset', '/webhooks/reset', [
            'mid'      => $machineId,
            'dateline' => time(),
        ], $i + 1);
        usleep($pauseUs);
    }

    return $results;
}

function fire(string $csharpUrl, string $eventType, string $path, array $payload, int $idx): array
{
    $resp = httpJson('POST', $csharpUrl . $path, $payload);
    $ok = $resp['status'] >= 200 && $resp['status'] < 300;
    $mark = $ok ? 'OK ' : 'FAIL';
    $extra = '';
    if (!$ok) {
        $snippet = substr($resp['body'], 0, 200);
        $extra = " :: {$snippet}";
    }
    echo "  [{$mark}] {$eventType} #{$idx} -> HTTP {$resp['status']}{$extra}\n";
    return [
        'event_type' => $eventType,
        'idx'        => $idx,
        'status'     => $resp['status'],
        'ok'         => $ok,
        'body'       => $resp['body'],
    ];
}

// ---------- REPORT ----------

function printSummary(array $results): bool
{
    $byType = [];
    foreach ($results as $r) {
        $t = $r['event_type'];
        $byType[$t] = $byType[$t] ?? ['fired' => 0, 'ok' => 0, 'fail' => 0];
        $byType[$t]['fired']++;
        $byType[$t][$r['ok'] ? 'ok' : 'fail']++;
    }

    echo "\n====== SUMMARY ======\n";
    printf("%-12s | %5s | %5s | %5s\n", 'event_type', 'fired', 'ok', 'fail');
    echo str_repeat('-', 40) . "\n";
    $allOk = true;
    foreach ($byType as $t => $c) {
        printf("%-12s | %5d | %5d | %5d\n", $t, $c['fired'], $c['ok'], $c['fail']);
        if ($c['fail'] > 0) $allOk = false;
    }
    echo "\nRESULT: " . ($allOk ? 'PASS' : 'FAIL') . "\n";
    if ($allOk) {
        echo "Sprawdz panel Revend zeby zobaczyc czy transakcje sie pojawily.\n";
    } else {
        echo "Niektore webhooki zwrocily blad - body odpowiedzi masz w logu wyzej.\n";
    }
    return $allOk;
}

// ---------- HTTP + LOG ----------

function httpJson(string $method, string $url, ?array $body = null): array
{
    $ch = curl_init();
    curl_setopt_array($ch, [
        CURLOPT_URL            => $url,
        CURLOPT_CUSTOMREQUEST  => $method,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => 60,
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
        throw new RuntimeException("HTTP {$method} {$url}: {$err}");
    }
    $status = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    return ['status' => $status, 'body' => (string) $respBody];
}

function info(string $msg): void
{
    echo "[*] {$msg}\n";
}
