"""Ver las pantallas sin deployar y sin base.

Levanta la app con datos de mentira y guarda cada pantalla como HTML en
`vista/`. Sirve para mirar un cambio de diseño antes de pushear: 43 máquinas
inventadas, kilos inventados, cero riesgo.

    python3 scripts/vista_local.py
"""
import os, sys, types, io
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MAQUINAS_DATABASE_URL", "postgresql://fake/fake")

for n in ("psycopg2", "psycopg2.extras", "psycopg2.pool"):
    sys.modules[n] = types.ModuleType(n)
sys.modules["psycopg2.extras"].RealDictCursor = object
class _P:
    def __init__(s, *a, **k): pass
sys.modules["psycopg2.pool"].SimpleConnectionPool = _P
sys.modules["psycopg2"].extras = sys.modules["psycopg2.extras"]
sys.modules["psycopg2"].pool = sys.modules["psycopg2.pool"]

import logging; logging.disable(logging.CRITICAL)
import config; config.PASSWORD = ""
import store, asinfo, app as A

hoy = date.today()
TIPOS = [
    {"id": 1, "nombre": "Cambio de agujas", "cada_kg": 250000, "cada_rollos": None,
     "cada_dias": None, "activo": True},
    {"id": 2, "nombre": "Limpieza", "cada_kg": 50000, "cada_rollos": None,
     "cada_dias": None, "activo": True},
]
MAQS = [{"id": 100 + n, "codigo": str(n), "nombre": f"TEJEDURIA-MQ {n:03d}", "numero": n}
        for n in range(1, 16)]

# Kilos de mentira, pero con la forma real: unas pocas máquinas tejen mucho.
def _kg(id_maquina, tipo_id):
    base = (id_maquina * 7919) % 300000
    return float(base if tipo_id == 1 else base / 4)

TOPES = {(101, 1): 120000.0, (102, 2): 15000.0}

store.tipos = lambda incluir_inactivos=False: TIPOS
store.topes_por_maquina = lambda: TOPES
store.ficha = lambda id_maquina: {"marca": "Mayer", "modelo": "MV4-3.2", "galga": 24,
                                  "diametro": 30, "alimentadores": 96, "agujas": 2260,
                                  "anio": 2015, "nota": None}
store.responsables = lambda: ["Luis", "Marco"]
store.historial = lambda id_maquina=None, limite=200: [
    {"fecha": hoy - timedelta(days=40), "tipo_nombre": "Limpieza",
     "hecho_por": "Luis", "nota": None, "maquina_nombre": "TEJEDURIA-MQ 001"},
    {"fecha": hoy - timedelta(days=300), "tipo_nombre": "Cambio de agujas",
     "hecho_por": "Marco", "nota": "Se cambiaron 12 agujas rotas",
     "maquina_nombre": "TEJEDURIA-MQ 001"},
]
store.ultimos_por_maquina_y_tipo = lambda: {
    (m["id"], t["id"]): {"fecha": hoy - timedelta(days=30 + (m["id"] % 90)),
                         "hecho_por": "Luis"}
    for m in MAQS for t in TIPOS if not (m["id"] == 115 and t["id"] == 1)
}
asinfo.maquinas = lambda: (MAQS, datetime.utcnow(), True)
asinfo.acumulados = lambda pares: (
    {(m, t): (_kg(m, t), int(_kg(m, t) / 22)) for m, t, _ in pares}, datetime.utcnow(), True)
asinfo.produccion_mensual = lambda id_maquina, meses=12: (
    [{"anio": 2026, "mes": mes, "kg": 8000 + (id_maquina * mes * 37) % 9000,
      "rollos": 300 + mes} for mes in range(8, 0, -1)], datetime.utcnow(), True)
A.ERROR_ARRANQUE = None

destino = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vista")
os.makedirs(destino, exist_ok=True)
c = A.app.test_client()

def guardar(nombre, respuesta):
    ruta = os.path.join(destino, nombre + ".html")
    with open(ruta, "wb") as f:
        f.write(respuesta.get_data())
    print(f"  {respuesta.status_code}  {nombre}.html")

for nombre, ruta in [("semaforo", "/"), ("semaforo-vencidas", "/?solo=vencidas"),
                     ("registrar", "/registrar"), ("tipos", "/tipos"),
                     ("arranque", "/arranque"), ("subir-excel", "/carga"),
                     ("ficha-maquina", "/maquina/101")]:
    guardar(nombre, c.get(ruta))

# La pantalla de revisión necesita un Excel: armamos uno parecido al de planta.
try:
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(["CONTROL DE MANTENIMIENTO TEJEDURIA"])
    ws.append([])
    ws.append(["Máquina", "Último cambio de agujas", "Agujas cada kg",
               "Última limpieza", "Limpieza cada kg", "Marca", "Modelo", "Galga", "Año"])
    for n in range(1, 13):
        ws.append([f"MQ {n}", f"{(n % 28) + 1:02d}/0{(n % 8) + 1}/2026", 200000 + n * 5000,
                   f"{(n % 28) + 1:02d}/07/2026", 40000 + n * 1000,
                   "Mayer" if n % 2 else "Terrot", f"MV4-{n}", 24, 2010 + n])
    ws.append(["MQ 99", "01/01/2026", 1000, None, None, None, None, None, None])
    buffer = io.BytesIO(); wb.save(buffer); buffer.seek(0)
    r = c.post("/carga", data={"archivo": (buffer, "planta.xlsx")},
               content_type="multipart/form-data")
    if r.status_code == 302:
        guardar("subir-excel-revisar", c.get(r.headers["Location"]))
except ImportError:
    print("  (sin openpyxl no se puede previsualizar la revisión del Excel)")

print(f"\nListo. Abrí los archivos de {destino}")
