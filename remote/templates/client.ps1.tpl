# Freyd remote client for Windows (PowerShell) — TLS + cert pinning.
# Канал: https с проверкой отпечатка сертификата (провайдер видит только шифротрафик).
# Самообновление: при старте сверяет хэши файлов с сервером и при расхождении
# сам стягивает новую версию и перезапускается.
# Фон 24/7: задача планировщика FreydClient (см. install.ps1).
# Ручной запуск: powershell -ExecutionPolicy Bypass -File client.ps1

$Server  = "https://5.129.212.74:8443"
$Token   = "__FREYD_TOKEN__"
$CertFp  = "__FREYD_CERT_FP__"
$PollWait = 25   # секунд long-poll

$dir = "$env:LOCALAPPDATA\Freyd"
$ProgressPreference = "SilentlyContinue"

# --- TLS 1.2 + пиннинг: принимаем ТОЛЬКО наш сертификат по SHA256-отпечатку ---
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

function Get-Sha256($path) {
    (Get-FileHash -Path $path -Algorithm SHA256).Hash.ToUpper()
}

# --- самообновление ---
function Try-SelfUpdate {
    try {
        $myHash = Get-Sha256 $PSCommandPath
        $notifyHash = ""
        if (Test-Path "$dir\notify.ps1") { $notifyHash = Get-Sha256 "$dir\notify.ps1" }
        $meta = (Invoke-WebRequest -Uri "$Server/api/client_meta?h_client=$myHash&h_notify=$notifyHash" `
            -Headers @{ "X-Token" = $Token } -UseBasicParsing -TimeoutSec 20).Content | ConvertFrom-Json
        if (-not $meta.update) { return $false }

        Write-Host "[*] доступно обновление, стягиваю..." -ForegroundColor Cyan
        Invoke-WebRequest -Uri "$Server/client.ps1?token=$Token" -OutFile "$dir\client.ps1.new" -UseBasicParsing -TimeoutSec 60
        Invoke-WebRequest -Uri "$Server/notify.ps1?token=$Token" -OutFile "$dir\notify.ps1.new" -UseBasicParsing -TimeoutSec 60
        Invoke-WebRequest -Uri "$Server/update.ps1?token=$Token" -OutFile "$dir\update.ps1" -UseBasicParsing -TimeoutSec 60

        if ((Get-Sha256 "$dir\client.ps1.new") -eq $meta.sha_client -and
            (Get-Sha256 "$dir\notify.ps1.new") -eq $meta.sha_notify) {
            Write-Host "[*] хэши совпали, перезапуск с новой версией" -ForegroundColor Green
            Start-Process powershell.exe -WindowStyle Hidden -ArgumentList `
                "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$dir\update.ps1`""
            return $true
        }
        Write-Host "[!] хэши не сошлись, обновление отменено" -ForegroundColor Yellow
    } catch {
        Write-Host "[!] self-update: $($_.Exception.Message)" -ForegroundColor Yellow
    }
    return $false
}

if (Try-SelfUpdate) { exit 0 }

function Send-Result($id, $cmd, $output, $exitCode) {
    $body = @{ id = $id; cmd = $cmd; output = $output; exit_code = $exitCode } | ConvertTo-Json -Depth 3
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
    try {
        Invoke-WebRequest -Uri "$Server/api/result" -Method Post -Body $bytes `
            -ContentType "application/json; charset=utf-8" `
            -Headers @{ "X-Token" = $Token } -UseBasicParsing -TimeoutSec 30 | Out-Null
    } catch {
        Write-Host "[!] не удалось отправить результат: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

function Send-Notify($text, $msg = "") {
    $o = @{ text = $text }
    if ($msg) { $o.msg = $msg }
    $body = $o | ConvertTo-Json
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
    try {
        Invoke-WebRequest -Uri "$Server/api/notify" -Method Post -Body $bytes `
            -ContentType "application/json; charset=utf-8" `
            -Headers @{ "X-Token" = $Token } -UseBasicParsing -TimeoutSec 8 | Out-Null
    } catch {
        Write-Host "[!] notify failed: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

# --- фоновый наблюдатель: событие System/1074 = выключение или перезагрузка ---
$watchJob = Start-Job -ScriptBlock {
    param($Server, $Token)
    $lastId = $null
    while ($true) {
        try {
            $e = Get-WinEvent -FilterHashtable @{ LogName = 'System'; Id = 1074 } -MaxEvents 1 -ErrorAction SilentlyContinue
            if ($e -and $e.RecordId -ne $lastId -and ((Get-Date) - $e.TimeCreated).TotalSeconds -lt 120) {
                $lastId = $e.RecordId
                $o = @{ text = "system event"; msg = $e.Message } | ConvertTo-Json
                $bytes = [System.Text.Encoding]::UTF8.GetBytes($o)
                Invoke-WebRequest -Uri "$Server/api/notify" -Method Post -Body $bytes `
                    -ContentType "application/json; charset=utf-8" `
                    -Headers @{ "X-Token" = $Token } -UseBasicParsing -TimeoutSec 8 | Out-Null
            }
        } catch { }
        Start-Sleep -Seconds 3
    }
} -ArgumentList $Server, $Token

Write-Host "Freyd client (TLS) -> $Server" -ForegroundColor Cyan
Send-Notify "включаюсь..."

$reconnectDelay = 3
while ($true) {
    try {
        $r = Invoke-WebRequest -Uri "$Server/api/poll?wait=$PollWait" `
            -Headers @{ "X-Token" = $Token } -UseBasicParsing -TimeoutSec ($PollWait + 15)
        $poll = $r.Content | ConvertFrom-Json
        $reconnectDelay = 3
    } catch {
        Write-Host "[!] нет связи с сервером, повтор через $reconnectDelay c..." -ForegroundColor Yellow
        Start-Sleep -Seconds $reconnectDelay
        if ($reconnectDelay -lt 60) { $reconnectDelay = $reconnectDelay * 2 }
        continue
    }

    if (-not $poll.id) { continue }   # команда не пришла, крутим дальше

    Write-Host "[$(Get-Date -Format HH:mm:ss)] -> $($poll.cmd)" -ForegroundColor Green

    # выполняем через cmd, перенаправляя stderr в stdout
    $output = ""
    $exitCode = 0
    try {
        $output = cmd /c "$($poll.cmd) 2>&1" | Out-String
        $exitCode = $LASTEXITCODE
        if ($null -eq $exitCode) { $exitCode = 0 }
    } catch {
        $output = $_.Exception.Message
        $exitCode = 1
    }
    if ([string]::IsNullOrEmpty($output)) { $output = "(пустой вывод)" }

    Write-Host "    exit=$exitCode, $($output.Length) символов" -ForegroundColor DarkGray
    Send-Result $poll.id $poll.cmd $output $exitCode
}
