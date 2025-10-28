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
        if (!($this->cfg['sync']['enabled'] ?? false)) { $this->log->log('INFO','Sync disabled'); return; }
        $machineId = $this->cfg['machineId'] ?? '';
        if ($machineId==='') { $this->log->log('ERROR','Machine ID empty'); return; }
        $paths = $this->cfg['paths'];
        $snapshotFile = $paths['snapshot']; $queueFile = $paths['queue'];
        if (!is_dir(dirname($snapshotFile))) mkdir(dirname($snapshotFile),0777,true);
        if (!is_dir(dirname($queueFile))) mkdir(dirname($queueFile),0777,true);

        try { $pdo = Db::pdo($this->cfg['db']); }
        catch (Throwable $e) { $this->log->log('ERROR','DB connect failed',['error'=>$e->getMessage()]); return; }

        $snap = file_exists($snapshotFile) ? json_decode(file_get_contents($snapshotFile), true): [];
        $http = new Http($this->cfg['api']['baseUrl'] ?? '', $this->cfg['api']['token'] ?? null);

        $newSnap = $snap; $any = false;
        foreach ($this->cfg['tables'] as $t) {
            try {
                [$diff, $section] = DbDiff::diff($pdo, $t, $snap);
                $newSnap[$t['name']] = $section;
                if (count($diff['inserted']) || count($diff['updated']) || count($diff['deleted'])) {
                    $any = true;
                    $payload = ["machineId"=>$machineId, "table"=>$t['name'], "timestamp"=>gmdate('c'), "changes"=>$diff];
                    try {
                        $res = $http->postJson('/sync/changes', $payload);
                        if ($res['status'] >= 200 && $res['status'] < 300) $this->log->log('INFO','Pushed changes',['table'=>$t['name']]);
                        else throw new RuntimeException('Bad status '.$res['status']);
                    } catch (Throwable $e) {
                        $this->queueOffline($queueFile, $payload);
                        $this->log->log('WARN','Queued changes (offline)',['table'=>$t['name'],'error'=>$e->getMessage()]);
                    }
                } else {
                    $this->log->log('INFO','No changes',['table'=>$t['name']]);
                }
            } catch (Throwable $e) {
                $this->log->log('ERROR','Diff error',['table'=>$t['name'] ?? 'unknown','error'=>$e->getMessage()]);
            }
        }
        file_put_contents($snapshotFile, json_encode($newSnap, JSON_PRETTY_PRINT));
        if (!$any) {
            $payload = ["machineId"=>$machineId, "timestamp"=>gmdate('c'), "kind"=>"heartbeat"];
            try { $res = $http->postJson('/sync/changes', $payload);
                  if ($res['status']>=200 && $res['status']<300) $this->log->log('INFO','Heartbeat sent');
                  else throw new RuntimeException('Bad status '.$res['status']);
            } catch (Throwable $e) {
                $this->queueOffline($queueFile, $payload);
                $this->log->log('WARN','Heartbeat queued',['error'=>$e->getMessage()]);
            }
        }
        $this->flushQueue($http, $queueFile);
    }
}
