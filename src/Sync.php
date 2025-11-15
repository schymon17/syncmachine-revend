<?php

class Sync {
    public function __construct(private array $cfg, private Logger $log) {}

    /* -------------------- Small safe helpers -------------------- */

    private function atomicWrite(string $path, string $data): void {
        try {
            $tmp = $path.'.tmp';
            file_put_contents($tmp, $data, LOCK_EX);
            @rename($tmp, $path);
        } catch (Throwable $e) {
            // as a fallback, try direct write (still non-fatal)
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

    private function queueOffline(string $path, array $payload): void {
        try {
            $line = json_encode($payload, JSON_UNESCAPED_UNICODE).PHP_EOL;
            file_put_contents($path, $line, FILE_APPEND | LOCK_EX);
        } catch (Throwable $e) {
            // best effort, never block
            $this->log->log('WARN', 'Failed to append queue file', ['error'=>$e->getMessage()]);
        }
    }

    private function flushQueue(Http $http, string $path): void {
        try {
            if (!file_exists($path)) return;
            $lines = file($path, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) ?: [];
            if (!$lines) return; // fixed: early return instead of dangling semicolon

            $remain = [];
            foreach ($lines as $line) {
                // tolerate broken lines
                $payload = json_decode($line, true);
                if (!is_array($payload)) { $remain[] = $line; continue; }

                try {
                    $res = $http->postJson('/sync/changes', $payload);
                    $status = (int)($res['status'] ?? 0);
                    if ($status < 200 || $status >= 300) {
                        throw new RuntimeException('Bad status '.$status);
                    }
                    $this->log->log('INFO', 'Flushed queued item', ['table'=>$payload['table']??'unknown']);
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
            $payload = ['machineId'=>$machineId,'timestamp'=>gmdate('c'),'kind'=>'heartbeat'];
            $res = $http->postJson('/heartbeat', $payload);
            $status = (int)($res['status'] ?? 0);
            if ($status >= 200 && $status < 300) {
                $this->log->log('INFO', 'Heartbeat sent');
            } else {
                throw new RuntimeException('Bad status '.$status);
            }
        } catch (Throwable $e) {
            $this->queueOffline($queueFile, [
                'machineId'=>$machineId,'timestamp'=>gmdate('c'),'kind'=>'heartbeat'
            ]);
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

    /* -------------------- Orchestration -------------------- */

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
        $http = new Http($this->cfg['api']['baseUrl'] ?? '', $this->cfg['api']['token'] ?? null);

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

        if ($this->cfg['sync']['enabledAdverts'] ?? false) {
            try {
                // no-op now
            } catch (Throwable $e) {
                $this->log->log('WARN','Adverts step failed (non-fatal)', ['error'=>$e->getMessage()]);
            }
        } else {
            $this->log->log('INFO','Sync disabled - Adverts are disabled');
        }
    }

    /* -------------------- Steps (isolated & non-blocking) -------------------- */

    private function runTransactions(PDO $pdo, Http $http, string $snapshotFile, string $queueFile, array &$snap, string $machineId): bool {
        try {
            $lastSync = (int)($snap['user_transaction_lastSync'] ?? 0);

            $latestDateline = (int)($pdo
                ->query("SELECT COALESCE(MAX(dateline),0) AS last_update FROM user_transaction")
                ?->fetchColumn() ?? 0);

            if ($latestDateline <= $lastSync) {
                $this->log->log('INFO', 'No new changes detected');
                return false;
            }

            $limit = max(0, (int)($this->cfg['sync']['transBatch'] ?? 5000));
            $sql   = "SELECT * FROM user_transaction WHERE dateline > :lastSync ORDER BY dateline ASC".($limit>0?" LIMIT $limit":"");
            $stmt  = $pdo->prepare($sql);
            $stmt->execute([':lastSync'=>$lastSync]);
            $rows  = $stmt->fetchAll(PDO::FETCH_ASSOC) ?: [];

            $data = [];
            foreach ($rows as $r) {
                $transactionId = $r['print_barcode'] ?? null;
                if (!$transactionId) continue;

                $dateline = (int)($r['dateline'] ?? 0);
                $formattedTime = $dateline > 0 ? date('Y-m-d H:i:s', $dateline) : null;

                $r['datetime'] = $formattedTime;
                $data[$transactionId]['details'][] = $r;

                $prev = $data[$transactionId]['last_transaction_time'] ?? null;
                if (!$prev || ($formattedTime && strtotime($formattedTime) > strtotime($prev))) {
                    $data[$transactionId]['last_transaction_time'] = $formattedTime;
                }
            }

            if (empty($data)) {
                $this->log->log('INFO', 'No new records to send (post-filter)');
                return false;
            }

            $payload = [
                'machineId'   => $machineId,
                'timestamp'   => gmdate('c'),
                'kind'        => 'transactions',
                'data'        => ['transactions'=>$data,'mid'=>$machineId],
                'integration' => $this->cfg['integration'] ?? null,
            ];

            $res = $http->postJson('/trans', $payload);
            $status = (int)($res['status'] ?? 0);
            if ($status >= 200 && $status < 300) {
                $snap['user_transaction_lastSync'] = $latestDateline;
                $this->safeSnapshotWrite($snapshotFile, $snap);
                $this->log->log('INFO', 'Sent transactions', [
                    'count' => count($data), 'from' => $lastSync, 'to' => $latestDateline
                ]);
                return true;
            }

            throw new RuntimeException('Bad status '.$status);
        } catch (Throwable $e) {
            // never block – queue and move on
            $this->queueOffline($queueFile, [
                'machineId'=>$machineId,
                'timestamp'=>gmdate('c'),
                'kind'=>'transactions',
                'error'=>$e->getMessage()
            ]);
            $this->log->log('WARN', 'Queued transactions (offline)', ['error'=>$e->getMessage()]);
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
        try {
            $stmt = $pdo->query("SELECT * FROM command ORDER BY id DESC LIMIT 1");
            $statusRow = $stmt?->fetch(PDO::FETCH_ASSOC) ?: [];

            $newHash = hash('sha256', json_encode($statusRow));
            $oldHash = $snap['status_hash'] ?? null;

            if ($newHash === $oldHash) {
                $this->log->log('INFO', 'No status change');
                return;
            }

            $payload = [
                'machineId' => $machineId,
                'timestamp' => gmdate('c'),
                'kind'      => 'status',
                'data'      => ['command'=>$statusRow],
            ];

            if (!empty($this->cfg['address']))                  $payload['address']                 = $this->cfg['address'];
            if (!empty($this->cfg['notification_emails']))      $payload['notification_emails']     = $this->cfg['notification_emails'];
            if (!empty($this->cfg['notification_emails_bcc']))  $payload['notification_emails_bcc'] = $this->cfg['notification_emails_bcc'];

            $res = $http->postJson('/status', $payload);
            $status = (int)($res['status'] ?? 0);
            if ($status >= 200 && $status < 300) {
                $this->log->log('INFO', 'Pushed status');
                $snap['status_hash'] = $newHash;
                $this->safeSnapshotWrite($snapshotFile, $snap);
            } else {
                throw new RuntimeException('Bad status '.$status);
            }
        } catch (Throwable $e) {
            $this->queueOffline($queueFile, [
                'machineId'=>$machineId,'timestamp'=>gmdate('c'),'kind'=>'status'
            ]);
            $this->log->log('WARN', 'Queued status (offline)', ['error'=>$e->getMessage()]);
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
}
