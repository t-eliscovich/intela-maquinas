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
# El cambio de agujas NO tiene tope de kilos a propósito: no se hace por
# desgaste. Así el semáforo lo muestra como fecha y no lo pinta de rojo.
TIPOS = [
    {"id": 1, "nombre": "Cambio de agujas", "cada_kg": None, "cada_rollos": None,
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
FICHA_FALSA = {"marca": "Mayer", "modelo": "MV4-3.2", "galga": 24, "diametro": 30,
               "alimentadores": 96, "agujas": 2260, "anio": 2015,
               "serie": "73830", "tipo_agujas": "VO LS 140.50 G0036", "nota": None}
store.ficha = lambda id_maquina: FICHA_FALSA
store.fichas = lambda: {m["id"]: FICHA_FALSA for m in MAQS}
store.responsables = lambda: list(store.ENCARGADOS)
store.archivos = lambda id_maquina=None: [
    {"id": 1, "id_maquina": 101, "nombre": "R01-MANTENIMIENTO.xlsx",
     "descripcion": "La planilla de planta", "tamano": 233472,
     "subido_por": "Roberto", "creado_en": datetime.utcnow()}]
store.historial = lambda id_maquina=None, limite=200: [
    {"fecha": hoy - timedelta(days=40), "tipo_nombre": "Limpieza",
     "hecho_por": "Roberto", "nota": "limpieza de cilindro", "repuestos": None,
     "horas": 1.5, "maquina_nombre": "TEJEDURIA-MQ 001"},
    # El mismo día se limpió y se cambiaron agujas: en la ficha van juntos.
    {"fecha": hoy - timedelta(days=300), "tipo_nombre": "Cambio de agujas",
     "hecho_por": "Darlin", "nota": "Se cambiaron 12 agujas rotas",
     "repuestos": "12 agujas de cilindro", "horas": 4,
     "maquina_nombre": "TEJEDURIA-MQ 001"},
    {"fecha": hoy - timedelta(days=300), "tipo_nombre": "Limpieza",
     "hecho_por": "Darlin", "nota": "limpieza general", "repuestos": None,
     "horas": 2, "maquina_nombre": "TEJEDURIA-MQ 001"},
    {"fecha": hoy - timedelta(days=520), "tipo_nombre": "Limpieza",
     "hecho_por": "Humberto", "nota": "limpiesa de memminger", "repuestos": None,
     "horas": None, "maquina_nombre": "TEJEDURIA-MQ 001"},
]
store.ultimos_por_maquina_y_tipo = lambda: {
    (m["id"], t["id"]): {"fecha": hoy - timedelta(days=30 + (m["id"] % 90)),
                         "hecho_por": "Roberto"}
    for m in MAQS for t in TIPOS if not (m["id"] == 115 and t["id"] == 1)
}
AJUSTES_FALSOS = [
    {"id": i, "id_maquina": 100 + (i % 12) + 1, "maquina_nombre": "TEJEDURIA-MQ 001",
     "fecha": (hoy - timedelta(days=i * 23)) if i % 4 else None,
     "tipo_maquina": "MAYER 32" if i % 2 else "JIUNN LONG",
     "cilindro": f"pro {20 + i}", "poleas": f"dibujo {i * 5} · pro {i + 24}",
     "ajuste_agujas": "lycra pro 6,5" if i % 3 else None, "estiraje": f"B/{i % 12}",
     "tela": ["FALSO F. KW", "TANIA SPUN LYCRA", "PIQUE", "JERSEY 3,50", "BOXER"][i % 5],
     "hilos": "20/1 KW · 16/1 EP", "gramaje_crudo": 180.5 + i,
     "malla_manual": f"{29 + (i % 5)},0 dibujo", "malla": f"{30 + (i % 4)},2 LM",
     "rendimiento": 4.05, "kg_m": None, "hoja": f"MAQ {i}", "orden": i}
    for i in range(1, 26)
]
store.ajustes = lambda id_maquina=None, tela=None, limite=400: AJUSTES_FALSOS
store.telas = lambda: [
    {"tela": t, "veces": 40 - i * 7, "maquinas": 9 - i, "ultima": hoy - timedelta(days=i * 30)}
    for i, t in enumerate(["FALSO F. KW", "TANIA SPUN LYCRA", "PIQUE", "BOXER"])]
store.resumen_ajustes = lambda: {"filas": 1153, "maquinas": 42, "con_fecha": 931,
                                 "desde": date(2021, 5, 31), "hasta": hoy}
store.cuantos_de_la_carga_vieja = lambda: 66
store.guardar_historial = lambda filas: {"mantenimientos": len(filas), "borrados": 66}
AGUJA_FALSA = {"id_maquina": 101, "descripcion": "MAYER", "plato": None,
               "cilindro": "VO LS-140,50 G00 36 · VO LS-140,50 G00 37",
               "platinas": "206085101G00", "nota": None}
store.agujas = lambda: {m["id"]: dict(AGUJA_FALSA, id_maquina=m["id"]) for m in MAQS}
store.levas = lambda: [
    {"id": 1, "maquinas": "MAYER (1 )", "codigo": "30-32 385953,0", "cantidad": 208,
     "ubicacion": "cilindro", "accionamiento": "TRABAJO RECOMIENDA LYCRA"},
    {"id": 2, "maquinas": "MAYER (2-3-9-10 )", "codigo": "30-32 274078-4",
     "cantidad": 414, "ubicacion": "cilindro", "accionamiento": "RETENIDO"}]
store.bandas = lambda clase="memminger": (
    [{"id": 1, "clase": "memminger", "maquinas": "MAYER JERSEY",
      "cantidad_maquinas": 4, "diametro": 32, "media": "7.2",
      "tres_cuartos": "8.8", "lycra": "11.4", "banda": None, "cobrador": None,
      "nota": None},
     {"id": 2, "clase": "memminger", "maquinas": "PILOTELLI JERSEY",
      "cantidad_maquinas": 3, "diametro": 30, "media": "7",
      "tres_cuartos": "8.4", "lycra": "9", "banda": None, "cobrador": None,
      "nota": None}]
    if clase == "memminger" else
    [{"id": 3, "clase": "motor", "maquinas": "MAYER", "cantidad_maquinas": 2,
      "diametro": 30, "media": None, "tres_cuartos": None, "lycra": None,
      "banda": "1040 M8 /50", "cobrador": "14/5/1015LI", "nota": None}])
store.banda_stock = lambda: [{"medida": 6.6, "cantidad": 10},
                             {"medida": 7.2, "cantidad": 20},
                             {"medida": 7.8, "cantidad": 0}]
store.eficiencias = lambda: {
    m["id"]: {"id_maquina": m["id"], "rpm": 25, "sistemas": "102", "diametro": 32,
              "alimentadores": 24, "tamano_rollo": 1410, "minutos_rollo": 56.4,
              "rollos_dia": 12, "kg_dia": 270,
              "rollos_dia_24": 25, "kg_dia_24": 612.5} for m in MAQS}
store.agujas_por_modelo = lambda: [
    {"id": 1, "modelo": "MAYER)1-2-3-4-5-7-9-10", "marca_aguja": "GROZ BECKERT",
     "codigos": "VO-LS 140.50 G0036 · VO-LS 140.50 G0037", "donde": "CILINDRO",
     "platinas": "206085101G00", "marca_platina": "KERN LIEBERS", "nota": None}]
store.consumo_hilo = lambda: [
    {"id": 1, "tela": "JAMES", "hilo": "poliester 75/36f", "codigo_hilo": "75F36",
     "rendimiento": 0.4786},
    {"id": 2, "tela": "TANIA", "hilo": "30/1 spun", "codigo_hilo": "30/1 SPUN",
     "rendimiento": 0.9829}]
store.gramajes = lambda id_maquina=None: [
    {"id": 1, "id_maquina": 101, "fecha": date(2021, 11, 10), "tela": "FALSO F",
     "hilos": "20/1 WARIL · 16/1 PERAL", "peso": 4.39, "orden": 1}]

asinfo.maquinas = lambda: (MAQS, datetime.utcnow(), True)
asinfo.acumulados = lambda pares: (
    {(m, t): (_kg(m, t), int(_kg(m, t) / 22)) for m, t, _ in pares}, datetime.utcnow(), True)
# Cuanto mas vieja la fecha, mas kilos lleva encima: asi la resta entre dos
# mantenimientos da un numero positivo, como en la realidad.
asinfo.kilos_desde = lambda id_maquina, fechas: {
    str(f)[:10]: float((hoy - f).days * 480) for f in fechas if f}
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
                     ("maquinas", "/maquinas"), ("ficha-maquina", "/maquina/101"),
                     ("ajustes", "/ajustes"), ("ajustes-por-tela", "/ajustes?tela=PIQUE"),
                     ("repuestos", "/repuestos"), ("produccion", "/produccion")]:
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

# La revisión de la planilla de control de ajuste, con la planilla de verdad si
# está a mano. No se commitea: el repo es público.
raiz = os.path.dirname(destino)
for nombre, ruta_planilla in (
        ("subir-ajuste-revisar",
         os.path.join(raiz, "CONTROL DE AGJUSTE", "CONTROL DE AGJUSTE ).xlsx")),
        ("subir-historial-revisar",
         os.path.join(raiz, "MANTENIMIENTO", "R01-MANTENIMIENTO(1).xlsx"))):
    if not os.path.exists(ruta_planilla):
        print(f"  (falta {os.path.basename(ruta_planilla)}: no se previsualiza)")
        continue
    with open(ruta_planilla, "rb") as f:
        buffer = io.BytesIO(f.read())
    r = c.post("/carga", data={"archivo": (buffer, "planilla.xlsx")},
               content_type="multipart/form-data")
    if r.status_code == 302:
        guardar(nombre, c.get(r.headers["Location"]))

print(f"\nListo. Abrí los archivos de {destino}")
