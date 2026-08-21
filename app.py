"""Mantenimiento de máquinas — Intela tejeduría.

Las pantallas:

    /            semáforo: qué máquina necesita mantenimiento
    /registrar   cargar un mantenimiento hecho
    /carga       subir el Excel de planta (fechas, kilos y ficha, de una vez)
    /tipos       los tipos de mantenimiento y cada cuántos kilos van
    /maquina/N   la ficha de una máquina

Los kilos NO se cargan a mano: salen de Asinfo, que ya registra cada rollo de
tela cruda con la máquina que lo tejió.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import tempfile
import time
from datetime import date, datetime
from decimal import Decimal
from functools import wraps

from flask import (Flask, Response, flash, g, redirect, render_template,
                   request, session, url_for)

import jinja2

import ajustes as ajustes_excel
import asinfo
import config
import excel
import store

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # un Excel de planta es chico

# Cuánto antes del tope avisa. Al 80% avisaba demasiado pronto: con topes de
# 20.000 kg quedaban 4.000 kg por delante, casi un mes de tejido, y la fila
# vivía en naranja. Decisión de la dueña, 20/08/2026.
AVISO = 0.90

CARPETA = os.path.dirname(os.path.abspath(__file__))


# Qué commit está corriendo. Lo escribe el auto-updater al reemplazar la
# carpeta; si el archivo no está, decimos que no se sabe en vez de inventar.
def _version():
    for ruta in (os.path.join(CARPETA, ".version"), r"C:\maquinas_update\.commit"):
        try:
            with open(ruta) as f:
                return f.read().strip()[:7] or "?"
        except OSError:
            continue
    return "?"


def _desplegado():
    """Cuándo se instaló el código que está corriendo AHORA.

    No sale de ningún archivo que alguien tenga que acordarse de escribir: es
    la fecha del código mismo. Existe porque el commit de arriba se quedó
    pegado en un deploy viejo y no había forma de darse cuenta — se pusheaba,
    la pantalla cambiaba, y `version` seguía mostrando lo de la mañana. Media
    hora perdida mirando el número equivocado.
    """
    ultimo = 0.0
    try:
        for nombre in os.listdir(CARPETA):
            if nombre.endswith(".py"):
                ultimo = max(ultimo, os.path.getmtime(os.path.join(CARPETA, nombre)))
    except OSError:
        return None
    if not ultimo:
        return None
    return datetime.fromtimestamp(ultimo).isoformat(timespec="seconds")


VERSION = _version()
DESPLEGADO = _desplegado()

# Dónde se guarda el Excel mientras se revisa antes de confirmar.
CARPETA_CARGA = os.path.join(tempfile.gettempdir(), "maquinas_carga")
_TOKEN = re.compile(r"^[0-9a-f]{16}$")

# ---------------------------------------------------------------------------
# Arranque del pool de la base — A NIVEL DE MÓDULO, a propósito.
#
# ⚠ Esto NO puede vivir dentro de `if __name__ == "__main__"`. En producción
# Waitress hace `import app` y toma el objeto `app`: el bloque __main__ nunca
# corre. Si el pool se abriera sólo ahí, la app levantaría igual, tomaría el
# puerto, la tarea diría "Running"... y CADA pantalla devolvería 500 con un
# `AssertionError: init_pool() no fue llamado`. Pasó exactamente eso el
# 18/08/2026 en el primer deploy.
# ---------------------------------------------------------------------------
ERROR_ARRANQUE: str | None = None
try:
    store.init_pool()
except Exception as _exc:  # noqa: BLE001
    ERROR_ARRANQUE = str(_exc)
    logging.getLogger(__name__).exception("No se pudo abrir el pool de la base")


@app.before_request
def _frenar_si_no_hay_base():
    """Si la base nunca abrió, decirlo en castellano en vez de tirar 500."""
    if ERROR_ARRANQUE and request.endpoint not in ("healthz", "login", "static"):
        return render_template("sin_base.html", error=ERROR_ARRANQUE), 503


# --------------------------------------------------------------------------
# Login (una sola contraseña compartida)
# --------------------------------------------------------------------------
def requiere_login(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if config.PASSWORD and not session.get("ok"):
            return redirect(url_for("login", next=request.path))
        return f(*a, **kw)

    return wrapper


def _tipo_elegido(escrito, tipos):
    """Cuál de los mantenimientos se eligió.

    Si el casillero llegó vacío, el `int()` pelado tiraba «invalid literal for
    int() with base 10», que en planta no le dice nada a nadie.
    """
    try:
        tipo_id = int(escrito)
    except (TypeError, ValueError):
        raise ValueError("Falta elegir qué se le hizo.")
    if not any(t["id"] == tipo_id for t in tipos):
        raise ValueError("Ese mantenimiento no existe.")
    return tipo_id


def _adonde_iba():
    """La pantalla que pedía antes de mandarlo a poner la contraseña.

    Tiene que ser una pantalla de acá: una dirección de afuera pegada en
    `?next=` mandaría a la gente a otro sitio justo después de escribir la
    contraseña. Se aceptan sólo las que empiezan con una barra sola.
    """
    pedida = request.args.get("next") or ""
    if pedida.startswith("/") and not pedida.startswith("//"):
        return pedida
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == config.PASSWORD:
            session["ok"] = True
            return redirect(_adonde_iba() or url_for("semaforo"))
        flash("Contraseña incorrecta.", "error")
    return render_template("login.html")


@app.route("/salir")
def salir():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------
# El cálculo: cuántos kilos lleva cada máquina desde su último mantenimiento
# --------------------------------------------------------------------------
def tope_de(topes, id_maquina, tipo):
    """Cada cuántos kilos va ese mantenimiento EN ESA máquina.

    Si la máquina tiene su número propio, manda ése. Si no, el del tipo. El
    fallback existe para no tener que cargar 43 × 2 filas antes de que el
    semáforo sirva para algo.
    """
    propio = topes.get((id_maquina, tipo["id"]))
    if propio:
        return float(propio), True
    return (float(tipo["cada_kg"]), False) if tipo["cada_kg"] else (None, False)


def armar_semaforo():
    """Una fila por MÁQUINA.

    El semáforo se prende con los mantenimientos que TIENEN un tope de kilos:
    ésos son los que se vencen por desgaste. El cambio de agujas no se hace
    porque pasó el tiempo — se hace cuando la tela lo pide — así que no pinta
    nada de rojo: va al lado, como fecha, para saber cuándo fue el último.
    Mezclarlos hacía que la lista mostrara la misma máquina dos veces con dos
    colores distintos, que era justo lo que había que evitar.
    """
    tipos = store.tipos()
    maquinas, _, _ = asinfo.maquinas()
    ultimos = store.ultimos_por_maquina_y_tipo()
    topes = store.topes_por_maquina()

    hoy = date.today()
    pares = []
    for maquina in maquinas:
        for tipo in tipos:
            ultimo = ultimos.get((maquina["id"], tipo["id"]))
            if ultimo:
                pares.append((maquina["id"], tipo["id"], ultimo["fecha"].isoformat()))

    acum, leido_en, fresco = asinfo.acumulados(pares)

    # Qué mantenimientos PRENDEN el semáforo: los que en alguna máquina tienen
    # puesto un tope de kilos. El cambio de agujas no lleva tope a propósito —se
    # hace cuando la tela lo pide—, así que una máquina a la que sólo le falta
    # el tope de limpieza no tiene que decir «sin tope · Cambio de agujas»: eso
    # hacía parecer que faltaba un número que nadie va a poner nunca.
    prenden = {tipo_id for (_, tipo_id) in topes}
    prenden |= {t["id"] for t in tipos if t["cada_kg"]}
    # Si todavía no hay ningún tope puesto en ningún lado no hay distinción que
    # hacer: cuentan todos, y la pantalla dice que falta el número.
    if not prenden:
        prenden = {t["id"] for t in tipos}

    filas, pendientes = [], []
    for maquina in maquinas:
        por_tipo, con_tope = {}, []
        for tipo in tipos:
            tope, propio = tope_de(topes, maquina["id"], tipo)
            ultimo = ultimos.get((maquina["id"], tipo["id"]))
            if not ultimo:
                por_tipo[tipo["id"]] = {"tipo": tipo, "tope": tope, "desde": None,
                                        "estado": "sin_arrancar"}
                if tope:
                    pendientes.append({"maquina": maquina, "tipo": tipo})
                continue

            desde = ultimo["fecha"]
            kg, rollos = acum.get((maquina["id"], tipo["id"]), (0.0, 0))
            pct = (float(kg) / tope) if tope else None
            if pct is None:
                estado = "sin_tope"
            elif pct >= 1.0:
                estado = "vencido"
            elif pct >= AVISO:
                estado = "por_vencer"
            else:
                estado = "ok"

            info = {
                "tipo": tipo, "desde": desde, "hecho_por": ultimo["hecho_por"],
                "kg": kg, "rollos": rollos, "dias": (hoy - desde).days,
                "tope": tope, "tope_propio": propio,
                "falta": (tope - float(kg)) if tope else None,
                "pct": pct, "estado": estado,
            }
            por_tipo[tipo["id"]] = info
            if tope:
                con_tope.append(info)

        if not por_tipo:
            continue
        # El peor de los que se vencen por kilos manda el color de la fila.
        if con_tope:
            principal = max(con_tope, key=lambda i: i["pct"] or 0)
        else:
            # Sin tope no hay semáforo, pero el dato sirve igual: se muestra
            # cuántos kilos lleva desde la última vez que se le hizo. Un guión
            # no dice nada; el número deja decidir a ojo hasta que el mecánico
            # ponga el tope. Se mira SÓLO el mantenimiento que prende el
            # semáforo: si esa máquina nunca tuvo una limpieza anotada, lo que
            # le falta es arrancar, no un tope.
            hechos = [i for i in por_tipo.values()
                      if i.get("desde") and i["tipo"]["id"] in prenden]
            principal = (max(hechos, key=lambda i: i["desde"]) if hechos else None)
        filas.append({
            "maquina": maquina,
            "principal": principal,
            "por_tipo": por_tipo,
            "estado": principal["estado"] if principal else "sin_arrancar",
            "pct": principal["pct"] if principal else None,
        })

    filas.sort(key=lambda f: (f["pct"] is None, -(f["pct"] or 0)))
    return filas, pendientes, leido_en, fresco


def _semaforo_cacheado():
    """El semáforo una sola vez por request: lo usan la pantalla y la campanita."""
    if not hasattr(g, "_semaforo"):
        g._semaforo = armar_semaforo()
    return g._semaforo


@app.context_processor
def _campanita():
    """Cuántas máquinas están vencidas, para la campanita de la barra.

    Si Asinfo no contesta devuelve None, NO cero. Un cero acá diría "está todo
    bien" por un problema de red, que es justo el error que este programa
    existe para evitar.
    """
    def contar():
        if ERROR_ARRANQUE:
            return None
        try:
            filas, _, _, _ = _semaforo_cacheado()
        except Exception:  # noqa: BLE001
            return None
        return sum(1 for f in filas if f["estado"] == "vencido")

    return {"vencidas": contar()}


# --------------------------------------------------------------------------
# Pantallas
# --------------------------------------------------------------------------
@app.route("/")
@requiere_login
def semaforo():
    try:
        filas, pendientes, leido_en, fresco = _semaforo_cacheado()
        error = None
    except asinfo.AsinfoNoDisponible as exc:
        filas, pendientes, leido_en, fresco = [], [], None, False
        error = str(exc)

    # La campanita lleva acá: ?solo=vencidas muestra sólo lo vencido.
    solo = request.args.get("solo")
    if solo == "vencidas":
        filas_ver = [f for f in filas if f["estado"] == "vencido"]
    else:
        filas_ver = filas

    resumen = {
        "vencido": sum(1 for f in filas if f["estado"] == "vencido"),
        "por_vencer": sum(1 for f in filas if f["estado"] == "por_vencer"),
        "ok": sum(1 for f in filas if f["estado"] == "ok"),
        "sin_tope": sum(1 for f in filas if f["estado"] == "sin_tope"),
        "sin_arrancar": sum(1 for f in filas if f["estado"] == "sin_arrancar"),
    }
    # Qué mantenimientos prenden el semáforo y cuáles van sólo como fecha. Se
    # decide por si ALGUNA máquina tiene puesto un tope de kilos, no por el
    # número general del tipo: los kilos se cargan por máquina.
    tipos = store.tipos()
    con_tope = {tid for f in filas for tid, i in f["por_tipo"].items() if i.get("tope")}
    return render_template(
        "semaforo.html",
        filas=filas_ver,
        solo=solo,
        tipos_con_tope=[t for t in tipos if t["id"] in con_tope],
        tipos_sin_tope=[t for t in tipos if t["id"] not in con_tope],
        pendientes=pendientes,
        resumen=resumen,
        aviso=AVISO,
        leido_en=leido_en,
        fresco=fresco,
        error=error,
        hay_tipos=bool(store.tipos()),
    )


def _buscar_maquina(escrito, maquinas):
    """Acepta el número con el que la llaman en planta: 1, 01, MQ 1, MQ 001.

    En planta nadie dice "TEJEDURIA-MQ 001": dicen "la uno". Si el número no
    existe, se avisa con el número puesto — no se elige una parecida.
    """
    import re as _re
    encontrados = _re.findall(r"\d+", str(escrito or ""))
    if not encontrados:
        raise ValueError("Poné el número de la máquina. Por ejemplo: 1")
    numero = int(encontrados[-1])
    for m in maquinas:
        if m.get("numero") == numero:
            return m["id"]
    raise ValueError(f"No hay ninguna máquina {numero}.")


@app.route("/registrar", methods=["GET", "POST"])
@requiere_login
def registrar():
    try:
        maquinas, _, _ = asinfo.maquinas()
    except asinfo.AsinfoNoDisponible:
        maquinas = []
    tipos = store.tipos()

    if request.method == "POST":
        try:
            id_maquina = _buscar_maquina(request.form.get("maquina"), maquinas)
            tipo_id = _tipo_elegido(request.form.get("tipo_id"), tipos)
            fecha = request.form.get("fecha") or date.today().isoformat()
            hecho_por = request.form.get("hecho_por", "").strip()
            if not hecho_por:
                raise ValueError("Falta poner quién lo hizo.")
            if datetime.strptime(fecha, "%Y-%m-%d").date() > date.today():
                raise ValueError("La fecha no puede ser futura.")

            # Cuánto llevó NO es opcional: sin las horas no se sabe cuánto
            # cuesta parar una máquina, que es media razón por la que esto se
            # anota. Decisión de la dueña, 20/08/2026.
            crudo = (request.form.get("horas") or "").strip().replace(",", ".")
            if not crudo:
                raise ValueError("Falta poner cuánto llevó, en horas.")
            try:
                horas = float(crudo)
            except ValueError:
                raise ValueError("Las horas tienen que ser un número. Por ejemplo 2,5.")
            if not (0 < horas <= 200):
                raise ValueError("Las horas tienen que ser un número razonable.")

            nombre = next(
                (m["nombre"] for m in maquinas if m["id"] == id_maquina), str(id_maquina)
            )
            store.registrar_service(
                id_maquina, nombre, tipo_id, fecha, hecho_por,
                request.form.get("nota"),
                repuestos=request.form.get("repuestos"),
                horas=horas,
            )
            flash(f"Cargado en {nombre}. Los kilos vuelven a cero.", "ok")
            return redirect(url_for("semaforo"))
        except Exception as exc:  # noqa: BLE001
            flash(str(exc), "error")

    return render_template(
        "registrar.html",
        maquinas=maquinas,
        # El nombre de Asinfo por número, para mostrarlo mientras se escribe.
        nombres_maquinas={str(m["numero"]): m["nombre"] for m in maquinas
                          if m.get("numero") is not None},
        tipos=tipos,
        hoy=date.today().isoformat(),
        responsables=store.responsables(),
        historial=store.historial(limite=25),
    )


@app.route("/tipos", methods=["GET", "POST"])
@requiere_login
def tipos_view():
    if request.method == "POST":
        try:
            nombre = request.form.get("nombre", "").strip()
            if not nombre:
                raise ValueError("Ponele un nombre.")
            crudo = (request.form.get("cada_kg") or "").strip()
            cada_kg = float(crudo.replace(".", "").replace(",", ".")) if crudo else None

            tipo_id = request.form.get("tipo_id")
            if tipo_id:
                store.editar_tipo(
                    int(tipo_id), nombre, cada_kg, None, None,
                    request.form.get("activo") == "on",
                )
                flash(f"«{nombre}» guardado.", "ok")
            else:
                store.crear_tipo(nombre, cada_kg, None, None)
                flash(f"«{nombre}» creado.", "ok")
            return redirect(url_for("tipos_view"))
        except Exception as exc:  # noqa: BLE001
            flash(str(exc), "error")

    return render_template("tipos.html", tipos=store.tipos(incluir_inactivos=True))


@app.route("/arranque", methods=["GET", "POST"])
@requiere_login
def arranque():
    """Poner a contar TODAS las máquinas de una vez, desde una misma fecha.

    Es el atajo cuando no hay Excel: sin esto hay que cargar 43 formularios a
    mano antes de ver la primera pantalla.
    """
    try:
        maquinas, _, _ = asinfo.maquinas()
    except asinfo.AsinfoNoDisponible:
        maquinas = []
    tipos = store.tipos()
    ultimos = store.ultimos_por_maquina_y_tipo()

    def faltantes(tipo_id):
        return [m for m in maquinas if (m["id"], tipo_id) not in ultimos]

    if request.method == "POST":
        try:
            tipo_id = _tipo_elegido(request.form.get("tipo_id"), tipos)
            fecha = request.form.get("fecha") or date.today().isoformat()
            hecho_por = request.form.get("hecho_por", "").strip() or "Arranque inicial"
            if datetime.strptime(fecha, "%Y-%m-%d").date() > date.today():
                raise ValueError("La fecha no puede ser futura.")

            pendientes = faltantes(tipo_id)
            if not pendientes:
                raise ValueError("Todas las máquinas ya están contando ese mantenimiento.")

            n = store.registrar_muchos([
                (m["id"], m["nombre"], tipo_id, fecha, hecho_por,
                 "Punto de partida cargado en el arranque")
                for m in pendientes
            ])
            flash(f"Listo: {n} máquinas empezaron a contar desde el {fecha}.", "ok")
            return redirect(url_for("semaforo"))
        except Exception as exc:  # noqa: BLE001
            flash(str(exc), "error")

    return render_template(
        "arranque.html",
        tipos=tipos,
        maquinas=maquinas,
        hoy=date.today().isoformat(),
        faltan={t["id"]: len(faltantes(t["id"])) for t in tipos},
    )


@app.route("/tipos/sugeridos", methods=["POST"])
@requiere_login
def tipos_sugeridos():
    try:
        n = store.crear_tipos_sugeridos()
        flash("Se cargaron los tipos sugeridos. Corregí los números con el mecánico."
              if n else "Ya estaban todos.", "ok")
    except Exception as exc:  # noqa: BLE001
        flash(str(exc), "error")
    return redirect(url_for("tipos_view"))


# --------------------------------------------------------------------------
# Subir el Excel de planta
# --------------------------------------------------------------------------
def _lugar_nuevo() -> tuple[str, str]:
    """Un nombre libre donde dejar la planilla, y limpia las viejas.

    Se guarda en disco en vez de en memoria porque la pantalla de revisión se
    puede recargar varias veces cambiando qué columna es cada cosa.
    """
    os.makedirs(CARPETA_CARGA, exist_ok=True)
    limite = time.time() - 2 * 3600
    for viejo in os.listdir(CARPETA_CARGA):
        ruta = os.path.join(CARPETA_CARGA, viejo)
        try:
            if os.path.getmtime(ruta) < limite:
                os.remove(ruta)
        except OSError:
            pass
    token = secrets.token_hex(8)
    return token, os.path.join(CARPETA_CARGA, token + ".xlsx")


def _guardar_temporal(archivo) -> str:
    token, ruta = _lugar_nuevo()
    archivo.save(ruta)
    return token


def _guardar_pegado(texto: str) -> str:
    token, ruta = _lugar_nuevo()
    excel.desde_texto(texto, ruta)
    return token


def _ruta_de(token: str) -> str:
    if not _TOKEN.match(token or ""):
        raise ValueError("El archivo ya no está. Subilo de nuevo.")
    ruta = os.path.join(CARPETA_CARGA, token + ".xlsx")
    if not os.path.exists(ruta):
        raise ValueError("El archivo ya no está. Subilo de nuevo.")
    return ruta


def _mapa_del_form(form, titulos, tipos, detectado):
    """El mapa que eligió la persona, o el detectado si todavía no eligió."""
    if not any(k.startswith("col_") for k in form):
        return detectado
    mapa = {}
    for clave, valor in form.items():
        if not clave.startswith("col_") or valor in ("", "-1"):
            continue
        try:
            i = int(valor)
        except ValueError:
            continue
        if 0 <= i < len(titulos):
            mapa[clave[4:]] = i
    return mapa


@app.route("/carga", methods=["GET", "POST"])
@requiere_login
def carga():
    """Subir el Excel que ya usan, revisarlo y recién ahí guardar."""
    tipos = store.tipos()
    if request.method == "GET":
        return render_template("carga.html", paso="subir", tipos=tipos)

    pegado = (request.form.get("pegado") or "").strip()
    if pegado:
        try:
            token = _guardar_pegado(pegado)
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("carga.html", paso="subir", tipos=tipos, pegado=pegado)
        return redirect(url_for("carga_revisar", token=token))

    archivo = request.files.get("archivo")
    if not archivo or not archivo.filename.lower().endswith((".xlsx", ".xlsm")):
        flash("Elegí un archivo .xlsx, o pegá las filas.", "error")
        return render_template("carga.html", paso="subir", tipos=tipos)
    token = _guardar_temporal(archivo)
    return redirect(url_for("carga_revisar", token=token))


@app.route("/carga/<token>", methods=["GET", "POST"])
@requiere_login
def carga_revisar(token):
    """Mostrar qué entendió del Excel, dejar corregirlo, y guardar."""
    tipos = store.tipos()
    if not tipos:
        flash("Primero definí los tipos de mantenimiento.", "error")
        return redirect(url_for("tipos_view"))

    try:
        ruta = _ruta_de(token)
        maquinas, _, _ = asinfo.maquinas()
    except (ValueError, asinfo.AsinfoNoDisponible) as exc:
        flash(str(exc), "error")
        return redirect(url_for("carga"))

    # Abrir el archivo puede fallar de mil formas: un .xlsx que en realidad no
    # lo es, uno cortado a la mitad, uno guardado por un programa raro. Todo eso
    # tiene que salir como una frase y no como la pantalla de error de Python.
    try:
        nombres_hojas = excel.hojas(ruta)
        es_de_ajuste = ajustes_excel.es_planilla_ajuste(ruta)
    except Exception:  # noqa: BLE001
        flash("No se pudo abrir el archivo. Fijate que sea el Excel de la "
              "planilla y que no esté dañado.", "error")
        return redirect(url_for("carga"))

    # La planilla de CONTROL DE AJUSTE es otra cosa: no trae mantenimientos,
    # trae cómo se pone cada máquina para tejer cada tela, más las agujas, las
    # levas y las bandas. Se reconoce sola por los nombres de las hojas y entra
    # por esta misma puerta a propósito: una segunda pantalla de carga sería un
    # segundo lugar donde equivocarse.
    if es_de_ajuste:
        return _revisar_planilla_ajuste(token, ruta, maquinas, nombres_hojas)

    # Dos formas de planilla, y se reconoce sola cuál es:
    #
    #   * La de planta: UNA HOJA POR MÁQUINA, con la ficha arriba y el
    #     historial abajo. No hay nada que mapear.
    #   * Una tabla comun: una fila por maquina. Ahi si hay que decir que
    #     columna es cada cosa.
    try:
        por_maquina, descartes_pm = excel.leer_por_maquina(ruta, maquinas, tipos)
    except Exception:  # noqa: BLE001
        flash("No se pudo leer el archivo. Fijate que sea la planilla de la "
              "fábrica y que no esté dañado.", "error")
        return redirect(url_for("carga"))
    es_por_maquina = len(por_maquina) >= 3

    if es_por_maquina:
        # La planilla de planta trae el historial ENTERO desde 2018. Antes se
        # cargaba sólo la última fecha de cada tipo — 66 filas de 1.366 — y con
        # eso el semáforo andaba, pero la ficha de la máquina quedaba muda.
        return _revisar_historial(token, ruta, maquinas, tipos, nombres_hojas)
    else:
        hoja = request.form.get("hoja") or nombres_hojas[0]
        try:
            titulos, filas = excel.leer(ruta, hoja)
        except Exception:  # noqa: BLE001
            flash(f"No se pudo leer la hoja «{hoja}».", "error")
            return redirect(url_for("carga"))
        detectado = excel.detectar(titulos, tipos)
        mapa = _mapa_del_form(request.form, titulos, tipos, detectado)
        listas, descartes = excel.armar(titulos, filas, mapa, maquinas, tipos)

    if request.method == "POST" and request.form.get("boton") == "confirmar":
        try:
            if not listas:
                raise ValueError("No hay ninguna fila para guardar.")
            quien = request.form.get("hecho_por", "").strip() or "Carga del Excel"
            servicios, topes, fichas = [], [], []
            for item in listas:
                m = item["maquina"]
                for mant in item["mantenimientos"]:
                    if mant["fecha"]:
                        servicios.append((m["id"], m["nombre"], mant["tipo"]["id"],
                                          mant["fecha"], quien, "Cargado desde el Excel"))
                    if mant["cada_kg"]:
                        topes.append((m["id"], mant["tipo"]["id"], mant["cada_kg"]))
                # .get: no todos los lectores traen todos los campos, y un
                # campo que no vino es NULL, no un error.
                if any(v is not None for v in item["ficha"].values()):
                    fichas.append((m["id"], *[item["ficha"].get(c) for c in store.CAMPOS_FICHA]))

            hecho = store.cargar_lote(servicios, topes, fichas)
            os.remove(ruta)
            flash(
                f"Listo. {hecho['servicios']} mantenimientos, "
                f"{hecho['topes']} topes de kilos y {hecho['fichas']} fichas.", "ok")
            return redirect(url_for("semaforo"))
        except Exception as exc:  # noqa: BLE001
            flash(str(exc), "error")

    return render_template(
        "carga.html",
        paso="revisar",
        formato="por_maquina" if es_por_maquina else "tabla",
        token=token,
        tipos=tipos,
        hojas=nombres_hojas,
        hoja=hoja,
        titulos=titulos,
        mapa=mapa,
        listas=listas,
        descartes=descartes,
        campos=CAMPOS_MAPA(tipos),
    )


def _revisar_historial(token, ruta, maquinas, tipos, nombres_hojas):
    """Mostrar el historial completo que trae la planilla de planta, y guardarlo."""
    mantenimientos, fichas, descartes = excel.leer_historial_por_maquina(
        ruta, maquinas, tipos)

    if request.method == "POST" and request.form.get("boton") == "confirmar":
        try:
            if not mantenimientos:
                raise ValueError("No se pudo leer ningún mantenimiento.")
            hecho = store.guardar_historial(mantenimientos)
            if fichas:
                store.cargar_lote([], [], [
                    (m["id"], *[f.get(c) for c in store.CAMPOS_FICHA])
                    for m, f in fichas if any(v is not None for v in f.values())])
            os.remove(ruta)
            aviso = f"Listo. {hecho['mantenimientos']} mantenimientos y {len(fichas)} fichas."
            if hecho["borrados"]:
                aviso += (f" Se reemplazaron los {hecho['borrados']} que había "
                          "cargado la primera versión, que sólo traía la última fecha.")
            flash(aviso, "ok")
            return redirect(url_for("semaforo"))
        except Exception as exc:  # noqa: BLE001
            flash(str(exc), "error")

    # Un resumen por máquina: cuántos trae y desde cuándo.
    por_maquina = {}
    for m in mantenimientos:
        d = por_maquina.setdefault(m["id_maquina"], {"n": 0, "desde": m["fecha"],
                                                     "hasta": m["fecha"],
                                                     "hoja": m["hoja"]})
        d["n"] += 1
        d["desde"] = min(d["desde"], m["fecha"])
        d["hasta"] = max(d["hasta"], m["fecha"])
    nombres = {m["id"]: m for m in maquinas}
    resumen = sorted(
        ({"maquina": nombres.get(k), **v} for k, v in por_maquina.items()),
        key=lambda f: (f["maquina"] or {}).get("numero") or 0)

    return render_template(
        "carga.html",
        paso="revisar",
        formato="historial",
        token=token,
        tipos=tipos,
        hojas=nombres_hojas,
        resumen=resumen,
        total=len(mantenimientos),
        fichas=len(fichas),
        ya_cargados=store.cuantos_de_la_carga_vieja(),
        descartes=descartes,
        listas=[], hoja=None, titulos=[], mapa={}, campos=[],
    )


def _revisar_planilla_ajuste(token, ruta, maquinas, nombres_hojas):
    """Mostrar qué entendió de la planilla de ajuste, y guardar."""
    bloques, descartes = ajustes_excel.leer(ruta, maquinas)

    if request.method == "POST" and request.form.get("boton") == "confirmar":
        try:
            if not any(bloques.values()):
                raise ValueError("No se pudo leer ninguna fila.")
            hecho = store.guardar_planilla_ajuste(bloques)
            os.remove(ruta)
            flash("Listo. " + ", ".join(
                f"{n} {ajustes_excel.NOMBRES[k].lower()}"
                for k, n in hecho.items() if n) + ".", "ok")
            return redirect(url_for("ajustes_view"))
        except Exception as exc:  # noqa: BLE001
            flash(str(exc), "error")

    # Una muestra de cada bloque, para que se vea qué entendió y no haya que
    # confiar en un número. Cinco filas alcanzan: si están bien, están bien.
    muestras = {k: v[:5] for k, v in bloques.items()}
    return render_template(
        "carga.html",
        paso="revisar",
        formato="ajuste",
        token=token,
        tipos=store.tipos(),
        hojas=nombres_hojas,
        bloques=bloques,
        muestras=muestras,
        nombres=ajustes_excel.NOMBRES,
        descartes=descartes,
        listas=[], hoja=None, titulos=[], mapa={}, campos=[],
    )


def CAMPOS_MAPA(tipos):
    """Qué se puede mapear, en el orden en que se muestra."""
    campos = [("numero", "Número de máquina")]
    for t in tipos:
        campos.append((f"fecha_{t['id']}", f"{t['nombre']} · último"))
        campos.append((f"kg_{t['id']}", f"{t['nombre']} · cada … kg"))
    campos += [
        ("marca", "Marca"), ("modelo", "Modelo"), ("galga", "Galga"),
        ("diametro", "Diámetro"), ("alimentadores", "Alimentadores"),
        ("agujas", "Agujas"), ("anio", "Año"), ("nota", "Nota"),
    ]
    return campos


# --------------------------------------------------------------------------
# Archivos
# --------------------------------------------------------------------------
# Un lugar para subir cosas desde el programa: la planilla de planta, el manual
# de una máquina, una foto. Van a la BASE, no a una carpeta del server: la
# actualización reemplaza la carpeta entera y se las llevaría puestas.
@app.route("/archivos", methods=["GET", "POST"])
@requiere_login
def archivos():
    try:
        maquinas, _, _ = asinfo.maquinas()
    except asinfo.AsinfoNoDisponible:
        maquinas = []

    if request.method == "POST":
        try:
            subido = request.files.get("archivo")
            if not subido or not subido.filename:
                raise ValueError("Elegí un archivo.")
            contenido = subido.read()
            if not contenido:
                raise ValueError("El archivo llegó vacío.")

            crudo = (request.form.get("id_maquina") or "").strip()
            id_maquina = _buscar_maquina(crudo, maquinas) if crudo else None

            store.guardar_archivo(
                os.path.basename(subido.filename), contenido,
                id_maquina=id_maquina,
                descripcion=request.form.get("descripcion"),
                subido_por=request.form.get("subido_por"))
            flash(f"«{os.path.basename(subido.filename)}» guardado.", "ok")
            return redirect(url_for("archivos"))
        except Exception as exc:  # noqa: BLE001
            flash(str(exc), "error")

    lista = store.archivos()
    nombres = {m["id"]: m for m in maquinas}
    for a in lista:
        a["maquina"] = nombres.get(a["id_maquina"])
    return render_template("archivos.html", archivos=lista, maquinas=maquinas,
                           responsables=store.responsables())


@app.route("/archivos/<int:id_archivo>")
@requiere_login
def archivo_bajar(id_archivo):
    a = store.archivo(id_archivo)
    if not a:
        flash("Ese archivo ya no está.", "error")
        return redirect(url_for("archivos"))
    return Response(
        bytes(a["contenido"]),
        headers={"Content-Disposition": f'attachment; filename="{a["nombre"]}"'},
        mimetype="application/octet-stream")


@app.route("/archivos/<int:id_archivo>/borrar", methods=["POST"])
@requiere_login
def archivo_borrar(id_archivo):
    store.borrar_archivo(id_archivo)
    flash("Archivo borrado.", "ok")
    return redirect(url_for("archivos"))


@app.route("/archivos/<int:id_archivo>/leer", methods=["POST"])
@requiere_login
def archivo_leer(id_archivo):
    """Leer una planilla ya subida, sin volver a subirla."""
    a = store.archivo(id_archivo)
    if not a:
        flash("Ese archivo ya no está.", "error")
        return redirect(url_for("archivos"))
    if not a["nombre"].lower().endswith((".xlsx", ".xlsm")):
        flash("Sólo se pueden leer planillas de Excel.", "error")
        return redirect(url_for("archivos"))

    os.makedirs(CARPETA_CARGA, exist_ok=True)
    token = secrets.token_hex(8)
    with open(os.path.join(CARPETA_CARGA, token + ".xlsx"), "wb") as f:
        f.write(bytes(a["contenido"]))
    return redirect(url_for("carga_revisar", token=token))


# --------------------------------------------------------------------------
# Todas las máquinas
# --------------------------------------------------------------------------
@app.route("/maquinas")
@requiere_login
def maquinas_lista():
    """Las 43 máquinas, para recorrerlas de a una y completar lo que falte.

    Ésta es la pantalla desde la que se va llenando el programa: dice de un
    vistazo a cuál le falta la ficha, a cuál el tope de kilos y cuál no arrancó
    a contar todavía. Sin eso hay que entrar a las 43 para descubrir cuáles ya
    están hechas.
    """
    try:
        maquinas, _, _ = asinfo.maquinas()
        error = None
    except asinfo.AsinfoNoDisponible as exc:
        maquinas, error = [], str(exc)

    fichas = store.fichas()
    ultimos = store.ultimos_por_maquina_y_tipo()
    topes = store.topes_por_maquina()
    tipos = store.tipos()

    filas = []
    for m in maquinas:
        ficha = fichas.get(m["id"], {})
        # El tope sólo se le exige a los mantenimientos que se vencen por
        # desgaste. El cambio de agujas no lleva tope: lo pide la tela.
        con_tope = [t for t in tipos if topes.get((m["id"], t["id"])) or t["cada_kg"]]
        falta = []
        if not any(ficha.get(c) for c in ("marca", "modelo", "galga", "diametro")):
            falta.append("la ficha")
        if not con_tope:
            falta.append("los kilos")
        if not any(ultimos.get((m["id"], t["id"])) for t in tipos):
            falta.append("arrancar")
        # El tope que se muestra en la lista: el del mantenimiento que prende el
        # semáforo. Es UNO en la práctica —la limpieza—, y es el número que hace
        # que la máquina se pinte de algún color.
        tope_visible = next((topes.get((m["id"], t["id"])) or t["cada_kg"]
                             for t in con_tope), None)
        filas.append({
            "maquina": m,
            "ficha": ficha,
            "falta": falta,
            "tope": tope_visible,
            "topes": {t["id"]: topes.get((m["id"], t["id"])) for t in tipos},
            "ultimos": {t["id"]: (ultimos.get((m["id"], t["id"])) or {}).get("fecha")
                        for t in tipos},
        })
    filas.sort(key=lambda f: (f["maquina"]["numero"] is None, f["maquina"]["numero"] or 0))

    return render_template("maquinas.html", filas=filas, tipos=tipos, error=error,
                           faltan=sum(1 for f in filas if f["falta"]))


# --------------------------------------------------------------------------
# Ficha de una máquina
# --------------------------------------------------------------------------
@app.route("/maquina/<int:id_maquina>", methods=["GET", "POST"])
@requiere_login
def maquina_detalle(id_maquina):
    try:
        maquinas, _, _ = asinfo.maquinas()
    except asinfo.AsinfoNoDisponible:
        maquinas = []
    maquina = next((m for m in maquinas if m["id"] == id_maquina), None)
    # Una máquina que no está en Asinfo no tiene ficha que mostrar. Se vuelve
    # al listado diciéndolo, en vez de dibujar media pantalla vacía.
    if not maquina and maquinas:
        flash("Esa máquina no está en Asinfo.", "error")
        return redirect(url_for("maquinas_lista"))

    tipos = store.tipos()
    if request.method == "POST" and request.form.get("que") == "eficiencia":
        # Cuánto DEBERÍA dar. Es el cálculo de la planilla y se corrige a mano;
        # lo que la máquina tejió de verdad lo mide Asinfo y no se toca de acá.
        try:
            store.guardar_eficiencia(id_maquina, {
                # Todo con `a_decimal_es`: acá el punto es de miles. Con
                # `a_decimal`, escribir «1.410» rpm guardaba 1,41 y la cuenta
                # salía mal en silencio.
                "rpm": excel.a_decimal_es(request.form.get("rpm")),
                "sistemas": (request.form.get("sistemas") or "").strip() or None,
                "diametro": excel.a_decimal_es(request.form.get("diametro_ef")),
                "alimentadores": excel.a_numero(request.form.get("alimentadores_ef")),
                "tamano_rollo": excel.a_kilos(request.form.get("tamano_rollo")),
                "minutos_rollo": excel.a_decimal_es(request.form.get("minutos_rollo")),
                "rollos_dia": excel.a_numero(request.form.get("rollos_dia")),
                "kg_dia": excel.a_kilos(request.form.get("kg_dia")),
                "rollos_dia_24": excel.a_numero(request.form.get("rollos_dia_24")),
                "kg_dia_24": excel.a_kilos(request.form.get("kg_dia_24")),
            })
            flash("Guardado cuánto debería dar.", "ok")
        except Exception as exc:  # noqa: BLE001
            flash(str(exc), "error")
        return redirect(url_for("maquina_detalle", id_maquina=id_maquina,
                                abrir="deberia") + "#deberia")

    if request.method == "POST":
        try:
            datos = {c: (request.form.get(c) or "").strip() or None
                     for c in store.CAMPOS_FICHA}
            for entero in ("galga", "alimentadores", "agujas", "anio"):
                datos[entero] = excel.a_numero(datos[entero])
            datos["diametro"] = excel.a_decimal_es(datos["diametro"])

            # Cada cuántos kilos va cada mantenimiento EN ESTA máquina. Se
            # carga acá y no en una pantalla aparte: el número es de la
            # máquina, y el mecánico lo decide mirándola a ella.
            #
            # Se revisan TODOS antes de escribir nada. Guardando sobre la
            # marcha, un número mal escrito en el tercer tope dejaba la ficha y
            # los dos primeros ya guardados, y la pantalla decía sólo «error»:
            # el mecánico se iba creyendo que no se había guardado nada.
            nuevos_topes = []
            for tipo in tipos:
                crudo = (request.form.get(f"tope_{tipo['id']}") or "").strip()
                # A mano, no con `a_kilos`: ése limpia todo lo que no sea
                # dígito y se lleva puesto el signo, así que un «-5» entraba
                # como 5 y la validación no se enteraba.
                crudo = crudo.replace(".", "").replace(",", ".")
                try:
                    kg = float(crudo) if crudo else None
                except ValueError:
                    raise ValueError(f"{tipo['nombre']}: eso no es un número de kilos.")
                if kg is not None and kg <= 0:
                    raise ValueError("Los kilos tienen que ser mayores que cero.")
                nuevos_topes.append((tipo["id"], kg))

            store.guardar_ficha(id_maquina, datos)
            for tipo_id, kg in nuevos_topes:
                store.guardar_tope(id_maquina, tipo_id, kg)

            flash("Ficha guardada.", "ok")
        except Exception as exc:  # noqa: BLE001
            flash(str(exc), "error")
        return redirect(url_for("maquina_detalle", id_maquina=id_maquina))

    try:
        mensual, _, _ = asinfo.produccion_mensual(id_maquina)
    except Exception:  # noqa: BLE001
        mensual = []

    historial = store.historial(id_maquina, limite=500)
    # Una sola vez, no una por tipo: `topes_por_maquina` trae la tabla entera.
    todos_los_topes = store.topes_por_maquina()
    return render_template(
        "maquina.html",
        maquina=maquina,
        ficha=store.ficha(id_maquina),
        archivos=store.archivos(id_maquina),
        historial=historial,
        dias=_dias_de_mantenimiento(id_maquina, historial),
        mensual=mensual,
        ajustes=store.ajustes(id_maquina=id_maquina, limite=30),
        aguja=store.agujas().get(id_maquina),
        eficiencia=store.eficiencias().get(id_maquina),
        tipos=tipos,
        topes={t["id"]: todos_los_topes.get((id_maquina, t["id"])) for t in tipos},
        abrir=request.args.get("abrir"),
    )


@app.route("/maquina")
@requiere_login
def ir_a_maquina():
    """El buscador de la ficha: se escribe el número y se salta a esa máquina."""
    try:
        maquinas, _, _ = asinfo.maquinas()
    except asinfo.AsinfoNoDisponible:
        maquinas = []
    try:
        id_maquina = _buscar_maquina(request.args.get("numero"), maquinas)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("maquinas_lista"))
    return redirect(url_for("maquina_detalle", id_maquina=id_maquina))


def _dias_de_mantenimiento(id_maquina, historial):
    """Un renglón por DÍA, no por registro.

    Si el mismo día se limpió la máquina y se le cambiaron las agujas, en la
    planilla son dos filas — pero para el mecánico es una sola parada. Se
    juntan, y al lado van los kilos que tejió desde la parada anterior, que es
    la pregunta de verdad: cuánto aguantó.
    """
    por_dia = {}
    for h in historial:
        d = por_dia.setdefault(h["fecha"], {"fecha": h["fecha"], "tipos": [],
                                            "notas": [], "repuestos": [],
                                            "quien": set(), "kg": None,
                                            "horas": None})
        if h["tipo_nombre"] not in d["tipos"]:
            d["tipos"].append(h["tipo_nombre"])
        # Si el mismo día se hicieron dos cosas, la máquina estuvo parada la
        # suma de las dos: lo que se pregunta es cuánto estuvo sin tejer.
        if h.get("horas"):
            d["horas"] = round((d["horas"] or 0) + float(h["horas"]), 2)
        for campo, clave in (("nota", "notas"), ("repuestos", "repuestos")):
            if h.get(campo) and h[campo] not in d[clave]:
                d[clave].append(h[campo])
        if h.get("hecho_por"):
            d["quien"].add(h["hecho_por"])

    dias = sorted(por_dia.values(), key=lambda d: d["fecha"], reverse=True)
    if not dias:
        return []

    # Los kilos entre paradas. Si Asinfo no contesta, se muestran los días y
    # las fechas igual: media pantalla es mejor que un error.
    try:
        acum = asinfo.kilos_desde(id_maquina, [d["fecha"] for d in dias])
    except Exception:  # noqa: BLE001
        acum = {}
    for i, d in enumerate(dias):
        desde = acum.get(d["fecha"].isoformat())
        if desde is None:
            continue
        if i == 0:
            d["kg"] = desde          # desde la última parada hasta hoy
            d["hasta_hoy"] = True
        else:
            anterior = acum.get(dias[i - 1]["fecha"].isoformat())
            if anterior is not None:
                d["kg"] = round(desde - anterior, 2)
    for d in dias:
        d["quien"] = ", ".join(sorted(d["quien"]))
    return dias


# --------------------------------------------------------------------------
# Ajustes: cómo se pone la máquina para tejer cada tela
# --------------------------------------------------------------------------
@app.route("/ajustes", methods=["GET", "POST"])
@requiere_login
def ajustes_view():
    """El histórico de puestas a punto.

    Se busca por máquina o por tela, porque son las dos preguntas reales: «qué
    le hicimos a la 21» y «cómo tejíamos la TANIA». Y se carga uno nuevo acá
    mismo: hasta ahora sólo entraban por la planilla, y una puesta a punto que
    se hizo hoy no puede esperar a que alguien vuelva a subir el Excel.
    """
    try:
        maquinas, _, _ = asinfo.maquinas()
    except asinfo.AsinfoNoDisponible:
        maquinas = []

    if request.method == "POST":
        return _guardar_ajuste_nuevo(maquinas)

    escrito = (request.args.get("maquina") or "").strip()
    tela = (request.args.get("tela") or "").strip()
    id_maquina = None
    if escrito:
        try:
            id_maquina = _buscar_maquina(escrito, maquinas)
        except ValueError as exc:
            flash(str(exc), "error")

    filas = store.ajustes(id_maquina=id_maquina, tela=tela or None)
    nombres = {m["id"]: m for m in maquinas}
    for f in filas:
        f["maquina"] = nombres.get(f["id_maquina"])

    # El consumo de hilo vive acá y no en una pantalla propia: la pregunta es
    # la misma — cómo se teje una tela — y tenerla partida en dos obligaba a
    # cambiar de pantalla para contestar media.
    return render_template("ajustes.html", filas=filas, telas=store.telas(),
                           resumen=store.resumen_ajustes(),
                           consumo=store.consumo_hilo(),
                           maquinas=maquinas, hoy=date.today().isoformat(),
                           abrir_nuevo=request.args.get("nuevo"),
                           escrito=escrito, tela=tela)


# Lo que se puede escribir a mano en un ajuste. `maquina_nombre`, `hoja` y
# `orden` no: el nombre sale de Asinfo y los otros dos son de la planilla.
CAMPOS_AJUSTE_A_MANO = ("tipo_maquina", "cilindro", "poleas", "ajuste_agujas",
                        "estiraje", "hilos", "malla_manual", "malla")


def _guardar_ajuste_nuevo(maquinas):
    """Un ajuste cargado desde la pantalla. Vuelve a la máquina que se cargó."""
    try:
        id_maquina = _buscar_maquina(request.form.get("maquina"), maquinas)
        tela = (request.form.get("tela") or "").strip()
        if not tela:
            raise ValueError("Falta poner qué tela se estaba tejiendo.")

        fecha = (request.form.get("fecha") or "").strip() or None
        if fecha:
            if datetime.strptime(fecha, "%Y-%m-%d").date() > date.today():
                raise ValueError("La fecha no puede ser futura.")

        datos = {c: (request.form.get(c) or "").strip() or None
                 for c in CAMPOS_AJUSTE_A_MANO}
        datos["id_maquina"] = id_maquina
        datos["maquina_nombre"] = next(
            (m["nombre"] for m in maquinas if m["id"] == id_maquina), None)
        datos["tela"] = tela
        datos["fecha"] = fecha
        # El gramaje es el único número con coma que se carga a mano.
        crudo = (request.form.get("gramaje_crudo") or "").strip().replace(",", ".")
        if crudo:
            try:
                datos["gramaje_crudo"] = float(crudo)
            except ValueError:
                raise ValueError("El gramaje tiene que ser un número. Por ejemplo 180,5.")

        store.crear_ajuste(datos)
        flash(f"Ajuste cargado en {datos['maquina_nombre'] or 'la máquina'}.", "ok")
        return redirect(url_for("ajustes_view", maquina=request.form.get("maquina")))
    except Exception as exc:  # noqa: BLE001
        flash(str(exc), "error")
        return redirect(url_for("ajustes_view", nuevo="1"))


# --------------------------------------------------------------------------
# Producción: cuánto debería dar cada máquina
# --------------------------------------------------------------------------
@app.route("/produccion")
@requiere_login
def produccion():
    """El cálculo de la planilla: cuántos kilos da cada máquina en 12 horas.

    No es lo que la máquina tejió —eso lo mide Asinfo—: es lo que debería dar.
    Que no coincidan no es un error: uno es el plan y el otro es lo que pasó.

    Estaba sólo adentro de la ficha de cada máquina, así que para comparar dos
    había que entrar y salir de las dos. Acá se ven las 43 juntas.
    """
    try:
        maquinas, _, _ = asinfo.maquinas()
    except asinfo.AsinfoNoDisponible:
        maquinas = []
    eficiencias = store.eficiencias()
    filas = [{"maquina": m, "e": eficiencias[m["id"]]} for m in maquinas
             if m["id"] in eficiencias]
    filas.sort(key=lambda f: (f["maquina"]["numero"] is None,
                              f["maquina"]["numero"] or 0))

    # El peso medido de la tela, de la hoja de gramajes. Estaba cargado y no se
    # veía en ninguna pantalla.
    nombres = {m["id"]: m for m in maquinas}
    gramajes = store.gramajes()
    for g in gramajes:
        g["maquina"] = nombres.get(g["id_maquina"])

    sin_dato = [m for m in maquinas if m["id"] not in eficiencias]
    sin_dato.sort(key=lambda m: (m["numero"] is None, m["numero"] or 0))
    return render_template("produccion.html", filas=filas, gramajes=gramajes,
                           sin_dato=sin_dato)


# --------------------------------------------------------------------------
# Repuestos: agujas, levas y bandas
# --------------------------------------------------------------------------
# Los seis cuadros, como se ven en pantalla. Las columnas de la base están en
# `store.CUADROS`; acá va sólo cómo se llaman y cómo se escriben. Un test
# chequea que las dos listas hablen de las mismas columnas.
CUADROS_REPUESTOS = [
    {"clave": "agujas", "titulo": "Qué aguja lleva cada máquina",
     "bajada": "Para buscarla parado al lado de la máquina",
     "columnas": [
         {"campo": "id_maquina", "titulo": "Máquina", "tipo": "maquina"},
         {"campo": "descripcion", "titulo": "Modelo", "tipo": "texto"},
         {"campo": "cilindro", "titulo": "Cilindro", "tipo": "lineas"},
         {"campo": "plato", "titulo": "Plato", "tipo": "lineas"},
         {"campo": "platinas", "titulo": "Platinas", "tipo": "lineas"},
         {"campo": "nota", "titulo": "Nota", "tipo": "texto"},
     ]},
    {"clave": "modelos", "titulo": "Cómo se piden",
     "bajada": "Por modelo, con la marca: es lo que se le pasa al proveedor",
     "columnas": [
         {"campo": "modelo", "titulo": "Modelo y máquinas", "tipo": "texto"},
         {"campo": "marca_aguja", "titulo": "Marca", "tipo": "texto"},
         {"campo": "codigos", "titulo": "Códigos", "tipo": "lineas"},
         {"campo": "donde", "titulo": "Dónde va", "tipo": "texto"},
         {"campo": "platinas", "titulo": "Platinas", "tipo": "lineas"},
         {"campo": "marca_platina", "titulo": "Marca de la platina", "tipo": "texto"},
         {"campo": "nota", "titulo": "Nota", "tipo": "texto"},
     ]},
    {"clave": "levas", "titulo": "Levas",
     "bajada": "La misma leva sirve para varias máquinas del mismo modelo",
     "columnas": [
         {"campo": "maquinas", "titulo": "Máquinas", "tipo": "texto"},
         {"campo": "codigo", "titulo": "Código", "tipo": "texto"},
         {"campo": "cantidad", "titulo": "Cantidad", "tipo": "entero"},
         {"campo": "ubicacion", "titulo": "Dónde va", "tipo": "texto"},
         {"campo": "accionamiento", "titulo": "Para qué", "tipo": "texto"},
     ]},
    # Memminger-IRO hace los alimentadores de hilo: su banda es la correa
    # dentada que los mueve a todos juntos, arriba de la máquina. Por eso hay
    # tres medidas por máquina — 1/2, 3/4 y la de lycra.
    # La segunda tabla de la hoja INVENTARIO LEVAS: no es el inventario, es
    # cómo se arma la máquina para tejer cada tela.
    {"clave": "levas_tela", "titulo": "Cuántas levas lleva cada tela",
     "bajada": "De trabajo, de retenido y de anulación",
     "columnas": [
         {"campo": "tela", "titulo": "Tela", "tipo": "texto"},
         {"campo": "marca", "titulo": "Máquina", "tipo": "texto"},
         {"campo": "diametro", "titulo": "Diámetro", "tipo": "texto"},
         {"campo": "alimentadores", "titulo": "Alimentadores", "tipo": "texto"},
         {"campo": "trabajo", "titulo": "De trabajo", "tipo": "texto"},
         {"campo": "retenido", "titulo": "De retenido", "tipo": "texto"},
         {"campo": "anulacion", "titulo": "De anulación", "tipo": "texto"},
         {"campo": "nota", "titulo": "Nota", "tipo": "texto"},
     ]},
    {"clave": "bandas", "titulo": "Bandas Memminger",
     "bajada": "La correa que mueve los alimentadores de hilo",
     "columnas": [
         {"campo": "maquinas", "titulo": "Máquinas", "tipo": "texto"},
         {"campo": "cantidad_maquinas", "titulo": "Cantidad", "tipo": "entero"},
         {"campo": "diametro", "titulo": "Diámetro", "tipo": "decimal", "sufijo": '"'},
         {"campo": "media", "titulo": "1/2", "tipo": "texto"},
         {"campo": "tres_cuartos", "titulo": "3/4", "tipo": "texto"},
         {"campo": "lycra", "titulo": "Lycra", "tipo": "texto"},
     ]},
    {"clave": "motor", "titulo": "Bandas de motor",
     "bajada": "La que va del motor a la máquina, y la del cobrador",
     "columnas": [
         {"campo": "maquinas", "titulo": "Máquinas", "tipo": "texto"},
         {"campo": "cantidad_maquinas", "titulo": "Cantidad", "tipo": "entero"},
         {"campo": "diametro", "titulo": "Diámetro", "tipo": "decimal", "sufijo": '"'},
         {"campo": "banda", "titulo": "Banda", "tipo": "texto"},
         {"campo": "cobrador", "titulo": "Cobrador", "tipo": "texto"},
         {"campo": "nota", "titulo": "Nota", "tipo": "texto"},
     ]},
    # Las columnas «CODIGO | CANTIDAD» de la hoja BANDAS: no es un código de
    # repuesto, es la MEDIDA de la banda (6,6 · 7,2 · 8,8), las mismas que salen
    # en las columnas de 1/2, 3/4 y lycra. Y la cantidad es cuántas hay
    # guardadas de esa medida.
    {"clave": "stock", "titulo": "Cuántas bandas hay en bodega",
     "bajada": "De cada medida, contadas",
     "columnas": [
         {"campo": "medida", "titulo": "Medida de la banda", "tipo": "decimal",
          "decimales": 1},
         {"campo": "cantidad", "titulo": "Cuántas hay", "tipo": "entero"},
     ]},
]


def _valor_de_columna(columna, crudo, maquinas):
    """Lo que se escribió en una celda, convertido a lo que guarda la base."""
    crudo = (crudo or "").strip()
    if not crudo:
        return None
    if columna["tipo"] == "maquina":
        return _buscar_maquina(crudo, maquinas)
    if columna["tipo"] == "entero":
        return excel.a_numero(crudo)
    if columna["tipo"] == "decimal":
        return excel.a_decimal(crudo)
    return crudo


def _en_criollo(exc):
    """El error de Postgres no lo lee nadie. Se dice qué pasó."""
    nombre = type(exc).__name__
    if nombre == "UniqueViolation":
        return "Ya hay una fila cargada con esos mismos datos."
    if nombre == "NotNullViolation":
        return "Falta llenar una columna que no puede quedar vacía."
    return str(exc)


# Tres pestañas, no seis. La planilla tiene una hoja por cada cosa, pero de esas
# seis hojas hay dos que hablan de lo mismo y tres que hablan de bandas:
#
#   * «AGUJAS» es POR MÁQUINA: la usa el mecánico parado al lado de la máquina.
#   * «CODIGO DE AGUJAS» es POR MODELO y trae lo que la otra no tiene —la marca
#     de la aguja, dónde va, la marca de la platina—: es la lista con la que se
#     COMPRA. No son dos datos, es el mismo dato para dos usos, así que van en
#     la misma pestaña: la de usar arriba y la de comprar abajo, plegada.
#   * Las Memminger, las de motor y el stock son las tres bandas: una pestaña.
PESTANAS_REPUESTOS = [
    {"clave": "agujas", "titulo": "Agujas", "cuadros": ["agujas", "modelos"]},
    {"clave": "levas", "titulo": "Levas", "cuadros": ["levas", "levas_tela"]},
    {"clave": "bandas", "titulo": "Bandas", "cuadros": ["bandas", "motor", "stock"]},
]


def _pestana_de(clave_cuadro):
    """En qué pestaña vive un cuadro, para volver ahí después de guardar."""
    for pestana in PESTANAS_REPUESTOS:
        if clave_cuadro in pestana["cuadros"]:
            return pestana["clave"]
    return PESTANAS_REPUESTOS[0]["clave"]


@app.route("/repuestos", methods=["GET", "POST"])
@requiere_login
def repuestos():
    """Qué repuesto lleva cada máquina y cuánto hay.

    Cada fila se edita con el lapicito, y abajo de cada tabla hay una fila en
    blanco para agregar.
    """
    ver = request.values.get("ver") or "agujas"
    pestana = next((p for p in PESTANAS_REPUESTOS if p["clave"] == ver), None)
    if pestana is None:
        pestana, ver = PESTANAS_REPUESTOS[0], PESTANAS_REPUESTOS[0]["clave"]
    try:
        maquinas, _, _ = asinfo.maquinas()
    except asinfo.AsinfoNoDisponible:
        maquinas = []

    if request.method == "POST":
        # `cuadro` dice qué tabla se tocó; `ver`, a qué pestaña volver.
        clave_cuadro = request.form.get("cuadro") or ver
        cuadro = next((c for c in CUADROS_REPUESTOS if c["clave"] == clave_cuadro), None)
        clave = (request.form.get("clave") or "").strip() or None
        try:
            if cuadro is None:
                raise ValueError("No se sabe qué cuadro se está editando.")
            if request.form.get("accion") == "borrar":
                if not clave:
                    raise ValueError("No se sabe qué fila borrar.")
                store.borrar_repuesto(clave_cuadro, clave)
                flash("Fila borrada.", "ok")
            else:
                datos = {c["campo"]: _valor_de_columna(c, request.form.get(c["campo"]),
                                                       maquinas)
                         for c in cuadro["columnas"]}
                store.guardar_repuesto(clave_cuadro, clave, datos)
                flash("Guardado." if clave else "Fila agregada.", "ok")
        except Exception as exc:  # noqa: BLE001
            flash(_en_criollo(exc), "error")
        return redirect(url_for("repuestos", ver=_pestana_de(clave_cuadro)))

    por_id = {m["id"]: m for m in maquinas}
    cuadros = []
    for clave_cuadro in pestana["cuadros"]:
        cuadro = next(c for c in CUADROS_REPUESTOS if c["clave"] == clave_cuadro)
        filas = store.filas_de(clave_cuadro)
        if clave_cuadro == "agujas":
            # Por número de máquina, no por el id de Asinfo: la MQ 30 tiene el
            # id 804 y quedaba en el medio de las de cincuenta y pico.
            filas.sort(key=lambda f: (por_id.get(f["id_maquina"], {}).get("numero") is None,
                                      por_id.get(f["id_maquina"], {}).get("numero") or 0))
        cuadros.append({**cuadro, "filas": filas,
                        "clave_fila": store.CUADROS[clave_cuadro]["clave"]})

    return render_template("repuestos.html", pestanas=PESTANAS_REPUESTOS,
                           pestana=pestana, ver=ver, cuadros=cuadros,
                           maquinas_por_id=por_id,
                           editando=(request.args.get("editar") or "").strip(),
                           cuadro_editando=(request.args.get("cuadro") or "").strip())


@app.route("/healthz")
def healthz():
    """Nunca levanta. Si algo está mal, lo DICE — un health que se cae con la
    app no sirve para saber por qué se cayó."""
    estado = {
        "ok": ERROR_ARRANQUE is None,
        "base": "error" if ERROR_ARRANQUE else "ok",
        "asinfo_configurado": asinfo.configurado(),
        # Qué versión está corriendo de verdad. Sin esto no hay forma de saber
        # si el server se actualizó: se pushea, pasa el CI, y uno se queda
        # mirando la pantalla vieja sin entender por qué.
        "version": VERSION,
        # Y cuándo se instaló. `version` sale de un archivo que escribe el
        # updater y ya se quedó pegado una vez; esto sale del código mismo, así
        # que no puede mentir.
        "desplegado": DESPLEGADO,
    }
    if ERROR_ARRANQUE:
        estado["error_arranque"] = ERROR_ARRANQUE
        return estado, 503
    # Si una sentencia del esquema no pudo correr, la app igual levanta pero
    # acá se dice cuál. Antes eso tiraba abajo el arranque, el server deshacía
    # el deploy, y desde afuera parecía que pushear no hacía nada.
    if store.AVISOS_ESQUEMA:
        estado["avisos_esquema"] = store.AVISOS_ESQUEMA
    try:
        estado["tipos_cargados"] = len(store.tipos())
    except Exception as exc:  # noqa: BLE001
        estado["ok"] = False
        estado["base"] = "error"
        estado["error"] = str(exc)
        return estado, 503
    return estado


# --------------------------------------------------------------------------
# Formato (español: punto para miles, coma para decimales)
# --------------------------------------------------------------------------
# Un dato que no está se muestra como «—», no tira la pantalla abajo. Jinja
# devuelve Undefined cuando falta una clave del diccionario, y eso no es None:
# sin este freno, un campo que no vino rompe la pantalla entera con un 500.
def _falta(valor):
    return valor is None or isinstance(valor, jinja2.Undefined)


@app.template_filter("num")
def _num(valor, decimales=0):
    if _falta(valor):
        return "—"
    try:
        s = f"{float(valor):,.{decimales}f}"
    except (TypeError, ValueError):
        return "—"
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


@app.template_filter("pct")
def _pct(valor):
    if _falta(valor):
        return "—"
    return f"{valor * 100:,.0f} %".replace(",", ".")


@app.template_filter("fecha_es")
def _fecha_es(valor):
    if _falta(valor) or not valor:
        return "—"
    return valor.strftime("%d/%m/%Y")


@app.template_filter("editable")
def _editable(valor):
    """El valor como se escribe a mano: con coma y sin punto de miles.

    El filtro `num` es para leer (1.410); acá hace falta lo que después se
    vuelve a guardar, y un punto de miles adentro de un campo se lee mal.
    """
    if valor is None:
        return ""
    if isinstance(valor, bool) or not isinstance(valor, (int, float, Decimal)):
        return str(valor)
    s = f"{float(valor):.4f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


@app.template_filter("mq")
def _mq(maquina):
    """Como la llaman en planta: MQ 3.

    Acepta la máquina entera o sólo su nombre de Asinfo: en el historial se
    guarda el nombre y no el número, y ahí también tiene que decir MQ 3 y no
    TEJEDURIA-MQ 003, para que la columna se lea igual en todas las pantallas.
    """
    if not maquina:
        return "—"
    if isinstance(maquina, str):
        encontrado = re.search(r"(\d+)\s*$", maquina)
        return f"MQ {int(encontrado.group(1))}" if encontrado else maquina
    numero = maquina.get("numero")
    return f"MQ {numero}" if numero is not None else maquina.get("nombre", "—")


if __name__ == "__main__":
    store.init_pool()
    app.run(host="0.0.0.0", port=config.PORT, debug=True)
