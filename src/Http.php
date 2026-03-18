<?php
class Http {
    private string $baseUrl;
    private ?string $token;
    private int $timeoutSeconds;
    private int $connectTimeoutSeconds;

    public function __construct(string $baseUrl, ?string $token, int $timeoutSeconds = 30, int $connectTimeoutSeconds = 10) {
        $this->baseUrl = rtrim($baseUrl, '/');
        $this->token = $token;
        $this->timeoutSeconds = max(1, $timeoutSeconds);
        $this->connectTimeoutSeconds = max(1, $connectTimeoutSeconds);
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
        $encoded = json_encode($payload);
        if ($encoded === false) {
            throw new RuntimeException('JSON encode failed: '.json_last_error_msg());
        }

        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_CONNECTTIMEOUT => $this->connectTimeoutSeconds,
            CURLOPT_TIMEOUT => $this->timeoutSeconds,
            CURLOPT_POST => true,
            CURLOPT_HTTPHEADER => $headers,
            CURLOPT_POSTFIELDS => $encoded,
        ]);

        $body = curl_exec($ch);
        $err  = curl_error($ch);
        $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);

        curl_close($ch);

        if ($err) throw new RuntimeException("HTTP error: $err");
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
