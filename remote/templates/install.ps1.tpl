# Freyd client installer - puts the client into autostart (hidden background).
# Also registers FreydNotify task: Windows event 1074 -> Telegram "powering off/restarting".
# Run:  powershell -ExecutionPolicy Bypass -File install.ps1
# Remove: schtasks /delete /tn "FreydClient" /f ; schtasks /delete /tn "FreydNotify" /f

$Server = "http://5.129.212.74:8093"
$Token  = "__FREYD_TOKEN__"

$dir = "$env:LOCALAPPDATA\Freyd"
New-Item -ItemType Directory -Force -Path $dir | Out-Null

Write-Host "[1/6] Downloading client.ps1 and notify.ps1 to $dir ..."
Invoke-WebRequest -Uri "$Server/client.ps1?token=$Token" -OutFile "$dir\client.ps1" `
    -UseBasicParsing -TimeoutSec 30
Invoke-WebRequest -Uri "$Server/notify.ps1?token=$Token" -OutFile "$dir\notify.ps1" `
    -UseBasicParsing -TimeoutSec 30

Write-Host "[2/6] Stopping old client instances..."
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object { $_.CommandLine -like "*client.ps1*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1

Write-Host "[3/6] Registering scheduled task FreydClient (at logon)..."
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$dir\client.ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable
Register-ScheduledTask -TaskName "FreydClient" -Action $action -Trigger $trigger `
    -Settings $settings -Force | Out-Null

Write-Host "[4/6] Registering scheduled task FreydNotify (event 1074: shutdown/restart)..."
$notifyAction = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$dir\notify.ps1`""
$eventXml = @"
<QueryList>
  <Query Id="0" Path="System">
    <Select Path="System">*[System[(EventID=1074)]]</Select>
  </Query>
</QueryList>
"@
Register-ScheduledTask -TaskName "FreydNotify" -Action $notifyAction `
    -Trigger (New-CimInstance -CimClass (Get-CimClass -ClassName MSFT_TaskEventTrigger -Namespace Root/Microsoft/Windows/TaskScheduler) -ClientOnly -Property @{ Enabled = $true; Subscription = $eventXml }) `
    -Force | Out-Null

Write-Host "[5/6] Starting client now..."
Start-ScheduledTask -TaskName "FreydClient"
Start-Sleep -Seconds 3

Write-Host "[6/6] Verifying..."
$running = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object { $_.CommandLine -like "*Freyd\client.ps1*" }
$task = Get-ScheduledTask -TaskName "FreydNotify" -ErrorAction SilentlyContinue
if ($running) {
    Write-Host "DONE: client running in background." -ForegroundColor Green
} else {
    Write-Host "FAILED: client process not found." -ForegroundColor Red
}
if ($task) { Write-Host "FreydNotify task: OK" -ForegroundColor Green }
else { Write-Host "FreydNotify task: FAILED" -ForegroundColor Red }
