#!/bin/bash
# deploy.sh — manda maquinas_app al EC2 de Intela.
#
# Corre solo desde GitHub Actions en cada push a main (.github/workflows/deploy.yml).
# También se puede correr a mano si tenés awscli configurado para us-east-2.
#
# Puertos del box:  80/443 Caddy · 3000 Metabase · 5001 formulas_app
#                   5002 Programa Core · 5003 este programa
#
# Qué hace:
#   1. VALIDA (si algo falla, corta y NO deploya nada)
#   2. Empaqueta, sube a S3, genera URL firmada
#   3. Un SSM al EC2: baja, extrae, pip install, reescribe el launcher,
#      re-registra la tarea MaquinasApp y verifica /healthz
#   4. Chequea que los tres vecinos sigan vivos
set -euo pipefail
cd "$(dirname "$0")"

echo "=== 1. Validación pre-deploy ==="
python3 -m py_compile app.py store.py asinfo.py config.py
python3 -c "import jinja2" 2>/dev/null || pip install --quiet jinja2
python3 - <<'PY'
import os
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('templates'))
for f in sorted(os.listdir('templates')):
    env.parse(open('templates/' + f).read())      # py_compile NO ve los templates
print("templates OK")
PY
python3 -c "import flask" 2>/dev/null \
  || pip install --quiet -r requirements.txt 2>/dev/null \
  || pip install --quiet --break-system-packages -r requirements.txt
python3 scripts/test_maquinas.py
echo "=== Validación OK ==="

INSTANCE_ID="${EC2_INSTANCE_ID:-i-0fcca4d7029f08489}"
REGION="us-east-2"
TASK="MaquinasApp"
PUERTO=5003
DESTINO='C:\maquinas_app'

correr() {
  local id
  id=$(aws ssm send-command --region $REGION --instance-ids $INSTANCE_ID \
       --document-name AWS-RunPowerShellScript \
       --parameters "commands=[\"$1\"]" --query Command.CommandId --output text)
  sleep "${2:-20}"
  aws ssm get-command-invocation --region $REGION --instance-id $INSTANCE_ID \
    --command-id "$id" --query '[Status,StandardOutputContent,StandardErrorContent]' --output text
}

echo "=== 2. Empaquetar y subir ==="
TAR=/tmp/maquinas_app.tar.gz
STAGE=$(mktemp -d); mkdir -p "$STAGE/maquinas_app"
tar --exclude='.git' --exclude='.github' --exclude='__pycache__' --exclude='.venv' \
    --exclude='logs' --exclude='*.pyc' -cf - . | (cd "$STAGE/maquinas_app" && tar -xf -)
tar -czf "$TAR" -C "$STAGE" maquinas_app
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="intela-deploy-${ACCOUNT}"
aws s3 mb "s3://$BUCKET" --region $REGION 2>/dev/null || true
aws s3 cp "$TAR" "s3://$BUCKET/maquinas_app.tar.gz" --region $REGION
URL=$(aws s3 presign "s3://$BUCKET/maquinas_app.tar.gz" --expires-in 3600 --region $REGION)

echo "=== 3. Extraer en el EC2 ==="
correr "\$ErrorActionPreference='Stop'; \
Invoke-WebRequest -Uri '$URL' -OutFile C:\\maquinas_app.tar.gz; \
Stop-ScheduledTask -TaskName '$TASK' -EA SilentlyContinue; Start-Sleep 3; \
if (Test-Path '$DESTINO') { Remove-Item -Recurse -Force '$DESTINO' }; \
tar -xzf C:\\maquinas_app.tar.gz -C C:\\; Remove-Item C:\\maquinas_app.tar.gz; \
'archivos: ' + (Get-ChildItem -Recurse '$DESTINO' -File).Count"

echo "=== 4. Librerías ==="
correr "& 'C:\\Python312\\python.exe' -m pip install --quiet -r '$DESTINO\\requirements.txt' 2>&1 | Out-String; \
& 'C:\\Python312\\python.exe' -c \\\"import flask, psycopg2, requests, waitress; print('librerias OK')\\\"" 60

echo "=== 5. Variables (la contraseña existente NO se pisa) ==="
correr "\$db=[System.Environment]::GetEnvironmentVariable('DATABASE_URL','Machine'); \
[Environment]::SetEnvironmentVariable('MAQUINAS_DATABASE_URL', \$db, 'Machine'); \
if (-not [System.Environment]::GetEnvironmentVariable('MAQUINAS_SECRET_KEY','Machine')) { \
  [Environment]::SetEnvironmentVariable('MAQUINAS_SECRET_KEY', [guid]::NewGuid().ToString('N'), 'Machine') }; \
[Environment]::SetEnvironmentVariable('MAQUINAS_PORT', '$PUERTO', 'Machine'); \
'contrasena: ' + \$(if ([System.Environment]::GetEnvironmentVariable('MAQUINAS_PASSWORD','Machine')) { 'ya existe, se respeta' } else { 'SIN CONTRASENA - poner una antes de publicar' })"

echo "=== 6. Launcher ==="
# TMT 2026-09-05 (plan de memoria de Programa Core, fase 3): el launcher ya
# no es un powershell.exe de 70 MB que se queda esperando toda la vida del
# programa; es launch.py, que viene en el tarball y hace lo mismo (variables
# de máquina del registro, logs rotados, waitress + supervisor) en el mismo
# proceso que sirve. El launch.ps1 viejo del server se borra para que no
# quede una segunda forma de arrancar.
correr "if (Test-Path '$DESTINO\\launch.ps1') { Remove-Item '$DESTINO\\launch.ps1' -Force }; if (Test-Path '$DESTINO\\launch.py') { 'launch.py OK' } else { 'FALTA launch.py' }"

echo "=== 7. Re-registrar y arrancar ==="
correr "Get-ScheduledTask -TaskName '$TASK' -EA SilentlyContinue | Unregister-ScheduledTask -Confirm:\$false; \
\$a=New-ScheduledTaskAction -Execute 'C:\\Python312\\python.exe' -Argument 'launch.py' -WorkingDirectory '$DESTINO'; \
\$t=New-ScheduledTaskTrigger -AtStartup; \
\$s=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1); \
\$p=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest; \
Register-ScheduledTask -TaskName '$TASK' -Action \$a -Trigger \$t -Settings \$s -Principal \$p | Out-Null; \
Start-ScheduledTask -TaskName '$TASK'; Start-Sleep 10; 'estado: ' + (Get-ScheduledTask -TaskName '$TASK').State" 30

echo "=== 8. Verificar ==="
SALIDA=$(correr "try { \$r=Invoke-WebRequest -UseBasicParsing http://127.0.0.1:$PUERTO/healthz -TimeoutSec 15; 'HEALTH ' + \$r.StatusCode + ' ' + \$r.Content } catch { 'HEALTH FALLO: ' + \$_.Exception.Message; Get-Content (Get-ChildItem C:\\maquinas_app\\logs\\*.log | Select -Last 1).FullName -Tail 30 }; \
''; 'vecinos:'; foreach (\$t in 'FormulasApp','ProgramaCoreApp','Metabase') { \$t + ' ' + (Get-ScheduledTask -TaskName \$t -EA SilentlyContinue).State }" 25)
echo "$SALIDA"
echo "$SALIDA" | grep -q "HEALTH 200" || { echo "### DEPLOY FALLIDO: /healthz no devolvió 200 ###"; exit 1; }
echo "=== Deploy OK ==="
