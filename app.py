"""Mantenimiento de máquinas — Intela tejeduría.

Tres pantallas y nada más:

    /            semáforo: qué máquina necesita service
    /registrar   cargar un service hecho
    /tipos       definir los tipos de service y cada cuánto van

Los kilos y los rollos NO se cargan a mano: salen de Asinfo, que ya registra
cada rollo de tela cruda con la máquina que lo tejió.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from functools import wraps

from flask import (Flask, flash, redirect, render_template, request, session,
                   url_for)

import asinfo
import config
import store

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

# Cuánto antes del umbral se pone amarillo.
UMBRAL_AVISO = 0.80

# ---------------------------------------------------------------------------
# Arranque del pool de la base — A NIVEL DE MÓDULO, a propósito.
#
# ⚠ Esto NO puede vivir dentro de `if __name__ == "__main__"`. En producción
# Waitress hace `import app` y toma el objeto `app`: el bloque __main__ nunca
# corre. Si el pool se abriera sólo ahí, la app levantaría igual, tomaría el
# puerto, la tarea diría "Running"... y CADA pantalla devolvería 500 con un
# `AssertionError: init_pool() no fue llamado`. Pasó exactamente eso el
# 18/08/2026 en el primer deploy.
#
# Si la base no está, no reventamos al importar: guardamos el error y lo
# mostramos en pantalla. Un 500 pelado no le dice nada a nadie.
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
# Login (una sola contraseña compartida — "muy muy fácil")
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
# El cálculo: cuánto lleva cada máquina desde su último service
# --------------------------------------------------------------------------
def _porcentaje(consumido, umbral):
    """Qué fracción del umbral se consumió. None si el umbral no aplica."""
    if not umbral:
        return None
    return float(consumido) / float(umbral)


def armar_semaforo():
    """Una fila por (máquina, tipo de service) que ya tenga un primer service.

    Las máquinas sin ningún service todavía no tienen punto cero, así que no
    entran al semáforo — se listan aparte como pendientes de arrancar.
    """
    tipos = store.tipos()
    maquinas, _, _ = asinfo.maquinas()
    ultimos = store.ultimos_por_maquina_y_tipo()

    hoy = date.today()
    filas, pendientes = [], []

    # Primero armamos la lista de pares que SÍ tienen punto cero, y le pedimos
    # a Asinfo los acumulados de todos juntos: una consulta agregada, no una
    # descarga de la producción día por día (ver asinfo.acumulados).
    pares = []
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
            dias = (hoy - desde).days

            pcts = [
                p
                for p in (
                    _porcentaje(kg, tipo["cada_kg"]),
                    _porcentaje(rollos, tipo["cada_rollos"]),
                    _porcentaje(dias, tipo["cada_dias"]),
                )
                if p is not None
            ]
            peor = max(pcts) if pcts else 0.0

            if not pcts:
                estado = "sin_umbral"
            elif peor >= 1.0:
                estado = "vencido"
            elif peor >= UMBRAL_AVISO:
                estado = "por_vencer"
            else:
                estado = "ok"

            filas.append(
                {
                    "maquina": maquina,
                    "tipo": tipo,
                    "desde": desde,
                    "hecho_por": ultimo["hecho_por"],
                    "kg": kg,
                    "rollos": rollos,
                    "dias": dias,
                    "pct_kg": _porcentaje(kg, tipo["cada_kg"]),
                    "pct_rollos": _porcentaje(rollos, tipo["cada_rollos"]),
                    "pct_dias": _porcentaje(dias, tipo["cada_dias"]),
                    "peor": peor,
                    "estado": estado,
                }
            )

    # Lo más urgente arriba.
    filas.sort(key=lambda f: f["peor"], reverse=True)
    return filas, pendientes, leido_en, fresco


# --------------------------------------------------------------------------
# Pantallas
# --------------------------------------------------------------------------
@app.route("/")
@requiere_login
def semaforo():
    try:
        filas, pendientes, leido_en, fresco = armar_semaforo()
        error = None
    except asinfo.AsinfoNoDisponible as exc:
        filas, pendientes, leido_en, fresco = [], [], None, False
        error = str(exc)

    resumen = {
        "vencido": sum(1 for f in filas if f["estado"] == "vencido"),
        "por_vencer": sum(1 for f in filas if f["estado"] == "por_vencer"),
        "ok": sum(1 for f in filas if f["estado"] == "ok"),
    }
    return render_template(
        "semaforo.html",
        filas=filas,
        pendientes=pendientes,
        resumen=resumen,
        leido_en=leido_en,
        fresco=fresco,
        error=error,
        hay_tipos=bool(store.tipos()),
    )


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
            id_maquina = int(request.form["id_maquina"])
            tipo_id = int(request.form["tipo_id"])
            fecha = request.form.get("fecha") or date.today().isoformat()
            hecho_por = request.form.get("hecho_por", "").strip()
            if not hecho_por:
                raise ValueError("Falta poner quién lo hizo.")
            if datetime.strptime(fecha, "%Y-%m-%d").date() > date.today():
                raise ValueError("La fecha no puede ser futura.")

            nombre = next(
                (m["nombre"] for m in maquinas if m["id"] == id_maquina), str(id_maquina)
            )
            store.registrar_service(
                id_maquina, nombre, tipo_id, fecha, hecho_por, request.form.get("nota")
            )
            flash(f"Service cargado en {nombre}. El contador arranca de cero.", "ok")
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
            def num(campo, entero=False):
                v = (request.form.get(campo) or "").strip()
                if not v:
                    return None
                return int(v) if entero else float(v)

            nombre = request.form.get("nombre", "").strip()
            if not nombre:
                raise ValueError("El tipo de service necesita un nombre.")

            cada_kg = num("cada_kg")
            cada_rollos = num("cada_rollos", entero=True)
            cada_dias = num("cada_dias", entero=True)
            if not any([cada_kg, cada_rollos, cada_dias]):
                raise ValueError(
                    "Poné al menos un umbral: cada cuántos kg, cuántos rollos o cuántos días."
                )

            tipo_id = request.form.get("tipo_id")
            if tipo_id:
                store.editar_tipo(
                    int(tipo_id), nombre, cada_kg, cada_rollos, cada_dias,
                    request.form.get("activo") == "on",
                )
                flash(f"«{nombre}» actualizado.", "ok")
            else:
                store.crear_tipo(nombre, cada_kg, cada_rollos, cada_dias)
                flash(f"«{nombre}» creado.", "ok")
            return redirect(url_for("tipos_view"))
        except Exception as exc:  # noqa: BLE001
            flash(str(exc), "error")

    return render_template("tipos.html", tipos=store.tipos(incluir_inactivos=True))


@app.route("/arranque", methods=["GET", "POST"])
@requiere_login
def arranque():
    """Poner el punto cero de TODAS las máquinas de una vez.

    Sin esto el programa es inusable: cada par (máquina, tipo) necesita un
    primer service para empezar a contar, y cargar 43 máquinas a mano antes de
    ver la primera pantalla no lo hace nadie. Acá se elige el tipo y la fecha
    desde la que se empieza a contar, y se crean todos juntos.
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
                raise ValueError("Todas las máquinas ya tienen punto de partida para ese service.")

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
        flash(f"Se cargaron {n} tipos sugeridos. Corregí los números con el mecánico."
              if n else "Ya estaban todos cargados.", "ok")
    except Exception as exc:  # noqa: BLE001
        flash(str(exc), "error")
    return redirect(url_for("tipos_view"))


@app.route("/maquina/<int:id_maquina>")
@requiere_login
def maquina_detalle(id_maquina):
    try:
        maquinas, _, _ = asinfo.maquinas()
    except asinfo.AsinfoNoDisponible:
        maquinas = []
    maquina = next((m for m in maquinas if m["id"] == id_maquina), None)
    return render_template(
        "maquina.html", maquina=maquina, historial=store.historial(id_maquina)
    )


@app.route("/healthz")
def healthz():
    """Nunca levanta. Si algo está mal, lo DICE — un health que se cae con la
    app no sirve para saber por qué se cayó."""
    estado = {
        "ok": ERROR_ARRANQUE is None,
        "base": "error" if ERROR_ARRANQUE else "ok",
        "asinfo_configurado": asinfo.configurado(),
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
# Filtros de formato (español: punto para miles, coma para decimales)
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


if __name__ == "__main__":
    store.init_pool()
    app.run(host="0.0.0.0", port=config.PORT, debug=True)
