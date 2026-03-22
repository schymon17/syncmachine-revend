<?php

class Version {
    public function __construct(private string $rootPath) {}

    public function current(): string {
        $versionFile = rtrim($this->rootPath, '/\\') . '/VERSION';
        if (!file_exists($versionFile)) {
            return '0.0.0';
        }

        $raw = trim((string)file_get_contents($versionFile));
        return $raw !== '' ? $raw : '0.0.0';
    }

    public function isNewerThanCurrent(string $candidate): bool {
        return version_compare($candidate, $this->current(), '>');
    }
}
