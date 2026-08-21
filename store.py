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
    # El rollback no es de adorno: al editar a mano se viola un índice único
    # cada tanto, y una conexión que vuelve al pool con la transacción abierta
    # hace fallar la consulta siguiente, que no tiene nada que ver.
    with _conn() as con, con.cursor() as cur:
        try:
            cur.execute(sql, args)
            con.commit()
        except Exception:
            con.rollback()
            raise


# --------------------------------------------------------------------------
# Qué sentencias del esquema no pudieron correr. Vacío es lo normal.
# Se muestra en /healthz.
AVISOS_ESQUEMA: list[str] = []


def _sentencias(sql: str) -> list[str]:
    """Parte el esquema en sentencias, ignorando los `;` que están adentro de
    un comentario. Postgres los ignora; un split a lo bruto, no."""
    sin_comentarios = "\n".join(
        linea.split("--")[0] if "--" in linea else linea
        for linea in sql.split("\n"))
    return [s.strip() for s in sin_comentarios.split(";") if s.strip()]


def bootstrap() -> None:
    """Crea o completa el esquema, UNA SENTENCIA POR TRANSACCIÓN.

    Antes iba todo junto en un solo execute. El problema no es el rendimiento:
    es que si una sola sentencia fallaba, `init_pool` reventaba, la app
    arrancaba en modo error, /healthz devolvía 503 y el auto-update del server
    deshacía el deploy — sin dejar rastro de CUÁL sentencia había fallado.
    Desde afuera parecía que pushear no hacía nada.

    Ahora una sentencia que falla queda anotada en AVISOS_ESQUEMA y se sigue.
    La app levanta, /healthz lo dice, y se ve qué arreglar.
    """
    AVISOS_ESQUEMA.clear()
    for sentencia in _sentencias(ESQUEMA):
        try:
            with _conn() as con, con.cursor() as cur:
                cur.execute(sentencia)
                con.commit()
        except Exception as exc:  # noqa: BLE001
            corta = " ".join(sentencia.split())[:120]
            AVISOS_ESQUEMA.append(f"{corta} → {exc}")


ESQUEMA = """
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

            -- Que se cambio y cuanto llevo. Lo pidio la dueña el 20/08/2026:
            -- la planilla de planta ya tenia una columna de repuestos.
            ALTER TABLE mantenimiento.service
                ADD COLUMN IF NOT EXISTS repuestos text;
            ALTER TABLE mantenimiento.service
                ADD COLUMN IF NOT EXISTS horas numeric(5,2);

            -- De que hoja y de que fila de la planilla salio este
            -- mantenimiento. Es lo que permite cargar el historial COMPLETO
            -- (1.366 filas desde 2018) y volver a cargarlo sin duplicarlo.
            -- Un mantenimiento cargado a mano no tiene hoja: por eso el indice
            -- es parcial, y la carga de la planilla nunca pisa lo que se
            -- escribio en la pantalla.
            ALTER TABLE mantenimiento.service
                ADD COLUMN IF NOT EXISTS hoja text;
            ALTER TABLE mantenimiento.service
                ADD COLUMN IF NOT EXISTS orden integer;
            CREATE UNIQUE INDEX IF NOT EXISTS service_hoja_orden_idx
                ON mantenimiento.service (hoja, orden)
                WHERE hoja IS NOT NULL;

            -- Archivos subidos desde el programa: la planilla de planta, el
            -- manual de una maquina, una foto. Guardados en la base y no en
            -- una carpeta del server: asi sobreviven a una actualizacion, que
            -- reemplaza la carpeta entera.
            CREATE TABLE IF NOT EXISTS mantenimiento.archivo (
                id           serial PRIMARY KEY,
                id_maquina   integer,
                nombre       text NOT NULL,
                descripcion  text,
                contenido    bytea NOT NULL,
                tamano       integer NOT NULL,
                subido_por   text,
                creado_en    timestamptz NOT NULL DEFAULT now()
            );

            -- Cómo se pone la máquina para tejer cada tela. Sale de la planilla
            -- CONTROL DE AJUSTE, que tiene una hoja por máquina y viene desde
            -- 2021. No es mantenimiento: es la receta de la puesta a punto, y
            -- es lo primero que busca el mecánico cuando vuelve una tela.
            --
            -- Las poleas y los hilos van juntos en un solo campo de texto: en
            -- la planilla son 3 o 4 columnas que dicen todas "Polea" y no
            -- significan siempre lo mismo. Partirlas en columnas fijas sería
            -- inventar una estructura que el papel no tiene.
            CREATE TABLE IF NOT EXISTS mantenimiento.ajuste (
                id             serial PRIMARY KEY,
                id_maquina     integer NOT NULL,
                maquina_nombre text,
                fecha          date,
                tipo_maquina   text,
                cilindro       text,
                poleas         text,
                ajuste_agujas  text,
                estiraje       text,
                tela           text,
                hilos          text,
                gramaje_crudo  numeric(10,2),
                malla_manual   text,
                malla          text,
                rendimiento    numeric(10,4),
                kg_m           numeric(10,4),
                hoja           text NOT NULL,
                orden          integer NOT NULL,
                creado_en      timestamptz NOT NULL DEFAULT now()
            );

            -- Un ajuste cargado desde la pantalla no salió de ninguna planilla:
            -- no tiene hoja ni fila. Con el NOT NULL puesto, la pantalla de
            -- «Cargar un ajuste nuevo» no podía guardar NUNCA — cada intento
            -- terminaba en un error de Postgres en la cara del mecánico.
            ALTER TABLE mantenimiento.ajuste ALTER COLUMN hoja DROP NOT NULL;

            ALTER TABLE mantenimiento.ajuste ALTER COLUMN orden DROP NOT NULL;

            -- (hoja, orden) = de qué hoja del Excel salió y en qué fila estaba.
            -- Es lo que hace que volver a cargar la misma planilla ACTUALICE en
            -- vez de duplicar: sin esto, cargarla dos veces deja 2.388 ajustes
            -- y ninguna forma de saber cuáles sobran. Los cargados a mano van
            -- con las dos en nulo y en Postgres dos nulos no chocan, así que no
            -- se pisan entre ellos ni con los de la planilla.
            CREATE UNIQUE INDEX IF NOT EXISTS ajuste_hoja_orden_idx
                ON mantenimiento.ajuste (hoja, orden);
            CREATE INDEX IF NOT EXISTS ajuste_maquina_idx
                ON mantenimiento.ajuste (id_maquina, fecha DESC NULLS LAST, orden);

            -- Qué aguja lleva cada máquina. Una fila por máquina.
            CREATE TABLE IF NOT EXISTS mantenimiento.aguja_maquina (
                id_maquina   integer PRIMARY KEY,
                descripcion  text,
                cilindro     text,
                plato        text,
                platinas     text,
                nota         text,
                editado_en   timestamptz NOT NULL DEFAULT now()
            );

            -- Las levas. No cuelgan de UNA máquina: la planilla las agrupa por
            -- modelo ("MAYER (2-3-9-10)"), porque la misma leva sirve para
            -- varias. Se guarda el texto tal cual, sin repartirlo.
            CREATE TABLE IF NOT EXISTS mantenimiento.leva (
                id            serial PRIMARY KEY,
                maquinas      text NOT NULL,
                codigo        text NOT NULL,
                cantidad      integer,
                ubicacion     text,
                accionamiento text,
                creado_en     timestamptz NOT NULL DEFAULT now()
            );
            CREATE UNIQUE INDEX IF NOT EXISTS leva_clave_idx
                ON mantenimiento.leva
                   (maquinas, codigo, coalesce(accionamiento, ''));

            -- Cuántas levas lleva cada TELA. Es la segunda tabla de la hoja
            -- «INVENTARIO LEVAS», pegada a la derecha: cuarenta renglones que
            -- no entraban a ningún lado y se perdían enteros. No es el
            -- inventario —eso es `leva`—: es cómo se arma la máquina para tejer
            -- esa tela, cuántas levas de trabajo, de retenido y de anulación.
            CREATE TABLE IF NOT EXISTS mantenimiento.leva_tela (
                id            serial PRIMARY KEY,
                marca         text,
                diametro      text,
                alimentadores text,
                tela          text,
                trabajo       text,
                retenido      text,
                anulacion     text,
                nota          text,
                creado_en     timestamptz NOT NULL DEFAULT now()
            );
            CREATE UNIQUE INDEX IF NOT EXISTS leva_tela_idx
                ON mantenimiento.leva_tela
                   (coalesce(marca, ''), coalesce(diametro, ''),
                    coalesce(alimentadores, ''), coalesce(tela, ''),
                    coalesce(trabajo, ''));

            -- Qué aguja lleva cada MODELO de máquina, con la marca. Es la hoja
            -- «CODIGO DE AGUJAS»: la misma información que `aguja_maquina`
            -- pero agrupada, y con un dato que la otra no tiene — de quién es
            -- la aguja (Groz Beckert) y de quién la platina (Kern Liebers).
            CREATE TABLE IF NOT EXISTS mantenimiento.aguja_modelo (
                id             serial PRIMARY KEY,
                modelo         text NOT NULL,
                marca_aguja    text,
                codigos        text,
                donde          text,
                platinas       text,
                marca_platina  text,
                nota           text,
                creado_en      timestamptz NOT NULL DEFAULT now()
            );
            CREATE UNIQUE INDEX IF NOT EXISTS aguja_modelo_idx
                ON mantenimiento.aguja_modelo (modelo);

            -- Las bandas, por modelo y diámetro.
            CREATE TABLE IF NOT EXISTS mantenimiento.banda (
                id                serial PRIMARY KEY,
                maquinas          text NOT NULL,
                cantidad_maquinas integer,
                diametro          numeric(6,2),
                media             text,
                tres_cuartos      text,
                lycra             text,
                creado_en         timestamptz NOT NULL DEFAULT now()
            );

            -- Memminger o de motor. Son dos repuestos distintos y la planilla
            -- los tiene en dos tablas al lado de la otra; sin esta columna se
            -- mezclarian y una medida de una taparia a la otra.
            ALTER TABLE mantenimiento.banda
                ADD COLUMN IF NOT EXISTS clase text NOT NULL DEFAULT 'memminger';
            ALTER TABLE mantenimiento.banda
                ADD COLUMN IF NOT EXISTS banda text;
            ALTER TABLE mantenimiento.banda
                ADD COLUMN IF NOT EXISTS cobrador text;
            ALTER TABLE mantenimiento.banda
                ADD COLUMN IF NOT EXISTS nota text;
            DROP INDEX IF EXISTS mantenimiento.banda_clave_idx;
            CREATE UNIQUE INDEX IF NOT EXISTS banda_clase_clave_idx
                ON mantenimiento.banda (clase, maquinas, coalesce(diametro, -1));

            -- Cuántas bandas hay de cada medida.
            CREATE TABLE IF NOT EXISTS mantenimiento.banda_stock (
                medida     numeric(6,2) PRIMARY KEY,
                cantidad   integer,
                editado_en timestamptz NOT NULL DEFAULT now()
            );

            -- Cuánto tiene que dar cada máquina en 12 horas. Es un cálculo de
            -- la planilla, NO lo que la máquina tejió: eso lo mide Asinfo.
            CREATE TABLE IF NOT EXISTS mantenimiento.eficiencia (
                id_maquina    integer PRIMARY KEY,
                rpm           numeric(8,2),
                sistemas      text,
                diametro      numeric(6,2),
                alimentadores integer,
                tamano_rollo  numeric(10,2),
                minutos_rollo numeric(10,2),
                rollos_dia    numeric(8,2),
                kg_dia        numeric(10,2),
                editado_en    timestamptz NOT NULL DEFAULT now()
            );

            -- La planilla calcula dos turnos: 12 horas y 24 horas.
            ALTER TABLE mantenimiento.eficiencia
                ADD COLUMN IF NOT EXISTS rollos_dia_24 numeric(8,2);
            ALTER TABLE mantenimiento.eficiencia
                ADD COLUMN IF NOT EXISTS kg_dia_24 numeric(10,2);

            -- Cuánto hilo lleva cada tela. Una fila por (tela, hilo).
            CREATE TABLE IF NOT EXISTS mantenimiento.consumo_hilo (
                id           serial PRIMARY KEY,
                tela         text NOT NULL,
                hilo         text NOT NULL,
                codigo_hilo  text,
                rendimiento  numeric(8,4),
                creado_en    timestamptz NOT NULL DEFAULT now()
            );
            CREATE UNIQUE INDEX IF NOT EXISTS consumo_hilo_clave_idx
                ON mantenimiento.consumo_hilo (tela, hilo);

            -- El peso medido de la tela que salió de cada máquina.
            CREATE TABLE IF NOT EXISTS mantenimiento.gramaje (
                id          serial PRIMARY KEY,
                id_maquina  integer NOT NULL,
                fecha       date,
                tela        text,
                hilos       text,
                peso        numeric(10,2),
                orden       integer NOT NULL,
                creado_en   timestamptz NOT NULL DEFAULT now()
            );
            CREATE UNIQUE INDEX IF NOT EXISTS gramaje_orden_idx
                ON mantenimiento.gramaje (orden);
"""


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
def registrar_service(id_maquina, maquina_nombre, tipo_id, fecha, hecho_por, nota,
                      repuestos=None, horas=None) -> None:
    _ejecutar(
        """INSERT INTO mantenimiento.service
               (id_maquina, maquina_nombre, tipo_id, fecha, hecho_por, nota,
                repuestos, horas)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (id_maquina, maquina_nombre, tipo_id, fecha, hecho_por.strip(),
         (nota or "").strip() or None, (repuestos or "").strip() or None, horas),
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


# El sello con el que entró la carga vieja, que sólo guardaba la ÚLTIMA fecha
# de cada tipo (66 filas de las 1.366 que tiene la planilla). Cuando entra el
# historial completo hay que sacarlas: son las mismas fechas, y dejarlas
# duplicaría cada máquina dos veces en su propio historial.
SELLO_CARGA_VIEJA = "Carga inicial de la planilla"


def guardar_historial(filas: list[dict]) -> dict:
    """Guarda el historial COMPLETO de la planilla de planta, en una transacción.

    `filas` = [{id_maquina, maquina_nombre, tipo_id, fecha, hecho_por, nota,
                repuestos, horas, hoja, orden}, ...]

    Volver a cargar la misma planilla ACTUALIZA por (hoja, fila) en vez de
    duplicar: la planilla sigue viva en planta y se vuelve a subir corregida.
    """
    if not filas:
        return {"mantenimientos": 0, "borrados": 0}
    campos = ("id_maquina", "maquina_nombre", "tipo_id", "fecha", "hecho_por",
              "nota", "repuestos", "horas", "hoja", "orden")
    columnas = ", ".join(campos)
    marcas = ", ".join(["%s"] * len(campos))
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in campos
                        if c not in ("hoja", "orden"))
    with _conn() as con, con.cursor() as cur:
        cur.execute(
            "DELETE FROM mantenimiento.service WHERE hoja IS NULL AND hecho_por = %s",
            (SELLO_CARGA_VIEJA,),
        )
        borrados = cur.rowcount or 0
        # Las hojas que vuelven a venir se cargan de cero. Antes sólo se
        # actualizaba fila por fila: una fila borrada del Excel se quedaba para
        # siempre en el programa, y una fila que pasó a ser DOS mantenimientos
        # dejaba al viejo al lado de los nuevos.
        hojas = sorted({f["hoja"] for f in filas if f.get("hoja")})
        if hojas:
            cur.execute(
                "DELETE FROM mantenimiento.service WHERE hoja = ANY(%s)", (hojas,)
            )
        # El `WHERE hoja IS NOT NULL` no es de más: el índice único es PARCIAL
        # (sólo mira las filas que vinieron de una planilla, para no molestar a
        # las cargadas a mano). Postgres exige que el ON CONFLICT repita esa
        # condición; sin ella no reconoce el índice y responde "there is no
        # unique or exclusion constraint matching the ON CONFLICT specification".
        cur.executemany(
            f"""INSERT INTO mantenimiento.service ({columnas})
                VALUES ({marcas})
                ON CONFLICT (hoja, orden) WHERE hoja IS NOT NULL
                DO UPDATE SET {updates}""",
            [tuple(f.get(c) for c in campos) for f in filas],
        )
        con.commit()
    return {"mantenimientos": len(filas), "borrados": borrados}


def cuantos_de_la_carga_vieja() -> int:
    """Cuántos mantenimientos quedaron de la carga que sólo traía la última
    fecha. Se muestra en la pantalla de revisión: si se van a reemplazar, hay
    que decirlo antes, no después."""
    filas = _todos(
        "SELECT COUNT(*) n FROM mantenimiento.service WHERE hoja IS NULL AND hecho_por = %s",
        (SELLO_CARGA_VIEJA,),
    )
    return filas[0]["n"] if filas else 0


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


# Los encargados de mantenimiento de tejeduria. Estan fijos porque son ellos:
# escribir el nombre a mano cada vez termina en "roberto", "Roberto " y
# "RObert", y despues no se puede contar quien hizo que.
ENCARGADOS = ("Roberto", "Darlin", "Humberto")


# Los sellos que le pone el programa a lo que entra por el Excel. Son máquina,
# no personas: no tienen que aparecer en «quién lo hizo» ofreciéndose para
# firmar un mantenimiento nuevo.
SELLOS = ("Carga inicial de la planilla", "Planilla de planta", "Carga del Excel",
          "Cargado desde el Excel")


def responsables() -> list[str]:
    """Para el campo 'quién lo hizo': los encargados primero, y después
    cualquier otro nombre que ya se haya usado."""
    filas = _todos(
        """SELECT hecho_por, COUNT(*) n FROM mantenimiento.service
            GROUP BY hecho_por ORDER BY n DESC LIMIT 30"""
    )
    usados = [f["hecho_por"] for f in filas]
    otros = [u for u in usados if u not in ENCARGADOS and u not in SELLOS]
    return list(ENCARGADOS) + otros


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


# --- Archivos --------------------------------------------------------------
def guardar_archivo(nombre, contenido, id_maquina=None, descripcion=None,
                    subido_por=None) -> int:
    with _conn() as con, con.cursor() as cur:
        cur.execute(
            """INSERT INTO mantenimiento.archivo
                   (id_maquina, nombre, descripcion, contenido, tamano, subido_por)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (id_maquina, nombre.strip(), (descripcion or "").strip() or None,
             psycopg2.Binary(contenido), len(contenido),
             (subido_por or "").strip() or None),
        )
        nuevo = cur.fetchone()[0]
        con.commit()
    return nuevo


def archivos(id_maquina=None) -> list[dict]:
    """La lista, SIN el contenido: traer los bytes de todos para pintar una
    tabla es la forma mas facil de tirar abajo la pantalla."""
    if id_maquina is None:
        return _todos(
            """SELECT id, id_maquina, nombre, descripcion, tamano, subido_por, creado_en
                 FROM mantenimiento.archivo ORDER BY creado_en DESC""")
    return _todos(
        """SELECT id, id_maquina, nombre, descripcion, tamano, subido_por, creado_en
             FROM mantenimiento.archivo WHERE id_maquina = %s
            ORDER BY creado_en DESC""", (id_maquina,))


def archivo(id_archivo: int) -> dict | None:
    filas = _todos("SELECT * FROM mantenimiento.archivo WHERE id = %s", (id_archivo,))
    return filas[0] if filas else None


def borrar_archivo(id_archivo: int) -> None:
    _ejecutar("DELETE FROM mantenimiento.archivo WHERE id = %s", (id_archivo,))


# --------------------------------------------------------------------------
# Ajustes: cómo se pone la máquina para tejer cada tela
# --------------------------------------------------------------------------
CAMPOS_AJUSTE = ("id_maquina", "maquina_nombre", "fecha", "tipo_maquina",
                 "cilindro", "poleas", "ajuste_agujas", "estiraje", "tela",
                 "hilos", "gramaje_crudo", "malla_manual", "malla",
                 "rendimiento", "kg_m", "hoja", "orden")


def ajustes(id_maquina: int | None = None, tela: str | None = None,
            limite: int = 400) -> list[dict]:
    """Los ajustes, del más nuevo al más viejo.

    Muchas filas de la planilla no tienen fecha: se anotó el ajuste y no el
    día. Van al final, no se descartan — el ajuste sirve igual.
    """
    donde, args = [], []
    if id_maquina is not None:
        donde.append("id_maquina = %s")
        args.append(id_maquina)
    if tela:
        donde.append("tela ILIKE %s")
        args.append(f"%{tela.strip()}%")
    filtro = ("WHERE " + " AND ".join(donde)) if donde else ""
    args.append(limite)
    return _todos(
        f"""SELECT * FROM mantenimiento.ajuste {filtro}
             ORDER BY fecha DESC NULLS LAST, id_maquina, orden
             LIMIT %s""",
        tuple(args),
    )


def telas() -> list[dict]:
    """Las telas que aparecen en los ajustes, con cuántas veces y en cuántas
    máquinas. Es el índice para entrar buscando por tela."""
    return _todos(
        """SELECT tela, COUNT(*) AS veces,
                  COUNT(DISTINCT id_maquina) AS maquinas,
                  MAX(fecha) AS ultima
             FROM mantenimiento.ajuste
            WHERE tela IS NOT NULL
            GROUP BY tela
            ORDER BY veces DESC, tela"""
    )


def resumen_ajustes() -> dict:
    filas = _todos(
        """SELECT COUNT(*) AS filas,
                  COUNT(DISTINCT id_maquina) AS maquinas,
                  COUNT(fecha) AS con_fecha,
                  MIN(fecha) AS desde, MAX(fecha) AS hasta
             FROM mantenimiento.ajuste"""
    )
    return filas[0] if filas else {}


def crear_ajuste(datos: dict) -> None:
    """Un ajuste cargado a mano, desde la pantalla.

    Va sin `hoja` ni `orden`: no salió de ninguna planilla. El índice único es
    de (hoja, orden) y en Postgres dos NULL no chocan, así que se pueden cargar
    todos los que hagan falta sin pisarse.
    """
    campos = tuple(c for c in CAMPOS_AJUSTE if c not in ("hoja", "orden"))
    columnas = ", ".join(campos)
    marcas = ", ".join(["%s"] * len(campos))
    _ejecutar(
        f"INSERT INTO mantenimiento.ajuste ({columnas}) VALUES ({marcas})",
        tuple(datos.get(c) for c in campos),
    )


def guardar_ajustes(filas: list[dict]) -> int:
    """Guarda los ajustes en UNA transacción.

    Si la planilla ya se cargó antes, la misma (hoja, fila) se actualiza en vez
    de duplicarse: la planilla de planta se sigue usando y se vuelve a subir
    corregida, y una carga que duplica es peor que no cargar.
    """
    if not filas:
        return 0
    columnas = ", ".join(CAMPOS_AJUSTE)
    marcas = ", ".join(["%s"] * len(CAMPOS_AJUSTE))
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in CAMPOS_AJUSTE
                        if c not in ("hoja", "orden"))
    with _conn() as con, con.cursor() as cur:
        cur.executemany(
            f"""INSERT INTO mantenimiento.ajuste ({columnas})
                VALUES ({marcas})
                ON CONFLICT (hoja, orden) DO UPDATE SET {updates}""",
            [tuple(f.get(c) for c in CAMPOS_AJUSTE) for f in filas],
        )
        con.commit()
    return len(filas)


# --------------------------------------------------------------------------
# Repuestos: agujas, levas, bandas
# --------------------------------------------------------------------------
def agujas() -> dict[int, dict]:
    return {f["id_maquina"]: f
            for f in _todos("SELECT * FROM mantenimiento.aguja_maquina")}


def levas() -> list[dict]:
    return _todos(
        "SELECT * FROM mantenimiento.leva ORDER BY maquinas, accionamiento, codigo")


def bandas(clase: str = "memminger") -> list[dict]:
    return _todos(
        "SELECT * FROM mantenimiento.banda WHERE clase = %s ORDER BY maquinas, diametro",
        (clase,))


def agujas_por_modelo() -> list[dict]:
    return _todos("SELECT * FROM mantenimiento.aguja_modelo ORDER BY id")


def banda_stock() -> list[dict]:
    return _todos(
        "SELECT * FROM mantenimiento.banda_stock ORDER BY medida")


def eficiencias() -> dict[int, dict]:
    return {f["id_maquina"]: f
            for f in _todos("SELECT * FROM mantenimiento.eficiencia")}


def consumo_hilo() -> list[dict]:
    return _todos(
        """SELECT * FROM mantenimiento.consumo_hilo
            ORDER BY tela, rendimiento DESC NULLS LAST""")


def gramajes(id_maquina: int | None = None) -> list[dict]:
    if id_maquina is None:
        return _todos(
            "SELECT * FROM mantenimiento.gramaje ORDER BY id_maquina, orden")
    return _todos(
        "SELECT * FROM mantenimiento.gramaje WHERE id_maquina=%s ORDER BY orden",
        (id_maquina,))


def _lote(cur, tabla: str, campos: tuple, conflicto: str, filas: list[dict]):
    """Un INSERT ... ON CONFLICT DO UPDATE en lote, para todas las tablas de
    repuestos: son todas iguales salvo el nombre y la clave."""
    if not filas:
        return
    columnas = ", ".join(campos)
    marcas = ", ".join(["%s"] * len(campos))
    claves = {c.strip() for c in conflicto.split(",")}
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in campos if c not in claves)
    cur.executemany(
        f"""INSERT INTO {tabla} ({columnas}) VALUES ({marcas})
            ON CONFLICT ({conflicto}) DO UPDATE SET {updates}""",
        [tuple(f.get(c) for c in campos) for f in filas],
    )


CAMPOS_AGUJA = ("id_maquina", "descripcion", "cilindro", "plato", "platinas", "nota")
CAMPOS_LEVA = ("maquinas", "codigo", "cantidad", "ubicacion", "accionamiento")
CAMPOS_LEVA_TELA = ("marca", "diametro", "alimentadores", "tela", "trabajo",
                    "retenido", "anulacion", "nota")
CAMPOS_BANDA = ("clase", "maquinas", "cantidad_maquinas", "diametro", "media",
                "tres_cuartos", "lycra", "banda", "cobrador", "nota")
CAMPOS_AGUJA_MODELO = ("modelo", "marca_aguja", "codigos", "donde", "platinas",
                       "marca_platina", "nota")
CAMPOS_EFICIENCIA = ("id_maquina", "rpm", "sistemas", "diametro", "alimentadores",
                     "tamano_rollo", "minutos_rollo", "rollos_dia", "kg_dia",
                     "rollos_dia_24", "kg_dia_24")
CAMPOS_CONSUMO = ("tela", "hilo", "codigo_hilo", "rendimiento")
CAMPOS_GRAMAJE = ("id_maquina", "fecha", "tela", "hilos", "peso", "orden")


# --- Editar los repuestos a mano -------------------------------------------
# Los seis cuadros de la pantalla de Repuestos, uno al lado del otro: dónde
# vive cada uno, con qué se reconoce una fila y qué columnas se guardan. La
# pantalla se arma con esto, así los seis se editan igual y no hay seis
# códigos parecidos que arreglar de a uno.
#
# `fijos` son las columnas que las decide la pestaña, no quien carga: las dos
# clases de banda viven en la misma tabla y sin esto una taparía a la otra.
CUADROS: dict[str, dict] = {
    "agujas": {"tabla": "mantenimiento.aguja_maquina", "clave": "id_maquina",
               "campos": CAMPOS_AGUJA, "fijos": {}, "orden": "id_maquina"},
    "modelos": {"tabla": "mantenimiento.aguja_modelo", "clave": "id",
                "campos": CAMPOS_AGUJA_MODELO, "fijos": {}, "orden": "modelo"},
    "levas": {"tabla": "mantenimiento.leva", "clave": "id",
              "campos": CAMPOS_LEVA, "fijos": {},
              "orden": "maquinas, accionamiento, codigo"},
    "levas_tela": {"tabla": "mantenimiento.leva_tela", "clave": "id",
                   "campos": CAMPOS_LEVA_TELA, "fijos": {},
                   "orden": "marca, diametro, tela"},
    "bandas": {"tabla": "mantenimiento.banda", "clave": "id",
               "campos": tuple(c for c in CAMPOS_BANDA if c != "clase"),
               "fijos": {"clase": "memminger"}, "orden": "maquinas, diametro"},
    "motor": {"tabla": "mantenimiento.banda", "clave": "id",
              "campos": tuple(c for c in CAMPOS_BANDA if c != "clase"),
              "fijos": {"clase": "motor"}, "orden": "maquinas, diametro"},
    "stock": {"tabla": "mantenimiento.banda_stock", "clave": "medida",
              "campos": ("medida", "cantidad"), "fijos": {}, "orden": "medida"},
}


def filas_de(cuadro: str) -> list[dict]:
    """Las filas de un cuadro de Repuestos, como se muestran."""
    c = CUADROS[cuadro]
    donde = ""
    if c["fijos"]:
        donde = "WHERE " + " AND ".join(f"{k} = %s" for k in c["fijos"])
    return _todos(f"SELECT * FROM {c['tabla']} {donde} ORDER BY {c['orden']}",
                  tuple(c["fijos"].values()))


def guardar_repuesto(cuadro: str, clave, datos: dict) -> None:
    """Guarda UNA fila. Si no viene la clave, la agrega.

    La clave de una fila que ya existe no se pisa: cambiarla no es corregir un
    dato, es otra fila. Para eso está agregar.
    """
    c = CUADROS[cuadro]
    valores = {k: (v if v not in ("",) else None)
               for k, v in datos.items() if k in c["campos"]}
    valores.update(c["fijos"])
    natural = c["clave"] in c["campos"]

    if clave in (None, ""):
        if not any(v is not None for k, v in valores.items() if k not in c["fijos"]):
            raise ValueError("La fila está vacía: no hay nada para agregar.")
        if natural and valores.get(c["clave"]) is None:
            raise ValueError("Falta la primera columna, que es la que identifica la fila.")
        columnas = list(valores)
        marcas = ", ".join(["%s"] * len(columnas))
        conflicto = ""
        if natural:
            # Cargar dos veces la misma máquina no es un error de quien carga:
            # es corregir lo que había.
            updates = ", ".join(f"{k}=EXCLUDED.{k}" for k in columnas if k != c["clave"])
            conflicto = f"ON CONFLICT ({c['clave']}) DO UPDATE SET {updates}"
        _ejecutar(
            f"""INSERT INTO {c['tabla']} ({', '.join(columnas)})
                VALUES ({marcas}) {conflicto}""",
            tuple(valores[k] for k in columnas),
        )
        return

    cambios = {k: v for k, v in valores.items() if k != c["clave"]}
    if not cambios:
        return
    sets = ", ".join(f"{k} = %s" for k in cambios)
    _ejecutar(f"UPDATE {c['tabla']} SET {sets} WHERE {c['clave']} = %s",
              (*cambios.values(), clave))


def borrar_repuesto(cuadro: str, clave) -> None:
    c = CUADROS[cuadro]
    _ejecutar(f"DELETE FROM {c['tabla']} WHERE {c['clave']} = %s", (clave,))


CAMPOS_EFICIENCIA_EDIT = tuple(c for c in CAMPOS_EFICIENCIA if c != "id_maquina")


def guardar_eficiencia(id_maquina: int, datos: dict) -> None:
    """Cuánto debería dar esa máquina. Es el cálculo de la planilla, no lo que
    tejió: lo que tejió lo mide Asinfo y no se toca desde acá."""
    valores = [datos.get(c) for c in CAMPOS_EFICIENCIA_EDIT]
    columnas = ", ".join(CAMPOS_EFICIENCIA_EDIT)
    marcas = ", ".join(["%s"] * len(CAMPOS_EFICIENCIA_EDIT))
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in CAMPOS_EFICIENCIA_EDIT)
    _ejecutar(
        f"""INSERT INTO mantenimiento.eficiencia (id_maquina, {columnas})
            VALUES (%s, {marcas})
            ON CONFLICT (id_maquina) DO UPDATE
               SET {updates}, editado_en = now()""",
        (id_maquina, *valores),
    )


def guardar_planilla_ajuste(datos: dict) -> dict:
    """Guarda TODA la planilla de control de ajuste en una transacción.

    O entra todo o no entra nada. Media planilla cargada es peor que ninguna:
    nadie sabría qué bloque quedó viejo.
    """
    with _conn() as con, con.cursor() as cur:
        if datos.get("ajustes"):
            columnas = ", ".join(CAMPOS_AJUSTE)
            marcas = ", ".join(["%s"] * len(CAMPOS_AJUSTE))
            updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in CAMPOS_AJUSTE
                                if c not in ("hoja", "orden"))
            cur.executemany(
                f"""INSERT INTO mantenimiento.ajuste ({columnas})
                    VALUES ({marcas})
                    ON CONFLICT (hoja, orden) DO UPDATE SET {updates}""",
                [tuple(f.get(c) for c in CAMPOS_AJUSTE) for f in datos["ajustes"]],
            )
        _lote(cur, "mantenimiento.aguja_maquina", CAMPOS_AGUJA,
              "id_maquina", datos.get("agujas") or [])
        _lote(cur, "mantenimiento.leva", CAMPOS_LEVA,
              "maquinas, codigo, coalesce(accionamiento, '')",
              datos.get("levas") or [])
        _lote(cur, "mantenimiento.leva_tela", CAMPOS_LEVA_TELA,
              "coalesce(marca, ''), coalesce(diametro, ''), "
              "coalesce(alimentadores, ''), coalesce(tela, ''), "
              "coalesce(trabajo, '')",
              datos.get("levas_tela") or [])
        _lote(cur, "mantenimiento.aguja_modelo", CAMPOS_AGUJA_MODELO,
              "modelo", datos.get("agujas_modelo") or [])
        _lote(cur, "mantenimiento.banda", CAMPOS_BANDA,
              "clase, maquinas, coalesce(diametro, -1)", datos.get("bandas") or [])
        _lote(cur, "mantenimiento.banda_stock", ("medida", "cantidad"),
              "medida", datos.get("banda_stock") or [])
        _lote(cur, "mantenimiento.eficiencia", CAMPOS_EFICIENCIA,
              "id_maquina", datos.get("eficiencia") or [])
        _lote(cur, "mantenimiento.consumo_hilo", CAMPOS_CONSUMO,
              "tela, hilo", datos.get("consumo_hilo") or [])
        _lote(cur, "mantenimiento.gramaje", CAMPOS_GRAMAJE,
              "orden", datos.get("gramajes") or [])
        con.commit()
    return {k: len(v or []) for k, v in datos.items()}
