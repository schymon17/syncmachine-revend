-- Machine database as the agent sees it. user_transaction columns are those
-- real machines send in v1 packets (October 2026); types are typical of
-- the machine software (strings everywhere, MyISAM/InnoDB, utf8).
CREATE TABLE user_transaction (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    transactionid VARCHAR(64) NULL,
    user VARCHAR(64) NULL DEFAULT 'unknown',
    dateline INT NOT NULL,
    statecode VARCHAR(16) NULL,
    barcode VARCHAR(64) NULL,
    bors VARCHAR(32) NULL,
    weight VARCHAR(16) NULL,
    diam VARCHAR(16) NULL,
    metal VARCHAR(8) NULL,
    recognitionstatus VARCHAR(8) NULL,
    rebateordonate VARCHAR(8) NULL,
    print_barcode VARCHAR(64) NULL,
    payplatform VARCHAR(16) NULL,
    bottlevalue VARCHAR(16) NULL,
    charityid VARCHAR(32) NULL,
    charityname VARCHAR(64) NULL,
    octreceipt VARCHAR(8) NULL,
    transactiondone INT NOT NULL DEFAULT 0,
    uploaddone INT NOT NULL DEFAULT 0
) DEFAULT CHARSET=utf8;

CREATE TABLE empty_record (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    mid VARCHAR(64) NULL,
    dateline INT NOT NULL,
    bin_type VARCHAR(16) NOT NULL,
    barcode VARCHAR(100) NOT NULL
) DEFAULT CHARSET=utf8;

CREATE TABLE command (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    storage INT NOT NULL DEFAULT 100,
    storageplastic INT NOT NULL DEFAULT 100,
    storagecan INT NOT NULL DEFAULT 100,
    errorcode VARCHAR(32) NOT NULL DEFAULT '0',
    printer_barcode VARCHAR(64) NULL
) DEFAULT CHARSET=utf8;

CREATE TABLE barcode (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    barcode VARCHAR(64) NOT NULL,
    brand VARCHAR(100) NULL,
    bottleinfo VARCHAR(100) NULL,
    value VARCHAR(16) NULL,
    maxsdiam VARCHAR(16) NULL,
    minsdiam VARCHAR(16) NULL,
    maxbdiam VARCHAR(16) NULL,
    minbdiam VARCHAR(16) NULL,
    material_type VARCHAR(32) NULL,
    metal TINYINT NULL,
    capacity VARCHAR(16) NULL,
    weight VARCHAR(16) NULL,
    version VARCHAR(32) NULL,
    UNIQUE KEY barcode_unique (barcode)
) DEFAULT CHARSET=utf8;

CREATE TABLE printer_barcode (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    barcode VARCHAR(64) NOT NULL,
    UNIQUE KEY printer_barcode_unique (barcode)
) DEFAULT CHARSET=utf8;
