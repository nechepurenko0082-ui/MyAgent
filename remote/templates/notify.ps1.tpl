# Freyd notify - triggered by Windows event 1074 (system shutdown/restart).
# Posts "перезагружаюсь..."/"выключаюсь..." to the Freyd server -> Telegram.
# TLS + пиннинг сертификата. Started by scheduled task FreydNotify.

$Server = "https://5.129.212.74:8443"
$Token  = "__FREYD_TOKEN__"
$CertFp = "__FREYD_CERT_FP__"

$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
if (-not ("FreydPin" -as [type])) {
    Add-Type @"
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
public static class FreydPin {
    public static string Expected;
    public static bool Validate(object s, X509Certificate c, X509Chain ch, SslPolicyErrors e) {
        using (var sha = System.Security.Cryptography.SHA256.Create()) {
            var h = sha.ComputeHash(c.RawData);
            var fp = System.BitConverter.ToString(h).Replace("-", ":");
            return fp == Expected;
        }
    }
}
"@
}
[FreydPin]::Expected = $CertFp
[Net.ServicePointManager]::ServerCertificateValidationCallback =
    [Net.Security.RemoteCertificateValidationCallback][FreydPin]::Validate

try {
    $e = Get-WinEvent -FilterHashtable @{ LogName = 'System'; Id = 1074; StartTime = (Get-Date).AddMinutes(-5) } `
        -MaxEvents 1 -ErrorAction SilentlyContinue
    if (-not $e) { exit 0 }
    # вердикт (перезагрузка/выключение) принимает сервер по msg
    $o = @{ text = "system event"; msg = $e.Message }
    $body = $o | ConvertTo-Json
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
    Invoke-WebRequest -Uri "$Server/api/notify" -Method Post -Body $bytes `
        -ContentType "application/json; charset=utf-8" `
        -Headers @{ "X-Token" = $Token } -UseBasicParsing -TimeoutSec 10 | Out-Null
} catch { }
