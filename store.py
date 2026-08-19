"""Las dos tablas propias del programa: el plan y los services hechos.

Todo lo demás (kilos, rollos) se calcula al vuelo leyendo Asinfo. Acá sólo
vive lo que ningún otro sistema sabe.

El schema se crea solo al arrancar (`CREATE ... IF NOT EXISTS`), igual que en
Programa Core: el deploy no corre migraciones.
"""
from __future__ import annotations

from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool

import config

_pool: SimpleConnectionPool | None = None


def init_pool() -> None:
    global _pool
    if not config.DATABASE_URL:
        raise RuntimeError("Falta MAQUINAS_DATABASE_URL")
    _pool = SimpleConnectionPool(1, 4, config.DATABASE_URL)
    bootstrap()


@contextmanager
def _conn():
    assert _pool is not None, "init_pool() no fue llamado"
    con = _pool.getconn()
    try:
        yield con
    finally:
        _pool.putconn(con)


def _todos(sql: str, args: tuple = ()) -> list[dict]:
    with _conn() as con, con.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, args)
        return [dict(r) for r in cur.fetchall()]


def _ejecutar(sql: str, args: tuple = ()) -> None:
    with _conn() as con, con.cursor() as cur:
        cur.execute(sql, args)
        con.commit()


# --------------------------------------------------------------------------
def bootstrap() -> None:
    with _conn() as con, con.cursor() as cur:
        cur.execute(
            """
            CREATE SCHEMA IF NOT EXISTS mantenimiento;

            CREATE TABLE IF NOT EXISTS mantenimiento.tipo_service (
                id           serial PRIMARY KEY,
                nombre       text NOT NULL UNIQUE,
                cada_kg      numeric(12,2),
                cada_rollos  integer,
                cada_dias    integer,
                activo       boolean NOT NULL DEFAULT true,
                creado_en    timestamptz NOT NULL DEFAULT now()
            );

            CREATE TABLE IF NOT EXISTS mantenimiento.service (
                id              serial PRIMARY KEY,
                id_maquina      integer NOT NULL,
                maquina_nombre  text NOT NULL,
                tipo_id         integer NOT NULL
                                REFERENCES mantenimiento.tipo_service(id),
                fecha           date NOT NULL,
                hecho_por       text NOT NULL,
                nota            text,
                creado_en       timestamptz NOT NULL DEFAULT now()
            );

            CREATE INDEX IF NOT EXISTS service_maquina_tipo_idx
                ON mantenimiento.service (id_maquina, tipo_id, fecha DESC);
            """
        )
        con.commit()


# --- Tipos de service ------------------------------------------------------
def tipos(incluir_inactivos: bool = False) -> list[dict]:
    filtro = "" if incluir_inactivos else "WHERE activo"
    return _todos(
        f"SELECT * FROM mantenimiento.tipo_service {filtro} ORDER BY nombre"
    )


def crear_tipo(nombre, cada_kg, cada_rollos, cada_dias) -> None:
    _ejecutar(
        """INSERT INTO mantenimiento.tipo_service (nombre, cada_kg, cada_rollos, cada_dias)
           VALUES (%s, %s, %s, %s)""",
        (nombre.strip(), cada_kg, cada_rollos, cada_dias),
    )


def editar_tipo(tipo_id, nombre, cada_kg, cada_rollos, cada_dias, activo) -> None:
    _ejecutar(
        """UPDATE mantenimiento.tipo_service
              SET nombre=%s, cada_kg=%s, cada_rollos=%s, cada_dias=%s, activo=%s
            WHERE id=%s""",
        (nombre.strip(), cada_kg, cada_rollos, cada_dias, activo, tipo_id),
    )


# --- Services hechos -------------------------------------------------------
def registrar_service(id_maquina, maquina_nombre, tipo_id, fecha, hecho_por, nota) -> None:
    _ejecutar(
        """INSERT INTO mantenimiento.service
               (id_maquina, maquina_nombre, tipo_id, fecha, hecho_por, nota)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (id_maquina, maquina_nombre, tipo_id, fecha, hecho_por.strip(), (nota or "").strip() or None),
    )


def registrar_muchos(filas: list[tuple]) -> int:
    """Alta en lote para el arranque inicial.

    `filas` = [(id_maquina, maquina_nombre, tipo_id, fecha, hecho_por, nota), ...]
    Devuelve cuántas se insertaron. Todo en una transacción: o entran todas o
    ninguna — un arranque a medias dejaría el semáforo mintiendo sobre la mitad
    de las máquinas.
    """
    if not filas:
        return 0
    with _conn() as con, con.cursor() as cur:
        cur.executemany(
            """INSERT INTO mantenimiento.service
                   (id_maquina, maquina_nombre, tipo_id, fecha, hecho_por, nota)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            filas,
        )
        n = cur.rowcount
        con.commit()
    return len(filas) if n in (None, -1) else n


# Punto de partida sugerido para una tejedora circular. Son EDITABLES: la idea
# es que el mecánico corrija números, no que invente la lista desde cero.
TIPOS_SUGERIDOS = [
    ("Limpieza general",   50000,  2500,  30),
    ("Engrase / lubricación", 80000, 4000,  60),
    ("Cambio de agujas",  250000, 12000, 365),
    ("Cambio de platinas", 400000, 20000, 730),
]


def crear_tipos_sugeridos() -> int:
    """Carga la lista sugerida. Idempotente: saltea los que ya existan."""
    creados = 0
    existentes = {t["nombre"].lower() for t in tipos(incluir_inactivos=True)}
    for nombre, kg, rollos, dias in TIPOS_SUGERIDOS:
        if nombre.lower() in existentes:
            continue
        crear_tipo(nombre, kg, rollos, dias)
        creados += 1
    return creados


def ultimos_por_maquina_y_tipo() -> dict[tuple[int, int], dict]:
    """El service más reciente de cada par (máquina, tipo). Es el punto cero
    de cada contador."""
    filas = _todos(
        """
        SELECT DISTINCT ON (id_maquina, tipo_id)
               id_maquina, tipo_id, fecha, hecho_por, nota, maquina_nombre
          FROM mantenimiento.service
         ORDER BY id_maquina, tipo_id, fecha DESC, id DESC
        """
    )
    return {(f["id_maquina"], f["tipo_id"]): f for f in filas}


def historial(id_maquina: int | None = None, limite: int = 200) -> list[dict]:
    if id_maquina is None:
        return _todos(
            """SELECT s.*, t.nombre AS tipo_nombre
                 FROM mantenimiento.service s
                 JOIN mantenimiento.tipo_service t ON t.id = s.tipo_id
                ORDER BY s.fecha DESC, s.id DESC LIMIT %s""",
            (limite,),
        )
    return _todos(
        """SELECT s.*, t.nombre AS tipo_nombre
             FROM mantenimiento.service s
             JOIN mantenimiento.tipo_service t ON t.id = s.tipo_id
            WHERE s.id_maquina = %s
            ORDER BY s.fecha DESC, s.id DESC LIMIT %s""",
        (id_maquina, limite),
    )


def responsables() -> list[str]:
    """Nombres ya usados, para autocompletar el campo 'quién lo hizo'."""
    filas = _todos(
        """SELECT hecho_por, COUNT(*) n FROM mantenimiento.service
            GROUP BY hecho_por ORDER BY n DESC LIMIT 30"""
    )
    return [f["hecho_por"] for f in filas]


def fecha_service_mas_vieja() -> str | None:
    filas = _todos("SELECT MIN(fecha) AS f FROM mantenimiento.service")
    f = filas[0]["f"] if filas else None
    return f.isoformat() if f else None
