# auto_update.ps1 — el server se actualiza solo desde GitHub.
#
# Corre cada 2 minutos como tarea programada (MaquinasAutoUpdate).
# Mira el ultimo commit de main; si cambio, baja el codigo, lo instala,
# reinicia la app y verifica que conteste. Si NO contesta, vuelve atras.
#
# Por que asi y no con GitHub Actions: Actions necesitaria llaves de AWS
# guardadas en el repo. Como el repo es publico, el server puede TIRAR del
# codigo sin ninguna credencial. Menos piezas, nada que rotar, nada que filtrar.
#
# IMPORTANTE: este script vive en C:\maquinas_update\, NO en C:\maquinas_app\.
# La actualizacion borra y reescribe C:\maquinas_app entero; si el script
# viviera adentro se borraria a si mismo a mitad de camino. Paso exactamente
# eso en el primer intento (2026-08-19).
#
# Nada de esto toca formulas_app, Programa Core ni Metabase.

$ErrorActionPreference = "Stop"
$repo    = "t-eliscovich/intela-maquinas"
$app     = "C:\maquinas_app"
$prev    = "C:\maquinas_app.prev"
$marca   = "C:\maquinas_update\.commit"
$tarea   = "MaquinasApp"
$puerto  = 5003
$logs    = "C:\maquinas_update\logs"
if (-not (Test-Path $logs)) { New-Item -ItemType Directory -Path $logs -Force | Out-Null }
$log = Join-Path $logs ("update-" + (Get-Date -Format "yyyy-MM") + ".log")
function Escribir($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" | Out-File -Append -Encoding UTF8 $log }

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $sha = (Invoke-RestMethod -UseBasicParsing -TimeoutSec 30 `
            -Uri "https://api.github.com/repos/$repo/commits/main" `
            -Headers @{ 'User-Agent' = 'maquinas-auto-update' }).sha
} catch {
    Escribir "no pude consultar GitHub: $($_ | Out-String)"
    exit 0    # sin internet no es un error nuestro; se reintenta en 2 min
}

$actual = if (Test-Path $marca) { (Get-Content $marca -Raw).Trim() } else { "" }
if ($sha -eq $actual) { exit 0 }        # nada nuevo: salir en silencio

Escribir "commit nuevo $($sha.Substring(0,8)) (tenia '$(if($actual){$actual.Substring(0,8)}else{'ninguno'})') - actualizando"

$tmp = Join-Path $env:TEMP ("maq_" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
try {
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 120 `
        -Uri "https://codeload.github.com/$repo/tar.gz/refs/heads/main" `
        -OutFile "$tmp\src.tar.gz"
    tar -xzf "$tmp\src.tar.gz" -C $tmp
    $nuevo = Get-ChildItem $tmp -Directory | Where-Object { $_.Name -like "intela-maquinas-*" } | Select-Object -First 1
    if (-not $nuevo) { throw "el tarball no traia la carpeta esperada" }

    # Guardar la version que anda, para poder volver.
    if (Test-Path $prev) { Remove-Item $prev -Recurse -Force }
    if (Test-Path $app)  { Copy-Item $app $prev -Recurse -Force }

    Stop-ScheduledTask -TaskName $tarea -ErrorAction SilentlyContinue
    Start-Sleep 3

    # Reemplazar el codigo, conservando logs.
    Get-ChildItem $app -Exclude 'logs' -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
    Copy-Item "$($nuevo.FullName)\*" $app -Recurse -Force

    & 'C:\Python312\python.exe' -m pip install --quiet -r "$app\requirements.txt" 2>&1 | Out-Null

    Start-ScheduledTask -TaskName $tarea
    Start-Sleep 12

    $ok = $false
    foreach ($i in 1..5) {
        try {
            if ((Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$puerto/healthz" -TimeoutSec 10).StatusCode -eq 200) { $ok = $true; break }
        } catch { Start-Sleep 6 }
    }

    if ($ok) {
        Set-Content -Path $marca -Value $sha -Encoding ASCII
        Escribir "OK - actualizado a $($sha.Substring(0,8)) y contestando"
    } else {
        Escribir "FALLO el health check - volviendo a la version anterior"
        Stop-ScheduledTask -TaskName $tarea -ErrorAction SilentlyContinue
        Start-Sleep 3
        Get-ChildItem $app -Exclude 'logs' -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
        Copy-Item "$prev\*" $app -Recurse -Force
        Start-ScheduledTask -TaskName $tarea
        Escribir "vuelta atras hecha - el commit $($sha.Substring(0,8)) queda sin aplicar"
    }
} catch {
    Escribir "ERROR: $($_ | Out-String)"
} finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
