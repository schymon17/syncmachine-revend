<?php
class Bootstrap {
    public function __construct(private Config $config, private Logger $log) {}
    public function run(): void {
        $cfg = $this->config->load();
        if (!isset($cfg['configDone']) || $cfg['configDone'] === true) {
            $this->log->log('INFO', 'Config already done');
            return;
        }
        if (empty($cfg['machineId'])) {
            echo "Enter machineId: ";
            $line = trim(fgets(STDIN));
            if ($line === '') {
                $this->log->log('ERROR', 'machineId not provided');
                throw new RuntimeException('machineId required');
            }
            $cfg['machineId'] = $line;
        }
        $baseUrl = $cfg['api']['baseUrl'] ?? '';
        if ($baseUrl === '') throw new RuntimeException('api.baseUrl is empty');
        $token = $cfg['api']['token'] ?? null;
        $http = new Http($baseUrl, $token);
        $payload = ["machineId"=>$cfg['machineId'], "integration"=>$cfg['integration'] ?? ''];
        $res = $http->postJson('/sync/bootstrap', $payload);
        if ($res['status'] < 200 || $res['status'] >= 300) {
            $this->log->log('ERROR', 'Bootstrap bad status', ['status'=>$res['status']]);
            throw new RuntimeException('Bootstrap status '.$res['status']);
        }
        $remote = is_array($res['body']) ? $res['body'] : [];
        foreach (['db','tables','api','sync','paths','integration'] as $k) if (isset($remote[$k])) $cfg[$k] = $remote[$k];
        $cfg['configDone'] = true;
        $this->config->save($cfg);
        $this->log->log('INFO', 'Config bootstrapped', ['machineId'=>$cfg['machineId']]);
        echo "Bootstrap completed.";
    }
}
