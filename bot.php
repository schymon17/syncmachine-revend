<?php

$cfg = [
    'host' => '127.0.0.1',
    'port' => 3306,
    'db'   => 'qcs',
    'user' => 'root',
    'pass' => 'chushengfeng123',
    'table'=> 'user_transaction',
    'interval_seconds' => 75,
    'max_runs' => 15,
    'bottles_min' => 1,
    'bottles_max' => 7,
];
function nowUnix(): int { return time(); }
function randomDigits(int $len): string {
    $s=''; for($i=0;$i<$len;$i++) $s .= (string)random_int(0,9);
    return $s;
}
function randomBarcode13(): string {
    return (string)random_int(1,9) . randomDigits(12);
}
function buildTransactionId(int $i): string {
    return 'm001' . nowUnix() . str_pad((string)$i, 2, '0', STR_PAD_LEFT);
}

$dsn = sprintf('mysql:host=%s;port=%d;dbname=%s;charset=utf8mb4', $cfg['host'], $cfg['port'], $cfg['db']);
$pdo = new PDO($dsn, $cfg['user'], $cfg['pass'], [
    PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
]);

$sql = "INSERT INTO `{$cfg['table']}` (
  `transactionid`,`user`,`print_barcode`,`dateline`,`statecode`,`barcode`,`bors`,`weight`,`diam`,`metal`,
  `recognitionstatus`,`rebateordonate`,`payplatform`,`bottlevalue`,`charityid`,`charityname`,`octreceipt`,`transactiondone`,`uploaddone`
) VALUES (
  :transactionid,:user,:print_barcode,:dateline,:statecode,:barcode,:bors,:weight,:diam,:metal,
  :recognitionstatus,:rebateordonate,:payplatform,:bottlevalue,:charityid,:charityname,:octreceipt,:transactiondone,:uploaddone
)";
$stmt = $pdo->prepare($sql);

for ($run=1; $run <= $cfg['max_runs']; $run++) {
    $dateline = nowUnix();
    $printBarcode = randomBarcode13();
    $transactionId = buildTransactionId($run);

    $bottles = random_int((int)$cfg['bottles_min'], (int)$cfg['bottles_max']);

    // żeby nie wylosowało dwa razy tego samego barcode w jednej transakcji
    $used = [];

    $pdo->beginTransaction();
    try {
        for ($n=1; $n <= $bottles; $n++) {
            do { $barcode = randomBarcode13(); } while (isset($used[$barcode]));
            $used[$barcode] = true;

            $payload = [
                'transactionid'     => $transactionId,
                'user'              => 'unknown',
                'print_barcode'     => $printBarcode,
                'dateline'          => (string)$dateline,
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
            ];

            $stmt->execute($payload);
        }

        $pdo->commit();
        echo "[{$run}/{$cfg['max_runs']}] print_barcode={$printBarcode} bottles={$bottles} dateline={$dateline}".PHP_EOL;

    } catch (Throwable $e) {
        $pdo->rollBack();
        fwrite(STDERR, "[{$run}] failed: ".$e->getMessage().PHP_EOL);
    }

    if ($run < $cfg['max_runs']) sleep((int)$cfg['interval_seconds']);
}

echo "Done.".PHP_EOL;