<?php
class Config {
    public function __construct(private string $file) {}
    public function load(): array {
        if (!file_exists($this->file)) throw new RuntimeException("Config not found: {$this->file}");
        $data = json_decode(file_get_contents($this->file), true);
        if (!is_array($data)) throw new RuntimeException("Invalid config JSON");
        return $data;
    }
    public function save(array $data): void {
        if (!is_dir(dirname($this->file))) mkdir(dirname($this->file), 0777, true);
        file_put_contents($this->file, json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES));
    }
    public function path(): string { return $this->file; }
}
