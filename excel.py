"""Leer el Excel que ya usan en planta, tal como viene.

No hay plantilla. Nadie va a recopiar 43 filas a otro formato: el programa se
adapta al archivo, no al revés.

Tres pasos, y el del medio es el importante:

  1. `leer` abre el archivo y encuentra sola la fila de títulos.
  2. `detectar` propone qué columna es cada cosa. **Es una propuesta**: la
     pantalla la muestra y se puede corregir antes de guardar.
  3. `armar` convierte las filas en datos, y lo que no entiende lo devuelve
     aparte con el motivo. Nunca adivina: una fila dudosa se descarta y se
     muestra, no se carga a medias.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime

from openpyxl import load_workbook

# Cuántas filas miramos buscando los títulos antes de rendirnos.
_FILAS_CABECERA = 15


def _limpio(texto) -> str:
    """Minúsculas, sin tildes, sin puntuación. Para comparar títulos."""
    s = str(texto or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def hojas(ruta: str) -> list[str]:
    wb = load_workbook(ruta, read_only=True, data_only=True)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def leer(ruta: str, hoja: str | None = None) -> tuple[list[str], list[list]]:
    """Devuelve (títulos, filas). Encuentra la fila de títulos sola.

    Muchos Excel de planta tienen un logo o un título arriba. La fila de
    títulos es la primera con al menos dos celdas con texto.
    """
    wb = load_workbook(ruta, read_only=True, data_only=True)
    try:
        ws = wb[hoja] if hoja and hoja in wb.sheetnames else wb[wb.sheetnames[0]]
        crudas = [list(f) for f in ws.iter_rows(values_only=True)]
    finally:
        wb.close()

    inicio = 0
    for i, fila in enumerate(crudas[:_FILAS_CABECERA]):
        textos = [c for c in fila if isinstance(c, str) and c.strip()]
        if len(textos) >= 2:
            inicio = i
            break

    titulos = [str(c).strip() if c is not None else "" for c in crudas[inicio]] if crudas else []
    filas = [f for f in crudas[inicio + 1:] if any(c not in (None, "") for c in f)]
    ancho = len(titulos)
    filas = [list(f) + [None] * (ancho - len(f)) for f in filas]
    return titulos, filas


# --------------------------------------------------------------------------
# Qué columna es cada cosa
# --------------------------------------------------------------------------
PISTAS = {
    "numero": ("maquina", "maq", "mq", "tejedora", "numero", "nro", "n"),
    "marca": ("marca",),
    "modelo": ("modelo",),
    "galga": ("galga", "gg"),
    "diametro": ("diametro", "diam"),
    "alimentadores": ("alimentador", "caida", "caidas", "sistemas"),
    "agujas": ("cantidad de agujas", "total agujas", "agujas totales", "agujas"),
    "anio": ("anio", "ano", "year", "fabricacion"),
    "nota": ("nota", "observacion", "observaciones", "comentario"),
}

_FECHA = ("fecha", "ultim", "cuando")
_KG = ("kg", "kilo", "cada")


def detectar(titulos: list[str], tipos: list[dict]) -> dict[str, int]:
    """Propone qué columna es cada cosa. Devuelve {campo: índice de columna}."""
    limpios = [_limpio(t) for t in titulos]
    mapa: dict[str, int] = {}
    usadas: set[int] = set()

    def tomar(campo, prueba):
        if campo in mapa:
            return
        for i, t in enumerate(limpios):
            if i in usadas or not t:
                continue
            if prueba(t):
                mapa[campo] = i
                usadas.add(i)
                return

    # Primero los tipos de mantenimiento: sus títulos llevan el nombre del tipo
    # ("último cambio de agujas"). Si no los reservamos, la palabra "agujas" se
    # la lleva la columna de la ficha y la fecha queda sin mapear.
    for tipo in tipos:
        palabras = [p for p in _limpio(tipo["nombre"]).split() if len(p) > 3]
        if not palabras:
            continue

        def del_tipo(t, palabras=palabras):
            return any(p in t for p in palabras)

        tomar(f"fecha_{tipo['id']}", lambda t, d=del_tipo: d(t) and any(k in t for k in _FECHA))
        tomar(f"kg_{tipo['id']}", lambda t, d=del_tipo: d(t) and any(k in t for k in _KG))

    for campo, pistas in PISTAS.items():
        tomar(campo, lambda t, p=pistas: any(t == x or t.startswith(x + " ") or f" {x} " in f" {t} " for x in p))

    return mapa


# --------------------------------------------------------------------------
# Convertir las celdas en datos
# --------------------------------------------------------------------------
def a_numero(valor) -> int | None:
    if valor is None or valor == "":
        return None
    if isinstance(valor, (int, float)):
        return int(valor)
    encontrados = re.findall(r"\d+", str(valor).replace(".", "").replace(",", ""))
    return int(encontrados[-1]) if encontrados else None


def a_decimal(valor) -> float | None:
    if valor is None or valor == "":
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    s = str(valor).strip().replace(",", ".")
    m = re.search(r"-?\d+(\.\d+)?", s)
    return float(m.group()) if m else None


def a_kilos(valor) -> float | None:
    """'30.000 kg' es treinta mil. El punto es de miles: en castellano nadie
    escribe 30.000 queriendo decir treinta."""
    if valor is None or valor == "":
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    s = re.sub(r"[^\d,.]", "", str(valor))
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def a_fecha(valor) -> date | None:
    if valor in (None, ""):
        return None
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    s = str(valor).strip()
    for formato in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, formato).date()
        except ValueError:
            continue
    return None


def _texto(valor) -> str | None:
    s = str(valor).strip() if valor not in (None, "") else ""
    s = re.sub(r"\s+", " ", s)
    # Las celdas de planta traen el año pegado al final ("PILOTELLI 2013").
    s = re.sub(r"\s+(19|20)\d{2}$", "", s)
    return s or None


def _resumen(fila) -> str:
    partes = [str(c).strip() for c in fila[:4] if c not in (None, "")]
    return " · ".join(partes)[:80] or "(vacía)"


def armar(titulos, filas, mapa, maquinas, tipos, hoy=None):
    """Convierte las filas del Excel en datos listos para guardar.

    Devuelve (listas, descartes). Cada descarte dice qué fila y por qué, y se
    muestra en pantalla antes de confirmar. Lo que no se entiende no entra.
    """
    hoy = hoy or date.today()
    por_numero = {m["numero"]: m for m in maquinas if m.get("numero") is not None}
    listas, descartes = [], []

    def celda(fila, campo):
        i = mapa.get(campo)
        if i is None or i >= len(fila):
            return None
        return fila[i]

    for n, fila in enumerate(filas, start=1):
        numero = a_numero(celda(fila, "numero"))
        if numero is None:
            descartes.append({"fila": n, "texto": _resumen(fila),
                              "motivo": "No tiene número de máquina"})
            continue
        maquina = por_numero.get(numero)
        if not maquina:
            descartes.append({"fila": n, "texto": _resumen(fila),
                              "motivo": f"La máquina {numero} no está en Asinfo"})
            continue

        item = {
            "fila": n,
            "maquina": maquina,
            "ficha": {
                "marca": _texto(celda(fila, "marca")),
                "modelo": _texto(celda(fila, "modelo")),
                "galga": a_numero(celda(fila, "galga")),
                "diametro": a_decimal(celda(fila, "diametro")),
                "alimentadores": a_numero(celda(fila, "alimentadores")),
                "agujas": a_numero(celda(fila, "agujas")),
                "anio": a_numero(celda(fila, "anio")),
                "nota": _texto(celda(fila, "nota")),
            },
            "mantenimientos": [],
            "avisos": [],
        }

        for tipo in tipos:
            fecha = a_fecha(celda(fila, f"fecha_{tipo['id']}"))
            kg = a_kilos(celda(fila, f"kg_{tipo['id']}"))
            if fecha is None and kg is None:
                continue
            if fecha and fecha > hoy:
                item["avisos"].append(f"{tipo['nombre']}: la fecha es futura, no se carga")
                fecha = None
            if fecha is None and kg is not None:
                item["avisos"].append(f"{tipo['nombre']}: sin fecha, empieza a contar hoy")
                fecha = hoy
            item["mantenimientos"].append({"tipo": tipo, "fecha": fecha, "cada_kg": kg})

        if not item["mantenimientos"] and not any(v for v in item["ficha"].values()):
            descartes.append({"fila": n, "texto": _resumen(fila), "motivo": "La fila está vacía"})
            continue
        listas.append(item)

    # Una máquina repetida en el Excel es un error de carga, no dos máquinas.
    veces: dict[int, int] = {}
    for item in listas:
        veces[item["maquina"]["id"]] = veces.get(item["maquina"]["id"], 0) + 1
    for item in listas:
        if veces[item["maquina"]["id"]] > 1:
            item["avisos"].append("Esta máquina aparece más de una vez en el Excel")

    return listas, descartes


# --------------------------------------------------------------------------
# La planilla que usan en planta: UNA HOJA POR MÁQUINA
# --------------------------------------------------------------------------
# Arriba la ficha de la máquina, abajo el historial desde 2018. Ninguna hoja
# está armada igual que la otra: los mismos datos aparecen en filas distintas,
# con nombres distintos ("AGUJAS" y "Cantidad de agujas"), y el diámetro y la
# galga a veces van juntos en una celda ("D 32   G24") y a veces separados
# bajo el título "DIAMETRO/ GG" ("32 / 24").
#
# Por eso no se lee por posición: se busca cada dato por su nombre, y lo que
# no aparece queda vacío. Vacío es correcto; inventado no.

# RELANIT y JVCE no van: son modelos, no marcas.
_MARCAS = ("MAYER", "TERROT", "FUKUHARA", "PAILUNG", "JIUNN LONG", "ORIZIO",
           "MONARCH", "UNITEX", "SANTONI", "WELLKNIT", "KEUMYONG")

# Con "aguja" adentro es cambio de agujas; todo lo demás es limpieza. Es la
# unica division que la planilla banca: el tipo no esta en una columna, esta
# escrito a mano adentro del texto de la actividad.
_ES_AGUJAS = re.compile(r"aguja", re.IGNORECASE)


def _celdas(filas, hasta):
    """El bloque de arriba como lista de (fila, columna, texto)."""
    salida = []
    for i, fila in enumerate(filas[:hasta]):
        for j, celda in enumerate(fila):
            if celda not in (None, ""):
                salida.append((i, j, celda))
    return salida


def _valor_de(bloque, etiquetas, filas, salteo=(), preferir="abajo"):
    """El valor que acompaña a una etiqueta: abajo o a la derecha.

    Se saltean las celdas que son otra etiqueta — si no, "Responsable:" se
    lleva el titulo de al lado en vez del nombre.
    """
    etiquetas = tuple(_limpio(e) for e in etiquetas)
    todas = tuple(_limpio(s) for s in salteo)
    for i, j, celda in bloque:
        if not isinstance(celda, str):
            continue
        texto = _limpio(celda)
        if not any(texto == e or texto.startswith(e) for e in etiquetas):
            continue
        abajo, derecha = [], []
        for di in (1, 2, 3):
            if i + di < len(filas) and j < len(filas[i + di]):
                abajo.append(filas[i + di][j])
        for dj in (1, 2, 3):
            if j + dj < len(filas[i]):
                derecha.append(filas[i][j + dj])
        candidatas = derecha + abajo if preferir == "derecha" else abajo + derecha
        for c in candidatas:
            if c in (None, ""):
                continue
            if isinstance(c, str):
                limpio = _limpio(c)
                if not limpio or limpio in todas or any(limpio.startswith(e) for e in etiquetas):
                    continue
            return c
    return None


def _fila_de_titulos(filas):
    """Dónde arranca el historial: la fila que dice «Fecha»."""
    for i, fila in enumerate(filas):
        for celda in fila:
            if isinstance(celda, str) and _limpio(celda) == "fecha":
                return i
    return None


def _ficha_de_hoja(filas, corte):
    bloque = _celdas(filas, corte)
    etiquetas = ("equipo", "modelo", "numero", "responsable", "tipo de aguja",
                 "tipo de agujas", "cantidad de agujas", "agujas",
                 "año de fabricacion", "diametro gg", "fecha", "observaciones",
                 "repuestos", "actividad realizada", "tipo de mantenimiento")

    texto_todo = " ".join(str(c) for _, _, c in bloque)

    # La marca esta al lado de "CIRCULAR": asi esta armada la planilla. Buscarla
    # suelta en todo el encabezado se equivoca, porque los nombres aparecen
    # tambien adentro de los codigos de aguja.
    # A la derecha, no abajo: abajo de "CIRCULAR" hay un codigo interno.
    marca = _valor_de(bloque, ("circular",), filas, etiquetas, preferir="derecha")
    if not isinstance(marca, str) or _limpio(marca) in ("", "circular"):
        marca = next((m for m in _MARCAS if m.lower() in texto_todo.lower()), None)
    modelo = _valor_de(bloque, ("modelo",), filas, etiquetas)
    serie = _valor_de(bloque, ("numero", "n de serie", "serie"), filas, etiquetas)
    responsable = _valor_de(bloque, ("responsable",), filas, etiquetas)
    anio = a_numero(_valor_de(bloque, ("año de fabricacion", "ano de fabricacion"),
                              filas, etiquetas))
    if anio is not None and not (1970 <= anio <= 2035):
        anio = None
    if anio is None:
        # Muchas hojas ponen el año suelto, sin etiqueta. Un numero entre 1990
        # y hoy es un año; la cantidad de agujas (2460, 3168) queda afuera del
        # rango, y el numero de serie tiene cinco cifras.
        for _, _, celda in bloque:
            if isinstance(celda, int) and 1990 <= celda <= date.today().year:
                anio = celda
                break

    # Diámetro y galga: juntos ("D 32  G24") o bajo "DIAMETRO/ GG" ("32 / 24").
    diametro = galga = None
    junto = re.search(r"D\s*(\d{2})\s*[/xX]?\s*G+\s*(\d{1,2})", texto_todo)
    if junto:
        diametro, galga = int(junto.group(1)), int(junto.group(2))
    else:
        crudo = _valor_de(bloque, ("diametro gg", "diametro"), filas, etiquetas)
        partido = re.findall(r"\d{1,2}", str(crudo or ""))
        if len(partido) >= 2:
            diametro, galga = int(partido[0]), int(partido[1])

    alimentadores = None
    ali = re.search(r"(\d{2,3})\s*ALIMENTADORES", texto_todo, re.IGNORECASE)
    if ali:
        alimentadores = int(ali.group(1))

    # Agujas: el numero grande que acompaña a "agujas".
    agujas = None
    for etiqueta in ("cantidad de agujas", "agujas", "n agujas"):
        agujas = a_numero(_valor_de(bloque, (etiqueta,), filas, etiquetas))
        if agujas and 500 <= agujas <= 20000:
            break
        agujas = None

    tipo_agujas = _valor_de(bloque, ("tipo de agujas", "tipo de aguja"), filas, etiquetas)

    return {
        "marca": _texto(marca),
        "modelo": _texto(modelo),
        "galga": galga,
        "diametro": diametro,
        "alimentadores": alimentadores,
        "agujas": agujas,
        "anio": anio,
        "serie": _texto(serie),
        "tipo_agujas": _texto(tipo_agujas),
        "nota": _texto(responsable) and f"Responsable: {_texto(responsable)}",
    }


def leer_por_maquina(ruta, maquinas, tipos, hoy=None):
    """Lee la planilla de planta: una hoja por máquina.

    De cada hoja saca la ficha y la ÚLTIMA fecha de cada tipo de mantenimiento.
    Devuelve (listas, descartes), igual que `armar`, para que la pantalla de
    revisión sea la misma.
    """
    hoy = hoy or date.today()
    por_numero = {m["numero"]: m for m in maquinas if m.get("numero") is not None}
    tipo_agujas = next((t for t in tipos if "aguja" in t["nombre"].lower()), None)
    tipo_limpieza = next((t for t in tipos if t is not tipo_agujas), None)

    wb = load_workbook(ruta, read_only=True, data_only=True)
    listas, descartes = [], []
    try:
        for nombre_hoja in wb.sheetnames:
            numero = a_numero(nombre_hoja)
            if numero is None:
                descartes.append({"fila": nombre_hoja, "texto": "",
                                  "motivo": "La hoja no dice de qué máquina es"})
                continue
            maquina = por_numero.get(numero)
            if not maquina:
                descartes.append({"fila": nombre_hoja, "texto": f"MQ {numero}",
                                  "motivo": f"La máquina {numero} no está en Asinfo"})
                continue

            filas = [list(f) for f in wb[nombre_hoja].iter_rows(values_only=True)]
            corte = _fila_de_titulos(filas)
            if corte is None:
                descartes.append({"fila": nombre_hoja, "texto": f"MQ {numero}",
                                  "motivo": "La hoja está vacía"})
                continue

            ficha = _ficha_de_hoja(filas, corte)

            # Última fecha de cada tipo. El tipo no está en una columna: está
            # escrito adentro del texto, así que se decide por lo que dice.
            ultimas: dict[int, date] = {}
            cuantas = 0
            for fila in filas[corte + 1:]:
                fecha = next((c.date() for c in fila if isinstance(c, datetime)), None)
                if not fecha or fecha > hoy:
                    continue
                cuantas += 1
                texto = " ".join(str(c) for c in fila if isinstance(c, str))
                tipo = tipo_agujas if (_ES_AGUJAS.search(texto) and tipo_agujas) else tipo_limpieza
                if not tipo:
                    continue
                if fecha > ultimas.get(tipo["id"], date.min):
                    ultimas[tipo["id"]] = fecha

            if not ultimas:
                descartes.append({"fila": nombre_hoja, "texto": f"MQ {numero}",
                                  "motivo": "La hoja no tiene ninguna fecha cargada"})
                continue

            item = {
                "fila": nombre_hoja,
                "maquina": maquina,
                "ficha": ficha,
                "mantenimientos": [],
                "avisos": [],
            }
            for tipo in tipos:
                fecha = ultimas.get(tipo["id"])
                if not fecha:
                    continue
                item["mantenimientos"].append(
                    {"tipo": tipo, "fecha": fecha, "cada_kg": None})
                meses = (hoy - fecha).days // 30
                if meses >= 4:
                    item["avisos"].append(
                        f"{tipo['nombre']}: el último anotado es de hace {meses} meses")
            item["avisos"].append(f"{cuantas} registros en la hoja")
            listas.append(item)
    finally:
        wb.close()

    return listas, descartes
