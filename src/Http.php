<?php
class Http {
    private string $baseUrl;
    private ?string $token;
    public function __construct(string $baseUrl, ?string $token) {
        $this->baseUrl = rtrim($baseUrl, '/');
        $this->token = $token;
    }
    public function postJson(string $path, array $payload): array {
        $url = $this->baseUrl . '/' . ltrim($path, '/');
        $ch = curl_init($url);
        $headers = ['Content-Type: application/json'];
        if ($this->token) $headers[] = 'Authorization: Bearer ' . $this->token;
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT => 20,
            CURLOPT_POST => true,
            CURLOPT_HTTPHEADER => $headers,
            CURLOPT_POSTFIELDS => json_encode($payload)
        ]);
        $body = curl_exec($ch);
        $err  = curl_error($ch);
        $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);
        if ($err) throw new RuntimeException("HTTP error: $err");
        return ["status"=>$code, "body"=>$body? json_decode($body,true):null];
    }
}
