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

    # Lo que vive SOLO en el server y no esta en el repo. Sin esto la carpeta
    # nueva queda sin launch.ps1 (el archivo que la tarea ejecuta) ni .env (la
    # base y las claves): la app no arranca, el health falla, se deshace la
    # actualizacion, y desde afuera parece que el deploy no hace nada. Paso el
    # 20/08/2026, en bucle, hasta que se miro que habia adentro de la carpeta.
    #
    # Se buscan primero en la carpeta que esta andando y, si no estan, en la
    # copia anterior — que es donde quedan si un intento fallido ya las perdio.
    foreach ($propio in @("launch.ps1", ".env")) {
        foreach ($origen in @($app, $viejo)) {
            if (Test-Path "$origen\$propio") {
                Copy-Item "$origen\$propio" $staging -Force
                break
            }
        }
    }
    # Freno duro: sin launch.ps1 la version nueva no puede arrancar. Mejor no
    # tocar nada que dejar la app abajo.
    if (-not (Test-Path "$staging\launch.ps1")) {
        throw "no encontre launch.ps1 ni en $app ni en $viejo - no toco nada"
    }

    # Que version es esta, adentro de la carpeta. El archivo .commit se escribe
    # recien al final, DESPUES de que la app arranco: si /healthz lo leyera de
    # ahi mostraria siempre el deploy anterior.
    Set-Content -Path "$staging\.version" -Value $sha -Encoding ASCII

    # --- 2. Recien ahora, el cambio: renombrar, no borrar ------------------
    # Renombrar es casi instantaneo. Nunca existe un momento con la carpeta
    # vacia. Si algo falla, la version que andaba sigue entera en $viejo.
    Stop-ScheduledTask -TaskName $tarea -ErrorAction SilentlyContinue
    Start-Sleep 3

    # Matar al que HAYA QUEDADO tomando el puerto. Parar la tarea no siempre
    # mata al python hijo: el nuevo entonces no puede tomar el puerto, se cae,
    # el health check falla y la actualizacion se deshace sola. Desde afuera
    # parece que el deploy "no hace nada". Paso el 20/08/2026.
    #
    # SOLO el duenio de ESTE puerto. En el mismo box viven formulas_app (5001),
    # Programa Core (5002) y Metabase (3000): matar todos los python.exe seria
    # llevarse puesta media fabrica.
    foreach ($idProceso in @(Get-NetTCPConnection -LocalPort $puerto -State Listen `
                             -ErrorAction SilentlyContinue |
                             Select-Object -ExpandProperty OwningProcess -Unique)) {
        Escribir "el puerto $puerto seguia tomado por el proceso $idProceso - lo bajo"
        Stop-Process -Id $idProceso -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep 2

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

    # Hasta ~70 s. Waitress con los imports tarda mas de lo que uno espera, y un
    # health check impaciente da falso negativo: se deshace una actualizacion
    # que estaba bien.
    $ok = $false
    foreach ($i in 1..10) {
        try {
            if ((Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$puerto/healthz" -TimeoutSec 10).StatusCode -eq 200) { $ok = $true; break }
        } catch { Start-Sleep 6 }
    }
    if (-not $ok) { throw "la version nueva no contesta /healthz" }

    Set-Content -Path $marca -Value $sha -Encoding ASCII

    # El updater se actualiza a si mismo. Sin esto, un arreglo A ESTE script
    # se pushea, llega a la carpeta de la app... y nunca se usa, porque el que
    # corre es la copia vieja de C:\maquinas_update. Cada arreglo del updater
    # obligaba a entrar al server a mano. PowerShell lee el archivo entero
    # antes de ejecutarlo, asi que pisarlo ahora no rompe esta corrida.
    $mio = Join-Path $PSScriptRoot "auto_update.ps1"
    $nuevoUpdater = Join-Path $app "scripts\auto_update.ps1"
    if ((Test-Path $nuevoUpdater) -and (Test-Path $mio)) {
        $a = (Get-FileHash $nuevoUpdater).Hash
        $b = (Get-FileHash $mio).Hash
        if ($a -ne $b) {
            Copy-Item $nuevoUpdater $mio -Force
            Escribir "el updater se actualizo a si mismo"
        }
    }

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
