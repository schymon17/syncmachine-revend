<?php
class DbDiff {
    public static function rowHash(array $row, array $columns): string {
        $parts = [];
        foreach ($columns as $c) $parts[] = (string)($row[$c] ?? '');
        return hash('sha256', implode('|', $parts));
    }
    public static function diff(PDO $pdo, array $tableCfg, array $snapshot): array {
        $name = $tableCfg['name']; $pk = $tableCfg['pk']; $cols = $tableCfg['columns'];
        $colList = implode(',', array_map(fn($c)=>"`$c`", array_unique(array_merge([$pk], $cols))));
        $stmt = $pdo->query("SELECT $colList FROM `$name`");
        $current = [];
        while ($row = $stmt->fetch(PDO::FETCH_ASSOC)) {
            $id = (string)$row[$pk];
            $hash = self::rowHash($row, $cols);
            $current[$id] = ["hash"=>$hash, "row"=>$row];
        }
        $prev = $snapshot[$name] ?? [];
        $inserted=[]; $updated=[]; $deleted=[];
        foreach ($current as $id=>$info) {
            if (!isset($prev[$id])) $inserted[] = $info['row'];
            elseif (($prev[$id]['hash'] ?? '') !== $info['hash']) $updated[] = $info['row'];
        }
        foreach ($prev as $id=>$_) if (!isset($current[$id])) $deleted[] = [$pk=>$id];
        $newSection = []; foreach ($current as $id=>$info) $newSection[$id] = ["hash"=>$info['hash']];
        return [["inserted"=>$inserted,"updated"=>$updated,"deleted"=>$deleted], $newSection];
    }
}
