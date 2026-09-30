# debug: показать текст последнего события 1074 и проверить notify.ps1
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$e = Get-WinEvent -FilterHashtable @{ LogName = 'System'; Id = 1074 } -MaxEvents 1 -ErrorAction SilentlyContinue
if ($e) {
    Write-Output ("TIME: " + $e.TimeCreated)
    Write-Output ("HAS_REBOOT_imatch: " + ($e.Message -imatch 'перезагруз'))
    Write-Output "--- MESSAGE BEGIN ---"
    Write-Output $e.Message
    Write-Output "--- MESSAGE END ---"
} else {
    Write-Output "no 1074 event"
}

Write-Output "--- notify.ps1 txt line:"
Select-String -Path "$env:LOCALAPPDATA\Freyd\notify.ps1" -Pattern 'txt = if' |
    ForEach-Object { $_.Line }
Write-Output "--- client.ps1 txt line:"
Select-String -Path "$env:LOCALAPPDATA\Freyd\client.ps1" -Pattern 'txt = if' |
    ForEach-Object { $_.Line }