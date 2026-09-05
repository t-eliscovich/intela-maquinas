"""El lanzador del programa de máquinas en el servidor — sin PowerShell.

    C:\\Python312\\python.exe C:\\maquinas_app\\launch.py

Hasta el 05/09/2026 la tarea programada arrancaba `powershell.exe -File
launch.ps1`, un proceso de ~70 MB (más su conhost) que sólo exportaba las
variables de máquina, rotaba los logs y se quedaba esperando. En un servidor
de 4 GB con cinco de esos, eran ~390 MB al pedo (plan de memoria de Programa
Core, docs/PLAN_MEMORIA_SERVIDOR_2026_09_05.md, fase 3). Esto hace lo mismo
en el mismo proceso que sirve:

  1. Lee las variables de MÁQUINA del registro y las pone en el entorno (sin
     pisar las que ya vienen): el Programador de tareas a veces arranca con
     un bloque de entorno viejo y una variable nueva no llega.
  2. Rota `logs/` (borra los de más de 14 días) y manda stdout/stderr y el
     `logging` a `logs/maquinas-YYYY-MM-DD.log`.
  3. Sirve con waitress en 127.0.0.1:MAQUINAS_PORT (Caddy está adelante) y,
     si waitress sale por lo que sea, lo relanza a los 5 s — el mismo
     supervisor que tenía el .ps1 — con una línea en el log.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
DIAS_DE_LOG = 14
NOMBRE = "maquinas"
HOST = "127.0.0.1"
OBLIGATORIAS = ("MAQUINAS_DATABASE_URL", "MAQUINAS_PORT")


def variables_de_maquina() -> dict[str, str]:
    if os.name != "nt":
        return {}
    try:
        import winreg

        clave = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
        salida: dict[str, str] = {}
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, clave) as k:
            i = 0
            while True:
                try:
                    nombre, valor, _tipo = winreg.EnumValue(k, i)
                except OSError:
                    break
                i += 1
                if isinstance(valor, str) and valor:
                    salida[nombre] = valor
        return salida
    except Exception:
        return {}


def cargar_entorno(extra: dict[str, str] | None = None) -> int:
    puestas = 0
    for nombre, valor in (extra if extra is not None else variables_de_maquina()).items():
        if nombre not in os.environ:
            os.environ[nombre] = valor
            puestas += 1
    return puestas


def rotar_logs(carpeta: Path = LOGS, dias: int = DIAS_DE_LOG, ahora: float | None = None) -> int:
    carpeta.mkdir(parents=True, exist_ok=True)
    ahora = time.time() if ahora is None else ahora
    borrados = 0
    for f in carpeta.glob("*.log"):
        try:
            if ahora - f.stat().st_mtime > dias * 86400:
                f.unlink()
                borrados += 1
        except OSError:
            continue
    return borrados


def archivo_de_log(carpeta: Path = LOGS, hoy: date | None = None) -> Path:
    hoy = hoy or date.today()
    return carpeta / f"{NOMBRE}-{hoy.isoformat()}.log"


def redirigir_salida(ruta: Path):
    f = open(ruta, "a", encoding="utf-8", buffering=1)
    sys.stdout = f
    sys.stderr = f
    logging.basicConfig(stream=f, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        force=True)
    return f


def faltantes(entorno=None) -> list[str]:
    entorno = os.environ if entorno is None else entorno
    return [v for v in OBLIGATORIAS if not (entorno.get(v) or "").strip()]


def main() -> int:
    cargar_entorno()
    rotar_logs()
    redirigir_salida(archivo_de_log())
    if faltan := faltantes():
        print(f"ERROR: faltan variables obligatorias: {', '.join(faltan)}")
        return 3
    puerto = int(os.environ["MAQUINAS_PORT"])
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    from waitress import serve

    from app import app

    while True:
        print(f"=== arranque {NOMBRE} {datetime.now().isoformat()} pid {os.getpid()} puerto {puerto} ===")
        try:
            serve(app, host=HOST, port=puerto, ident="Intela")
            print(f"=== SALIO {datetime.now().isoformat()} — se relanza en 5 s ===")
        except Exception as e:  # noqa: BLE001 -- el supervisor no se muere
            print(f"=== SALIO {datetime.now().isoformat()} error: {e!r} — se relanza en 5 s ===")
        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
