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
from functools import wraps

from flask import (Flask, flash, g, redirect, render_template, request,
                   session, url_for)

import asinfo
import config
import excel
import store

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # un Excel de planta es chico

# Cuánto antes del tope se pone amarillo.
AVISO = 0.80

# Qué commit está corriendo. Lo escribe el auto-updater al reemplazar la
# carpeta; si el archivo no está, decimos que no se sabe en vez de inventar.
def _version():
    for ruta in (os.path.join(os.path.dirname(os.path.abspath(__file__)), ".version"),
                 r"C:\maquinas_update\.commit"):
        try:
            with open(ruta) as f:
                return f.read().strip()[:7] or "?"
        except OSError:
            continue
    return "?"


VERSION = _version()

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


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == config.PASSWORD:
            session["ok"] = True
            return redirect(request.args.get("next") or url_for("semaforo"))
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
    """Una fila por (máquina, tipo) que ya tenga un primer mantenimiento.

    Las máquinas sin ninguno todavía no tienen desde cuándo contar, así que no
    entran al semáforo: se listan aparte como pendientes de arrancar.
    """
    tipos = store.tipos()
    maquinas, _, _ = asinfo.maquinas()
    ultimos = store.ultimos_por_maquina_y_tipo()
    topes = store.topes_por_maquina()

    hoy = date.today()
    filas, pendientes, pares = [], [], []

    for maquina in maquinas:
        for tipo in tipos:
            ultimo = ultimos.get((maquina["id"], tipo["id"]))
            if not ultimo:
                pendientes.append({"maquina": maquina, "tipo": tipo})
                continue
            pares.append((maquina["id"], tipo["id"], ultimo["fecha"].isoformat()))

    acum, leido_en, fresco = asinfo.acumulados(pares)

    for maquina in maquinas:
        for tipo in tipos:
            ultimo = ultimos.get((maquina["id"], tipo["id"]))
            if not ultimo:
                continue

            desde = ultimo["fecha"]
            kg, rollos = acum.get((maquina["id"], tipo["id"]), (0.0, 0))
            tope, propio = tope_de(topes, maquina["id"], tipo)

            # El semáforo va por KILOS. Los días se muestran como dato, no
            # prenden nada: el desgaste es lo que la máquina tejió.
            pct = (float(kg) / tope) if tope else None
            if pct is None:
                estado = "sin_tope"
            elif pct >= 1.0:
                estado = "vencido"
            elif pct >= AVISO:
                estado = "por_vencer"
            else:
                estado = "ok"

            filas.append({
                "maquina": maquina,
                "tipo": tipo,
                "desde": desde,
                "hecho_por": ultimo["hecho_por"],
                "kg": kg,
                "rollos": rollos,
                "dias": (hoy - desde).days,
                "tope": tope,
                "tope_propio": propio,
                "falta": (tope - float(kg)) if tope else None,
                "pct": pct,
                "estado": estado,
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
    }
    return render_template(
        "semaforo.html",
        filas=filas_ver,
        solo=solo,
        pendientes=pendientes,
        resumen=resumen,
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
            tipo_id = int(request.form["tipo_id"])
            fecha = request.form.get("fecha") or date.today().isoformat()
            hecho_por = request.form.get("hecho_por", "").strip()
            if not hecho_por:
                raise ValueError("Falta poner quién lo hizo.")
            if datetime.strptime(fecha, "%Y-%m-%d").date() > date.today():
                raise ValueError("La fecha no puede ser futura.")

            crudo = (request.form.get("horas") or "").strip().replace(",", ".")
            horas = float(crudo) if crudo else None
            if horas is not None and not (0 < horas <= 200):
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
            tipo_id = int(request.form["tipo_id"])
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

    nombres_hojas = excel.hojas(ruta)

    # Dos formas de planilla, y se reconoce sola cuál es:
    #
    #   * La de planta: UNA HOJA POR MÁQUINA, con la ficha arriba y el
    #     historial abajo. No hay nada que mapear.
    #   * Una tabla comun: una fila por maquina. Ahi si hay que decir que
    #     columna es cada cosa.
    por_maquina, descartes_pm = excel.leer_por_maquina(ruta, maquinas, tipos)
    es_por_maquina = len(por_maquina) >= 3

    if es_por_maquina:
        listas, descartes = por_maquina, descartes_pm
        hoja, titulos, mapa = None, [], {}
    else:
        hoja = request.form.get("hoja") or nombres_hojas[0]
        titulos, filas = excel.leer(ruta, hoja)
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
# Todas las máquinas
# --------------------------------------------------------------------------
@app.route("/maquinas")
@requiere_login
def maquinas_lista():
    """El listado de las máquinas con su ficha. Se entra a cada una desde acá.

    Las máquinas salen de Asinfo; la ficha, de lo que se cargó. Una máquina sin
    ficha aparece igual: falta el dato, no falta la máquina.
    """
    try:
        maquinas, _, _ = asinfo.maquinas()
        error = None
    except asinfo.AsinfoNoDisponible as exc:
        maquinas, error = [], str(exc)

    fichas = store.fichas()
    ultimos = store.ultimos_por_maquina_y_tipo()
    tipos = store.tipos()

    filas = [{
        "maquina": m,
        "ficha": fichas.get(m["id"], {}),
        "ultimos": {t["id"]: (ultimos.get((m["id"], t["id"])) or {}).get("fecha")
                    for t in tipos},
    } for m in maquinas]
    filas.sort(key=lambda f: (f["maquina"]["numero"] is None, f["maquina"]["numero"] or 0))

    return render_template("maquinas.html", filas=filas, tipos=tipos, error=error)


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

    if request.method == "POST":
        try:
            datos = {c: (request.form.get(c) or "").strip() or None
                     for c in store.CAMPOS_FICHA}
            for entero in ("galga", "alimentadores", "agujas", "anio"):
                datos[entero] = excel.a_numero(datos[entero])
            datos["diametro"] = excel.a_decimal(datos["diametro"])
            store.guardar_ficha(id_maquina, datos)
            flash("Ficha guardada.", "ok")
        except Exception as exc:  # noqa: BLE001
            flash(str(exc), "error")
        return redirect(url_for("maquina_detalle", id_maquina=id_maquina))

    try:
        mensual, _, _ = asinfo.produccion_mensual(id_maquina)
    except Exception:  # noqa: BLE001
        mensual = []

    return render_template(
        "maquina.html",
        maquina=maquina,
        ficha=store.ficha(id_maquina),
        historial=store.historial(id_maquina),
        mensual=mensual,
    )


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
    }
    if ERROR_ARRANQUE:
        estado["error_arranque"] = ERROR_ARRANQUE
        return estado, 503
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
@app.template_filter("num")
def _num(valor, decimales=0):
    if valor is None:
        return "—"
    s = f"{float(valor):,.{decimales}f}"
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


@app.template_filter("pct")
def _pct(valor):
    if valor is None:
        return "—"
    return f"{valor * 100:,.0f} %".replace(",", ".")


@app.template_filter("fecha_es")
def _fecha_es(valor):
    return valor.strftime("%d/%m/%Y") if valor else "—"


@app.template_filter("mq")
def _mq(maquina):
    """Como la llaman en planta: MQ 3, y al lado el nombre de Asinfo."""
    if not maquina:
        return "—"
    numero = maquina.get("numero")
    return f"MQ {numero}" if numero is not None else maquina.get("nombre", "—")


if __name__ == "__main__":
    store.init_pool()
    app.run(host="0.0.0.0", port=config.PORT, debug=True)
