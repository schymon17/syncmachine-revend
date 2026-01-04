<?php

class Sync
{
    private ?PDO $pdo = null;
    private int $pdoFailStreak = 0;

    public function __construct(private array $cfg, private Logger $log) {}

    /* -------------------- IO helpers -------------------- */

    private function atomicWrite(string $path, string $data): void
    {
        try {
            $tmp = $path . '.tmp';
            file_put_contents($tmp, $data, LOCK_EX);
            @rename($tmp, $path);
        } catch (Throwable) {
            @file_put_contents($path, $data, LOCK_EX);
        }
    }

    private function readJsonFile(string $path): array
    {
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

    /**
     * Offline queue format (JSONL):
     * {"endpoint":"/trans","payload":{...},"queuedAt":"...","error":"..."}
     */
    private function queueOffline(string $path, array $item): void
    {
        try {
            $line = json_encode($item, JSON_UNESCAPED_UNICODE) . PHP_EOL;
            file_put_contents($path, $line, FILE_APPEND | LOCK_EX);
        } catch (Throwable $e) {
            $this->log->log('WARN', 'Failed to append queue file', ['error' => $e->getMessage()]);
        }
    }

    private function safeSnapshotWrite(string $path, array $snap): void
    {
        try {
            $this->atomicWrite($path, json_encode($snap, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE));
        } catch (Throwable $e) {
            $this->log->log('WARN', 'Snapshot write failed (non-fatal)', ['error' => $e->getMessage()]);
        }
    }

    /**
     * Every 12 hours: delete the old queue file format once, so maintenance is easy.
     */
    private function cleanupOldQueueFormatEvery12h(string $queueFile, string $snapshotFile, array &$snap): void
    {
        $now  = time();
        $last = (int)($snap['queue_cleanup_last_check_ts'] ?? 0);

        if ($last > 0 && ($now - $last) < 12 * 3600) return;

        $snap['queue_cleanup_last_check_ts'] = $now;
        $this->safeSnapshotWrite($snapshotFile, $snap);

        try {
            if (!file_exists($queueFile)) return;

            $fh = @fopen($queueFile, 'rb');
            if (!$fh) return;

            $firstLine = null;
            while (!feof($fh)) {
                $line = fgets($fh);
                if ($line === false) break;
                $line = trim($line);
                if ($line !== '') { $firstLine = $line; break; }
            }
            fclose($fh);

            if ($firstLine === null) return;

            $dec = json_decode($firstLine, true);
            $isNewFormat = is_array($dec)
                && isset($dec['endpoint'])
                && isset($dec['payload'])
                && is_array($dec['payload']);

            if (!$isNewFormat) {
                @unlink($queueFile);
                $this->log->log('WARN', 'Deleted old-format queue file (scheduled check)', [
                    'queueFile' => $queueFile,
                ]);
            }
        } catch (Throwable $e) {
            $this->log->log('WARN', 'Queue cleanup check failed (non-fatal)', [
                'error' => $e->getMessage(),
            ]);
        }
    }

    /* -------------------- DB stability -------------------- */

    private function getPdoOrNull(): ?PDO
    {
        try {
            if ($this->pdo instanceof PDO) {
                try {
                    $this->pdo->query('SELECT 1');
                    $this->pdoFailStreak = 0;
                    return $this->pdo;
                } catch (Throwable) {
                    $this->pdo = null;
                }
            }

            $this->pdo = Db::pdo($this->cfg['db']);
            try { $this->pdo->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION); } catch (Throwable) {}
            try { $this->pdo->setAttribute(PDO::ATTR_DEFAULT_FETCH_MODE, PDO::FETCH_ASSOC); } catch (Throwable) {}

            $this->pdo->query('SELECT 1');
            $this->pdoFailStreak = 0;

            $this->log->log('INFO', 'DB connected');
            return $this->pdo;

        } catch (Throwable $e) {
            $this->pdoFailStreak++;
            $this->pdo = null;

            $this->log->log('ERROR', 'DB connect/ping failed', [
                'error' => $e->getMessage(),
                'fail_streak' => $this->pdoFailStreak,
            ]);
            return null;
        }
    }

    /* -------------------- Outbox install (safe mode) -------------------- */

    /**
     * SAFE MODE:
     * - creates sync_outbox table if missing
     * - creates triggers ONLY if missing (or force recreate)
     * - never alters user_transaction
     * - never drops triggers unless outboxForceRecreateTriggers=true
     *
     * Triggers:
     * - use NULL-safe comparison (<=>) so NULL->value updates are detected
     * - build payload_json via CONCAT (no JSON_OBJECT needed)
     *
     * Config:
     * - sync.outboxForceRecreateTriggers = true  (drop+recreate triggers on the next check)
     */
    private function ensureOutboxInstalledEvery12hSafe(PDO $pdo, string $snapshotFile, array &$snap): void
    {
        $now  = time();
        $last = (int)($snap['outbox_install_last_check_ts'] ?? 0);

        if ($last > 0 && ($now - $last) < 12 * 3600) return;

        $snap['outbox_install_last_check_ts'] = $now;
        $this->safeSnapshotWrite($snapshotFile, $snap);

        $forceRecreate = (bool)($this->cfg['sync']['outboxForceRecreateTriggers'] ?? false);

        try {
            // 1) table
            $pdo->exec("
                CREATE TABLE IF NOT EXISTS sync_outbox (
                  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                  kind VARCHAR(50) NOT NULL,
                  entity VARCHAR(50) NOT NULL,
                  entity_id BIGINT NOT NULL,
                  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  payload_json LONGTEXT NOT NULL,
                  sent_at TIMESTAMP NULL DEFAULT NULL,
                  send_attempts INT NOT NULL DEFAULT 0,
                  last_error VARCHAR(255) NULL DEFAULT NULL,
                  PRIMARY KEY (id),
                  KEY idx_sent_at (sent_at),
                  KEY idx_kind_sent (kind, sent_at),
                  KEY idx_entity (entity, entity_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            ");

            // 2) triggers existence
            $hasIns = false;
            $hasUpd = false;

            try {
                $q = $pdo->query("SHOW TRIGGERS LIKE 'user_transaction'");
                $tr = $q ? $q->fetchAll(PDO::FETCH_ASSOC) : [];
                foreach ($tr as $t) {
                    $name = (string)($t['Trigger'] ?? '');
                    if ($name === 'trg_user_transaction_outbox_ins') $hasIns = true;
                    if ($name === 'trg_user_transaction_outbox_upd') $hasUpd = true;
                }
            } catch (Throwable) {
                // ignore
            }

            // 3) drop triggers only if forced
            if ($forceRecreate) {
                try { $pdo->exec("DROP TRIGGER IF EXISTS trg_user_transaction_outbox_ins"); } catch (Throwable) {}
                try { $pdo->exec("DROP TRIGGER IF EXISTS trg_user_transaction_outbox_upd"); } catch (Throwable) {}
                $hasIns = false;
                $hasUpd = false;
            }

            // IMPORTANT: PDO does NOT support DELIMITER, so we must include END; in the statement.
            $insertTriggerSql = "
                CREATE TRIGGER trg_user_transaction_outbox_ins
                AFTER INSERT ON user_transaction
                FOR EACH ROW
                BEGIN
                  INSERT INTO sync_outbox (kind, entity, entity_id, payload_json)
                  VALUES (
                    'transactions',
                    'user_transaction',
                    NEW.id,
                    CONCAT(
                      '{',
                        '\"id\":', NEW.id, ',',
                        '\"print_barcode\":', IF(NEW.print_barcode IS NULL, 'null',
                          CONCAT('\"',
                            REPLACE(REPLACE(NEW.print_barcode, '\\\\', '\\\\\\\\'), '\"', '\\\\\"'),
                          '\"')
                        ), ',',
                        '\"dateline\":', IF(NEW.dateline IS NULL, 'null', NEW.dateline),
                      '}'
                    )
                  );
                END;
            ";

            // NULL-safe change detection (<=>)
            $updateTriggerSql = "
                CREATE TRIGGER trg_user_transaction_outbox_upd
                AFTER UPDATE ON user_transaction
                FOR EACH ROW
                BEGIN
                  IF NOT (OLD.print_barcode <=> NEW.print_barcode)
                     OR NOT (OLD.dateline <=> NEW.dateline) THEN
                    INSERT INTO sync_outbox (kind, entity, entity_id, payload_json)
                    VALUES (
                      'transactions',
                      'user_transaction',
                      NEW.id,
                      CONCAT(
                        '{',
                          '\"id\":', NEW.id, ',',
                          '\"print_barcode\":', IF(NEW.print_barcode IS NULL, 'null',
                            CONCAT('\"',
                              REPLACE(REPLACE(NEW.print_barcode, '\\\\', '\\\\\\\\'), '\"', '\\\\\"'),
                            '\"')
                          ), ',',
                          '\"dateline\":', IF(NEW.dateline IS NULL, 'null', NEW.dateline),
                        '}'
                      )
                    );
                  END IF;
                END;
            ";

            if (!$hasIns) {
                try {
                    $pdo->exec($insertTriggerSql);
                    $this->log->log('INFO', 'Created trigger trg_user_transaction_outbox_ins');
                } catch (Throwable $e) {
                    $this->log->log('ERROR', 'Failed to create trg_user_transaction_outbox_ins', [
                        'error' => $e->getMessage(),
                    ]);
                }
            }

            if (!$hasUpd) {
                try {
                    $pdo->exec($updateTriggerSql);
                    $this->log->log('INFO', 'Created trigger trg_user_transaction_outbox_upd');
                } catch (Throwable $e) {
                    $this->log->log('ERROR', 'Failed to create trg_user_transaction_outbox_upd', [
                        'error' => $e->getMessage(),
                    ]);
                }
            }

            $this->log->log('INFO', 'Outbox ensured (safe mode)', [
                'forceRecreate' => $forceRecreate,
                'hasIns' => $hasIns,
                'hasUpd' => $hasUpd,
            ]);

        } catch (Throwable $e) {
            $this->log->log('ERROR', 'Outbox ensure failed (non-fatal)', [
                'error' => $e->getMessage(),
            ]);
        }
    }

    /* -------------------- Offline flush -------------------- */

    private function flushQueue(Http $http, string $path): void
    {
        try {
            if (!file_exists($path)) return;

            $lines = file($path, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) ?: [];
            if (!$lines) return;

            $remain = [];
            foreach ($lines as $line) {
                $item = json_decode($line, true);
                if (!is_array($item)) { $remain[] = $line; continue; }

                $endpoint = (string)($item['endpoint'] ?? '');
                $payload  = $item['payload'] ?? null;

                if ($endpoint === '' || !is_array($payload)) { $remain[] = $line; continue; }

                try {
                    $res = $http->postJson($endpoint, $payload);
                    $status = (int)($res['status'] ?? 0);
                    if ($status < 200 || $status >= 300) throw new RuntimeException('Bad status '.$status);

                    $this->log->log('INFO', 'Flushed queued item', [
                        'endpoint' => $endpoint,
                        'kind' => $payload['kind'] ?? null,
                    ]);
                } catch (Throwable $e) {
                    $remain[] = $line;
                    $this->log->log('WARN', 'Still offline for queued item', [
                        'endpoint' => $endpoint,
                        'error' => $e->getMessage(),
                    ]);
                }
            }

            file_put_contents($path, implode(PHP_EOL, $remain) . (count($remain) ? PHP_EOL : ''), LOCK_EX);
        } catch (Throwable $e) {
            $this->log->log('WARN', 'Flush queue failed (non-fatal)', ['error' => $e->getMessage()]);
        }
    }

    /* -------------------- HTTP safe send -------------------- */

    private function postOrQueue(Http $http, string $queueFile, string $endpoint, array $payload): bool
    {
        try {
            $res = $http->postJson($endpoint, $payload);
            $status = (int)($res['status'] ?? 0);
            if ($status >= 200 && $status < 300) return true;
            throw new RuntimeException('Bad status ' . $status);
        } catch (Throwable $e) {
            $this->queueOffline($queueFile, [
                'endpoint' => $endpoint,
                'payload'  => $payload,
                'queuedAt' => gmdate('c'),
                'error'    => $e->getMessage(),
            ]);
            $this->log->log('WARN', 'Queued payload (offline)', [
                'endpoint' => $endpoint,
                'kind' => $payload['kind'] ?? null,
                'error' => $e->getMessage(),
            ]);
            return false;
        }
    }

    /* -------------------- Transactions via Outbox (GROUPED PAYLOAD) -------------------- */

    /**
     * Sends transactions in the REQUIRED format:
     * data.transactions[print_barcode].details[] + last_transaction_time
     *
     * Marks outbox rows as sent only after 2xx.
     */
    private function runTransactionsOutbox(PDO $pdo, Http $http, string $queueFile, string $machineId): bool
    {
        try {
            $batch = max(1, (int)($this->cfg['sync']['transBatch'] ?? 500));

            $stmt = $pdo->prepare("
                SELECT id, entity_id, payload_json, created_at, send_attempts
                FROM sync_outbox
                WHERE sent_at IS NULL AND kind = 'transactions'
                ORDER BY id ASC
                LIMIT $batch
            ");
            $stmt->execute();
            $events = $stmt->fetchAll(PDO::FETCH_ASSOC) ?: [];
            if (!$events) return false;

            $transactions = [];
            $outboxIdsIncluded = [];

            foreach ($events as $ev) {
                $outboxId = (int)($ev['id'] ?? 0);
                $entityId = (int)($ev['entity_id'] ?? 0);
                if ($outboxId <= 0 || $entityId <= 0) continue;

                // Increase attempts
                try {
                    $pdo->prepare("UPDATE sync_outbox SET send_attempts = send_attempts + 1 WHERE id = ?")
                        ->execute([$outboxId]);
                } catch (Throwable) {}

                // Fetch current row for correctness
                $row = null;
                try {
                    $st = $pdo->prepare("SELECT * FROM user_transaction WHERE id = ?");
                    $st->execute([$entityId]);
                    $row = $st->fetch(PDO::FETCH_ASSOC) ?: null;
                } catch (Throwable) {}

                // Fallback to payload_json if row fetch failed
                if (!is_array($row)) {
                    $payloadRow = json_decode((string)($ev['payload_json'] ?? ''), true);
                    $row = is_array($payloadRow) ? $payloadRow : null;
                }

                if (!is_array($row)) {
                    try {
                        $pdo->prepare("UPDATE sync_outbox SET last_error = 'Row missing or invalid payload_json' WHERE id = ?")
                            ->execute([$outboxId]);
                    } catch (Throwable) {}
                    continue;
                }

                $printBarcode = (string)($row['print_barcode'] ?? '');
                if ($printBarcode === '') {
                    // not finalized yet -> stop this cycle to keep ordering strict
                    try {
                        $pdo->prepare("UPDATE sync_outbox SET last_error = 'print_barcode not ready' WHERE id = ?")
                            ->execute([$outboxId]);
                    } catch (Throwable) {}
                    return false;
                }

                $dl = (int)($row['dateline'] ?? 0);
                $row['datetime'] = $dl > 0 ? date('Y-m-d H:i:s', $dl) : null;

                if (!isset($transactions[$printBarcode])) {
                    $transactions[$printBarcode] = [
                        'details' => [],
                        'last_transaction_time' => $row['datetime'],
                    ];
                }

                $transactions[$printBarcode]['details'][] = $row;

                $prev = $transactions[$printBarcode]['last_transaction_time'] ?? null;
                if (!$prev || ($row['datetime'] && strtotime($row['datetime']) > strtotime((string)$prev))) {
                    $transactions[$printBarcode]['last_transaction_time'] = $row['datetime'];
                }

                $outboxIdsIncluded[] = $outboxId;
            }

            if (!$transactions) return false;

            $payload = [
                'machineId'   => $machineId,
                'timestamp'   => gmdate('c'),
                'kind'        => 'transactions',
                'data'        => [
                    'transactions' => $transactions,
                    'mid' => $machineId,
                ],
                'integration' => $this->cfg['integration'] ?? null,
            ];

            $ok = $this->postOrQueue($http, $queueFile, '/trans', $payload);
            if (!$ok) {
                // queued offline; keep outbox unsent
                try {
                    if ($outboxIdsIncluded) {
                        $in = implode(',', array_fill(0, count($outboxIdsIncluded), '?'));
                        $pdo->prepare("UPDATE sync_outbox SET last_error = 'queued offline' WHERE id IN ($in)")
                            ->execute($outboxIdsIncluded);
                    }
                } catch (Throwable) {}
                return false;
            }

            // mark sent only on success
            if ($outboxIdsIncluded) {
                $in = implode(',', array_fill(0, count($outboxIdsIncluded), '?'));
                $pdo->prepare("UPDATE sync_outbox SET sent_at = NOW(), last_error = NULL WHERE id IN ($in)")
                    ->execute($outboxIdsIncluded);
            }

            $rowsCount = 0;
            foreach ($transactions as $g) {
                $rowsCount += is_array($g['details'] ?? null) ? count($g['details']) : 0;
            }

            $this->log->log('INFO', 'Sent transactions (grouped)', [
                'groups' => count($transactions),
                'rows' => $rowsCount,
                'outbox_sent' => count($outboxIdsIncluded),
            ]);

            return true;

        } catch (Throwable $e) {
            $this->log->log('ERROR', 'Transactions outbox step failed (non-fatal)', ['error' => $e->getMessage()]);
            return false;
        }
    }

    /* -------------------- Heartbeat -------------------- */

    private function safeHeartbeat(Http $http, string $queueFile, string $machineId): void
    {
        $payload = [
            'machineId' => $machineId,
            'timestamp' => gmdate('c'),
            'kind'      => 'heartbeat',
        ];

        try {
            $res = $http->postJson('/heartbeat', $payload);
            $status = (int)($res['status'] ?? 0);
            if ($status >= 200 && $status < 300) {
                $this->log->log('INFO', 'Heartbeat sent');
            } else {
                throw new RuntimeException('Bad status ' . $status);
            }
        } catch (Throwable $e) {
            $this->queueOffline($queueFile, [
                'endpoint' => '/heartbeat',
                'payload'  => $payload,
                'queuedAt' => gmdate('c'),
                'error'    => $e->getMessage(),
            ]);
            $this->log->log('WARN', 'Heartbeat queued', ['error' => $e->getMessage()]);
        }
    }

    /* -------------------- EANs -------------------- */

    private function runEans(?PDO $pdo, Http $http, string $snapshotFile, array &$snap, string $machineId): void
    {
        try {
            $res = $http->postJson('/eans', [
                'machineId' => $machineId,
                'timestamp' => gmdate('c'),
                'integration' => $this->cfg['integration'] ?? null,
            ]);

            $status = (int)($res['status'] ?? 0);
            if ($status < 200 || $status >= 300) throw new RuntimeException('Bad status ' . $status);

            $response = $res['body'] ?? null;
            if ($response === null) throw new RuntimeException('Fetch EANs: empty body');

            $items = is_array($response) && array_key_exists('data', $response) ? ($response['data'] ?? []) : $response;
            if (!is_array($items)) throw new RuntimeException('Unexpected EANs format');

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
                $this->log->log('WARN', 'EANs fetched but DB not available; skipping import');
                return;
            }

            try { $pdo->exec("ALTER TABLE barcode MODIFY bottleinfo VARCHAR(255)"); } catch (Throwable) {}
            try { $pdo->exec("ALTER TABLE barcode MODIFY brand VARCHAR(255)"); } catch (Throwable) {}

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
                    ':brand'         => $it['brand'] ?? null,
                    ':bottleinfo'    => $bottleinfo,
                    ':value'         => $it['value'] ?? null,
                    ':maxsdiam'      => $it['maxsdiam'] ?? null,
                    ':minsdiam'      => $it['minsdiam'] ?? null,
                    ':maxbdiam'      => $it['maxbdiam'] ?? null,
                    ':minbdiam'      => $it['minbdiam'] ?? null,
                    ':material_type' => $it['material_type'] ?? null,
                    ':metal'         => isset($it['metal']) ? (int)$it['metal'] : null,
                    ':capacity'      => $it['capacity'] ?? null,
                    ':weight'        => $it['weight'] ?? null,
                    ':version'       => $it['version'] ?? null,
                ]);
                $inserted++;
            }

            if ($pdo->inTransaction()) $pdo->commit();

            $snap['eans_hash']  = $newHash;
            $snap['eans_count'] = $inserted;
            $this->safeSnapshotWrite($snapshotFile, $snap);
            $this->log->log('INFO', 'EANs imported', ['count' => $inserted]);

        } catch (Throwable $e) {
            $this->log->log('ERROR', 'EANs step failed (non-fatal)', ['error' => $e->getMessage()]);
            if ($pdo && $pdo->inTransaction()) { try { $pdo->rollBack(); } catch (Throwable) {} }
        }
    }

    /* -------------------- Status -------------------- */

    private function runStatus(PDO $pdo, Http $http, string $snapshotFile, string $queueFile, array &$snap, string $machineId): void
    {
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
                'data'      => ['command' => $statusRow],
            ];

            if (!empty($this->cfg['address']))                  $payload['address']                 = $this->cfg['address'];
            if (!empty($this->cfg['notification_emails']))      $payload['notification_emails']     = $this->cfg['notification_emails'];
            if (!empty($this->cfg['notification_emails_bcc']))  $payload['notification_emails_bcc'] = $this->cfg['notification_emails_bcc'];

            $ok = $this->postOrQueue($http, $queueFile, '/status', $payload);
            if ($ok) {
                $this->log->log('INFO', 'Pushed status');
                $snap['status_hash'] = $newHash;
                $this->safeSnapshotWrite($snapshotFile, $snap);
            } else {
                $this->log->log('WARN', 'Queued status (offline)');
            }
        } catch (Throwable $e) {
            $this->queueOffline($queueFile, [
                'endpoint' => '/status',
                'payload' => [
                    'machineId' => $machineId,
                    'timestamp' => gmdate('c'),
                    'kind' => 'status',
                ],
                'queuedAt' => gmdate('c'),
                'error' => $e->getMessage(),
            ]);
            $this->log->log('WARN', 'Queued status (offline)', ['error' => $e->getMessage()]);
        }

        try { $this->flushQueue($http, $queueFile); } catch (Throwable) {}
    }

    /* -------------------- Coupons -------------------- */

    private function runCoupons(PDO $pdo, Http $http, string $machineId): void
    {
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
            if ($status < 200 || $status >= 300) throw new RuntimeException('Bad status ' . $status);

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
            $existingSet = array_flip($existingSet);

            $toInsert = [];
            foreach ($normalized as $bc) {
                if (!isset($existingSet[$bc])) {
                    $toInsert[] = $bc;
                    if (count($toInsert) >= $need) break;
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

    /* -------------------- Adverts -------------------- */

    private function runAdverts(PDO $pdo, Http $http, string $snapshotFile, array &$snap, string $machineId): void
    {
        try {
            $res = $http->postJson('/adverts', [
                'machineId' => $machineId,
                'timestamp' => gmdate('c'),
                'integration' => $this->cfg['integration'] ?? null,
            ]);

            $status = (int)($res['status'] ?? 0);
            if ($status < 200 || $status >= 300) throw new RuntimeException('Bad status ' . $status);

            $body = $res['body'] ?? null;
            if ($body === null) throw new RuntimeException('Empty adverts body');

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

            $baseDir = 'C:\\phpStudy\\PHPTutorial\\WWW\\downadpic\\img';
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
                if (!is_array($ad)) continue;

                $slot = $ad['slot'] ?? $slotKey;
                if (!in_array($slot, ['p1', 'p2', 'p3', 'p4', 'p5'], true)) continue;

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
                    $fileName = $slot . '_' . $nameFromApi;
                } else {
                    $basename = basename(parse_url($url, PHP_URL_PATH) ?: '');
                    if ($basename === '' || $basename === '/') $basename = $slot . '.bin';
                    $fileName = $slot . '_' . $basename;
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
            $stmt->execute([$pDown0, $pDown1, $pDown2, $pDown3, $pDown4, $machineId]);

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
                $stmt2->execute([$pDown0, $pDown1, $pDown2, $pDown3, $pDown4]);

                $this->log->log('INFO', 'Fallback machineinformation update', [
                    'affected' => $stmt2->rowCount(),
                ]);
            } else {
                $this->log->log('INFO', 'machineinformation update by mid', [
                    'mid' => $machineId,
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
            $this->log->log('ERROR', 'Adverts step failed (non-fatal)', ['error' => $e->getMessage()]);
        }
    }

    /* -------------------- Binary download -------------------- */

    private function downloadBinary(string $url): string
    {
        $ch = curl_init();
        if ($ch === false) throw new RuntimeException('curl_init failed');

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
            throw new RuntimeException('cURL download failed: ' . $err . ' (code ' . $code . ')');
        }

        $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);

        if ($httpCode < 200 || $httpCode >= 300) {
            throw new RuntimeException('HTTP status ' . $httpCode . ' while downloading ' . $url);
        }

        return $data;
    }

    /* -------------------- MAIN ENTRY -------------------- */

    public function runOnce(): void
    {
        $machineId = (string)($this->cfg['machineId'] ?? '');
        if ($machineId === '') {
            $this->log->log('ERROR', 'Machine ID empty');
            return;
        }

        $paths        = $this->cfg['paths'] ?? [];
        $snapshotFile = (string)($paths['snapshot'] ?? (__DIR__ . '/var/sync/snapshot.json'));
        $queueFile    = (string)($paths['queue'] ?? (__DIR__ . '/var/sync/queue.log'));

        @is_dir(dirname($snapshotFile)) || @mkdir(dirname($snapshotFile), 0777, true);
        @is_dir(dirname($queueFile))    || @mkdir(dirname($queueFile), 0777, true);

        $snap = $this->readJsonFile($snapshotFile);

        // 12h maintenance
        $this->cleanupOldQueueFormatEvery12h($queueFile, $snapshotFile, $snap);

        $http = new Http(
            (string)($this->cfg['api']['baseUrl'] ?? ''),
            $this->cfg['api']['token'] ?? null
        );

        // DB connect/ping (stable)
        $pdo = $this->getPdoOrNull();

        // Ensure outbox + triggers (safe)
        if ($pdo) {
            $this->ensureOutboxInstalledEvery12hSafe($pdo, $snapshotFile, $snap);
        } else {
            $this->log->log('WARN', 'DB not available - skipping DB dependent steps');
        }

        // Flush queued offline payloads first
        $this->flushQueue($http, $queueFile);

        // Transactions (grouped payload, outbox driven)
        if (($this->cfg['sync']['enabledTrans'] ?? true) && $pdo) {
            $maxLoops = max(1, (int)($this->cfg['sync']['transMaxLoops'] ?? 10));
            for ($i = 0; $i < $maxLoops; $i++) {
                $sentAny = $this->runTransactionsOutbox($pdo, $http, $queueFile, $machineId);
                if (!$sentAny) break;
            }
        } else {
            $this->log->log('INFO', 'Sync disabled - Transactions are disabled or DB not ready');
        }

        // Flush queue again
        $this->flushQueue($http, $queueFile);

        // EANs
        if ($this->cfg['sync']['enabledEans'] ?? false) {
            $this->runEans($pdo, $http, $snapshotFile, $snap, $machineId);
        } else {
            $this->log->log('INFO', 'Sync disabled - Eans are disabled');
        }

        // Status
        if (($this->cfg['sync']['enabledStatus'] ?? false) && $pdo) {
            $this->runStatus($pdo, $http, $snapshotFile, $queueFile, $snap, $machineId);
        } else {
            $this->log->log('INFO', 'Sync disabled - Status are disabled or DB not ready');
        }

        // Coupons
        if (($this->cfg['sync']['enabledCoupons'] ?? false) && $pdo) {
            $this->runCoupons($pdo, $http, $machineId);
        } else {
            $this->log->log('INFO', 'Sync disabled - Coupons are disabled or DB not ready');
        }

        // Adverts
        if (($this->cfg['sync']['enabledAdverts'] ?? false) && $pdo) {
            $this->runAdverts($pdo, $http, $snapshotFile, $snap, $machineId);
        } else {
            $this->log->log('INFO', 'Sync disabled - Adverts are disabled or DB not ready');
        }

        // Heartbeat
        $this->safeHeartbeat($http, $queueFile, $machineId);

        // Persist snapshot at end
        $this->safeSnapshotWrite($snapshotFile, $snap);
    }
}
