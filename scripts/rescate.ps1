# rescate.ps1 — levantar la app cuando una actualizacion la dejo abajo.
#
# Para el auto-updater para que no siga dando vueltas, devuelve a su lugar los
# archivos que viven solo en el server (launch.ps1 y .env), libera el puerto y
# arranca la app. No toca formulas_app, ni Programa Core, ni Metabase.
#
# Al final vuelve a prender el auto-updater.

$app    = "C:\maquinas_app"
$viejo  = "C:\maquinas_app.prev"
$puerto = 5003

Write-Output "1. parando el auto-updater"
Stop-ScheduledTask -TaskName MaquinasAutoUpdate -ErrorAction SilentlyContinue
Disable-ScheduledTask -TaskName MaquinasAutoUpdate -ErrorAction SilentlyContinue | Out-Null

Write-Output "2. devolviendo lo que vive solo en el server"
foreach ($f in @('launch.ps1', '.env')) {
    $destino = Join-Path $app $f
    $origen  = Join-Path $viejo $f
    if ((Test-Path $origen) -and -not (Test-Path $destino)) {
        Copy-Item $origen $destino -Force
        Write-Output "   repuesto: $f"
    } elseif (Test-Path $destino) {
        Write-Output "   ya estaba: $f"
    } else {
        Write-Output "   NO APARECE: $f"
    }
}

Write-Output "3. liberando el puerto $puerto"
foreach ($idProceso in @(Get-NetTCPConnection -LocalPort $puerto -State Listen `
                         -ErrorAction SilentlyContinue |
                         Select-Object -ExpandProperty OwningProcess -Unique)) {
    Stop-Process -Id $idProceso -Force -ErrorAction SilentlyContinue
    Write-Output "   baje el proceso $idProceso"
}

Write-Output "4. arrancando la app"
Stop-ScheduledTask -TaskName MaquinasApp -ErrorAction SilentlyContinue
Start-Sleep 2
Start-ScheduledTask -TaskName MaquinasApp
Start-Sleep 20

Write-Output "5. healthz"
try { (Invoke-WebRequest -UseBasicParsing "http://localhost:$puerto/healthz" -TimeoutSec 15).Content }
catch { Write-Output "   no contesta: $($_.Exception.Message)" }

Write-Output "6. ultimo log de la app"
$log = Get-ChildItem "$app\logs" -EA SilentlyContinue | Sort-Object LastWriteTime | Select-Object -Last 1
if ($log) { Get-Content $log.FullName -Tail 12 } else { Write-Output "   sin logs" }

Write-Output "7. auto-updater de nuevo prendido"
Enable-ScheduledTask -TaskName MaquinasAutoUpdate -ErrorAction SilentlyContinue | Out-Null
