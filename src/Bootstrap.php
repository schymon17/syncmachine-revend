<?php
class Bootstrap {
    public function __construct(private Config $config, private Config $configBasic, private Logger $log) {}
    public function run(): void {
        $cfg = $this->config->load();
        $cfgB = $this->configBasic->load();

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

        $url = $cfgB['api']['baseUrl'] ?? '';
        if ($url === '') throw new RuntimeException('api.baseUrl is empty');
        $token = $cfgB['api']['token'] ?? null;
        if ($token === '') throw new RuntimeException('api.token is empty');

        $http = new Http($url, $token);
        $payload = [
            'machineId' => $cfg['machineId']
        ];
        $res = $http->postJsonBasic($url, $payload);

        if ($res['status'] < 200 || $res['status'] >= 300) {
            $this->log->log('ERROR', 'Bootstrap bad status', ['status'=>$res['status']]);
            throw new RuntimeException('Bootstrap status '.$res['status']);
        }

        $remote = is_array($res['body']) ? $res['body']['data']['attributes'] : [];
        if ($remote) {
            foreach (['db','api','sync','paths','integration', 'address', 'notification_emails', 'notification_emails_bcc'] as $k) if (isset($remote[$k])) $cfg[$k] = $remote[$k];
            $cfg['configDone'] = true;
            $this->config->save($cfg);
            $this->log->log('INFO', 'Config bootstrapped', ['machineId'=>$cfg['machineId']]);
            echo "Bootstrap completed.";
        } else {
            $this->log->log('ERROR', 'Bootstrap failed');
            echo "Bootstrap failed.";
        }
    }
}
