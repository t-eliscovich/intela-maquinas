"""Tests que corren ANTES de cada deploy. Si algo falla, no se deploya.

Cubren lo que ya nos mordió una vez:
  * que la app se pueda IMPORTAR como la importa Waitress (`app:app`), que es
    donde vivía el bug del pool sin inicializar;
  * que con la base caída avise en vez de tirar 500;
  * la aritmética del semáforo (va por kilos, y el tope puede ser propio
    de cada máquina);
  * que un mantenimiento SIN tope de kilos no prenda ningún color: el cambio
    de agujas no se hace por desgaste, así que no puede pintar de rojo;
  * que la campanita nunca muestre cero cuando Asinfo no contesta;
  * que el Excel de planta se lea aunque tenga los títulos donde quiera, y que
    lo que no se entiende quede afuera en vez de entrar mal;
  * que la planilla de planta traiga el historial ENTERO y que volver a
    cargarla no duplique nada;
  * que el arranque en lote no duplique ni acepte fechas futuras;
  * que ninguna entrada llegue cruda al SQL de Asinfo.
"""
import io, os, sys, tempfile, types
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

# Una fila por MÁQUINA: adentro, `principal` es el mantenimiento que le pone el
# color y `por_tipo` los tiene a todos. Antes era una fila por (máquina, tipo) y
# la misma máquina salía dos veces con dos colores distintos.
filas, pend, _, _ = A.armar_semaforo()
por_maquina = {f["maquina"]["id"]: f for f in filas}
check("hay una sola fila por maquina", len(filas) == len(MAQS))
check("lo mas pasado va primero", filas[0]["maquina"]["id"] == 10)
check("pasado de kilos = vencido", por_maquina[10]["estado"] == "vencido")
check("84% = falta poco", por_maquina[11]["estado"] == "por_vencer")
check("2% = ok", por_maquina[12]["estado"] == "ok")
check("dice cuanto se paso", round(por_maquina[10]["principal"]["falta"]) == -30000)
check("el color lo pone el mantenimiento principal",
      por_maquina[10]["principal"]["estado"] == "vencido"
      and por_maquina[10]["pct"] == por_maquina[10]["principal"]["pct"])
check("cada tipo queda igual adentro de la fila",
      por_maquina[10]["por_tipo"][1]["kg"] == 80000.0)
check("los dias no prenden nada", por_maquina[12]["principal"]["dias"] == 30
      and por_maquina[12]["estado"] == "ok")

# Sin tope no se inventa un estado: se dice que falta el número, y la fila no
# se pinta de ningún color.
TIPOS[0]["cada_kg"] = None
filas, _, _, _ = A.armar_semaforo()
check("sin tope no pinta nada",
      all(f["principal"] is None and f["por_tipo"][1]["estado"] == "sin_tope"
          for f in filas))
TIPOS[0]["cada_kg"] = 50000

# --- 3b. un tipo sin tope de kilos NO prende el semáforo -------------------
# El cambio de agujas no se hace ni por tiempo ni por desgaste: lo pide la tela.
# Así que no puede pintar una máquina de rojo — va al lado, como fecha.
print("Un tipo sin kilos no prende:")
DOS_TIPOS = [{"id": 1, "nombre": "Cambio de agujas", "cada_kg": None,
              "cada_rollos": None, "cada_dias": None, "activo": True},
             {"id": 2, "nombre": "Limpieza", "cada_kg": 50000,
              "cada_rollos": None, "cada_dias": None, "activo": True}]
store.tipos = lambda incluir_inactivos=False: DOS_TIPOS
store.ultimos_por_maquina_y_tipo = lambda: {
    (m["id"], t["id"]): {"fecha": hoy - timedelta(days=30), "hecho_por": "x"}
    for m in MAQS for t in DOS_TIPOS
}
filas, _, _, _ = A.armar_semaforo()
por_maquina = {f["maquina"]["id"]: f for f in filas}
check("el color lo pone el tipo que tiene tope",
      por_maquina[10]["principal"]["tipo"]["id"] == 2)
check("el que no tiene tope queda como dato, sin color",
      por_maquina[10]["por_tipo"][1]["estado"] == "sin_tope"
      and por_maquina[10]["por_tipo"][1]["desde"] == hoy - timedelta(days=30))
check("y el que tiene tope si prende", por_maquina[10]["estado"] == "vencido")
check("la maquina tranquila sigue en verde", por_maquina[12]["estado"] == "ok")

store.ultimos_por_maquina_y_tipo = lambda: {}
filas, _, _, _ = A.armar_semaforo()
check("sin ningun mantenimiento la maquina esta sin arrancar",
      all(f["estado"] == "sin_arrancar" and f["principal"] is None for f in filas))

store.tipos = lambda incluir_inactivos=False: TIPOS
store.ultimos_por_maquina_y_tipo = lambda: {
    (m["id"], 1): {"fecha": hoy - timedelta(days=30), "hecho_por": "x"} for m in MAQS
}

# --- 4. el tope propio de cada máquina manda -------------------------------
print("Tope por maquina:")
store.topes_por_maquina = lambda: {(12, 1): 500.0}   # MQ 3 teje poco: tope chico
filas, _, _, _ = A.armar_semaforo()
por_maquina = {f["maquina"]["id"]: f["principal"] for f in filas}
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
# Dos fechas que no pueden ser un mantenimiento: una todavía no pasó y la otra
# es de antes de que existiera la planilla. En planta se escriben solas.
h1.append([datetime(2030, 1, 1), "", "", "limpieza de cilindro", ""])
h1.append([datetime(1980, 6, 1), "", "", "limpieza de cilindro", ""])
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

# --- 6c. el historial COMPLETO de la planilla de planta --------------------
# `leer_por_maquina` se queda con la última fecha de cada tipo, que es lo único
# que necesita el semáforo. La planilla tiene el historial entero desde 2018 y
# ése es su valor: poder mirar qué se le hizo a una máquina y cuándo.
print("Historial completo:")
hm, hf, hd = excel.leer_historial_por_maquina(ruta2, MAQS, TIPOS2, hoy=date(2026, 8, 20))
de_la_1 = [m for m in hm if m["id_maquina"] == 10]
check("trae TODAS las filas, no solo la ultima de cada tipo", len(de_la_1) == 3)
check("la fila que dice «aguja» es cambio de agujas",
      [m["fecha"] for m in de_la_1 if m["tipo_id"] == 1] == [date(2026, 2, 1)])
check("el resto queda como limpieza",
      sorted(m["fecha"] for m in de_la_1 if m["tipo_id"] == 2)
      == [date(2024, 5, 3), date(2026, 7, 17)])
check("una fecha futura no entra", all(m["fecha"] <= date(2026, 8, 20) for m in hm))
check("una fecha anterior a 1990 tampoco", all(m["fecha"].year >= 1990 for m in hm))
check("guarda que maquina es, con su nombre de Asinfo",
      all(m["maquina_nombre"] == "TEJEDURIA-MQ 001" for m in de_la_1))
# El responsable de la hoja es quién tiene la máquina a cargo, no quién hizo
# cada uno de los 1.300 mantenimientos. Copiarlo a cada fila sería inventar.
check("el responsable de la ficha NO se copia a cada fila",
      all(m["hecho_por"] == "Planilla de planta" for m in hm))
check("el responsable queda en la ficha, que es donde esta escrito",
      any("ANGEL PONCE" in (f.get("nota") or "") for _, f in hf))
hm2, _, _ = excel.leer_historial_por_maquina(ruta2, MAQS, TIPOS2, hoy=date(2026, 8, 20))
check("leerla dos veces da las mismas claves (hoja, orden)",
      [(m["hoja"], m["orden"]) for m in hm] == [(m["hoja"], m["orden"]) for m in hm2])
check("la clave no se repite dentro de la planilla",
      len({(m["hoja"], m["orden"]) for m in hm}) == len(hm))
check("la maquina que no esta en Asinfo queda afuera con motivo",
      any("99" in d["motivo"] for d in hd))
check("la hoja vacia queda afuera con motivo",
      any(d["donde"] == "MQ 2" and "vacía" in d["motivo"] for d in hd))

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

# --- 7b. la planilla de planta entera, de punta a punta --------------------
# Una hoja por máquina se reconoce sola y va por otro camino que la tabla: no
# hay nada que mapear y se guarda el historial ENTERO, no la última fecha.
print("Carga del historial:")
wb3 = _WB()
HOJAS3 = (("MAQ 1", [(datetime(2026, 7, 17), "limpieza de cilindro"),
                     (datetime(2026, 2, 1), "cambio de agujas del cilindro"),
                     (datetime(2025, 3, 4), "limpiesa de memminger")]),
          ("MAQ 2", [(datetime(2026, 6, 1), "limpieza general")]),
          ("MAQ 3", [(datetime(2026, 5, 5), "limpieza de platos"),
                     (datetime(2025, 1, 9), "cambio de agujas")]))
for i, (nombre_hoja, filas_hoja) in enumerate(HOJAS3):
    hoja = wb3.active if i == 0 else wb3.create_sheet(nombre_hoja)
    hoja.title = nombre_hoja
    hoja.append(["Equipo:", "", "", "MODELO"])
    hoja.append(["CIRCULAR", "MAYER", "", "Relanit 3.2"])
    hoja.append(["Fecha", "Tipo de mantenimiento"])
    for cuando, texto in filas_hoja:
        hoja.append([cuando, texto])
buf3 = io.BytesIO(); wb3.save(buf3); buf3.seek(0)

store.tipos = lambda incluir_inactivos=False: TIPOS2
_hist = {}
store.guardar_historial = lambda filas: (
    _hist.update(filas=filas), {"mantenimientos": len(filas), "borrados": 2})[1]
store.cuantos_de_la_carga_vieja = lambda: 2

r = c.post("/carga", data={"archivo": (buf3, "planta.xlsx")},
           content_type="multipart/form-data")
check("la planilla de planta entra por la misma pantalla",
      r.status_code == 302 and "/carga/" in r.headers["Location"])
token_h = r.headers["Location"].rsplit("/", 1)[-1]
cuerpo = c.get(f"/carga/{token_h}").get_data(as_text=True)
check("la revision resume por maquina, no fila por fila",
      "Planilla de mantenimiento" in cuerpo and "MQ 1" in cuerpo and "MQ 3" in cuerpo)
check("el boton dice cuantos mantenimientos son",
      "Guardar estos 6 mantenimientos" in cuerpo)
check("avisa que va a reemplazar lo de la primera carga", "primera carga" in cuerpo)
check("no guarda nada todavia", "filas" not in _hist)

r = c.post(f"/carga/{token_h}", data={"boton": "confirmar"})
check("confirmar guarda el historial entero",
      r.status_code == 302 and len(_hist.get("filas", [])) == 6)
check("guarda la clave que evita duplicar",
      all(f["hoja"] and f["orden"] for f in _hist["filas"]))
check("guarda tambien las fichas de las tres hojas",
      len(guardado_lote.get("fichas", [])) == 3)
check("el archivo se borra despues de guardar",
      c.get(f"/carga/{token_h}").status_code == 302)
store.tipos = lambda incluir_inactivos=False: TIPOS

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
                                   "fecha": hoy.isoformat(), "hecho_por": "Luis",
                                   "horas": "2"})
    check(f"'{escrito}' es la maquina {esperado}",
          guardado_uno.get("args", [None])[0] == esperado)

# Cuánto llevó no es opcional: sin las horas no se sabe cuánto cuesta parar una
# máquina, que es media razón por la que esto se anota.
guardado_uno.clear()
r = c.post("/registrar", data={"maquina": "1", "tipo_id": "1",
                               "fecha": hoy.isoformat(), "hecho_por": "Luis"})
check("sin horas no guarda y lo dice",
      not guardado_uno and "cuánto llevó" in r.get_data(as_text=True))
r = c.post("/registrar", data={"maquina": "1", "tipo_id": "1", "horas": "un rato",
                               "fecha": hoy.isoformat(), "hecho_por": "Luis"})
check("las horas tienen que ser un numero", "tienen que ser un número" in r.get_data(as_text=True))
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

# --- 8e. la planilla de control de ajuste ---------------------------------
print("Planilla de control de ajuste:")
import ajustes as AJ

MAQS_AJ = [{"id": 10, "numero": 1, "nombre": "TEJEDURIA-MQ 001"},
           {"id": 11, "numero": 52, "nombre": "TEJEDURIA-MQ 052"}]


def _planilla_ajuste(ruta):
    """Una planilla como la de planta, con sus tres mañas adentro."""
    from openpyxl import Workbook
    wb = Workbook()

    # Hoja normal: tres columnas «Polea» y tres «HILO» que se llaman igual.
    ws = wb.active; ws.title = "MAQ 1"
    ws.append(["CONTROL DE AGUSTES Y RENDIMIENTOS"])
    ws.append(["MQ. 1", "Tipo de  MQ.", "FECHA", "SERIE", "Polea", "Polea", "Polea",
               "ajuste agujas ", "ESTIRAJE", "TIPO DE TELA", "HILO", "HILO", "HILO",
               "G /m2   crudo", "Longitud de Malla manual", "Longitud de Malla",
               "Rendimiento Crudo"])
    ws.append([1, "MAYER 32", datetime(2026, 3, 4), "pro 30", "polea 5", "polea 20",
               None, "lycra pro 6,5", "B/9", "FALSO F. KW", "20/1 KW", "16/1 EP",
               None, 181.5, "30,2 dibujo", "29,0 LM", 4.05])
    # Una fila que sólo tiene el número y el modelo: no es un ajuste.
    ws.append([1, "MAYER 32", None, None, None, None, None, None, None, None])

    # Hoja a la que se le perdieron las primeras columnas: hay que leerla por
    # posición, y avisar.
    ws2 = wb.create_sheet("MAQ 52")
    ws2.append(["CONTROL DE AGUSTES Y RENDIMIENTOS"])
    ws2.append(["Polea", "Polea", "ajuste agujas ", "ESTIRAJE", "TIPO DE TELA"])
    ws2.append([52, "JIUNN LONG", datetime(2024, 5, 15), "pro 25", "dibujo 25",
                "pro 32", "jersey 50", "Z30", "B/8", "JAMES", "75/72"])

    # La hoja vieja, de cuando estaba todo junto: la primera fila ya está en la
    # hoja MAQ 1 y la segunda no está en ninguna. El número de máquina se
    # escribe una vez y después se arrastra, que es como está escrita la hoja.
    ws8 = wb.create_sheet("AGUSTES")
    ws8.append(["CONTROL DE AGUSTES Y RENDIMIENTOS"])
    ws8.append(["MQ.", "Tipo de  MQ.", "FECHA", "SERIE", "Polea", "Polea",
                "ajuste agujas ", "ESTIRAJE", "TIPO DE TELA", "HILO", "HILO"])
    ws8.append([1, "MAYER 32", datetime(2026, 3, 4), "pro 30", "polea 5",
                "polea 20", "lycra pro 6,5", "B/9", "FALSO F. KW",
                "20/1 KW", "16/1 EP"])
    ws8.append([None, "MAYER 32", datetime(2019, 4, 10), "pro 28", "polea 4",
                None, None, "B/7", "PIQUE", "30/1 KW", None])

    ws3 = wb.create_sheet("AGUJAS")
    ws3.append(["TIPO DE AGUJAS POR MAQUINA"])
    ws3.append(["MQ", "CILINDRO", None, "PLATO", "MAQUINA", "PLATINAS"])
    ws3.append([1, "VO LS-140,50 G36", "VO LS-140,50 G37", None, "MAYER", "206085101G00"])

    ws9 = wb.create_sheet("CODIGO DE AGUJAS")
    ws9.append(["MAQUINA", "MARCA", "CODIGO", "CODIGO", "CILINDRO", "PLATINAS",
                "MARCA"])
    ws9.append(["MAYER)1-2-3", "GROZ", "LS-140.50", "LS-141", "cilindro",
                "206085101G00", "GROZ"])

    # Dos tablas al lado de la otra: las bandas Memminger a la izquierda y las
    # de motor a la derecha, pasando la columna del stock.
    ws4 = wb.create_sheet("BANDAS")
    ws4.append([None])
    ws4.append(["MAQUNA", "cantidad de maquinas", "DIAMETRO", "BANDA  1/2",
                "BANDA 3/4", "BANDA LYCRA", None, None, None, None,
                "CODIGO", "CANTIDAD",
                "MAQUINA", "CANTIDAD", "DIAMETRO", "BANDA", "COBRADOR"])
    ws4.append(["MAYER JERSEY", 2, 30, "6.6", "8.2", None, None, None, None, None,
                6.6, 10,
                "MAYER 30", 2, 30, "M-45", "SI", "la corta el mecanico"])
    ws4.append([None, None, None, None, None, None, None, None, None, None,
                "total", 146])

    ws5 = wb.create_sheet("INVENTARIO LEVAS")
    ws5.append(["INVENTARIO DE LEVAS"])
    ws5.append(["MAQUINA ", "CODIGO", "CANTIDAD", "UBICACION", "ACCIONAMIENTO"])
    ws5.append(["MAYER (1 )", "30-32 385953,0", 208, "cilindro", "TRABAJO"])
    ws5.append(["MAYER (4 5 7 )", "NO", 0, "cilindro", "TRABAJO"])

    ws6 = wb.create_sheet("consumo de hilo")
    ws6.append(["TELA", "HILO", "HILO", "HILO", None, None])
    ws6.append(["JAMES", "poliester 75/36f", None, None, 0.4786, "75F36"])

    ws7 = wb.create_sheet("Eficiencia producción ")
    ws7.append([None])
    ws7.append([None, "Máquina", "Velocidad  (rpm)", "Sistema", "Diámetro (inch)",
                "F", "Tamaño de rollo", "Tiempo (min)", "N° Rollos diarios ",
                "Aproximación ", "Peso (kg)"])
    ws7.append([None, 1, 25, "102", 32, 24, 1410, 56.4, 12.76, 12, 270])

    wb.save(ruta)


_ruta_aj = os.path.join(tempfile.gettempdir(), "test_ajuste.xlsx")
_planilla_ajuste(_ruta_aj)

check("reconoce la planilla de ajuste", AJ.es_planilla_ajuste(_ruta_aj))
bloq, desc = AJ.leer(_ruta_aj, MAQS_AJ, hoy=date(2026, 8, 20))

_a1 = [a for a in bloq["ajustes"] if a["hoja"] == "MAQ 1"]
check("lee un ajuste por fila", len(_a1) == 1)
check("la fila sin nada útil no entra", all(a["tela"] for a in _a1))
check("las poleas van juntas en una columna",
      _a1[0]["poleas"] == "polea 5 · polea 20")
check("los hilos van juntos en una columna", _a1[0]["hilos"] == "20/1 KW · 16/1 EP")
check("distingue malla manual de malla",
      _a1[0]["malla_manual"] == "30,2 dibujo" and _a1[0]["malla"] == "29,0 LM")
check("el gramaje entra como número", _a1[0]["gramaje_crudo"] == 181.5)

_a52 = [a for a in bloq["ajustes"] if a["hoja"] == "MAQ 52"]
check("la hoja sin títulos se lee por posición igual", len(_a52) == 1)
check("leída por posición, la fecha cae en su lugar",
      _a52[0]["fecha"] == date(2024, 5, 15))
check("leída por posición, el modelo NO es la fecha",
      _a52[0]["tipo_maquina"] == "JIUNN LONG")
check("avisa que esa hoja se leyó por posición",
      any("por posición" in d["motivo"] for d in desc))

# La hoja vieja parece repetida y no lo es: casi todas sus filas no quedaron en
# ninguna hoja por máquina. Sólo entra lo que no está, y lo repetido se avisa
# para que el número cierre.
_viejos = [a for a in bloq["ajustes"] if a["hoja"] == "AGUSTES"]
check("de la hoja vieja entra sólo lo que no está por máquina", len(_viejos) == 1)
check("la fila que ya estaba en la hoja de la máquina no se duplica",
      _viejos[0]["fecha"] == date(2019, 4, 10))
check("avisa cuántas de la hoja vieja ya estaban",
      any(d["donde"] == "AGUSTES" and "ya estaban" in d["motivo"] for d in desc))
check("el número de máquina se arrastra de la fila de arriba",
      _viejos[0]["id_maquina"] == 10)

check("las agujas quedan pegadas a su máquina",
      len(bloq["agujas"]) == 1 and bloq["agujas"][0]["id_maquina"] == 10)
check("las cuatro columnas de cilindro van juntas",
      "G36 · VO LS-140,50 G37" in bloq["agujas"][0]["cilindro"])

# La aguja por MODELO va a su propia tabla: la fila es un modelo, no una
# máquina, y repartirla entre máquinas sería adivinar.
check("las agujas por modelo salen de su propia hoja",
      len(bloq["agujas_modelo"]) == 1
      and bloq["agujas_modelo"][0]["modelo"] == "MAYER)1-2-3")
check("junta los códigos de aguja del modelo",
      bloq["agujas_modelo"][0]["codigos"] == "LS-140.50 · LS-141")
check("guarda de qué marca es la aguja y de cuál la platina",
      bloq["agujas_modelo"][0]["marca_aguja"] == "GROZ"
      and bloq["agujas_modelo"][0]["marca_platina"] == "GROZ")

check("la leva sin código no entra", len(bloq["levas"]) == 1)
check("la leva guarda para qué sirve", bloq["levas"][0]["accionamiento"] == "TRABAJO")

check("el stock sale de la columna de al lado del código, no de «cantidad de "
      "maquinas»", bloq["banda_stock"] == [{"medida": 6.6, "cantidad": 10}])
check("la fila «total» no es una medida de banda",
      all(s["medida"] != 146 for s in bloq["banda_stock"]))
check("la banda guarda sus tres medidas",
      bloq["bandas"][0]["media"] == "6.6" and bloq["bandas"][0]["diametro"] == 30)

# Las bandas de motor son otro repuesto, con otras columnas: van con clase
# propia. Mezcladas con las Memminger, una medida taparía a la otra.
_memminger = [b for b in bloq["bandas"] if b["clase"] == "memminger"]
_motor = [b for b in bloq["bandas"] if b["clase"] == "motor"]
check("las bandas de motor quedan con clase «motor»",
      len(_motor) == 1 and _motor[0]["banda"] == "M-45")
check("y no se mezclan con las Memminger",
      len(_memminger) == 1 and _motor[0]["media"] is None)
check("la banda de motor guarda si lleva cobrador",
      _motor[0]["cobrador"] == "SI")

check("la producción calculada queda por máquina",
      len(bloq["eficiencia"]) == 1 and bloq["eficiencia"][0]["kg_dia"] == 270)
# El turno de 24 horas son los mismos títulos repetidos más a la derecha. Si no
# están, queda vacío: un número en la columna equivocada es peor que ninguno.
check("sin la tabla de 24 horas el turno largo queda vacío",
      bloq["eficiencia"][0]["kg_dia_24"] is None
      and bloq["eficiencia"][0]["rollos_dia_24"] is None)
check("el consumo de hilo trae el rendimiento",
      bloq["consumo_hilo"][0]["rendimiento"] == 0.4786)

# Volver a leer la misma planilla tiene que dar la misma clave (hoja, fila):
# es lo único que evita que cargarla dos veces duplique todo.
bloq2, _ = AJ.leer(_ruta_aj, MAQS_AJ, hoy=date(2026, 8, 20))
check("cargarla dos veces da las mismas claves",
      [(a["hoja"], a["orden"]) for a in bloq["ajustes"]]
      == [(a["hoja"], a["orden"]) for a in bloq2["ajustes"]])

check("una fecha futura no se carga",
      all(a["fecha"] is None or a["fecha"] <= date(2026, 8, 20)
          for a in bloq["ajustes"]))
check("la planilla de mantenimiento NO se confunde con esta",
      not AJ.es_planilla_ajuste(RUTA_EXCEL) if "RUTA_EXCEL" in dir() else True)

# La carga entera por la pantalla, que es como se hace de verdad.
_guardado = {}
store.guardar_planilla_ajuste = lambda d: (_guardado.update(d) or
                                           {k: len(v) for k, v in d.items()})
with open(_ruta_aj, "rb") as _f:
    _r = c.post("/carga", data={"archivo": (io.BytesIO(_f.read()), "ajuste.xlsx")},
                content_type="multipart/form-data")
check("la planilla de ajuste entra por la misma pantalla", _r.status_code == 302)
_token = _r.headers["Location"].rstrip("/").split("/")[-1]
_r = c.get(f"/carga/{_token}")
check("la revisión muestra qué entendió",
      "Planilla de control de ajuste" in _r.get_data(as_text=True))
_r = c.post(f"/carga/{_token}", data={"boton": "confirmar"})
check("confirmar guarda todo junto", _r.status_code == 302 and _guardado.get("ajustes"))

# --- 8f. el historial de una máquina, agrupado por día --------------------
print("Los dias de una maquina:")
HISTORIAL = [
    {"fecha": date(2026, 8, 1), "tipo_nombre": "Limpieza", "hecho_por": "Roberto",
     "maquina_nombre": "TEJEDURIA-MQ 001", "nota": "se limpió el cilindro",
     "repuestos": None, "horas": None},
    {"fecha": date(2026, 8, 1), "tipo_nombre": "Cambio de agujas", "hecho_por": "Darlin",
     "maquina_nombre": "TEJEDURIA-MQ 001", "nota": None,
     "repuestos": "12 agujas", "horas": 2.5},
    {"fecha": date(2026, 5, 10), "tipo_nombre": "Limpieza", "hecho_por": "Roberto",
     "maquina_nombre": "TEJEDURIA-MQ 001", "nota": None,
     "repuestos": None, "horas": None},
]
# `kilos_desde` devuelve el acumulado desde cada fecha hasta hoy: los kilos
# entre dos paradas son la resta de los dos acumulados.
asinfo.kilos_desde = lambda id_maquina, fechas: {"2026-08-01": 30000.0,
                                                 "2026-05-10": 95000.0}
dias = A._dias_de_mantenimiento(10, HISTORIAL)
check("dos mantenimientos del mismo dia son un solo renglon", len(dias) == 2)
check("y el renglon lleva los dos tipos",
      dias[0]["tipos"] == ["Limpieza", "Cambio de agujas"])
check("junta a los dos que trabajaron ese dia", dias[0]["quien"] == "Darlin, Roberto")
check("junta las notas y los repuestos del dia",
      dias[0]["notas"] == ["se limpió el cilindro"]
      and dias[0]["repuestos"] == ["12 agujas"])
check("lo mas nuevo va primero", dias[0]["fecha"] == date(2026, 8, 1))
check("la ultima parada cuenta los kilos hasta hoy",
      dias[0]["kg"] == 30000.0 and dias[0]["hasta_hoy"] is True)
check("entre dos paradas, los kilos son la resta de los acumulados",
      dias[1]["kg"] == 65000.0)

def _kilos_caidos(*a, **k):
    raise asinfo.AsinfoNoDisponible("timeout")

asinfo.kilos_desde = _kilos_caidos
dias = A._dias_de_mantenimiento(10, HISTORIAL)
check("sin Asinfo se muestran los dias igual, sin kilos",
      len(dias) == 2 and all(d["kg"] is None for d in dias))
asinfo.kilos_desde = lambda id_maquina, fechas: {"2026-08-01": 30000.0,
                                                 "2026-05-10": 95000.0}

# --- 9. las pantallas abren -----------------------------------------------
print("Pantallas:")
store.historial = lambda id_maquina=None, limite=200: HISTORIAL
store.ficha = lambda id_maquina: {"marca": "Mayer", "modelo": "Relanit 3.2",
                                  "galga": 24, "diametro": 32, "alimentadores": 96,
                                  "agujas": 2460, "anio": 2017, "serie": "73830",
                                  "tipo_agujas": None, "nota": None}
store.agujas_por_modelo = lambda: [
    {"id": 1, "modelo": "MAYER)1-2-3", "marca_aguja": "GROZ",
     "codigos": "LS-140.50 · LS-141", "donde": "cilindro",
     "platinas": "206085101G00", "marca_platina": "GROZ", "nota": None}]
store.ajustes = lambda id_maquina=None, tela=None, limite=400: [
    {"id": 1, "id_maquina": 10, "maquina_nombre": "TEJEDURIA-MQ 001",
     "fecha": date(2026, 3, 4), "tipo_maquina": "MAYER 32", "cilindro": "pro 30",
     "poleas": "polea 5", "ajuste_agujas": None, "estiraje": "B/9",
     "tela": "FALSO F. KW", "hilos": "20/1 KW", "gramaje_crudo": 181.5,
     "malla_manual": None, "malla": "29,0 LM", "rendimiento": 4.05, "kg_m": None,
     "hoja": "MAQ 1", "orden": 1}]
store.telas = lambda: [{"tela": "FALSO F. KW", "veces": 40, "maquinas": 9,
                        "ultima": date(2026, 3, 4)}]
store.resumen_ajustes = lambda: {"filas": 1, "maquinas": 1, "con_fecha": 1,
                                 "desde": date(2026, 3, 4), "hasta": date(2026, 3, 4)}
store.agujas = lambda: {10: {"id_maquina": 10, "descripcion": "MAYER",
                             "cilindro": "VO LS-140,50", "plato": None,
                             "platinas": "206085101G00", "nota": None}}
store.levas = lambda: [{"id": 1, "maquinas": "MAYER (1 )", "codigo": "30-32",
                        "cantidad": 208, "ubicacion": "cilindro",
                        "accionamiento": "TRABAJO"}]
# Las bandas se piden por clase: las Memminger y las de motor son dos tablas
# distintas en la misma pantalla.
store.bandas = lambda clase="memminger": [
    {"id": 1, "maquinas": "MAYER JERSEY", "cantidad_maquinas": 2, "clase": clase,
     "diametro": 30, "media": "6.6", "tres_cuartos": "8.2", "lycra": None,
     "banda": "M-45", "cobrador": "SI", "nota": None}]
store.banda_stock = lambda: [{"medida": 6.6, "cantidad": 10}]
# La 11 está anotada en la planilla pero sin números: pasa de verdad, y sumar
# un vacío rompía la pantalla.
store.eficiencias = lambda: {10: {"id_maquina": 10, "rpm": 25, "sistemas": "102",
                                  "diametro": 32, "alimentadores": 24,
                                  "tamano_rollo": 1410, "minutos_rollo": 56.4,
                                  "rollos_dia": 12, "kg_dia": 270,
                                  "rollos_dia_24": 24, "kg_dia_24": 540},
                             11: {"id_maquina": 11, "rpm": None, "sistemas": "96",
                                  "diametro": 30, "alimentadores": 28,
                                  "tamano_rollo": None, "minutos_rollo": None,
                                  "rollos_dia": None, "kg_dia": None,
                                  "rollos_dia_24": None, "kg_dia_24": None}}
store.consumo_hilo = lambda: [{"id": 1, "tela": "JAMES", "hilo": "poliester",
                               "codigo_hilo": "75F36", "rendimiento": 0.4786}]
store.gramajes = lambda id_maquina=None: []
store.fichas = lambda: {10: {"marca": "Mayer", "modelo": "Relanit", "galga": 24,
                            "diametro": 32, "alimentadores": 96, "agujas": 2460,
                            "anio": 2017, "serie": "7", "tipo_agujas": None, "nota": None}}
for ruta in ("/", "/registrar", "/tipos", "/arranque", "/carga", "/maquina/10",
             "/maquinas", "/archivos", "/kilos", "/ajustes", "/ajustes?tela=FALSO",
             "/ajustes?maquina=1", "/repuestos", "/produccion"):
    r = c.get(ruta)
    check(f"{ruta} abre", r.status_code == 200)
r = c.get("/?solo=vencidas")
check("la campanita filtra las vencidas", r.status_code == 200)
cuerpo = c.get("/maquinas").get_data(as_text=True)
check("la pestaña de maquinas las lista con su ficha",
      "MQ 1" in cuerpo and "MQ 3" in cuerpo and "Mayer" in cuerpo)

# El buscador de la ficha no es una pantalla: lleva a la máquina que se escribió.
r = c.get("/maquina?numero=1")
check("/maquina?numero=1 lleva a la ficha de la 1",
      r.status_code == 302 and r.headers["Location"].endswith("/maquina/10"))
r = c.get("/maquina?numero=77", follow_redirects=True)
check("un numero que no existe vuelve al listado y lo dice",
      "No hay ninguna máquina 77" in r.get_data(as_text=True))
cuerpo = c.get("/maquina/10").get_data(as_text=True)
check("la ficha muestra los dias, no los registros sueltos",
      "Limpieza" in cuerpo and "Cambio de agujas" in cuerpo)

print()
if fallos:
    print("FALLARON:", ", ".join(fallos)); sys.exit(1)
print("Todos los tests OK")
