<?php
class Logger {
    public function __construct(private string $file) {
        if (!is_dir(dirname($file))) mkdir(dirname($file), 0777, true);
    }
    public function log(string $level, string $message, array $ctx = []): void {
        $row = ["ts"=>gmdate('c'), "level"=>strtoupper($level), "message"=>$message, "ctx"=>$ctx];
        file_put_contents($this->file, json_encode($row).PHP_EOL, FILE_APPEND);
    }
}
