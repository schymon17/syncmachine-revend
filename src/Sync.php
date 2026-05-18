<?php

class Sync {
    public function __construct(private array $cfg, private Logger $log) {}
    private function atomicWrite(string $path, string $data): void {
        try {
            $tmp = $path.'.tmp';
            file_put_contents($tmp, $data, LOCK_EX);
            @rename($tmp, $path);
        } catch (Throwable $e) {
            @file_put_contents($path, $data, LOCK_EX);
        }
    }

    private function readJsonFile(string $path): array {
        try {
            if (!file_exists($path)) return [];
            $raw = file_get_contents($path);
            if ($raw === false || $raw === '') return [];
            $dec = json_decode($raw, true);
            return is_array($dec) ? $dec : [];
        } catch (Throwable) {
            return [];
        }
    }

    private function queueOffline(string $path, string $endpoint, array $payload, ?string $kind = null, ?string $error = null): void {
        try {
            $entry = [
                'endpoint' => $endpoint,
                'payload' => $payload,
                'queuedAt' => gmdate('c'),
            ];

            if ($kind !== null && $kind !== '') {
                $entry['kind'] = $kind;
            }
            if ($error !== null && $error !== '') {
                $entry['error'] = $error;
            }

            $line = json_encode($entry, JSON_UNESCAPED_UNICODE).PHP_EOL;
            file_put_contents($path, $line, FILE_APPEND | LOCK_EX);
        } catch (Throwable $e) {
            $this->log->log('WARN', 'Failed to append queue file', ['error'=>$e->getMessage()]);
        }
    }

    private function inferEndpointFromPayload(array $payload): string {
        $kind = (string)($payload['kind'] ?? '');
        return match ($kind) {
            'transactions' => '/trans',
            'status' => '/status',
            'heartbeat' => '/heartbeat',
            'sync_bins' => '/bins',
            default => '/sync/changes',
        };
    }

    private function flushQueue(Http $http, string $path): void {
        try {
            if (!file_exists($path)) return;
            $lines = file($path, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) ?: [];
            if (!$lines) return; // fixed: early return instead of dangling semicolon

            $remain = [];
            foreach ($lines as $line) {
                // tolerate broken lines
                $entry = json_decode($line, true);
                if (!is_array($entry)) { $remain[] = $line; continue; }

                $endpoint = '/sync/changes';
                $payload = $entry;
                if (isset($entry['endpoint']) && is_string($entry['endpoint']) && isset($entry['payload']) && is_array($entry['payload'])) {
                    $endpoint = $entry['endpoint'];
                    $payload = $entry['payload'];
                } else {
                    $endpoint = $this->inferEndpointFromPayload($entry);
                }

                try {
                    $res = $http->postJson($endpoint, $payload);
                    $status = (int)($res['status'] ?? 0);
                    if ($status < 200 || $status >= 300) {
                        throw new RuntimeException('Bad status '.$status);
                    }
                    $this->log->log('INFO', 'Flushed queued item', [
                        'endpoint' => $endpoint,
                        'kind' => $payload['kind'] ?? 'unknown',
                    ]);
                } catch (Throwable $e) {
                    $remain[] = $line;
                    $this->log->log('WARN', 'Still offline for queued item', ['error'=>$e->getMessage()]);
                }
            }
            $content = implode(PHP_EOL, $remain).(count($remain)?PHP_EOL:'');
            file_put_contents($path, $content, LOCK_EX);
        } catch (Throwable $e) {
            $this->log->log('WARN', 'Flush queue failed (non-fatal)', ['error'=>$e->getMessage()]);
        }
    }

    private function safeHeartbeat(Http $http, string $queueFile, string $machineId): void {
        try {
            $payload = ['machineId' => $machineId, 'timestamp' => gmdate('c'), 'kind' => 'heartbeat'];
            $res = $http->postJson('/heartbeat', $payload);
            $status = (int)($res['status'] ?? 0);
            if ($status >= 200 && $status < 300) {
                $this->log->log('INFO', 'Heartbeat sent');
            } else {
                throw new RuntimeException('Bad status '.$status);
            }
        } catch (Throwable $e) {
            $payload = ['machineId' => $machineId, 'timestamp' => gmdate('c'), 'kind' => 'heartbeat'];
            $this->queueOffline($queueFile, '/heartbeat', $payload, 'heartbeat', $e->getMessage());
            $this->log->log('WARN', 'Heartbeat queued', ['error'=>$e->getMessage()]);
        }
    }

    private function safeSnapshotWrite(string $path, array $snap): void {
        try {
            $this->atomicWrite($path, json_encode($snap, JSON_PRETTY_PRINT|JSON_UNESCAPED_UNICODE));
        } catch (Throwable $e) {
            $this->log->log('WARN', 'Snapshot write failed (non-fatal)', ['error'=>$e->getMessage()]);
        }
    }

    private function isMissingTableError(Throwable $e): bool {
        if ((string)$e->getCode() === '42S02') {
            return true;
        }

        $msg = strtolower($e->getMessage());
        return str_contains($msg, 'sqlstate[42s02]')
            || (str_contains($msg, '1146') && str_contains($msg, "doesn't exist"));
    }

    public function runOnce(): void {
        $machineId = $this->cfg['machineId'] ?? '';
        if ($machineId === '') {
            $this->log->log('ERROR','Machine ID empty'); return;
        }

        $paths        = $this->cfg['paths'] ?? [];
        $snapshotFile = $paths['snapshot'] ?? __DIR__.'/var/sync/snapshot.json';
        $queueFile    = $paths['queue']    ?? __DIR__.'/var/sync/queue.log';

        @is_dir(dirname($snapshotFile)) || @mkdir(dirname($snapshotFile),0777,true);
        @is_dir(dirname($queueFile))    || @mkdir(dirname($queueFile),0777,true);

        // build once; reuse
        try {
            $pdo = Db::pdo($this->cfg['db']);
        } catch (Throwable $e) {
            $this->log->log('ERROR','DB connect failed', ['error'=>$e->getMessage()]);
            // We can still run API heartbeats/queue flush
            $pdo = null;
        }

        $snap = $this->readJsonFile($snapshotFile);
        $http = new Http(
            $this->cfg['api']['baseUrl'] ?? '',
            $this->cfg['api']['token'] ?? null,
            max(1, (int)($this->cfg['api']['timeoutSeconds'] ?? 30)),
            max(1, (int)($this->cfg['api']['connectTimeoutSeconds'] ?? 10)),
            max(262144, (int)($this->cfg['api']['maxPayloadBytes'] ?? 5242880)),
            max(262144, (int)($this->cfg['api']['maxResponseBytes'] ?? 8388608))
        );

        if (($this->cfg['sync']['enabledTrans'] ?? false) && $pdo) {
            $this->runTransactions($pdo, $http, $snapshotFile, $queueFile, $snap, $machineId);
        } else {
            $this->log->log('INFO','Sync disabled - Transactions are disabled or DB not ready');
        }
        $this->flushQueue($http, $queueFile);

        if ($this->cfg['sync']['enabledEans'] ?? false) {
            $this->runEans($pdo, $http, $snapshotFile, $snap, $machineId);
        } else {
            $this->log->log('INFO','Sync disabled - Eans are disabled');
        }

        if (($this->cfg['sync']['enabledStatus'] ?? false) && $pdo) {
            $this->runStatus($pdo, $http, $snapshotFile, $queueFile, $snap, $machineId);
        } else {
            $this->log->log('INFO','Sync disabled - Status are disabled or DB not ready');
        }

        if (($this->cfg['sync']['enabledCoupons'] ?? false) && $pdo) {
            $this->runCoupons($pdo, $http, $machineId);
        } else {
            $this->log->log('INFO','Sync disabled - Coupons are disabled or DB not ready');
        }

        if (($this->cfg['sync']['enabledAdverts'] ?? false) && $pdo) {
            $this->runAdverts($pdo, $http, $snapshotFile, $snap, $machineId);
        } else {
            $this->log->log('INFO','Sync disabled - Adverts are disabled or DB not ready');
        }

        $this->runBins($pdo, $http, $snapshotFile, $queueFile, $snap, $machineId);

        try {
            $this->safeHeartbeat($http, $queueFile, $machineId);
        } catch (Throwable $e) {
            $this->log->log('WARN','Heartbeat failed at end (non-fatal)', ['error'=>$e->getMessage()]);
        }
    }

    public function resendCoupon(): void
    {
        $ask = static function (string $prompt): string {
            echo $prompt;
            $line = fgets(STDIN);
            return $line === false ? '' : trim($line);
        };

        $machineId = $this->cfg['machineId'] ?? '';
        if ($machineId === '') {
            echo "BLAD: machineId pusty w konfiguracji. Przerwano.\n";
            $this->log->log('ERROR', 'resend-coupon: machineId empty');
            return;
        }

        try {
            $pdo = Db::pdo($this->cfg['db']);
        } catch (Throwable $e) {
            echo "BLAD polaczenia z baza maszyny: " . $e->getMessage() . "\n";
            $this->log->log('ERROR', 'resend-coupon: DB connect failed', ['error' => $e->getMessage()]);
            return;
        }

        $coupon = $ask("Podaj numer kuponu (print_barcode): ");
        if ($coupon === '') {
            echo "Anulowano (pusty numer kuponu). Nic nie wyslano.\n";
            return;
        }

        try {
            $stmt = $pdo->prepare(
                "SELECT * FROM user_transaction WHERE print_barcode = :pb ORDER BY dateline ASC"
            );
            $stmt->execute([':pb' => $coupon]);
            $rows = $stmt->fetchAll(PDO::FETCH_ASSOC) ?: [];
        } catch (Throwable $e) {
            echo "BLAD odczytu z bazy: " . $e->getMessage() . "\n";
            $this->log->log('ERROR', 'resend-coupon: DB read failed', ['coupon' => $coupon, 'error' => $e->getMessage()]);
            return;
        }

        if (!$rows) {
            echo "Nie znaleziono transakcji o numerze kuponu '{$coupon}' w bazie maszyny. Nic nie wyslano.\n";
            $this->log->log('INFO', 'resend-coupon: coupon not found', ['coupon' => $coupon]);
            return;
        }

        $finished = false;
        foreach ($rows as $r) {
            if (in_array((int)($r['transactiondone'] ?? 0), [2, 4], true)) {
                $finished = true;
                break;
            }
        }
        if (!$finished) {
            echo "Transakcja '{$coupon}' istnieje, ale NIE jest zakonczona (brak wiersza transactiondone IN 2,4). Nic nie wyslano.\n";
            $this->log->log('WARN', 'resend-coupon: transaction not finished', ['coupon' => $coupon, 'rows' => count($rows)]);
            return;
        }

        $maxDateline = 0;
        $states = [];
        foreach ($rows as $r) {
            $dl = (int)($r['dateline'] ?? 0);
            if ($dl > $maxDateline) $maxDateline = $dl;
            $states[(string)($r['transactiondone'] ?? '')] = true;
        }
        $lastTime = $maxDateline > 0 ? gmdate('Y-m-d H:i:s', $maxDateline) : null;
        $integration = $this->cfg['integration'] ?? null;
        $baseUrl = rtrim((string)($this->cfg['api']['baseUrl'] ?? ''), '/');

        echo "\n--- Podsumowanie transakcji ---\n";
        echo "Numer kuponu (print_barcode): {$coupon}\n";
        echo "Liczba wierszy (butelek): " . count($rows) . "\n";
        echo "Data (ostatni dateline): " . ($lastTime !== null ? $lastTime . " UTC" : "brak") . "\n";
        echo "Stany transactiondone: " . implode(',', array_keys($states)) . "\n";
        echo "Maszyna (machineId): {$machineId}\n";
        echo "Integracja: " . ($integration !== null && $integration !== '' ? $integration : '(brak)') . "\n";
        echo "Cel wysylki: {$baseUrl}/trans\n";
        echo "-------------------------------\n";

        $answer = strtolower($ask("Wyslac te transakcje do panelu Revend? [t/N]: "));
        if (!in_array($answer, ['t', 'tak', 'y', 'yes'], true)) {
            echo "Anulowano. Nic nie wyslano.\n";
            $this->log->log('INFO', 'resend-coupon: cancelled by user', ['coupon' => $coupon]);
            return;
        }

        $details = [];
        foreach ($rows as $r) {
            $dl = (int)($r['dateline'] ?? 0);
            $r['datetime'] = $dl > 0 ? gmdate('Y-m-d H:i:s', $dl) : null;
            $details[] = $r;
        }

        $payload = [
            'machineId'   => $machineId,
            'timestamp'   => gmdate('c'),
            'kind'        => 'transactions',
            'data'        => [
                'transactions' => [
                    $coupon => [
                        'details'               => $details,
                        'last_transaction_time' => $lastTime,
                    ],
                ],
                'mid' => $machineId,
            ],
            'integration' => $integration,
        ];

        $http = new Http(
            $this->cfg['api']['baseUrl'] ?? '',
            $this->cfg['api']['token'] ?? null,
            max(1, (int)($this->cfg['api']['timeoutSeconds'] ?? 30)),
            max(1, (int)($this->cfg['api']['connectTimeoutSeconds'] ?? 10)),
            max(262144, (int)($this->cfg['api']['maxPayloadBytes'] ?? 5242880)),
            max(262144, (int)($this->cfg['api']['maxResponseBytes'] ?? 8388608))
        );

        try {
            $res = $http->postJson('/trans', $payload);
            $status = (int)($res['status'] ?? 0);
            if ($status >= 200 && $status < 300) {
                echo "OK - transakcja '{$coupon}' wyslana do panelu Revend (HTTP {$status}).\n";
                $this->log->log('INFO', 'resend-coupon: sent', [
                    'coupon' => $coupon,
                    'rows'   => count($rows),
                    'http'   => $status,
                ]);
                return;
            }
            throw new RuntimeException('Bad status ' . $status);
        } catch (Throwable $e) {
            echo "BLAD wysylki: " . $e->getMessage() . "\nTransakcja NIE zostala wyslana. Mozesz uruchomic skrypt ponownie.\n";
            $this->log->log('ERROR', 'resend-coupon: send failed', [
                'coupon' => $coupon,
                'error'  => $e->getMessage(),
            ]);
            return;
        }
    }

    /* -------------------- Steps (isolated & non-blocking) -------------------- */

    private function runTransactions(PDO $pdo, Http $http, string $snapshotFile, string $queueFile, array &$snap, string $machineId): bool
    {
        $payload = null;
        try {
            $lastSync = (int)($snap['user_transaction_lastSync'] ?? 0);
            $overlapBuffer = max(0, (int)($this->cfg['sync']['transOverlapBufferSeconds'] ?? 300));
            $queryLastSync = max(0, $lastSync - $overlapBuffer);
            $checkStmt = $pdo->prepare(
                "SELECT 1 FROM user_transaction WHERE dateline > :lastSync AND transactiondone IN (2, 4) LIMIT 1"
            );
            $checkStmt->execute([':lastSync' => $queryLastSync]);
            $hasFinished = (bool)$checkStmt->fetchColumn();

            if (!$hasFinished) {
                $this->log->log('INFO', 'No finished transactions (transactiondone IN 2,4) detected');
                return false;
            }

            $limitIds = max(1, (int)($this->cfg['sync']['transBatch'] ?? 500));
            $maxTransactionsPerPayload = max(1, (int)($this->cfg['sync']['transMaxTransactionsPerPayload'] ?? 120));
            $maxRowsPerPayload = max(1, (int)($this->cfg['sync']['transMaxRowsPerPayload'] ?? 3000));
            $maxRowsPerTransaction = max(1, (int)($this->cfg['sync']['transMaxRowsPerTransaction'] ?? 1500));

            $idsSql =
                "SELECT print_barcode AS transactionId, MAX(dateline) AS max_dateline FROM user_transaction WHERE dateline > :lastSync AND transactiondone IN (2, 4) AND print_barcode IS NOT NULL AND print_barcode <> '' GROUP BY print_barcode ORDER BY max_dateline ASC" . ($limitIds > 0 ? " LIMIT $limitIds" : "");

            $idsStmt = $pdo->prepare($idsSql);
            $idsStmt->execute([':lastSync' => $queryLastSync]);
            $idRows = $idsStmt->fetchAll(PDO::FETCH_ASSOC) ?: [];

            if (!$idRows) {
                $this->log->log('INFO', 'No finished transaction IDs to send');
                return false;
            }

            $transactionIds = [];
            foreach ($idRows as $row) {
                $tid = (string)($row['transactionId'] ?? '');
                if ($tid === '') continue;
                $transactionIds[] = $tid;
            }

            if (!$transactionIds) {
                $this->log->log('INFO', 'No valid transaction IDs after filtering');
                return false;
            }

            $placeholders = implode(',', array_fill(0, count($transactionIds), '?'));
            $rowsStmt = $pdo->prepare("SELECT * FROM user_transaction WHERE print_barcode IN ($placeholders) ORDER BY print_barcode ASC, dateline ASC");
            $rowsStmt->execute($transactionIds);
            $data = [];
            $rowsCount = 0;
            $maxSentDateline = $lastSync;

            while ($r = $rowsStmt->fetch(PDO::FETCH_ASSOC)) {
                $transactionId = $r['print_barcode'] ?? null;
                if (!$transactionId) continue;

                if (!isset($data[$transactionId])) {
                    if (count($data) >= $maxTransactionsPerPayload || $rowsCount >= $maxRowsPerPayload) {
                        break;
                    }
                    $data[$transactionId] = ['details' => [], 'last_transaction_time' => null, '_last_transaction_ts' => 0];
                }

                $dateline = (int)($r['dateline'] ?? 0);
                $formattedTime = $dateline > 0 ? gmdate('Y-m-d H:i:s', $dateline) : null;

                if (count($data[$transactionId]['details']) >= $maxRowsPerTransaction) {
                    throw new RuntimeException(
                        'Transaction '.$transactionId.' exceeds rows limit '.$maxRowsPerTransaction.' (memory guard)'
                    );
                }

                $r['datetime'] = $formattedTime;
                $data[$transactionId]['details'][] = $r;
                $rowsCount++;

                if ($dateline > (int)$data[$transactionId]['_last_transaction_ts']) {
                    $data[$transactionId]['_last_transaction_ts'] = $dateline;
                    $data[$transactionId]['last_transaction_time'] = $formattedTime;
                }

                if ($dateline > $maxSentDateline) $maxSentDateline = $dateline;
            }
            $rowsStmt->closeCursor();

            foreach ($data as &$transaction) {
                unset($transaction['_last_transaction_ts']);
            }
            unset($transaction);

            if (!$data) {
                $this->log->log('INFO', 'No new records to send (post-filter)');
                return false;
            }

            $payload = [
                'machineId' => $machineId,
                'timestamp' => gmdate('c'),
                'kind' => 'transactions',
                'data' => ['transactions' => $data, 'mid' => $machineId],
                'integration' => $this->cfg['integration'] ?? null,
            ];

            $res = $http->postJson('/trans', $payload);
            $status = (int)($res['status'] ?? 0);

            if ($status >= 200 && $status < 300) {
                $snap['user_transaction_lastSync'] = $maxSentDateline;
                $this->safeSnapshotWrite($snapshotFile, $snap);

                $this->log->log('INFO', 'Sent transactions', [
                    'transactions' => count($data),
                    'rows' => $rowsCount,
                    'from' => $lastSync,
                    'to' => $maxSentDateline,
                ]);

                return true;
            }

            throw new RuntimeException('Bad status ' . $status);
        } catch (Throwable $e) {
            if (is_array($payload)) {
                $this->queueOffline($queueFile, '/trans', $payload, 'transactions', $e->getMessage());
                $this->log->log('WARN', 'Queued transactions (offline)', ['error' => $e->getMessage()]);
            } else {
                $this->log->log('ERROR', 'Transactions step failed before payload build', ['error' => $e->getMessage()]);
            }
            return false;
        }
    }

    private function runEans(?PDO $pdo, Http $http, string $snapshotFile, array &$snap, string $machineId): void {
        try {
            $res = $http->postJson('/eans', [
                'machineId'=>$machineId,
                'timestamp'=>gmdate('c'),
                'integration'=>$this->cfg['integration'] ?? null,
            ]);

            $status = (int)($res['status'] ?? 0);
            if ($status < 200 || $status >= 300) {
                throw new RuntimeException('Bad status '.$status);
            }

            $response = $res['body'] ?? null;
            if ($response === null) {
                throw new RuntimeException('Fetch EANs: empty body');
            }

            $items = is_array($response) && array_key_exists('data', $response) ? ($response['data'] ?? []) : $response;
            if (!is_array($items)) {
                throw new RuntimeException('Unexpected EANs format');
            }

            $attrs = $items['attributes'] ?? null;
            if (!is_array($attrs)) {
                $this->log->log('INFO', 'EANs: no attributes to import');
                return;
            }

            $newHash = hash('sha256', json_encode($attrs));
            if (($snap['eans_hash'] ?? null) === $newHash) {
                $this->log->log('INFO', 'EANs unchanged - nothing to import');
                return;
            }

            if (!$pdo) {
                $this->log->log('WARN','EANs fetched but DB not available; skipping import');
                return;
            }

            // Try to make columns safer; ignore errors to avoid blocking
            try {
                $pdo->exec("ALTER TABLE barcode MODIFY bottleinfo VARCHAR(255)");
            } catch (Throwable) {}
            try {
                $pdo->exec("ALTER TABLE barcode MODIFY brand VARCHAR(255)");
            } catch (Throwable) {}

            // Truncate table; if fails, try delete (both non-fatal)
            try {
                $pdo->exec('TRUNCATE TABLE barcode');
            } catch (Throwable) {
                try { $pdo->exec('DELETE FROM barcode'); } catch (Throwable) {}
            }

            $pdo->beginTransaction();
            $sql = 'INSERT INTO barcode (barcode, brand, bottleinfo, value, maxsdiam, minsdiam, maxbdiam, minbdiam, material_type, metal, capacity, weight, version)
                    VALUES (:barcode, :brand, :bottleinfo, :value, :maxsdiam, :minsdiam, :maxbdiam, :minbdiam, :material_type, :metal, :capacity, :weight, :version)';
            $stmt = $pdo->prepare($sql);

            $inserted = 0;
            foreach ($attrs as $it) {
                if (!is_array($it)) continue;
                $barcode = $it['barcode'] ?? null;
                if (!$barcode) continue;

                $bottleinfo = $it['bottleinfo'] ?? null;
                if (is_array($bottleinfo) || is_object($bottleinfo)) {
                    $bottleinfo = json_encode($bottleinfo, JSON_UNESCAPED_UNICODE);
                }

                $stmt->execute([
                    ':barcode'       => (string)$barcode,
                    ':brand'         => $it['brand']        ?? null,
                    ':bottleinfo'    => $bottleinfo,
                    ':value'         => $it['value']        ?? null,
                    ':maxsdiam'      => $it['maxsdiam']     ?? null,
                    ':minsdiam'      => $it['minsdiam']     ?? null,
                    ':maxbdiam'      => $it['maxbdiam']     ?? null,
                    ':minbdiam'      => $it['minbdiam']     ?? null,
                    ':material_type' => $it['material_type'] ?? null,
                    ':metal'         => isset($it['metal']) ? (int)$it['metal'] : null,
                    ':capacity'      => $it['capacity']     ?? null,
                    ':weight'        => $it['weight']       ?? null,
                    ':version'       => $it['version']      ?? null,
                ]);
                $inserted++;
            }

            if ($pdo->inTransaction()) $pdo->commit();

            $snap['eans_hash']  = $newHash;
            $snap['eans_count'] = $inserted;
            $this->safeSnapshotWrite($snapshotFile, $snap);
            $this->log->log('INFO', 'EANs imported', ['count'=>$inserted]);

        } catch (Throwable $e) {
            // non-blocking
            $this->log->log('ERROR', 'EANs step failed (non-fatal)', ['error'=>$e->getMessage()]);
            if ($pdo && $pdo->inTransaction()) { try { $pdo->rollBack(); } catch (Throwable) {} }
        }
    }

    private function runStatus(PDO $pdo, Http $http, string $snapshotFile, string $queueFile, array &$snap, string $machineId): void {
        $now = gmdate('c');
        try {
            $stmt = $pdo->query("SELECT * FROM command ORDER BY id DESC LIMIT 1");
            $statusRow = $stmt?->fetch(PDO::FETCH_ASSOC) ?: [];
        } catch (Throwable $e) {
            $payload = [
                'machineId'  => $machineId,
                'timestamp'  => $now,
                'kind'       => 'status',
                'data'       => ['command' => null],
            ];
            $this->queueOffline($queueFile, '/status', $payload, 'status', $e->getMessage());
            $this->log->log('WARN', 'Queued status (DB read failed)', ['error' => $e->getMessage()]);

            try { $this->flushQueue($http, $queueFile); } catch (Throwable) {}
            return;
        }

        $payload = [
            'machineId' => $machineId,
            'timestamp' => $now,
            'kind'      => 'status',
            'data'      => ['command' => $statusRow],
        ];

        if (!empty($this->cfg['address']))                  $payload['address']                 = $this->cfg['address'];
        if (!empty($this->cfg['notification_emails']))      $payload['notification_emails']     = $this->cfg['notification_emails'];
        if (!empty($this->cfg['notification_emails_bcc']))  $payload['notification_emails_bcc'] = $this->cfg['notification_emails_bcc'];

        try {
            $res = $http->postJson('/status', $payload);

            $status = (int)($res['status'] ?? 0);
            if ($status >= 200 && $status < 300) {
                $this->log->log('INFO', 'Pushed status', [
                    'command_id' => $statusRow['id'] ?? null,
                    'http'       => $status,
                ]);
                $snap['status_last_push_at'] = $now;
                $this->safeSnapshotWrite($snapshotFile, $snap);
                try { $this->flushQueue($http, $queueFile); } catch (Throwable) {}
                return;
            }
            $msg = $res['message'] ?? ($res['body'] ?? null);
            throw new RuntimeException('Bad status '.$status.($msg ? ' - '.substr((string)$msg, 0, 200) : ''));
        } catch (Throwable $e) {
            $queuedPayload = [
                'machineId'  => $machineId,
                'timestamp'  => $now,
                'kind'       => 'status',
                'data'       => ['command' => $statusRow],
            ];
            $this->queueOffline($queueFile, '/status', $queuedPayload, 'status', $e->getMessage());

            $this->log->log('WARN', 'Queued status (offline)', [
                'error'      => $e->getMessage(),
                'command_id' => $statusRow['id'] ?? null,
            ]);
        }
        try { $this->flushQueue($http, $queueFile); } catch (Throwable) {}
    }

    private function runBins(?PDO $pdo, Http $http, string $snapshotFile, string $queueFile, array &$snap, string $machineId): void {
        if (!$pdo) {
            $this->log->log('INFO', 'Sync disabled - Bins are disabled or DB not ready');
            return;
        }

        $payload = null;
        try {
            $lastId = (int)($snap['empty_records_last_id'] ?? 0);
            $batchSize = max(1, (int)($this->cfg['sync']['binsBatch'] ?? 1000));
            $maxBatchesPerRun = max(1, (int)($this->cfg['sync']['binsMaxBatchesPerRun'] ?? 2));
            $totalSent = 0;

            for ($batch = 1; $batch <= $maxBatchesPerRun; $batch++) {
                $stmt = $pdo->prepare(
                    "SELECT id, mid, dateline, bin_type, barcode FROM empty_record WHERE id > :lastId ORDER BY id ASC LIMIT :batchSize"
                );
                $stmt->bindValue(':lastId', $lastId, PDO::PARAM_INT);
                $stmt->bindValue(':batchSize', $batchSize, PDO::PARAM_INT);
                $stmt->execute();

                $rows = $stmt->fetchAll(PDO::FETCH_ASSOC) ?: [];
                if (!$rows) {
                    if ($totalSent === 0) {
                        $this->log->log('INFO', 'No new bins rows to sync', ['last_id' => $lastId]);
                    }
                    break;
                }

                $payload = [
                    'machineId' => $machineId,
                    'integration' => $this->cfg['integration'] ?? null,
                    'timestamp' => gmdate('c'),
                    'kind' => 'sync_bins',
                    'data' => [
                        'empty_records' => $rows,
                    ],
                ];

                $res = $http->postJson('/bins', $payload);
                $status = (int)($res['status'] ?? 0);
                if ($status < 200 || $status >= 300) {
                    throw new RuntimeException('Bad status bin '.$status);
                }

                $batchMaxId = $lastId;
                foreach ($rows as $row) {
                    $rid = (int)($row['id'] ?? 0);
                    if ($rid > $batchMaxId) $batchMaxId = $rid;
                }

                $lastId = $batchMaxId;
                $snap['empty_records_last_id'] = $lastId;
                $this->safeSnapshotWrite($snapshotFile, $snap);

                $sentNow = count($rows);
                $totalSent += $sentNow;
                $this->log->log('INFO', 'Pushed bins sync batch', [
                    'batch' => $batch,
                    'count' => $sentNow,
                    'last_id' => $lastId,
                ]);

                if ($sentNow < $batchSize) {
                    break;
                }
            }

            if ($totalSent > 0) {
                $this->log->log('INFO', 'Pushed bins sync total', [
                    'count' => $totalSent,
                    'last_id' => $lastId,
                ]);
            }
        } catch (Throwable $e) {
            if ($this->isMissingTableError($e)) {
                $this->log->log('WARN', 'Skipping bins sync - source table missing', [
                    'table' => 'empty_record',
                    'error' => $e->getMessage(),
                ]);
                return;
            }

            $fallbackPayload = [
                'machineId' => $machineId,
                'integration' => $this->cfg['integration'] ?? null,
                'timestamp' => gmdate('c'),
                'kind' => 'sync_bins',
                'data' => ['empty_records' => []],
            ];
            $this->queueOffline($queueFile, '/bins', is_array($payload) ? $payload : $fallbackPayload, 'sync_bins', $e->getMessage());
            $this->log->log('WARN', 'Queued bins sync (offline)', ['error' => $e->getMessage()]);
        }

        // try to flush after attempts
        try { $this->flushQueue($http, $queueFile); } catch (Throwable) {}
    }

    private function runCoupons(PDO $pdo, Http $http, string $machineId): void {
        try {
            $thresholdMin = 25;
            $targetMax = 50;

            $existingCount = (int)($pdo->query("SELECT COUNT(*) FROM printer_barcode")->fetchColumn() ?? 0);
            if ($existingCount >= $thresholdMin) {
                $this->log->log('INFO', sprintf('Printer queue OK (>= %d) — no fetch.', $thresholdMin));
                return;
            }

            $need = max(0, $targetMax - $existingCount);
            if ($need === 0) {
                $this->log->log('INFO', 'Nothing to add (already at target).');
                return;
            }

            $res = $http->postJson('/coupons', [
                'machineId'   => $machineId,
                'timestamp'   => gmdate('c'),
                'integration' => $this->cfg['integration'] ?? null,
            ]);

            $status = (int)($res['status'] ?? 0);
            if ($status < 200 || $status >= 300) {
                throw new RuntimeException('Bad status '.$status);
            }

            $body = $res['body'] ?? null;
            if ($body === null) throw new RuntimeException('Empty coupons body');

            $coupons = is_array($body) && array_key_exists('data', $body) ? ($body['data'] ?? []) : $body;
            $attrs   = is_array($coupons) && array_key_exists('attributes', $coupons) ? ($coupons['attributes'] ?? []) : $coupons;

            if (!is_array($attrs) || !$attrs) {
                $this->log->log('INFO', 'No coupons returned by API.');
                return;
            }

            $normalized = [];
            foreach ($attrs as $c) {
                $bc = is_array($c) ? (string)($c['barcode'] ?? '') : (string)$c;
                if ($bc !== '') $normalized[] = $bc;
            }
            $normalized = array_values(array_unique($normalized));

            if (!$normalized) {
                $this->log->log('INFO', 'No coupons to queue after normalization.');
                return;
            }

            $placeholders = implode(',', array_fill(0, count($normalized), '?'));
            $checkStmt = $pdo->prepare("SELECT barcode FROM printer_barcode WHERE barcode IN ($placeholders)");
            $checkStmt->execute($normalized);
            $existingSet = $checkStmt->fetchAll(PDO::FETCH_COLUMN) ?: [];
            $existingSet = array_flip($existingSet); // for O(1) lookups

            $toInsert = [];
            foreach ($normalized as $bc) {
                if (!isset($existingSet[$bc])) {
                    $toInsert[] = $bc;
                    if (count($toInsert) >= $need) break; // only top up to target max
                }
            }

            if (!$toInsert) {
                $this->log->log('INFO', 'No new coupons to queue (duplicates or already full to target).');
                return;
            }

            $pdo->beginTransaction();

            $ins = $pdo->prepare("INSERT IGNORE INTO printer_barcode (barcode) VALUES (:barcode)");
            $added = 0;
            foreach ($toInsert as $bc) {
                $ins->execute([':barcode' => $bc]);
                $added += (int)$ins->rowCount();
                if ($existingCount + $added >= $targetMax) break;
            }

            if ($pdo->inTransaction()) $pdo->commit();

            $this->log->log('INFO', 'Queued barcodes from coupons API', [
                'requested' => count($toInsert),
                'added' => $added,
                'total' => $existingCount + $added,
                'threshold' => $thresholdMin,
                'target' => $targetMax,
                'endpoint' => 'coupons',
            ]);
        } catch (Throwable $e) {
            if ($pdo->inTransaction()) { try { $pdo->rollBack(); } catch (Throwable) {} }
            $this->log->log('ERROR', 'Coupons queue update failed (non-fatal)', ['error' => $e->getMessage()]);
        }
    }

    private function runAdverts(PDO $pdo, Http $http, string $snapshotFile, array &$snap, string $machineId): void
    {
        try {
            $res = $http->postJson('/adverts', [
                'machineId' => $machineId,
                'timestamp' => gmdate('c'),
                'integration' => $this->cfg['integration'] ?? null,
            ]);

            $status = (int)($res['status'] ?? 0);
            if ($status < 200 || $status >= 300) {
                throw new RuntimeException('Bad status '.$status);
            }

            $body = $res['body'] ?? null;
            if ($body === null) {
                throw new RuntimeException('Empty adverts body');
            }

            if (isset($body['adverts'])) {
                $adverts = $body['adverts'];
            } elseif (isset($body['data']['adverts'])) {
                $adverts = $body['data']['adverts'];
            } else {
                $this->log->log('INFO', 'No adverts section in API response');
                return;
            }

            if (!is_array($adverts) || !$adverts) {
                $this->log->log('INFO', 'Adverts list is empty');
                return;
            }

            $newHash = hash('sha256', json_encode($adverts));
            $oldHash = $snap['adverts_hash'] ?? null;

            if ($newHash === $oldHash) {
                $this->log->log('INFO', 'Adverts unchanged - skipping download and DB update');
                return;
            }

            $baseDir = (string)($this->cfg['paths']['advertsDir'] ?? 'C:\\phpStudy\\PHPTutorial\\WWW\\downadpic\\img');
            if (!is_dir($baseDir)) {
                @mkdir($baseDir, 0777, true);
            }

            $savedPaths = [
                'p1' => null,
                'p2' => null,
                'p3' => null,
                'p4' => null,
                'p5' => null,
            ];

            foreach ($adverts as $slotKey => $ad) {
                if (!is_array($ad)) {
                    continue;
                }

                $slot = $ad['slot'] ?? $slotKey;

                if (!in_array($slot, ['p1', 'p2', 'p3', 'p4', 'p5'], true)) {
                    continue;
                }

                if (!empty($ad['placeholder'])) {
                    $this->log->log('INFO', sprintf('Advert %s is placeholder, skipping download', $slot));
                    $savedPaths[$slot] = null;
                    continue;
                }

                $url = $ad['url'] ?? null;
                if (!$url) {
                    $this->log->log('WARN', sprintf('Advert %s has no URL, skipping', $slot));
                    continue;
                }

                $nameFromApi = $ad['name'] ?? null;
                if ($nameFromApi) {
                    $fileName = $slot . '_' . $this->sanitizeFileName((string)$nameFromApi);
                } else {
                    $basename = basename(parse_url($url, PHP_URL_PATH) ?: '');
                    if ($basename === '' || $basename === '/') {
                        $basename = $slot . '.bin';
                    }
                    $fileName = $slot . '_' . $this->sanitizeFileName($basename);
                }

                $targetPath   = rtrim($baseDir, '\\/') . DIRECTORY_SEPARATOR . $fileName;
                $relativePath = 'img/' . $fileName;

                try {
                    $data = $this->downloadBinary($url);

                    $this->atomicWrite($targetPath, $data);
                    $this->log->log('INFO', sprintf('Downloaded advert %s to %s', $slot, $targetPath));
                    $savedPaths[$slot] = $relativePath;
                } catch (Throwable $e) {
                    $this->log->log('WARN', sprintf('Failed to download advert %s', $slot), [
                        'error' => $e->getMessage(),
                        'url' => $url,
                    ]);
                    continue;
                }
            }

            $pDown0 = $savedPaths['p1'] ?? null;
            $pDown1 = $savedPaths['p2'] ?? null;
            $pDown2 = $savedPaths['p3'] ?? null;
            $pDown3 = $savedPaths['p4'] ?? null;
            $pDown4 = $savedPaths['p5'] ?? null;

            $sql = "UPDATE machineinformation
                SET p_down0 = ?,
                    p_down1 = ?,
                    p_down2 = ?,
                    p_down3 = ?,
                    p_down4 = ?
                WHERE mid = ?";

            $stmt = $pdo->prepare($sql);
            $stmt->execute([
                $pDown0,
                $pDown1,
                $pDown2,
                $pDown3,
                $pDown4,
                $machineId,
            ]);

            $affected = $stmt->rowCount();

            if ($affected === 0) {
                $this->log->log('WARN', 'No machineinformation row matched mid, applying fallback update without WHERE', [
                    'mid' => $machineId,
                ]);

                $fallbackSql = "UPDATE machineinformation
                            SET p_down0 = ?,
                                p_down1 = ?,
                                p_down2 = ?,
                                p_down3 = ?,
                                p_down4 = ?
                            LIMIT 1";

                $stmt2 = $pdo->prepare($fallbackSql);
                $stmt2->execute([
                    $pDown0,
                    $pDown1,
                    $pDown2,
                    $pDown3,
                    $pDown4,
                ]);

                $affectedFallback = $stmt2->rowCount();

                $this->log->log('INFO', 'Fallback machineinformation update', [
                    'affected' => $affectedFallback,
                ]);
            } else {
                $this->log->log('INFO', 'machineinformation update by mid', [
                    'mid'      => $machineId,
                    'affected' => $affected,
                ]);
            }

            $snap['adverts_hash'] = $newHash;
            $this->safeSnapshotWrite($snapshotFile, $snap);

            $this->log->log('INFO', 'Updated machineinformation adverts paths', [
                'mid' => $machineId,
                'p_down0' => $pDown0,
                'p_down1' => $pDown1,
                'p_down2' => $pDown2,
                'p_down3' => $pDown3,
                'p_down4' => $pDown4,
            ]);
        } catch (Throwable $e) {
            $this->log->log('ERROR', 'Adverts step failed (non-fatal)', [
                'error' => $e->getMessage(),
            ]);
        }
    }

    private function downloadBinary(string $url): string
    {
        $ch = curl_init();

        if ($ch === false) {
            throw new RuntimeException('curl_init failed');
        }

        curl_setopt_array($ch, [
            CURLOPT_URL => $url,
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_FOLLOWLOCATION => true,
            CURLOPT_MAXREDIRS => 5,
            CURLOPT_CONNECTTIMEOUT => 10,
            CURLOPT_TIMEOUT => 30,
            CURLOPT_SSL_VERIFYPEER => false,
            CURLOPT_SSL_VERIFYHOST => false,
        ]);

        $data = curl_exec($ch);

        if ($data === false) {
            $err = curl_error($ch);
            $code = curl_errno($ch);
            curl_close($ch);
            throw new RuntimeException('cURL download failed: '.$err.' (code '.$code.')');
        }

        $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);

        if ($httpCode < 200 || $httpCode >= 300) {
            throw new RuntimeException('HTTP status '.$httpCode.' while downloading '.$url);
        }

        return $data;
    }

    private function sanitizeFileName(string $name): string
    {
        $clean = preg_replace('/[^A-Za-z0-9._-]/', '_', $name) ?? '';
        $clean = trim($clean, '._-');
        return $clean !== '' ? $clean : 'file.bin';
    }
}
