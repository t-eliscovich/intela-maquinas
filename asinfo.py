"""Puente de SÓLO LECTURA a Asinfo, vía la API de Metabase.

Dos cosas se leen de Asinfo y nada más:

  1. El catálogo de máquinas (`maquina`).
  2. Cuántos kg y cuántos rollos entregó cada máquina DESDE la fecha de su
     último service.

Reglas duras de este módulo. Las tres se pagaron caras:

  * Nunca se escribe en Asinfo. Sólo SELECT.
  * Se cachea SÓLO el éxito. Un timeout devuelve la última lectura buena, no
    un cero — un cero acá pondría el semáforo en verde por un problema de red,
    que es exactamente el fallo que este programa existe para evitar.
  * **La suma la hace SQL Server, no Python.** Metabase corta cualquier
    resultado en 2.000 filas por defecto, en silencio. Traer la producción
    día por día y sumarla acá significaba perder los kilos de las últimas
    máquinas y mostrarlas como recién atendidas. Agregando del lado del motor,
    la respuesta es una fila por (máquina, tipo de service) — decenas de filas,
    imposible de truncar. Además hay un guard explícito por si acaso.
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timedelta

import requests

import config

log = logging.getLogger(__name__)

_lock = threading.Lock()
_session_token: str | None = None
_cache: dict[str, dict] = {}

# Tope que le pedimos a Metabase. Muy por encima de lo que puede devolver
# cualquiera de nuestras consultas agregadas (decenas de filas).
_TOPE_FILAS = 100_000

# Cuántos pares (máquina, tipo) mandamos por consulta. SQL Server admite hasta
# 1.000 filas en un VALUES; nos quedamos cómodos.
_LOTE = 400

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class AsinfoNoDisponible(Exception):
    """Asinfo no contestó y no hay lectura previa para mostrar."""


class RespuestaTruncada(Exception):
    """Metabase devolvió el tope de filas: el resultado está incompleto.

    Se trata como error y NO como dato. Un resultado truncado se vería como
    'esta máquina produjo menos', que es un falso verde en el semáforo.
    """


def configurado() -> bool:
    return bool(config.METABASE_URL and config.METABASE_USERNAME and config.METABASE_PASSWORD)


def _login() -> str:
    global _session_token
    r = requests.post(
        f"{config.METABASE_URL}/api/session",
        json={"username": config.METABASE_USERNAME, "password": config.METABASE_PASSWORD},
        timeout=30,
    )
    r.raise_for_status()
    _session_token = r.json()["id"]
    return _session_token


def _consultar(sql: str) -> list[list]:
    """Corre un SELECT contra Asinfo. Reintenta una vez si el token venció."""
    if not configurado():
        raise AsinfoNoDisponible("El puente a Asinfo no está configurado.")

    global _session_token
    for intento in (1, 2):
        token = _session_token or _login()
        r = requests.post(
            f"{config.METABASE_URL}/api/dataset",
            headers={"X-Metabase-Session": token},
            json={
                "database": config.ASINFO_DB_ID,
                "type": "native",
                "native": {"query": sql},
                # Sin esto, Metabase corta en 2.000 filas sin avisar.
                "constraints": {
                    "max-results": _TOPE_FILAS,
                    "max-results-bare-rows": _TOPE_FILAS,
                },
            },
            timeout=120,
        )
        if r.status_code == 401 and intento == 1:
            _session_token = None
            continue
        r.raise_for_status()
        cuerpo = r.json()
        if cuerpo.get("error"):
            raise RuntimeError(f"Asinfo rechazó la consulta: {cuerpo['error']}")
        filas = cuerpo["data"]["rows"]
        if len(filas) >= _TOPE_FILAS:
            raise RespuestaTruncada(
                f"Metabase devolvió {len(filas)} filas (el tope). El resultado "
                "está incompleto y no se puede usar para contar kilos."
            )
        return filas
    raise AsinfoNoDisponible("No se pudo autenticar contra Metabase.")


def _cacheado(clave: str, cargar):
    """Devuelve `cargar()` cacheado. Guarda SÓLO el éxito; ante error devuelve
    la última lectura buena marcada como vieja."""
    ahora = datetime.utcnow()
    with _lock:
        entrada = _cache.get(clave)
    if entrada and ahora - entrada["momento"] < timedelta(minutes=config.CACHE_MINUTOS):
        return entrada["datos"], entrada["momento"], True

    try:
        datos = cargar()
    except Exception as exc:  # noqa: BLE001
        log.warning("Asinfo no contestó (%s): %s", clave, exc)
        if entrada:
            return entrada["datos"], entrada["momento"], False
        raise AsinfoNoDisponible(str(exc)) from exc

    with _lock:
        _cache[clave] = {"datos": datos, "momento": ahora}
    return datos, ahora, True


# --------------------------------------------------------------------------
# Catálogo de máquinas
# --------------------------------------------------------------------------
def maquinas() -> tuple[list[dict], datetime, bool]:
    """Las máquinas activas del prefijo configurado, ordenadas por nombre."""

    def cargar():
        prefijo = config.PREFIJO_MAQUINAS.replace("'", "''")
        filas = _consultar(
            f"""
            SELECT id_maquina, codigo, nombre
              FROM maquina
             WHERE activo = 1
               AND nombre LIKE '{prefijo}%'
             ORDER BY nombre
            """
        )
        return [{"id": f[0], "codigo": f[1], "nombre": f[2]} for f in filas]

    return _cacheado("maquinas", cargar)


# --------------------------------------------------------------------------
# Acumulado desde el último service — agregado por SQL Server
# --------------------------------------------------------------------------
def acumulados(pares: list[tuple[int, int, str]]) -> tuple[dict, datetime, bool]:
    """kg y rollos por (máquina, tipo) desde la fecha del último service.

    `pares` es [(id_maquina, tipo_id, 'YYYY-MM-DD'), ...].
    Devuelve {(id_maquina, tipo_id): (kg, rollos)}.

    La fecha es EXCLUSIVA: la producción del día del service no cuenta como
    desgaste posterior a ese service.

    Una fila de respuesta por par. Con 43 máquinas y un puñado de tipos son
    decenas de filas — no hay forma de que un tope de filas la trunque.
    """
    if not pares:
        return {}, datetime.utcnow(), True

    # Validación dura: estos valores se interpolan en el SQL.
    limpios: list[tuple[int, int, str]] = []
    for id_maquina, tipo_id, desde in pares:
        desde = str(desde)[:10]
        if not _ISO.match(desde):
            raise ValueError(f"Fecha con formato inesperado: {desde!r}")
        limpios.append((int(id_maquina), int(tipo_id), desde))

    piso = max(config.FECHA_PISO, "0001-01-01")

    def cargar():
        resultado: dict[tuple[int, int], tuple[float, int]] = {}
        for i in range(0, len(limpios), _LOTE):
            lote = limpios[i : i + _LOTE]
            values = ", ".join(
                f"({m}, {t}, '{max(d, piso)}')" for m, t, d in lote
            )
            filas = _consultar(
                f"""
                SELECT s.id_maquina, s.tipo_id,
                       SUM(d.cantidad) AS kg,
                       COUNT(*)        AS rollos
                  FROM (VALUES {values}) AS s(id_maquina, tipo_id, desde)
                  JOIN detalle_movimiento_inventario d
                    ON d.id_maquina = s.id_maquina
                   AND d.id_bodega_destino = {config.BODEGA_TELA_CRUDA}
                  JOIN movimiento_inventario mi
                    ON mi.id_movimiento_inventario = d.id_movimiento_inventario
                   AND mi.fecha > CONVERT(date, s.desde)
                 GROUP BY s.id_maquina, s.tipo_id
                """
            )
            for id_maquina, tipo_id, kg, rollos in filas:
                resultado[(int(id_maquina), int(tipo_id))] = (
                    round(float(kg or 0), 2),
                    int(rollos or 0),
                )
        # Un par sin filas en Asinfo es legítimo: la máquina no produjo nada
        # desde el service. Eso es CERO de verdad, no un cero por error.
        for m, t, _ in limpios:
            resultado.setdefault((m, t), (0.0, 0))
        return resultado

    clave = "acum:" + str(hash(tuple(limpios)))
    return _cacheado(clave, cargar)
