<?php
class Db {
    public static function pdo(array $cfg): PDO {
        $host = $cfg['host']; $db = $cfg['database']; $port = (int)$cfg['port'];
        $dsn = "mysql:host=$host;port=$port;dbname=$db;charset=utf8mb4";
        $pdo = new PDO($dsn, $cfg['username'], $cfg['password'], [PDO::ATTR_ERRMODE=>PDO::ERRMODE_EXCEPTION]);
        return $pdo;
    }
}
