<?php
class Http {
    private string $baseUrl;
    private ?string $token;
    private int $timeoutSeconds;
    private int $connectTimeoutSeconds;
    private int $maxPayloadBytes;
    private int $maxResponseBytes;

    public function __construct(
        string $baseUrl,
        ?string $token,
        int $timeoutSeconds = 30,
        int $connectTimeoutSeconds = 10,
        int $maxPayloadBytes = 5242880,
        int $maxResponseBytes = 8388608
    ) {
        $this->baseUrl = rtrim($baseUrl, '/');
        $this->token = $token;
        $this->timeoutSeconds = max(1, $timeoutSeconds);
        $this->connectTimeoutSeconds = max(1, $connectTimeoutSeconds);
        $this->maxPayloadBytes = max(262144, $maxPayloadBytes);
        $this->maxResponseBytes = max(262144, $maxResponseBytes);
    }

    private function estimatePayloadBytes(mixed $value, int $depth = 0): int {
        if ($depth > 25) return 0;

        if ($value === null) return 4;
        if (is_bool($value)) return $value ? 4 : 5;
        if (is_int($value) || is_float($value)) return 24;
        if (is_string($value)) return strlen($value) + 2;
        if (!is_array($value)) return 16;

        $bytes = 2; // [] or {}
        foreach ($value as $k => $v) {
            $bytes += is_int($k) ? 2 : (strlen((string)$k) + 4);
            $bytes += $this->estimatePayloadBytes($v, $depth + 1) + 1;
            if ($bytes > $this->maxPayloadBytes) {
                return $bytes;
            }
        }

        return $bytes;
    }

    private function postJsonToUrl(string $url, array $payload): array {
        $ch = curl_init($url);
        if ($ch === false) {
            throw new RuntimeException('curl_init failed');
        }

        curl_setopt($ch, CURLOPT_SSL_VERIFYPEER, false);
        curl_setopt($ch, CURLOPT_SSL_VERIFYHOST, 0);

        $headers = ['Content-Type: application/json'];
        if (!empty($this->token)) {
            $headers[] = 'x-api-key-machine: ' . $this->token;
        }

        $estimatedBytes = $this->estimatePayloadBytes($payload);
        if ($estimatedBytes > $this->maxPayloadBytes) {
            throw new RuntimeException(
                'Payload too large for request: estimated '.$estimatedBytes.' bytes, max '.$this->maxPayloadBytes
            );
        }

        $encoded = json_encode($payload);
        if ($encoded === false) {
            throw new RuntimeException('JSON encode failed: '.json_last_error_msg());
        }

        $body = '';
        $receivedBytes = 0;
        $maxResponseBytes = $this->maxResponseBytes;

        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => false,
            CURLOPT_CONNECTTIMEOUT => $this->connectTimeoutSeconds,
            CURLOPT_TIMEOUT => $this->timeoutSeconds,
            CURLOPT_POST => true,
            CURLOPT_HTTPHEADER => $headers,
            CURLOPT_POSTFIELDS => $encoded,
        ]);
        curl_setopt($ch, CURLOPT_WRITEFUNCTION, static function ($ch, string $chunk) use (&$body, &$receivedBytes, $maxResponseBytes) {
            $len = strlen($chunk);
            $receivedBytes += $len;
            if ($receivedBytes > $maxResponseBytes) {
                return 0;
            }
            $body .= $chunk;
            return $len;
        });

        $ok = curl_exec($ch);
        $err  = curl_error($ch);
        $errno = curl_errno($ch);
        $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);

        curl_close($ch);

        if ($ok === false || $err) {
            if ($errno === CURLE_WRITE_ERROR && $receivedBytes > $this->maxResponseBytes) {
                throw new RuntimeException(
                    'HTTP response too large: received '.$receivedBytes.' bytes, max '.$this->maxResponseBytes
                );
            }
            throw new RuntimeException("HTTP error: $err");
        }
        return ["status"=>$code, "body"=>$body? json_decode($body,true):null];
    }

    public function postJson(string $path, array $payload): array {
        $url = $this->baseUrl . '/' . ltrim($path, '/');
        return $this->postJsonToUrl($url, $payload);
    }

    public function postJsonBasic(string $path, array $payload): array {
        return $this->postJsonToUrl($path, $payload);
    }
}
