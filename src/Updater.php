<?php

class Updater {
    public function __construct(
        private array $cfg,
        private Logger $log,
        private string $rootPath,
        private Version $version
    ) {}

    public function maybeCheckForUpdate(bool $force = false): array {
        $updateCfg = $this->updateCfg();
        if (!($updateCfg['enabled'] ?? false)) {
            return ['skipped' => true, 'reason' => 'disabled'];
        }

        $interval = max(60, (int)($updateCfg['checkIntervalSeconds'] ?? 3600));
        $state = $this->readJsonFile($this->statePath());
        $lastCheckTs = (int)($state['lastCheckTs'] ?? 0);
        if (!$force && $lastCheckTs > 0 && (time() - $lastCheckTs) < $interval) {
            return ['skipped' => true, 'reason' => 'interval'];
        }

        $result = $this->checkForUpdate();
        $result['checkedAt'] = gmdate('c');
        $this->writeState([
            'lastCheckTs' => time(),
            'lastCheckAt' => $result['checkedAt'],
            'currentVersion' => $this->version->current(),
            'lastResult' => $result,
        ]);

        return $result;
    }

    public function prepareUpdate(array $checkResult): ?array {
        $isAvailable = (bool)($checkResult['updateAvailable'] ?? false);
        if (!$isAvailable) {
            return null;
        }

        $update = $checkResult['update'] ?? null;
        if (!is_array($update)) {
            throw new RuntimeException('Invalid update payload');
        }

        $version = (string)($update['version'] ?? '');
        if ($version === '' || !$this->version->isNewerThanCurrent($version)) {
            return null;
        }

        $packageUrl = (string)($update['packageUrl'] ?? '');
        if ($packageUrl === '') {
            throw new RuntimeException('Missing packageUrl in update payload');
        }

        $sha256 = (string)($update['sha256'] ?? '');
        $packagePath = $this->downloadPackage($packageUrl, $version, $sha256);

        $pending = [
            'version' => $version,
            'packageUrl' => $packageUrl,
            'packagePath' => $this->toRelativePath($packagePath),
            'sha256' => $sha256,
            'downloadedAt' => gmdate('c'),
        ];
        $this->writeJsonFile($this->pendingPath(), $pending);
        $this->log->log('INFO', 'Update package prepared', [
            'version' => $version,
            'packagePath' => $pending['packagePath'],
        ]);

        return $pending;
    }

    public function applyPendingUpdate(): array {
        $pending = $this->readJsonFile($this->pendingPath());
        if (!$pending) {
            throw new RuntimeException('No pending update');
        }

        $packagePath = $this->toAbsolutePath((string)($pending['packagePath'] ?? ''));
        if ($packagePath === '' || !file_exists($packagePath)) {
            throw new RuntimeException('Pending package file not found');
        }

        $expectedVersion = (string)($pending['version'] ?? '');
        $result = $this->applyPackage($packagePath, $expectedVersion);

        @unlink($this->pendingPath());
        $state = $this->readJsonFile($this->statePath());
        $state['lastInstalled'] = [
            'version' => $result['installedVersion'],
            'installedAt' => gmdate('c'),
        ];
        $this->writeState($state);

        return $result;
    }

    private function checkForUpdate(): array {
        $updateCfg = $this->updateCfg();
        $baseUrl = (string)($this->cfg['api']['baseUrl'] ?? '');
        $token = $this->cfg['api']['token'] ?? null;
        $http = new Http(
            $baseUrl,
            $token,
            max(1, (int)($this->cfg['api']['timeoutSeconds'] ?? 30)),
            max(1, (int)($this->cfg['api']['connectTimeoutSeconds'] ?? 10)),
            max(262144, (int)($this->cfg['api']['maxPayloadBytes'] ?? 5242880)),
            max(262144, (int)($this->cfg['api']['maxResponseBytes'] ?? 8388608))
        );

        $payload = [
            'machineId' => (string)($this->cfg['machineId'] ?? ''),
            'integration' => $this->cfg['integration'] ?? null,
            'currentVersion' => $this->version->current(),
            'channel' => (string)($updateCfg['channel'] ?? 'stable'),
            'platform' => PHP_OS_FAMILY,
            'runtime' => 'php-cli',
            'timestamp' => gmdate('c'),
        ];

        $checkUrl = (string)($updateCfg['checkUrl'] ?? '');
        $checkPath = (string)($updateCfg['checkPath'] ?? '/update/check');
        $res = $checkUrl !== '' ? $http->postJsonBasic($checkUrl, $payload) : $http->postJson($checkPath, $payload);

        $status = (int)($res['status'] ?? 0);
        if ($status < 200 || $status >= 300) {
            throw new RuntimeException('Update check bad status ' . $status);
        }

        $body = is_array($res['body']) ? $res['body'] : [];
        $raw = $this->extractUpdatePayload($body);
        $remoteVersion = (string)($raw['version'] ?? '');
        $explicit = $raw['updateAvailable'] ?? null;
        $available = is_bool($explicit)
            ? $explicit
            : ($remoteVersion !== '' && version_compare($remoteVersion, $this->version->current(), '>'));

        if ($remoteVersion !== '' && !version_compare($remoteVersion, $this->version->current(), '>')) {
            $available = false;
        }

        return [
            'updateAvailable' => $available,
            'currentVersion' => $this->version->current(),
            'update' => [
                'version' => $remoteVersion,
                'packageUrl' => (string)($raw['packageUrl'] ?? $raw['url'] ?? ''),
                'sha256' => (string)($raw['sha256'] ?? ''),
                'notes' => (string)($raw['notes'] ?? ''),
                'minSupportedVersion' => (string)($raw['minSupportedVersion'] ?? ''),
            ],
            'raw' => $body,
        ];
    }

    private function extractUpdatePayload(array $body): array {
        if (isset($body['data']['attributes']) && is_array($body['data']['attributes'])) {
            return $body['data']['attributes'];
        }
        if (isset($body['data']) && is_array($body['data'])) {
            return $body['data'];
        }
        return $body;
    }

    private function applyPackage(string $zipPath, string $expectedVersion = ''): array {
        if (!class_exists('ZipArchive')) {
            throw new RuntimeException('ZipArchive extension is required for apply-update');
        }

        $zip = new ZipArchive();
        if ($zip->open($zipPath) !== true) {
            throw new RuntimeException('Cannot open update package: ' . $zipPath);
        }

        $stagingDir = $this->absPath('data/updates/staging/' . gmdate('Ymd_His'));
        @mkdir($stagingDir, 0777, true);
        if (!$zip->extractTo($stagingDir)) {
            $zip->close();
            throw new RuntimeException('Cannot extract update package');
        }
        $zip->close();

        $manifestPath = $stagingDir . '/update.manifest.json';
        $manifest = $this->readJsonFile($manifestPath);
        if (!$manifest) {
            throw new RuntimeException('Missing update.manifest.json in package');
        }

        $version = (string)($manifest['version'] ?? '');
        if ($version === '') {
            throw new RuntimeException('Invalid manifest: missing version');
        }
        if ($expectedVersion !== '' && $version !== $expectedVersion) {
            throw new RuntimeException('Manifest version mismatch');
        }

        $currentVersion = $this->version->current();
        $minSupported = (string)($manifest['minSupportedVersion'] ?? '');
        if ($minSupported !== '' && version_compare($currentVersion, $minSupported, '<')) {
            throw new RuntimeException(
                'Current version ' . $currentVersion . ' is lower than minSupportedVersion ' . $minSupported
            );
        }
        if (!version_compare($version, $currentVersion, '>')) {
            throw new RuntimeException('Package version is not newer than current version');
        }

        $files = $manifest['files'] ?? null;
        if (!is_array($files) || !$files) {
            throw new RuntimeException('Invalid manifest: files[] is required');
        }

        $backupDir = $this->absPath('data/updates/backups/' . gmdate('Ymd_His'));
        @mkdir($backupDir, 0777, true);

        $updatedCount = 0;
        foreach ($files as $file) {
            if (!is_array($file)) {
                continue;
            }

            $from = (string)($file['from'] ?? '');
            $to = (string)($file['to'] ?? '');
            $sha256 = (string)($file['sha256'] ?? '');

            if (!$this->isSafeRelativePath($from) || !$this->isSafeRelativePath($to)) {
                throw new RuntimeException('Unsafe file path in manifest');
            }
            if ($this->isBlockedTarget($to)) {
                throw new RuntimeException('Blocked destination path: ' . $to);
            }

            $sourcePath = $stagingDir . '/' . str_replace('\\', '/', $from);
            $targetPath = $this->absPath($to);
            if (!file_exists($sourcePath)) {
                throw new RuntimeException('Source file missing in package: ' . $from);
            }
            if ($sha256 !== '' && hash_file('sha256', $sourcePath) !== strtolower($sha256)) {
                throw new RuntimeException('SHA256 mismatch for file: ' . $from);
            }

            if (file_exists($targetPath)) {
                $backupTarget = $backupDir . '/' . str_replace('\\', '/', $to);
                @mkdir(dirname($backupTarget), 0777, true);
                if (!@copy($targetPath, $backupTarget)) {
                    throw new RuntimeException('Failed to backup target file: ' . $to);
                }
            }

            @mkdir(dirname($targetPath), 0777, true);
            if (!@copy($sourcePath, $targetPath)) {
                throw new RuntimeException('Failed to replace target file: ' . $to);
            }
            $updatedCount++;
        }

        if (!file_exists($this->absPath('VERSION'))) {
            file_put_contents($this->absPath('VERSION'), $version . PHP_EOL);
        } else {
            $versionPath = $this->absPath('VERSION');
            $versionFromPackage = false;
            foreach ($files as $f) {
                if (!is_array($f)) continue;
                if ((string)($f['to'] ?? '') === 'VERSION') {
                    $versionFromPackage = true;
                    break;
                }
            }
            if (!$versionFromPackage) {
                file_put_contents($versionPath, $version . PHP_EOL);
            }
        }

        $this->log->log('INFO', 'Update applied', [
            'fromVersion' => $currentVersion,
            'toVersion' => $version,
            'updatedFiles' => $updatedCount,
            'backupDir' => $this->toRelativePath($backupDir),
        ]);

        return [
            'installedVersion' => $version,
            'previousVersion' => $currentVersion,
            'updatedFiles' => $updatedCount,
            'backupDir' => $this->toRelativePath($backupDir),
        ];
    }

    private function downloadPackage(string $url, string $version, string $expectedSha256 = ''): string {
        if (!function_exists('curl_init')) {
            throw new RuntimeException('cURL extension is required for update downloads');
        }

        $dir = $this->absPath('data/updates/downloads');
        @mkdir($dir, 0777, true);

        $safeVersion = preg_replace('/[^0-9A-Za-z._-]/', '_', $version) ?: 'unknown';
        $out = $dir . '/update-' . $safeVersion . '.zip';
        $tmp = $out . '.tmp';

        $fh = fopen($tmp, 'wb');
        if ($fh === false) {
            throw new RuntimeException('Cannot create temp update file');
        }

        $updateCfg = $this->updateCfg();
        $maxBytes = max(1048576, (int)($updateCfg['maxPackageBytes'] ?? 104857600));

        $headers = [];
        $token = (string)($this->cfg['api']['token'] ?? '');
        if ($token !== '') {
            $headers[] = 'x-api-key-machine: ' . $token;
        }

        $received = 0;
        $ch = curl_init($url);
        if ($ch === false) {
            fclose($fh);
            @unlink($tmp);
            throw new RuntimeException('curl_init failed');
        }

        curl_setopt_array($ch, [
            CURLOPT_FOLLOWLOCATION => true,
            CURLOPT_CONNECTTIMEOUT => max(1, (int)($this->cfg['api']['connectTimeoutSeconds'] ?? 10)),
            CURLOPT_TIMEOUT => max(1, (int)($this->cfg['api']['timeoutSeconds'] ?? 120)),
            CURLOPT_HTTPHEADER => $headers,
            CURLOPT_WRITEFUNCTION => static function ($ch, string $chunk) use ($fh, &$received, $maxBytes) {
                $len = strlen($chunk);
                $received += $len;
                if ($received > $maxBytes) {
                    return 0;
                }
                return fwrite($fh, $chunk);
            },
        ]);

        $ok = curl_exec($ch);
        $errno = curl_errno($ch);
        $err = curl_error($ch);
        $status = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);
        fclose($fh);

        if ($ok === false || $status < 200 || $status >= 300) {
            @unlink($tmp);
            if ($errno === CURLE_WRITE_ERROR && $received > $maxBytes) {
                throw new RuntimeException('Update package too large');
            }
            throw new RuntimeException('Update download failed: HTTP ' . $status . ' ' . $err);
        }

        @rename($tmp, $out);

        if ($expectedSha256 !== '') {
            $hash = strtolower(hash_file('sha256', $out));
            if ($hash !== strtolower($expectedSha256)) {
                @unlink($out);
                throw new RuntimeException('Downloaded package SHA256 mismatch');
            }
        }

        return $out;
    }

    private function statePath(): string {
        return $this->absPath((string)($this->updateCfg()['stateFile'] ?? 'data/update.state.json'));
    }

    private function pendingPath(): string {
        return $this->absPath((string)($this->updateCfg()['pendingFile'] ?? 'data/update.pending.json'));
    }

    private function writeState(array $state): void {
        $this->writeJsonFile($this->statePath(), $state);
    }

    private function updateCfg(): array {
        $cfg = $this->cfg['update'] ?? [];
        return is_array($cfg) ? $cfg : [];
    }

    private function isBlockedTarget(string $target): bool {
        $t = str_replace('\\', '/', ltrim($target, '/'));
        $blocked = ['data/', 'php/', '.git/'];
        foreach ($blocked as $prefix) {
            if (str_starts_with($t, $prefix)) {
                return true;
            }
        }
        return false;
    }

    private function isSafeRelativePath(string $path): bool {
        $path = str_replace('\\', '/', trim($path));
        if ($path === '' || str_starts_with($path, '/') || preg_match('/^[A-Za-z]:\//', $path)) {
            return false;
        }

        foreach (explode('/', $path) as $segment) {
            if ($segment === '' || $segment === '.' || $segment === '..') {
                return false;
            }
        }
        return true;
    }

    private function absPath(string $relative): string {
        $relative = str_replace('\\', '/', ltrim($relative, '/'));
        return rtrim($this->rootPath, '/\\') . '/' . $relative;
    }

    private function toRelativePath(string $absolute): string {
        $root = rtrim(str_replace('\\', '/', $this->rootPath), '/') . '/';
        $abs = str_replace('\\', '/', $absolute);
        return str_starts_with($abs, $root) ? substr($abs, strlen($root)) : $absolute;
    }

    private function toAbsolutePath(string $path): string {
        if ($path === '') {
            return '';
        }
        if (str_starts_with($path, '/') || preg_match('/^[A-Za-z]:[\\\\\\/]/', $path)) {
            return $path;
        }
        return $this->absPath($path);
    }

    private function readJsonFile(string $path): array {
        try {
            if (!file_exists($path)) {
                return [];
            }
            $raw = file_get_contents($path);
            if ($raw === false || trim($raw) === '') {
                return [];
            }
            $decoded = json_decode($raw, true);
            return is_array($decoded) ? $decoded : [];
        } catch (Throwable) {
            return [];
        }
    }

    private function writeJsonFile(string $path, array $data): void {
        @mkdir(dirname($path), 0777, true);
        $encoded = json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
        if ($encoded === false) {
            throw new RuntimeException('JSON encode failed while writing ' . $path);
        }
        file_put_contents($path, $encoded . PHP_EOL, LOCK_EX);
    }
}
