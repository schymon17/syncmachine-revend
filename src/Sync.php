<?php
class Sync {
    public function __construct(private array $cfg, private Logger $log) {}
    private function queueOffline(string $path, array $payload): void {
        file_put_contents($path, json_encode($payload).PHP_EOL, FILE_APPEND);
    }

    private function flushQueue(Http $http, string $path): void {
        if (!file_exists($path)) return;
        $lines = file($path, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) ?: [];
        if (!$lines) return;
        $remain = [];
        foreach ($lines as $line) {
            $payload = json_decode($line, true);
            try {
                $res = $http->postJson('/sync/changes', $payload);
                if ($res['status'] < 200 || $res['status'] >= 300) throw new RuntimeException('Bad status '.$res['status']);
                $this->log->log('INFO', 'Flushed queued item', ['table'=>$payload['table']??'unknown']);
            } catch (Throwable $e) {
                $remain[] = $line;
                $this->log->log('WARN', 'Still offline for queued item', ['error'=>$e->getMessage()]);
            }
        }
        file_put_contents($path, implode(PHP_EOL, $remain).(count($remain)?PHP_EOL:''));
    }

    public function runOnce(): void {
        $machineId = $this->cfg['machineId'] ?? '';
        if ($machineId==='') {
            $this->log->log('ERROR','Machine ID empty'); return;
        }

        $paths = $this->cfg['paths'];
        $snapshotFile = $paths['snapshot']; $queueFile = $paths['queue'];
        if (!is_dir(dirname($snapshotFile))) mkdir(dirname($snapshotFile),0777,true);
        if (!is_dir(dirname($queueFile))) mkdir(dirname($queueFile),0777,true);

        try {
            $pdo = Db::pdo($this->cfg['db']);
        } catch (Throwable $e) {
            $this->log->log('ERROR','DB connect failed',['error'=>$e->getMessage()]);
            return;
        }

        $snap = file_exists($snapshotFile) ? json_decode(file_get_contents($snapshotFile), true): [];

        $http = new Http($this->cfg['api']['baseUrl'] ?? '', $this->cfg['api']['token'] ?? null);

        if (!($this->cfg['sync']['enabledTrans'] ?? false)) {
            $this->log->log('INFO','Sync disabled - Transactions are disabled');
        } else {
            $lastSync = (int)($snap['user_transaction_lastSync'] ?? 0);

            try {
                $stmt = $pdo->query("SELECT COALESCE(MAX(dateline),0) AS last_update FROM user_transaction");
                $latestDateline = (int)($stmt?->fetchColumn() ?? 0);
            } catch (Throwable $e) {
                $this->log->log('ERROR', 'MAX(dateline) query failed', ['error' => $e->getMessage()]);
                $latestDateline = $lastSync;
            }

            $any = false;

            if ($latestDateline > $lastSync) {
                $limit = (int)($this->cfg['sync']['transBatch'] ?? 5000);
                $sql = "SELECT * FROM user_transaction WHERE dateline > :lastSync ORDER BY dateline ASC" . ($limit > 0 ? " LIMIT $limit" : "");
                try {
                    $stmt = $pdo->prepare($sql);
                    $stmt->execute([':lastSync' => $lastSync]);
                    $rows = $stmt->fetchAll(PDO::FETCH_ASSOC) ?: [];
                } catch (Throwable $e) {
                    $this->log->log('ERROR', 'Fetch transactions failed', ['error' => $e->getMessage()]);
                    $rows = [];
                }
                $midRow = [];
                try {
                    $midStmt = $pdo->query("SELECT * FROM mid ORDER BY id DESC LIMIT 1");
                    $midRow = $midStmt?->fetch(PDO::FETCH_ASSOC) ?: [];
                } catch (Throwable $e) {
                    $this->log->log('WARN', 'MID read failed', ['error' => $e->getMessage()]);
                }

                $data = [];
                foreach ($rows as $r) {
                    $transactionId = $r['print_barcode'] ?? null;
                    if (!$transactionId) continue;

                    $dateline = (int)($r['dateline'] ?? 0);
                    $formattedTime = $dateline > 0 ? date('Y-m-d H:i:s', $dateline) : null;

                    $r['datetime'] = $formattedTime;
                    $data[$transactionId]['details'][] = $r;

                    $prev = $data[$transactionId]['last_transaction_time'] ?? null;
                    if (!$prev || $dateline > strtotime($prev)) {
                        $data[$transactionId]['last_transaction_time'] = $formattedTime;
                    }
                }

                if (!empty($data)) {
                    $any = true;
                    $http = new Http($this->cfg['api']['baseUrl'] ?? '', $this->cfg['api']['token'] ?? null);
                    $payload = [
                        'machineId' => $machineId,
                        'timestamp' => gmdate('c'),
                        'kind' => 'transactions',
                        'data' => [
                            'transactions' => $data,
                            'mid' => $midRow,
                        ],
                        'integration' => $this->cfg['integration'],
                    ];

                    try {
                        $res = $http->postJson('/trans', $payload);
                        if ($res['status'] >= 200 && $res['status'] < 300) {
                            $snap['user_transaction_lastSync'] = $latestDateline;
                            @file_put_contents(
                                $snapshotFile,
                                json_encode($snap, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE)
                            );
                            $this->log->log('INFO', 'Sent transactions', [
                                'count' => count($data),
                                'from' => $lastSync,
                                'to' => $latestDateline
                            ]);
                        } else {
                            throw new RuntimeException('Bad status ' . $res['status']);
                        }
                    } catch (Throwable $e) {
                        $this->queueOffline($queueFile, $payload);
                        $this->log->log('WARN', 'Queued transactions (offline)', ['error' => $e->getMessage()]);
                    }
                } else {
                    $this->log->log('INFO', 'No new records to send (post-filter)');
                }
            } else {
                $this->log->log('INFO', 'No new changes detected');
            }

            if (!$any) {
                $http = new Http($this->cfg['api']['baseUrl'] ?? '', $this->cfg['api']['token'] ?? null);
                $payload = [
                    'machineId' => $machineId,
                    'timestamp' => gmdate('c'),
                    'kind' => 'heartbeat',
                ];
                try {
                    $res = $http->postJson('/heartbeat', $payload);
                    if ($res['status'] >= 200 && $res['status'] < 300) {
                        $this->log->log('INFO', 'Heartbeat sent');
                    } else {
                        throw new RuntimeException('Bad status ' . $res['status']);
                    }
                } catch (Throwable $e) {
                    $this->queueOffline($queueFile, $payload);
                    $this->log->log('WARN', 'Heartbeat queued', ['error' => $e->getMessage()]);
                }
            }

            $http = new Http($this->cfg['api']['baseUrl'] ?? '', $this->cfg['api']['token'] ?? null);
            $this->flushQueue($http, $queueFile);
        }

        if (!($this->cfg['sync']['enabledEans'] ?? false)) {
            $this->log->log('INFO','Sync disabled - Eans are disabled');
        } else {
            try {
                $payload = [
                    'machineId' => $machineId,
                ];
                $res = $http->postJson('/eans', $payload);
                if (($res['status'] ?? 0) < 200 || ($res['status'] ?? 0) >= 300) {
                    throw new RuntimeException('Bad status ' . ($res['status'] ?? ''));
                }
            } catch (Throwable $e) {
                $this->log->log('ERROR', 'Fetch EANs failed', ['error' => $e->getMessage()]);
                return;
            }

            $response = $res['json'] ?? null;
            if ($response === null) {
                $this->log->log('ERROR', 'Fetch EANs: empty body or invalid JSON');
                return;
            }

            $items = is_array($response) && array_key_exists('data', $response) ? ($response['data'] ?? []) : $response;
            if (!is_array($items)) {
                $this->log->log('ERROR', 'Unexpected EANs format');
                return;
            }

            $newHash = hash('sha256', json_encode($items));
            if (($snap['eans_hash'] ?? null) === $newHash) {
                $this->log->log('INFO', 'EANs unchanged - nothing to import');
                return;
            }

            $inserted = 0;
            try {
                $pdo->beginTransaction();
                $pdo->exec('TRUNCATE TABLE barcode');

                $sql = 'INSERT INTO barcode (barcode, bottleinfo, value, material_type, metal, capacity, weight) VALUES (:barcode, :bottleinfo, :value, :material_type, :metal, :capacity, :weight)';
                $stmt = $pdo->prepare($sql);

                foreach ($items as $it) {
                    if (!is_array($it)) continue;

                    $barcode = $it['barcode'] ?? null;
                    if (!$barcode) continue;

                    $bottleinfo = $it['bottleinfo'] ?? null;
                    if (is_array($bottleinfo) || is_object($bottleinfo)) {
                        $bottleinfo = json_encode($bottleinfo, JSON_UNESCAPED_UNICODE);
                    }

                    $stmt->execute([
                        ':barcode' => (string)$barcode,
                        ':bottleinfo' => $bottleinfo,
                        ':value' => isset($it['value']) ? $it['value'] : null,
                        ':material_type' => $it['material_type'] ?? null,
                        ':metal' => isset($it['metal']) ? (int)$it['metal'] : null,
                        ':capacity' => isset($it['capacity']) ? $it['capacity'] : null,
                        ':weight' => isset($it['weight']) ? $it['weight'] : null,
                    ]);
                    $inserted++;
                }

                $pdo->commit();
                $snap['eans_hash'] = $newHash;
                $snap['eans_count'] = $inserted;
                @file_put_contents($snapshotFile, json_encode($snap, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE));

                $this->log->log('INFO', 'EANs imported', ['count' => $inserted]);
            } catch (Throwable $e) {
                if ($pdo->inTransaction()) $pdo->rollBack();
                $this->log->log('ERROR', 'EANs import failed', ['error' => $e->getMessage()]);
                return;
            }
        }

        if (!($this->cfg['sync']['enabledStatus'] ?? false)) {
            $this->log->log('INFO','Sync disabled - Status are disabled');
        } else {
            try {
                $stmt = $pdo->query("SELECT * FROM command ORDER BY id DESC LIMIT 1");
                $statusRow = $stmt?->fetch(PDO::FETCH_ASSOC) ?: [];
            } catch (Throwable $e) {
                $this->log->log('ERROR', 'Status query failed', [ 'error' => $e->getMessage() ]);
                $statusRow = [];
            }

            $newHash = hash('sha256', json_encode($statusRow));
            $oldHash = $snap['status_hash'] ?? null;
            $hasChange = $newHash !== $oldHash;

            $any = false;

            if ($hasChange) {
                $any = true;
                $payload = [
                    'machineId' => $machineId,
                    'timestamp' => gmdate('c'),
                    'kind' => 'status',
                    'data' => [
                        'command' => $statusRow,
                    ],
                ];

                if (!empty($this->cfg['address'])) {
                    $payload['address'] = $this->cfg['address'];
                }
                if (!empty($this->cfg['notification_emails'])) {
                    $payload['notification_emails'] = $this->cfg['notification_emails'];
                }
                if (!empty($this->cfg['notification_emails_bcc'])) {
                    $payload['notification_emails_bcc'] = $this->cfg['notification_emails_bcc'];
                }

                try {
                    $res = $http->postJson('/status', $payload);
                    if ($res['status'] >= 200 && $res['status'] < 300) {
                        $this->log->log('INFO', 'Pushed status');
                        $snap['status_hash'] = $newHash;
                    } else {
                        throw new RuntimeException('Bad status ' . $res['status']);
                    }
                } catch (Throwable $e) {
                    $this->queueOffline($queueFile, $payload);
                    $this->log->log('WARN', 'Queued status (offline)', ['error' => $e->getMessage()]);
                }
            } else {
                $this->log->log('INFO', 'No status change');
            }

            @file_put_contents($snapshotFile, json_encode($snap, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE));

            if (!$any) {
                $payload = [
                    'machineId' => $machineId,
                    'timestamp' => gmdate('c'),
                    'kind' => 'heartbeat',
                ];
                try {
                    $res = $http->postJson('/heartbeat', $payload);
                    if ($res['status'] >= 200 && $res['status'] < 300) {
                        $this->log->log('INFO', 'Heartbeat sent');
                    } else {
                        throw new RuntimeException('Bad status ' . $res['status']);
                    }
                } catch (Throwable $e) {
                    $this->queueOffline($queueFile, $payload);
                    $this->log->log('WARN', 'Heartbeat queued', ['error' => $e->getMessage()]);
                }
            }

            $this->flushQueue($http, $queueFile);
        }

        if (!($this->cfg['sync']['enabledAdverts'] ?? false)) {
            $this->log->log('INFO','Adverts disabled - Status are disabled');
        } else {

        }
    }
}
