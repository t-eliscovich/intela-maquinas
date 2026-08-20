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
