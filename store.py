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

            -- Cada cuántos kilos va un mantenimiento EN ESA MÁQUINA.
            -- Existe porque el número no es el mismo para todas: la más
            -- cargada teje ~139.000 kg/año y la menos ~7.400. Un número
            -- único deja media planta en verde para siempre.
            -- Si una máquina no tiene fila acá, manda el del tipo.
            CREATE TABLE IF NOT EXISTS mantenimiento.plan_maquina (
                id_maquina  integer NOT NULL,
                tipo_id     integer NOT NULL
                            REFERENCES mantenimiento.tipo_service(id),
                cada_kg     numeric(12,2),
                creado_en   timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (id_maquina, tipo_id)
            );

            -- La ficha de la máquina. Sólo lo que Asinfo NO sabe: el nombre y
            -- el estado siguen saliendo de allá, no se copian acá.
            CREATE TABLE IF NOT EXISTS mantenimiento.maquina_ficha (
                id_maquina     integer PRIMARY KEY,
                marca          text,
                modelo         text,
                galga          integer,
                diametro       numeric(6,2),
                alimentadores  integer,
                agujas         integer,
                anio           integer,
                serie          text,
                tipo_agujas    text,
                nota           text,
                editado_en     timestamptz NOT NULL DEFAULT now()
            );

            -- La tabla se crea con CREATE IF NOT EXISTS, asi que una columna
            -- agregada despues no llega sola a una base que ya existia.
            ALTER TABLE mantenimiento.maquina_ficha
                ADD COLUMN IF NOT EXISTS serie text;
            ALTER TABLE mantenimiento.maquina_ficha
                ADD COLUMN IF NOT EXISTS tipo_agujas text;
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


# --- Cada cuántos kg, por máquina ------------------------------------------
def topes_por_maquina() -> dict[tuple[int, int], float]:
    """{(id_maquina, tipo_id): cada_kg}. Sólo las máquinas con número propio."""
    filas = _todos(
        "SELECT id_maquina, tipo_id, cada_kg FROM mantenimiento.plan_maquina"
    )
    return {
        (f["id_maquina"], f["tipo_id"]): float(f["cada_kg"])
        for f in filas
        if f["cada_kg"] is not None
    }


def guardar_tope(id_maquina: int, tipo_id: int, cada_kg) -> None:
    """Pone (o borra, si viene vacío) el número de esa máquina."""
    if cada_kg in (None, ""):
        _ejecutar(
            "DELETE FROM mantenimiento.plan_maquina WHERE id_maquina=%s AND tipo_id=%s",
            (id_maquina, tipo_id),
        )
        return
    _ejecutar(
        """INSERT INTO mantenimiento.plan_maquina (id_maquina, tipo_id, cada_kg)
           VALUES (%s, %s, %s)
           ON CONFLICT (id_maquina, tipo_id)
           DO UPDATE SET cada_kg = EXCLUDED.cada_kg""",
        (id_maquina, tipo_id, cada_kg),
    )


# --- Ficha de la máquina ---------------------------------------------------
CAMPOS_FICHA = ("marca", "modelo", "galga", "diametro", "alimentadores",
                "agujas", "anio", "serie", "tipo_agujas", "nota")


def fichas() -> dict[int, dict]:
    return {f["id_maquina"]: f for f in _todos("SELECT * FROM mantenimiento.maquina_ficha")}


def ficha(id_maquina: int) -> dict:
    filas = _todos(
        "SELECT * FROM mantenimiento.maquina_ficha WHERE id_maquina=%s", (id_maquina,)
    )
    return filas[0] if filas else {}


def guardar_ficha(id_maquina: int, datos: dict) -> None:
    """Guarda sólo los campos conocidos. Lo que viene vacío queda en NULL:
    una ficha a medias es correcta, una ficha inventada no."""
    valores = [datos.get(c) if datos.get(c) not in ("",) else None for c in CAMPOS_FICHA]
    columnas = ", ".join(CAMPOS_FICHA)
    marcas = ", ".join(["%s"] * len(CAMPOS_FICHA))
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in CAMPOS_FICHA)
    _ejecutar(
        f"""INSERT INTO mantenimiento.maquina_ficha (id_maquina, {columnas})
            VALUES (%s, {marcas})
            ON CONFLICT (id_maquina) DO UPDATE
               SET {updates}, editado_en = now()""",
        (id_maquina, *valores),
    )


# --- Carga en lote desde el Excel ------------------------------------------
def cargar_lote(servicios: list[tuple], topes: list[tuple], fichas_: list[tuple]) -> dict:
    """Guarda todo lo que trajo el Excel en UNA transacción.

    O entra todo o no entra nada: una carga a medias dejaría el semáforo
    mintiendo sobre la mitad de las máquinas, que es peor que no cargar.

    servicios = [(id_maquina, maquina_nombre, tipo_id, fecha, hecho_por, nota), ...]
    topes     = [(id_maquina, tipo_id, cada_kg), ...]
    fichas_   = [(id_maquina, marca, modelo, galga, diametro, alimentadores,
                  agujas, anio, nota), ...]
    """
    columnas = ", ".join(CAMPOS_FICHA)
    marcas = ", ".join(["%s"] * len(CAMPOS_FICHA))
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in CAMPOS_FICHA)
    with _conn() as con, con.cursor() as cur:
        if servicios:
            cur.executemany(
                """INSERT INTO mantenimiento.service
                       (id_maquina, maquina_nombre, tipo_id, fecha, hecho_por, nota)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                servicios,
            )
        if topes:
            cur.executemany(
                """INSERT INTO mantenimiento.plan_maquina (id_maquina, tipo_id, cada_kg)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (id_maquina, tipo_id)
                   DO UPDATE SET cada_kg = EXCLUDED.cada_kg""",
                topes,
            )
        if fichas_:
            cur.executemany(
                f"""INSERT INTO mantenimiento.maquina_ficha (id_maquina, {columnas})
                    VALUES (%s, {marcas})
                    ON CONFLICT (id_maquina) DO UPDATE
                       SET {updates}, editado_en = now()""",
                fichas_,
            )
        con.commit()
    return {"servicios": len(servicios), "topes": len(topes), "fichas": len(fichas_)}
