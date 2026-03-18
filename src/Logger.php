<?php
class Logger {
    public function __construct(private string $file) {
        try {
            $dir = dirname($file);
            if (!is_dir($dir)) {
                @mkdir($dir, 0777, true);
            }
        } catch (Throwable $e) {
            error_log('[sync-logger] init failed: '.$e->getMessage());
        }
    }

    public function log(string $level, string $message, array $ctx = []): void {
        $row = ["ts"=>gmdate('c'), "level"=>strtoupper($level), "message"=>$message, "ctx"=>$ctx];
        $line = json_encode($row, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        if ($line === false) {
            $line = json_encode([
                'ts' => gmdate('c'),
                'level' => 'ERROR',
                'message' => 'Logger JSON encode failed',
                'ctx' => ['jsonError' => json_last_error_msg()],
            ], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
            if ($line === false) {
                $line = '{"ts":"'.gmdate('c').'","level":"ERROR","message":"Logger failed to serialize entry","ctx":{}}';
            }
        }

        try {
            $written = @file_put_contents($this->file, $line.PHP_EOL, FILE_APPEND | LOCK_EX);
            if ($written === false) {
                error_log('[sync-logger] write failed for '.$this->file);
            }
        } catch (Throwable $e) {
            error_log('[sync-logger] write exception: '.$e->getMessage());
        }
    }
}
