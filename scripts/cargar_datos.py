"""Cargar mantenimientos y fichas desde un archivo de datos.

Existe para el arranque: la planilla de planta vive en la computadora de la
oficina y no siempre se puede subir por la pantalla. Los datos se preparan
aparte, se dejan en un JSON, y esto los guarda **por el mismo camino que usa
la pantalla** (`store.cargar_lote`): las mismas validaciones, la misma
transacción, el mismo resultado.

    python scripts/cargar_datos.py C:\\tmp\\datos.json
    python scripts/cargar_datos.py datos.json --de-verdad

Sin `--de-verdad` no guarda nada: muestra qué haría. Se mira primero y se
guarda después.

El formato:

    {"quien": "Carga inicial",
     "maquinas": [{"numero": 1,
                   "ficha": {"marca": "MAYER", "anio": 2017, ...},
                   "mantenimientos": [{"tipo": "Limpieza", "fecha": "2026-07-17"}]}]}
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def cargar_env(carpeta):
    """Lee el .env de la app. Las claves viven en el server, no en el repo."""
    ruta = os.path.join(carpeta, ".env")
    if not os.path.exists(ruta):
        return
    with open(ruta, encoding="utf-8-sig") as f:
        for linea in f:
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            clave, valor = linea.split("=", 1)
            os.environ.setdefault(clave.strip(), valor.strip().strip('"').strip("'"))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    archivo = sys.argv[1]
    de_verdad = "--de-verdad" in sys.argv

    carpeta = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cargar_env(carpeta)

    import asinfo
    import store

    # .gz porque el archivo suele llegar por un canal donde el tamaño importa.
    if archivo.endswith(".gz"):
        import gzip
        with gzip.open(archivo, "rt", encoding="utf-8") as f:
            datos = json.load(f)
    else:
        with open(archivo, encoding="utf-8") as f:
            datos = json.load(f)

    quien = datos.get("quien") or "Carga inicial"
    store.init_pool()

    maquinas, _, _ = asinfo.maquinas()
    por_numero = {m["numero"]: m for m in maquinas if m.get("numero") is not None}
    tipos = {t["nombre"].lower(): t for t in store.tipos()}
    ya_estan = store.ultimos_por_maquina_y_tipo()

    servicios, fichas, salteadas = [], [], []
    for entrada in datos["maquinas"]:
        maquina = por_numero.get(int(entrada["numero"]))
        if not maquina:
            salteadas.append(f"MQ {entrada['numero']}: no está en Asinfo")
            continue

        for mant in entrada.get("mantenimientos", []):
            tipo = tipos.get(str(mant["tipo"]).lower())
            if not tipo:
                salteadas.append(f"MQ {entrada['numero']}: no existe el tipo «{mant['tipo']}»")
                continue
            fecha = datetime.strptime(mant["fecha"], "%Y-%m-%d").date()
            if fecha > date.today():
                salteadas.append(f"MQ {entrada['numero']}: fecha futura, no se carga")
                continue
            # Si ya está cargado ese mismo día, no se repite. Correr esto dos
            # veces no tiene que duplicar el historial.
            previo = ya_estan.get((maquina["id"], tipo["id"]))
            if previo and previo["fecha"] == fecha:
                salteadas.append(f"MQ {entrada['numero']} {tipo['nombre']}: ya estaba")
                continue
            servicios.append((maquina["id"], maquina["nombre"], tipo["id"], fecha,
                              quien, "Cargado de la planilla de planta"))

        ficha = entrada.get("ficha") or {}
        if any(v is not None for v in ficha.values()):
            fichas.append((maquina["id"], *[ficha.get(c) for c in store.CAMPOS_FICHA]))

    print(f"máquinas en el archivo: {len(datos['maquinas'])}")
    print(f"mantenimientos a cargar: {len(servicios)}")
    print(f"fichas a guardar: {len(fichas)}")
    for s in salteadas:
        print(f"  se saltea → {s}")

    if not de_verdad:
        print("\nNo se guardó nada. Para guardar, agregá --de-verdad")
        return 0

    hecho = store.cargar_lote(servicios, [], fichas)
    print(f"\nGuardado: {hecho}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
