<?php

class TransactionOutbox
{
    private const TRIGGERS = [
        'sync_user_transaction_ai' => <<<'SQL'
CREATE TRIGGER sync_user_transaction_ai
AFTER INSERT ON user_transaction
FOR EACH ROW
BEGIN
    IF NEW.transactiondone IN (2,4,5) AND NEW.print_barcode IS NOT NULL AND NEW.print_barcode <> '' THEN
        INSERT INTO sync_outbox (event_type, source_table, source_pk, status, created_at)
        VALUES ('transaction_finished', 'user_transaction', NEW.print_barcode, 'pending', NOW())
        ON DUPLICATE KEY UPDATE status='pending', last_error=NULL, created_at=NOW();
    END IF;
END
SQL,
        'sync_user_transaction_au' => <<<'SQL'
CREATE TRIGGER sync_user_transaction_au
AFTER UPDATE ON user_transaction
FOR EACH ROW
BEGIN
    IF NEW.transactiondone IN (2,4,5)
       AND NEW.print_barcode IS NOT NULL
       AND NEW.print_barcode <> ''
       AND (COALESCE(OLD.transactiondone, 0) NOT IN (2,4,5) OR NOT (OLD.print_barcode <=> NEW.print_barcode)) THEN
        INSERT INTO sync_outbox (event_type, source_table, source_pk, status, created_at)
        VALUES ('transaction_finished', 'user_transaction', NEW.print_barcode, 'pending', NOW())
        ON DUPLICATE KEY UPDATE status='pending', last_error=NULL, created_at=NOW();
    END IF;
END
SQL,
    ];

    public function __construct(private PDO $pdo)
    {
    }

    public function install(bool $forceRecreateTriggers = false): void
    {
        $this->pdo->exec(
            "CREATE TABLE IF NOT EXISTS sync_outbox (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                event_type VARCHAR(64) NOT NULL,
                source_table VARCHAR(64) NOT NULL,
                source_pk VARCHAR(191) NOT NULL,
                status VARCHAR(24) NOT NULL DEFAULT 'pending',
                attempts INT UNSIGNED NOT NULL DEFAULT 0,
                last_error TEXT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                locked_at DATETIME NULL,
                sent_at DATETIME NULL,
                PRIMARY KEY (id),
                UNIQUE KEY sync_outbox_unique_event (event_type, source_table, source_pk),
                KEY sync_outbox_status_id (status, id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
        );

        foreach (self::TRIGGERS as $name => $sql) {
            if ($forceRecreateTriggers) {
                $this->pdo->exec("DROP TRIGGER IF EXISTS `$name`");
            }

            $stmt = $this->pdo->prepare(
                'SELECT 1 FROM information_schema.TRIGGERS
                 WHERE TRIGGER_SCHEMA = DATABASE() AND TRIGGER_NAME = :name LIMIT 1'
            );
            $stmt->execute([':name' => $name]);
            if (!$stmt->fetchColumn()) {
                $this->pdo->exec($sql);
            }
        }

        // A killed daemon must not leave an event permanently locked.
        $this->pdo->exec(
            "UPDATE sync_outbox
             SET status='pending', locked_at=NULL
             WHERE event_type='transaction_finished'
               AND status='processing'
               AND (locked_at IS NULL OR locked_at < DATE_SUB(NOW(), INTERVAL 5 MINUTE))"
        );
    }

    public function claim(int $limit = 50): array
    {
        $limit = max(1, min(500, $limit));
        $this->pdo->beginTransaction();

        try {
            $rows = $this->pdo->query(
                "SELECT id, source_pk
                 FROM sync_outbox
                 WHERE event_type='transaction_finished'
                   AND (status='pending' OR (status='failed' AND locked_at < DATE_SUB(NOW(), INTERVAL 30 SECOND)))
                 ORDER BY id ASC
                 LIMIT $limit
                 FOR UPDATE"
            )->fetchAll(PDO::FETCH_ASSOC) ?: [];

            if ($rows) {
                $ids = array_map(static fn (array $row): int => (int)$row['id'], $rows);
                $placeholders = implode(',', array_fill(0, count($ids), '?'));
                $stmt = $this->pdo->prepare(
                    "UPDATE sync_outbox SET status='processing', locked_at=NOW() WHERE id IN ($placeholders)"
                );
                $stmt->execute($ids);
            }

            $this->pdo->commit();
            return $rows;
        } catch (Throwable $e) {
            if ($this->pdo->inTransaction()) {
                $this->pdo->rollBack();
            }
            throw $e;
        }
    }

    public function markDone(array $ids): void
    {
        $this->updateClaimed($ids, "status='done', sent_at=NOW(), last_error=NULL, locked_at=NULL");
    }

    public function markFailed(array $ids, string $error): void
    {
        if (!$ids) {
            return;
        }

        $ids = array_values(array_map('intval', $ids));
        $placeholders = implode(',', array_fill(0, count($ids), '?'));
        $stmt = $this->pdo->prepare(
            "UPDATE sync_outbox
             SET status='failed', attempts=attempts+1, last_error=?, locked_at=NOW()
             WHERE status='processing' AND id IN ($placeholders)"
        );
        $stmt->execute(array_merge([substr($error, 0, 4000)], $ids));
    }

    private function updateClaimed(array $ids, string $set): void
    {
        if (!$ids) {
            return;
        }

        $ids = array_values(array_map('intval', $ids));
        $placeholders = implode(',', array_fill(0, count($ids), '?'));
        $stmt = $this->pdo->prepare(
            "UPDATE sync_outbox SET $set WHERE status='processing' AND id IN ($placeholders)"
        );
        $stmt->execute($ids);
    }
}
