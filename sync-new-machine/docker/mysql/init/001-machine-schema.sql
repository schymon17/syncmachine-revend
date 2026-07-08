CREATE DATABASE IF NOT EXISTS qcs CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE qcs;

CREATE TABLE IF NOT EXISTS machineinformation (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    mid VARCHAR(80) NOT NULL,
    p_down0 VARCHAR(255) NULL,
    p_down1 VARCHAR(255) NULL,
    p_down2 VARCHAR(255) NULL,
    p_down3 VARCHAR(255) NULL,
    p_down4 VARCHAR(255) NULL,
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY machineinformation_mid_unique (mid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS user_transaction (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    transactionid VARCHAR(191) NULL,
    user VARCHAR(191) NULL DEFAULT 'unknown',
    dateline INT UNSIGNED NOT NULL,
    statecode VARCHAR(32) NULL DEFAULT '0',
    barcode VARCHAR(64) NULL,
    bors VARCHAR(64) NULL,
    weight VARCHAR(32) NULL,
    diam VARCHAR(32) NULL,
    metal VARCHAR(32) NULL,
    print_barcode VARCHAR(191) NULL,
    recognitionstatus VARCHAR(32) NULL DEFAULT '1',
    rebateordonate VARCHAR(32) NULL DEFAULT '0',
    payplatform VARCHAR(32) NULL DEFAULT 'RVM',
    bottlevalue VARCHAR(32) NULL DEFAULT '6',
    charityid VARCHAR(64) NULL,
    charityname VARCHAR(191) NULL,
    octreceipt VARCHAR(32) NULL DEFAULT '0',
    transactiondone INT NOT NULL DEFAULT 0,
    uploaddone INT NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    KEY user_transaction_print_barcode_idx (print_barcode),
    KEY user_transaction_dateline_idx (dateline),
    KEY user_transaction_done_idx (transactiondone)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS empty_record (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    mid VARCHAR(80) NULL,
    dateline INT UNSIGNED NOT NULL,
    bin_type VARCHAR(64) NOT NULL,
    barcode VARCHAR(191) NOT NULL,
    PRIMARY KEY (id),
    KEY empty_record_dateline_idx (dateline)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS command (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    storage INT NOT NULL DEFAULT 100,
    storageplastic INT NOT NULL DEFAULT 100,
    storagecan INT NOT NULL DEFAULT 100,
    errorcode VARCHAR(191) NOT NULL DEFAULT '0',
    printer_barcode VARCHAR(191) NULL,
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY command_printer_barcode_idx (printer_barcode)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS barcode (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    barcode VARCHAR(64) NOT NULL,
    brand VARCHAR(255) NULL,
    bottleinfo VARCHAR(255) NULL,
    value VARCHAR(32) NULL,
    maxsdiam VARCHAR(32) NULL,
    minsdiam VARCHAR(32) NULL,
    maxbdiam VARCHAR(32) NULL,
    minbdiam VARCHAR(32) NULL,
    material_type VARCHAR(64) NULL,
    metal TINYINT NULL,
    capacity VARCHAR(32) NULL,
    weight VARCHAR(32) NULL,
    version VARCHAR(64) NULL,
    PRIMARY KEY (id),
    UNIQUE KEY barcode_unique (barcode)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS printer_barcode (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    barcode VARCHAR(191) NOT NULL,
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY printer_barcode_unique (barcode)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO machineinformation (mid) VALUES ('DEV_MACHINE_001');
INSERT INTO command (storage, storageplastic, storagecan, errorcode)
SELECT 80, 75, 70, '0'
WHERE NOT EXISTS (SELECT 1 FROM command LIMIT 1);
