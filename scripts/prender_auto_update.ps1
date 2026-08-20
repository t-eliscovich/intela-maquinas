# prender_auto_update.ps1 — deja al server tirando del repo solo.
#
# Se corre UNA VEZ en el EC2. Despues de esto, todo entra por GitHub: se
# pushea a main y en menos de 2 minutos el server tiene el codigo nuevo.
#
# El 19/08/2026 el auto-updater estaba escrito y pusheado, pero la tarea
# nunca se habia registrado en el box: se pusheaba, el CI daba verde, y la
# pantalla seguia vieja sin que nada avisara. Esto es lo que faltaba.
#
# Es idempotente: se puede correr las veces que haga falta.

$ErrorActionPreference = "Stop"
$carpeta = "C:\maquinas_update"
$script  = Join-Path $carpeta "auto_update.ps1"
$tarea   = "MaquinasAutoUpdate"
$repo    = "https://raw.githubusercontent.com/t-eliscovich/intela-maquinas/main/scripts/auto_update.ps1"

# 1. La carpeta del updater vive AFUERA de C:\maquinas_app, que es la que se
#    reemplaza entera en cada actualizacion. Adentro se borraria a si mismo.
if (-not (Test-Path $carpeta)) { New-Item -ItemType Directory -Path $carpeta -Force | Out-Null }

# 2. Bajar la ultima version del updater desde GitHub.
#    OJO: raw.githubusercontent.com cachea hasta 5 minutos. Sin romper el
#    cache, uno pushea el arreglo, corre esto, y el server se baja igual la
#    version vieja — y el error que acabas de arreglar vuelve a aparecer.
#    Paso el 20/08/2026, dos veces seguidas.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$sinCache = $repo + "?cb=" + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
Invoke-WebRequest -UseBasicParsing -Uri $sinCache -OutFile $script -Headers @{ 'Cache-Control' = 'no-cache' }
Write-Output "updater bajado en $script"

# 3. Registrar la tarea: cada 2 minutos, como SYSTEM, sin limite de tiempo.
$accion = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
$gatillo = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 2)
$opciones = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

Unregister-ScheduledTask -TaskName $tarea -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $tarea -Action $accion -Trigger $gatillo `
    -Settings $opciones -User "SYSTEM" -RunLevel Highest | Out-Null
Write-Output "tarea $tarea registrada"

# 4. Correrla ahora mismo y mostrar como fue.
Start-ScheduledTask -TaskName $tarea
Start-Sleep 45
$log = Get-ChildItem "C:\maquinas_update\logs" -Filter "update-*.log" -EA SilentlyContinue |
       Sort-Object LastWriteTime | Select-Object -Last 1
if ($log) { Get-Content $log.FullName -Tail 12 } else { Write-Output "todavia no hay log" }

Write-Output "--- healthz ---"
try { (Invoke-WebRequest -UseBasicParsing http://localhost:5003/healthz).Content }
catch { Write-Output "no contesta: $($_.Exception.Message)" }
