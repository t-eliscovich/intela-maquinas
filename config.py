"""Configuración por variables de entorno. Nada hardcodeado, nada secreto en el repo."""
import os

# --- Base propia (las dos tablas del programa) -----------------------------
# Mismo cluster RDS que el resto. Schema propio `mantenimiento`.
DATABASE_URL = os.environ.get("MAQUINAS_DATABASE_URL", "")

# --- Puente a Asinfo (sólo lectura, vía Metabase) --------------------------
METABASE_URL = os.environ.get("METABASE_URL", "")
METABASE_USERNAME = os.environ.get("METABASE_USERNAME", "")
METABASE_PASSWORD = os.environ.get("METABASE_PASSWORD", "")
ASINFO_DB_ID = int(os.environ.get("ASINFO_DB_ID", "2"))

# --- Reglas de negocio -----------------------------------------------------
# Bodega de tela cruda. Todo lo que entra acá con una máquina asignada
# cuenta como producción de esa máquina.
BODEGA_TELA_CRUDA = int(os.environ.get("BODEGA_TELA_CRUDA", "52"))

# Prefijo de las máquinas que nos interesan en el catálogo de Asinfo.
PREFIJO_MAQUINAS = os.environ.get("PREFIJO_MAQUINAS", "TEJEDURIA")

# La producción anterior a esta fecha no se lee nunca (protege la query).
FECHA_PISO = os.environ.get("FECHA_PISO", "2022-07-01")

# --- App -------------------------------------------------------------------
SECRET_KEY = os.environ.get("MAQUINAS_SECRET_KEY", "cambiar-esto-en-produccion")
PASSWORD = os.environ.get("MAQUINAS_PASSWORD", "")  # vacío = sin login
# 5001 = formulas_app, 5002 = Programa Core. Este programa va al 5003.
PORT = int(os.environ.get("MAQUINAS_PORT", "5003"))

# Minutos que se cachea la producción de Asinfo. Sólo se cachea el ÉXITO.
CACHE_MINUTOS = int(os.environ.get("MAQUINAS_CACHE_MINUTOS", "10"))
