"""Tests que corren ANTES de cada deploy. Si algo falla, no se deploya.

Cubren lo que ya nos mordió una vez:
  * que la app se pueda IMPORTAR como la importa Waitress (`app:app`), que es
    donde vivía el bug del pool sin inicializar;
  * que con la base caída avise en vez de tirar 500;
  * la aritmética del semáforo (va por kilos, y el tope puede ser propio
    de cada máquina);
  * que la campanita nunca muestre cero cuando Asinfo no contesta;
  * que el Excel de planta se lea aunque tenga los títulos donde quiera, y que
    lo que no se entiende quede afuera en vez de entrar mal;
  * que el arranque en lote no duplique ni acepte fechas futuras;
  * que ninguna entrada llegue cruda al SQL de Asinfo.
"""
import os, sys, types
from datetime import date, timedelta, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MAQUINAS_DATABASE_URL", "postgresql://fake/fake")

def _stub(pool_ok=True):
    for n in ("psycopg2", "psycopg2.extras", "psycopg2.pool", "requests"):
        sys.modules[n] = types.ModuleType(n)
    sys.modules["psycopg2.extras"].RealDictCursor = object
    if pool_ok:
        class P:
            def __init__(s, *a, **k): pass
        sys.modules["psycopg2.pool"].SimpleConnectionPool = P
    else:
        def roto(*a, **k):
            raise RuntimeError("could not connect to server: Connection refused")
        sys.modules["psycopg2.pool"].SimpleConnectionPool = roto
    sys.modules["psycopg2"].extras = sys.modules["psycopg2.extras"]
    sys.modules["psycopg2"].pool = sys.modules["psycopg2.pool"]

fallos = []
def check(nombre, cond):
    print(("  OK   " if cond else "  FALLA ") + nombre)
    if not cond:
        fallos.append(nombre)

# --- 1. base caída: importa igual y AVISA ---------------------------------
_stub(pool_ok=False)
import config; config.DATABASE_URL = "postgresql://fake/fake"; config.PASSWORD = ""
import store, asinfo, app as A
A.app.config["TESTING"] = True
c = A.app.test_client()
print("Base caída:")
check("importa sin reventar (asi la importa waitress)", A.ERROR_ARRANQUE is not None)
r = c.get("/healthz")
check("healthz devuelve 503 y explica", r.status_code == 503 and "error_arranque" in r.get_json())
r = c.get("/")
check("pantalla explica en vez de 500", r.status_code == 503 and "No hay conexión" in r.get_data(as_text=True))

# --- 2. seguridad del SQL a Asinfo ----------------------------------------
print("Asinfo:")
vistos = []
asinfo._consultar = lambda sql: (vistos.append(sql), [])[1]
asinfo._cache.clear()
asinfo.acumulados([(10, 1, "2026-01-01'; DROP TABLE maquina--")])
check("inyeccion neutralizada", "DROP" not in vistos[0].upper() and "--" not in vistos[0])
for mala in ("no-soy-fecha", ""):
    try:
        asinfo._cache.clear(); asinfo.acumulados([(10, 1, mala)]); ok = False
    except ValueError:
        ok = True
    check(f"fecha invalida rechazada ({mala!r})", ok)
check("guard de truncado existe", hasattr(asinfo, "RespuestaTruncada"))

# --- 3. el semáforo va por kilos ------------------------------------------
print("Semaforo:")
hoy = date.today()
TIPOS = [{"id": 1, "nombre": "Cambio de agujas", "cada_kg": 50000,
          "cada_rollos": None, "cada_dias": None, "activo": True}]
MAQS = [{"id": 10, "codigo": "1", "nombre": "TEJEDURIA-MQ 001", "numero": 1},
        {"id": 11, "codigo": "2", "nombre": "TEJEDURIA-MQ 002", "numero": 2},
        {"id": 12, "codigo": "3", "nombre": "TEJEDURIA-MQ 003", "numero": 3}]
KG = {10: 80000.0, 11: 42000.0, 12: 1000.0}   # pasado / falta poco / tranquilo

store.tipos = lambda incluir_inactivos=False: TIPOS
store.historial = lambda id_maquina=None, limite=200: []
store.responsables = lambda: []
store.ficha = lambda id_maquina: {}
store.topes_por_maquina = lambda: {}
store.ultimos_por_maquina_y_tipo = lambda: {
    (m["id"], 1): {"fecha": hoy - timedelta(days=30), "hecho_por": "x"} for m in MAQS
}
asinfo.maquinas = lambda: (MAQS, datetime.utcnow(), True)
asinfo.acumulados = lambda pares: (
    {(m, t): (KG[m], int(KG[m] / 20)) for m, t, d in pares}, datetime.utcnow(), True)
asinfo.produccion_mensual = lambda id_maquina, meses=12: ([], datetime.utcnow(), True)
A.ERROR_ARRANQUE = None

filas, pend, _, _ = A.armar_semaforo()
por_maquina = {f["maquina"]["id"]: f for f in filas}
check("lo mas pasado va primero", filas[0]["maquina"]["id"] == 10)
check("pasado de kilos = vencido", por_maquina[10]["estado"] == "vencido")
check("84% = falta poco", por_maquina[11]["estado"] == "por_vencer")
check("2% = ok", por_maquina[12]["estado"] == "ok")
check("dice cuanto se paso", round(por_maquina[10]["falta"]) == -30000)
check("los dias no prenden nada", por_maquina[12]["dias"] == 30
      and por_maquina[12]["estado"] == "ok")

# Sin tope no se inventa un estado: se dice que falta el número.
TIPOS[0]["cada_kg"] = None
filas, _, _, _ = A.armar_semaforo()
check("sin tope no pinta verde", all(f["estado"] == "sin_tope" for f in filas))
TIPOS[0]["cada_kg"] = 50000

# --- 4. el tope propio de cada máquina manda -------------------------------
print("Tope por maquina:")
store.topes_por_maquina = lambda: {(12, 1): 500.0}   # MQ 3 teje poco: tope chico
filas, _, _, _ = A.armar_semaforo()
por_maquina = {f["maquina"]["id"]: f for f in filas}
check("el tope propio pisa al del tipo", por_maquina[12]["tope"] == 500.0)
check("y con eso la maquina chica se prende", por_maquina[12]["estado"] == "vencido")
check("la que no tiene tope propio usa el del tipo", por_maquina[10]["tope"] == 50000)
check("se marca cual es propio", por_maquina[12]["tope_propio"] is True
      and por_maquina[10]["tope_propio"] is False)
store.topes_por_maquina = lambda: {}

# --- 5. la campanita nunca miente -----------------------------------------
print("Campanita:")
with A.app.test_request_context("/"):
    check("cuenta las vencidas", A._campanita()["vencidas"] == 1)

def _asinfo_caido():
    raise asinfo.AsinfoNoDisponible("timeout")

guardado = asinfo.maquinas
asinfo.maquinas = lambda: _asinfo_caido()
with A.app.test_request_context("/"):
    check("con Asinfo caido NO dice cero", A._campanita()["vencidas"] is None)
asinfo.maquinas = guardado

# --- 6. leer el Excel de planta -------------------------------------------
print("Excel:")
import excel
titulos = ["Máquina", "Último cambio de agujas", "Cambio agujas cada kg",
           "Marca", "Modelo", "Galga", "Año"]
mapa = excel.detectar(titulos, TIPOS)
check("encuentra la columna de la maquina", mapa.get("numero") == 0)
check("encuentra la fecha del tipo", mapa.get("fecha_1") == 1)
check("encuentra los kilos del tipo", mapa.get("kg_1") == 2)
check("la ficha no se roba la columna 'agujas'", mapa.get("marca") == 3
      and mapa.get("galga") == 5)

check("30.000 son treinta mil", excel.a_kilos("30.000 kg") == 30000)
check("lee fecha dd/mm/aaaa", excel.a_fecha("12/07/2026") == date(2026, 7, 12))
check("una fecha imposible no revienta", excel.a_fecha("cuando se pueda") is None)
check("MQ 003 es la 3", excel.a_numero("MQ 003") == 3)

filas_excel = [
    ["MQ 1", "12/07/2026", "30.000", "Mayer", "MV4", "24", "2015"],
    ["3",    None,          "5.000",  None,    None,  None, None],   # sin fecha
    ["99",   "01/01/2026", "10.000", None,    None,  None, None],    # no existe
    [None,   None,          None,     None,    None,  None, None],   # vacía
]
listas, descartes = excel.armar(titulos, filas_excel, mapa, MAQS, TIPOS, hoy=date(2026, 8, 19))
check("entran las dos que se entienden", len(listas) == 2)
check("la maquina que no esta en Asinfo queda afuera",
      any("no está en Asinfo" in d["motivo"] for d in descartes))
check("la fila vacia queda afuera", len(descartes) == 2)
check("sin fecha guarda el tope pero no inventa un mantenimiento",
      listas[1]["mantenimientos"][0]["fecha"] is None
      and listas[1]["mantenimientos"][0]["cada_kg"] == 5000
      and any("sólo se guarda el tope" in a for a in listas[1]["avisos"]))
check("lee la ficha", listas[0]["ficha"]["marca"] == "Mayer"
      and listas[0]["ficha"]["galga"] == 24)

futuras, _ = excel.armar(titulos, [["MQ 1", "31/12/2030", "1000", None, None, None, None]],
                         mapa, MAQS, TIPOS, hoy=date(2026, 8, 19))
check("una fecha futura no se carga",
      any("futura" in a for a in futuras[0]["avisos"]))

repetida, _ = excel.armar(titulos,
    [["MQ 1", "12/07/2026", "1000", None, None, None, None],
     ["MQ 1", "13/07/2026", "2000", None, None, None, None]],
    mapa, MAQS, TIPOS, hoy=date(2026, 8, 19))
check("avisa la maquina repetida",
      all(any("más de una vez" in a for a in r["avisos"]) for r in repetida))

# --- 6b. la planilla de planta: una hoja por maquina ----------------------
print("Planilla por maquina:")
from openpyxl import Workbook as _WB
wb2 = _WB()
h1 = wb2.active; h1.title = "MAQ 1"
h1.append(["R01-Registro de Mantenimiento"])
h1.append([])
h1.append([])
h1.append(["Equipo:", "", "Cantidad de agujas", "MODELO", "Responsable", "NUMERO"])
h1.append(["CIRCULAR", "MAYER", 2460, "Relanit 3.2 HS", "", 73830])
h1.append(["17////1", "", "", "D 32   G24"])
h1.append(["", "", "", "", "ANGEL PONCE"])
h1.append(["Año de fabricación:", "", "", 2017])
h1.append(["Fecha", "", "Fecha", "Tipo de mantenimiento", "Actividad realizada"])
h1.append([datetime(2024, 5, 3), "", "", "limpiesa de cilindro", ""])
h1.append([datetime(2026, 7, 17), "", "", "limpieza de cilindro", ""])
h1.append([datetime(2026, 2, 1), "", "", "cambio de agujas del cilindro", ""])
h2 = wb2.create_sheet("MQ 2"); h2.append(["Registro"])   # hoja vacia
h3 = wb2.create_sheet("MAQ.3")
h3.append(["Equipo:", "", "AGUJAS", "MODELO", "DIAMETRO/ GG", "NUMERO"])
h3.append(["CIRCULAR", "JIUNN LONG", 3168, "JLD-T", "36  /  28", 230502])
h3.append(["", "", "100 ALIMENTADORES", "", "", ""])
h3.append(["Fecha", "Tipo de mantenimiento"])
h3.append([datetime(2026, 3, 5), "limpiesa de memminger"])
h4 = wb2.create_sheet("MQ 99")                            # no esta en Asinfo
h4.append(["Fecha", "x"]); h4.append([datetime(2026, 1, 1), "limpieza"])
import io as _io
buf2 = _io.BytesIO(); wb2.save(buf2); buf2.seek(0)
import tempfile as _tmp, os as _os
ruta2 = _os.path.join(_tmp.gettempdir(), "planilla_test.xlsx")
open(ruta2, "wb").write(buf2.getvalue())

TIPOS2 = [{"id": 1, "nombre": "Cambio de agujas", "cada_kg": None, "activo": True},
          {"id": 2, "nombre": "Limpieza", "cada_kg": None, "activo": True}]
pm, pd_ = excel.leer_por_maquina(ruta2, MAQS, TIPOS2, hoy=date(2026, 8, 20))
porn = {i["maquina"]["numero"]: i for i in pm}
check("lee una hoja por maquina", len(pm) == 2)
f1 = porn[1]["ficha"]
check("marca de al lado de CIRCULAR", f1["marca"] == "MAYER")
check("modelo", f1["modelo"] == "Relanit 3.2 HS")
check("diametro y galga de 'D 32  G24'", (f1["diametro"], f1["galga"]) == (32, 24))
check("agujas", f1["agujas"] == 2460)
check("anio", f1["anio"] == 2017)
check("numero de serie", str(f1["serie"]) == "73830")
m1 = {x["tipo"]["nombre"]: x["fecha"] for x in porn[1]["mantenimientos"]}
check("ultima limpieza", m1["Limpieza"] == date(2026, 7, 17))
check("el cambio de agujas se separa por el texto",
      m1["Cambio de agujas"] == date(2026, 2, 1))
f3 = porn[3]["ficha"]
check("diametro y galga de '36 / 28'", (f3["diametro"], f3["galga"]) == (36, 28))
check("alimentadores", f3["alimentadores"] == 100)
check("avisa el mantenimiento viejo",
      any("meses" in a for a in porn[3]["avisos"]))
check("la hoja vacia queda afuera con motivo",
      any(d["fila"] == "MQ 2" and "vacía" in d["motivo"] for d in pd_))
check("la maquina que no esta en Asinfo queda afuera",
      any("99" in str(d["texto"]) for d in pd_))

# --- 7. la carga entera, de punta a punta ---------------------------------
print("Carga del Excel:")
import io
from openpyxl import Workbook

wb = Workbook(); ws = wb.active
ws.append(["PLANILLA DE MANTENIMIENTO"])          # basura arriba, como en planta
ws.append([])
ws.append(titulos)
ws.append(["MQ 1", "12/07/2026", "30.000", "Mayer", "MV4", 24, 2015])
ws.append(["MQ 2", "01/06/2026", "40.000", "Terrot", "S296", 28, 2011])
buffer = io.BytesIO(); wb.save(buffer); buffer.seek(0)

guardado_lote = {}
store.cargar_lote = lambda s_, t_, f_: (
    guardado_lote.update(servicios=s_, topes=t_, fichas=f_),
    {"servicios": len(s_), "topes": len(t_), "fichas": len(f_)})[1]

r = c.post("/carga", data={"archivo": (buffer, "planta.xlsx")},
           content_type="multipart/form-data")
check("subir el Excel lleva a revisar", r.status_code == 302 and "/carga/" in r.headers["Location"])
token = r.headers["Location"].rsplit("/", 1)[-1]

r = c.get(f"/carga/{token}")
cuerpo = r.get_data(as_text=True)
check("la revision encuentra las dos maquinas", r.status_code == 200 and "MQ 1" in cuerpo and "MQ 2" in cuerpo)
check("no guarda nada todavia", "servicios" not in guardado_lote)

r = c.post(f"/carga/{token}", data={"boton": "confirmar", "hecho_por": "Mecánico"})
check("confirmar guarda y vuelve al semaforo", r.status_code == 302)
check("guarda los dos mantenimientos", len(guardado_lote.get("servicios", [])) == 2)
check("guarda los topes por maquina", len(guardado_lote.get("topes", [])) == 2)
check("guarda las dos fichas", len(guardado_lote.get("fichas", [])) == 2)
check("el tope quedo en kilos de verdad", guardado_lote["topes"][0][2] == 30000)

r = c.get(f"/carga/{token}")
check("el archivo se borra despues de guardar", r.status_code == 302)

# Pegar las filas tiene que terminar en la MISMA pantalla de revisión que
# subir el archivo: si fueran dos caminos, serían dos lugares donde fallar.
print("Pegar en vez de subir:")
pegado = "\t".join(titulos) + "\nMQ 1\t12/07/2026\t30.000\tMayer\tMV4\t24\t2015"
r = c.post("/carga", data={"pegado": pegado})
check("pegar lleva a la misma revision", r.status_code == 302 and "/carga/" in r.headers["Location"])
token_p = r.headers["Location"].rsplit("/", 1)[-1]
cuerpo = c.get(f"/carga/{token_p}").get_data(as_text=True)
check("lo pegado se lee igual que el archivo", "MQ 1" in cuerpo)
guardado_lote.clear()
c.post(f"/carga/{token_p}", data={"boton": "confirmar", "hecho_por": "Mecánico"})
check("lo pegado guarda el tope", guardado_lote.get("topes", [[None, None, None]])[0][2] == 30000)

r = c.post("/carga", data={"pegado": "solo una linea"}, follow_redirects=True)
check("una sola linea no pasa", "títulos" in r.get_data(as_text=True))
r = c.post("/carga", data={"pegado": "titulo\notro"}, follow_redirects=True)
check("sin separador no pasa", "dos columnas" in r.get_data(as_text=True))
r = c.post("/carga", data={}, follow_redirects=True)
check("sin archivo ni pegado avisa", "pegá las filas" in r.get_data(as_text=True))

# --- 8. arranque en lote ---------------------------------------------------
print("Arranque:")
store.ultimos_por_maquina_y_tipo = lambda: {}
capt = {}
store.registrar_muchos = lambda f: (capt.update(f=f), len(f))[1]
r = c.post("/arranque", data={"tipo_id": "1", "fecha": hoy.isoformat(), "hecho_por": "x"})
check("carga las 3 de una vez", r.status_code == 302 and len(capt["f"]) == 3)
r = c.post("/arranque", data={"tipo_id": "1", "fecha": (hoy + timedelta(days=1)).isoformat(), "hecho_por": "x"})
check("rechaza fecha futura", "futura" in r.get_data(as_text=True))
store.ultimos_por_maquina_y_tipo = lambda: {(m["id"], 1): {"fecha": hoy, "hecho_por": "x"} for m in MAQS}
antes = len(capt["f"])
c.post("/arranque", data={"tipo_id": "1", "fecha": hoy.isoformat(), "hecho_por": "x"})
check("no duplica si ya arrancaron", len(capt["f"]) == antes)

# --- 8b. cargar uno escribiendo el numero ---------------------------------
print("Cargar uno:")
guardado_uno = {}
store.registrar_service = lambda *a, **k: guardado_uno.update(args=a, kw=k)
for escrito, esperado in (("1", 10), ("MQ 2", 11), ("003", 12)):
    guardado_uno.clear()
    r = c.post("/registrar", data={"maquina": escrito, "tipo_id": "1",
                                   "fecha": hoy.isoformat(), "hecho_por": "Luis"})
    check(f"'{escrito}' es la maquina {esperado}",
          guardado_uno.get("args", [None])[0] == esperado)
# repuestos, horas y los tres encargados
guardado_uno.clear()
c.post("/registrar", data={"maquina": "1", "tipo_id": "1", "fecha": hoy.isoformat(),
                           "hecho_por": "Roberto", "repuestos": "12 agujas",
                           "horas": "2,5", "nota": "se trabo el plato"})
args = guardado_uno.get("args", ())
kw = guardado_uno.get("kw", {})
check("guarda quien lo hizo", args[4] == "Roberto")
check("guarda los repuestos", kw.get("repuestos") == "12 agujas")
check("2,5 horas se entienden como 2.5", kw.get("horas") == 2.5)
r = c.post("/registrar", data={"maquina": "1", "tipo_id": "1", "fecha": hoy.isoformat(),
                               "hecho_por": "Roberto", "horas": "999"})
check("rechaza horas absurdas", "razonable" in r.get_data(as_text=True))
check("los tres encargados estan en la lista",
      tuple(store.ENCARGADOS) == ("Roberto", "Darlin", "Humberto"))

r = c.post("/registrar", data={"maquina": "77", "tipo_id": "1",
                               "fecha": hoy.isoformat(), "hecho_por": "Luis"})
check("una maquina que no existe se avisa", "No hay ninguna máquina 77" in r.get_data(as_text=True))
r = c.post("/registrar", data={"maquina": "", "tipo_id": "1",
                               "fecha": hoy.isoformat(), "hecho_por": "Luis"})
check("sin numero pide el numero", "Poné el número" in r.get_data(as_text=True))

# --- 8c. archivos ----------------------------------------------------------
print("Archivos:")
guardados = {}
store.guardar_archivo = lambda nombre, contenido, **k: (
    guardados.update(nombre=nombre, contenido=contenido, extra=k), 1)[1]
store.archivos = lambda id_maquina=None: [
    {"id": 1, "id_maquina": 10, "nombre": "planilla.xlsx", "descripcion": "la de planta",
     "tamano": 2048, "subido_por": "Roberto", "creado_en": datetime(2026, 8, 20)}]
store.archivo = lambda i: {"id": 1, "id_maquina": 10, "nombre": "planilla.xlsx",
                           "contenido": b"PK\x03\x04hola", "descripcion": None,
                           "tamano": 9, "subido_por": None, "creado_en": None}
store.borrar_archivo = lambda i: guardados.update(borrado=i)

r = c.post("/archivos", data={"archivo": (io.BytesIO(b"unos bytes"), "manual.pdf"),
                              "id_maquina": "3", "descripcion": "manual",
                              "subido_por": "Darlin"},
           content_type="multipart/form-data")
check("sube un archivo", r.status_code == 302 and guardados.get("nombre") == "manual.pdf")
check("lo liga a la maquina por el numero", guardados["extra"]["id_maquina"] == 12)
r = c.post("/archivos", data={"descripcion": "sin archivo"},
           content_type="multipart/form-data")
check("sin archivo avisa", "Elegí un archivo" in r.get_data(as_text=True))
r = c.get("/archivos/1")
check("se puede bajar", r.status_code == 200 and b"hola" in r.data)
check("baja con su nombre", "planilla.xlsx" in r.headers.get("Content-Disposition", ""))
r = c.post("/archivos/1/borrar")
check("se puede borrar", guardados.get("borrado") == 1)

# --- 8d. los kilos de cada maquina, todos juntos --------------------------
print("Kilos por maquina:")
puestos = {}
store.guardar_tope = lambda m, t, kg: puestos.update({(m, t): kg})
r = c.post("/kilos", data={"kg_10_1": "250.000", "kg_11_1": "", "kg_12_1": "40000"})
check("guarda los tres de una vez", r.status_code == 302 and len(puestos) == 3)
check("250.000 son doscientos cincuenta mil", puestos[(10, 1)] == 250000)
check("el vacio borra el numero propio", puestos[(11, 1)] is None)
r = c.post("/kilos", data={"kg_10_1": "-5"})
check("rechaza un numero que no es kilos", "mayores que cero" in r.get_data(as_text=True))

# --- 9. las pantallas abren -----------------------------------------------
print("Pantallas:")
store.fichas = lambda: {10: {"marca": "Mayer", "modelo": "Relanit", "galga": 24,
                            "diametro": 32, "alimentadores": 96, "agujas": 2460,
                            "anio": 2017, "serie": "7", "tipo_agujas": None, "nota": None}}
for ruta in ("/", "/registrar", "/tipos", "/arranque", "/carga", "/maquina/10", "/maquinas", "/archivos", "/kilos"):
    r = c.get(ruta)
    check(f"{ruta} abre", r.status_code == 200)
r = c.get("/?solo=vencidas")
check("la campanita filtra las vencidas", r.status_code == 200)
cuerpo = c.get("/maquinas").get_data(as_text=True)
check("la pestaña de maquinas las lista con su ficha",
      "MQ 1" in cuerpo and "MQ 3" in cuerpo and "Mayer" in cuerpo)

print()
if fallos:
    print("FALLARON:", ", ".join(fallos)); sys.exit(1)
print("Todos los tests OK")
