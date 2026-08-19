"""Tests que corren ANTES de cada deploy. Si algo falla, no se deploya.

Cubren lo que ya nos mordió una vez:
  * que la app se pueda IMPORTAR como la importa Waitress (`app:app`), que es
    donde vivía el bug del pool sin inicializar;
  * que con la base caída avise en vez de tirar 500;
  * la aritmética del semáforo (gana el umbral que se cumpla primero);
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

# --- 3. aritmética del semáforo -------------------------------------------
print("Semaforo:")
hoy = date.today()
TIPOS = [{"id":1,"nombre":"T","cada_kg":50000,"cada_rollos":2500,"cada_dias":90,"activo":True}]
MAQS = [{"id":10,"codigo":"1","nombre":"MQ 1"},{"id":11,"codigo":"2","nombre":"MQ 2"},{"id":12,"codigo":"3","nombre":"MQ 3"}]
store.tipos = lambda incluir_inactivos=False: TIPOS
store.historial = lambda id_maquina=None, limite=200: []
store.responsables = lambda: []
store.ultimos_por_maquina_y_tipo = lambda: {
    (10,1): {"fecha": hoy - timedelta(days=120), "hecho_por": "x"},   # vencido por dias
    (11,1): {"fecha": hoy - timedelta(days=75),  "hecho_por": "x"},   # 83% -> falta poco
    (12,1): {"fecha": hoy - timedelta(days=10),  "hecho_por": "x"},   # ok
}
asinfo.maquinas = lambda: (MAQS, datetime.utcnow(), True)
asinfo.acumulados = lambda pares: (
    {(m,t): ((hoy - date.fromisoformat(d)).days * 400.0, (hoy - date.fromisoformat(d)).days * 2)
     for m, t, d in pares}, datetime.utcnow(), True)
A.ERROR_ARRANQUE = None
filas, pend, _, _ = A.armar_semaforo()
estados = [f["estado"] for f in filas]
check("lo mas urgente primero", estados[0] == "vencido")
check("un vencido, un por_vencer, un ok", sorted(estados) == ["ok","por_vencer","vencido"])
check("gana el umbral que se cumple primero (dias)", filas[0]["pct_dias"] > 1 and filas[0]["pct_kg"] < 1)

# --- 4. arranque en lote ---------------------------------------------------
print("Arranque:")
store.ultimos_por_maquina_y_tipo = lambda: {}
capt = {}
store.registrar_muchos = lambda f: (capt.update(f=f), len(f))[1]
r = c.post("/arranque", data={"tipo_id":"1","fecha":hoy.isoformat(),"hecho_por":"x"})
check("carga las 3 de una vez", r.status_code == 302 and len(capt["f"]) == 3)
r = c.post("/arranque", data={"tipo_id":"1","fecha":(hoy+timedelta(days=1)).isoformat(),"hecho_por":"x"})
check("rechaza fecha futura", "futura" in r.get_data(as_text=True))
store.ultimos_por_maquina_y_tipo = lambda: {(m["id"],1):{"fecha":hoy,"hecho_por":"x"} for m in MAQS}
antes = len(capt["f"])
c.post("/arranque", data={"tipo_id":"1","fecha":hoy.isoformat(),"hecho_por":"x"})
check("no duplica si ya arrancaron", len(capt["f"]) == antes)

print()
if fallos:
    print("FALLARON:", ", ".join(fallos)); sys.exit(1)
print("Todos los tests OK")
