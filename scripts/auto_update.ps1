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
$staging = "C:\maquinas_app.nuevo"
$viejo   = "C:\maquinas_app.prev"
try {
    # --- 1. Preparar la version nueva COMPLETA en una carpeta aparte --------
    # Nunca se toca la app que esta andando hasta que esto termine bien.
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 120 `
        -Uri "https://codeload.github.com/$repo/tar.gz/refs/heads/main" `
        -OutFile "$tmp\src.tar.gz"
    tar -xzf "$tmp\src.tar.gz" -C $tmp
    if ($LASTEXITCODE -ne 0) { throw "tar salio con codigo $LASTEXITCODE" }
    $nuevo = Get-ChildItem $tmp -Directory | Where-Object { $_.Name -like "intela-maquinas-*" } | Select-Object -First 1
    if (-not $nuevo) { throw "el tarball no traia la carpeta esperada" }
    if (-not (Test-Path "$($nuevo.FullName)\app.py")) { throw "falta app.py: descarga incompleta" }

    if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
    Copy-Item $nuevo.FullName $staging -Recurse -Force
    if (Test-Path "$app\logs") { Copy-Item "$app\logs" $staging -Recurse -Force -EA SilentlyContinue }

    # --- 2. Recien ahora, el cambio: renombrar, no borrar ------------------
    # Renombrar es casi instantaneo. Nunca existe un momento con la carpeta
    # vacia. Si algo falla, la version que andaba sigue entera en $viejo.
    Stop-ScheduledTask -TaskName $tarea -ErrorAction SilentlyContinue
    Start-Sleep 3
    if (Test-Path $viejo) { Remove-Item $viejo -Recurse -Force }
    Rename-Item $app  $viejo   -Force
    Rename-Item $staging $app  -Force

    # OJO con esto. Con $ErrorActionPreference = "Stop", CUALQUIER cosa que un
    # programa externo escriba en stderr se convierte en error que aborta, aunque
    # el programa haya terminado bien. pip escribe avisos ahi todo el tiempo.
    # El 20/08/2026 la primera actualizacion real (la que agrego openpyxl) murio
    # justo aca, hizo la vuelta atras, y el server se quedo en la version vieja.
    # Lo que decide si pip anduvo es su codigo de salida, no si dijo algo.
    $salidaPip = & 'C:\Python312\python.exe' -m pip install --quiet `
        --disable-pip-version-check -r "$app\requirements.txt" 2>&1
    if ($LASTEXITCODE -ne 0) { throw "pip salio con codigo $LASTEXITCODE : $salidaPip" }
    Start-ScheduledTask -TaskName $tarea
    Start-Sleep 12

    $ok = $false
    foreach ($i in 1..5) {
        try {
            if ((Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$puerto/healthz" -TimeoutSec 10).StatusCode -eq 200) { $ok = $true; break }
        } catch { Start-Sleep 6 }
    }
    if (-not $ok) { throw "la version nueva no contesta /healthz" }

    Set-Content -Path $marca -Value $sha -Encoding ASCII
    Escribir "OK - actualizado a $($sha.Substring(0,8)) y contestando"
} catch {
    Escribir "FALLO: $($_ | Out-String)"
    # Vuelta atras. Cubre cualquier error, no solo el health check: el intento
    # del 19/08 murio a mitad del reemplazo y dejo la app caida porque el
    # rollback solo cubria "arranco pero no contesta".
    try {
        if (Test-Path $viejo) {
            Stop-ScheduledTask -TaskName $tarea -ErrorAction SilentlyContinue
            Start-Sleep 2
            if (Test-Path $app) { Remove-Item $app -Recurse -Force -EA SilentlyContinue }
            Rename-Item $viejo $app -Force
            Start-ScheduledTask -TaskName $tarea
            Escribir "vuelta atras hecha - sigue andando la version anterior"
        } else {
            Escribir "NO habia copia anterior para restaurar"
        }
    } catch { Escribir "la vuelta atras tambien fallo: $($_ | Out-String)" }
} finally {
    Remove-Item "C:\maquinas_app.nuevo" -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
