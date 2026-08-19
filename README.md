# Máquinas · Mantenimiento de tejeduría

Programa chico para saber **qué máquina de tejeduría necesita service**.

Tres pantallas:

| Ruta | Para qué |
|---|---|
| `/` | El semáforo. Qué está vencido, qué falta poco, qué está en regla. |
| `/registrar` | Cargar un service hecho. Al guardar, el contador de esa máquina vuelve a cero. |
| `/tipos` | Definir los tipos de service y cada cuántos kg / rollos / días va cada uno. |

---

## La idea en una frase

**Los kilos no se cargan a mano.** Asinfo ya registra cada rollo de tela cruda que entra
a bodega con la máquina que lo tejió — desde julio de 2022. Este programa sólo guarda
*cuándo se hizo cada service*, y resta.

```
kg desde el último service  =  Σ (rollos a bodega 52 de esa máquina, posteriores a la fecha del service)
```

## Qué guarda este programa (y qué no)

Sólo dos tablas propias, en el schema `mantenimiento`:

- `tipo_service` — el plan: nombre + hasta tres umbrales (`cada_kg`, `cada_rollos`, `cada_dias`).
  Cualquiera puede quedar vacío. **El umbral que se cumple primero manda.**
- `service` — cada service hecho: máquina, tipo, fecha, quién, nota.

Todo lo demás se calcula al vuelo. **A Asinfo sólo se lo lee, nunca se le escribe.**

## El punto cero

Una máquina **no aparece en el semáforo hasta que se le carga su primer service**. Antes de
eso no hay desde cuándo contar, y un número inventado sería peor que ninguno. Las
combinaciones que faltan arrancar se listan aparte, con un botón para cargarlas.

---

## Definición del contador (importante)

Cuenta **todo lo que la máquina tejió**: jersey, fleece, piqué, rib, cuellos, puños. Todos los
productos `TC-*` que entran a la bodega 52.

⚠ Esto **no coincide** con la pantalla *Producción Tejeduría* de Programa Core, y está bien
que no coincida. Para agosto 2026, Asinfo crudo da 181.620 kg y Programa Core 146.686: esa
pantalla es una vista de **costo** de tejeduría y deja categorías afuera. Para desgaste de
máquina hay que contar todo lo que la máquina hiló. Si algún día los dos números tienen que
coincidir, hay que decidir cuál definición gana — no emparcharlo acá.

⚠ **`MAQUINA 000`.** En agosto entraron 12.567 kg (7 % del total) cargados a esa máquina
genérica en vez de a una real. Esos kilos no se le imputan a nadie. No es un bug de este
programa: es un hábito de carga en Asinfo que conviene corregir en el origen.

---

## Configuración

Variables de entorno:

```bash
# Base propia (el mismo cluster RDS; schema `mantenimiento`)
MAQUINAS_DATABASE_URL=postgresql://usuario:pwd@intela-db.…:5432/postgres?sslmode=require

# Puente de lectura a Asinfo (el mismo Metabase que ya usa Programa Core)
METABASE_URL=http://localhost:3000
METABASE_USERNAME=integracion@intela.com.ec
METABASE_PASSWORD=…
ASINFO_DB_ID=2

# App
MAQUINAS_SECRET_KEY=<algo largo y random>
MAQUINAS_PASSWORD=<contraseña compartida; vacío = sin login>
MAQUINAS_PORT=5002
```

Las tablas **se crean solas** al arrancar (`CREATE ... IF NOT EXISTS`). No hay migraciones.

## Correr

```bash
pip install -r requirements.txt
python app.py                                  # desarrollo
python -m waitress --port=5002 app:app         # producción
```

`GET /healthz` devuelve si el puente a Asinfo está configurado y cuántos tipos hay cargados.

## Deploy

Correr `2-deploy-maquinas.sh` en CloudShell. Sigue el patrón de las otras apps del box:
Scheduled Task como SYSTEM, Waitress, `-ExecutionTimeLimit [TimeSpan]::Zero`, `-RestartCount 3`,
y un `launch.ps1` que loguea a `C:\maquinas_app\logs\` para que un crash no sea invisible.

El último paso (el bloque de Caddy) es a mano **a propósito**: un Caddyfile roto se lleva
puesto `formulas.intela.com.ec` y Metabase junto con él.

## El bug del tope de filas (encontrado antes de deployar, 17/08/2026)

La primera versión traía de Asinfo la producción **día por día** y la sumaba en Python.
Probando las consultas reales contra el Asinfo de producción apareció que **Metabase corta
cualquier resultado en 2.000 filas, en silencio**: tanto la consulta de 90 días como la de
4 años devolvían exactamente 2.000 filas.

El efecto habría sido el peor posible: los kilos de las últimas máquinas del listado se
perdían, esas máquinas aparecían con menos desgaste del real, y el semáforo las pintaba de
**verde estando vencidas**. Un mantenimiento vencido mostrado como al día es justo lo que
este programa existe para evitar.

Se arregló de raíz, no con un parche:

1. **La suma la hace SQL Server**, no Python. `acumulados()` manda los pares
   (máquina, tipo, fecha) en un `VALUES` y recibe **una fila por par** — decenas de filas,
   imposibles de truncar. De paso es más rápido (~450 ms).
2. Se mandan `constraints` explícitos en el request a Metabase.
3. **Guard que falla cerrado**: si alguna respuesta llega con el tope de filas, se levanta
   `RespuestaTruncada` y la pantalla avisa. Nunca se usa un resultado incompleto para contar
   kilos.

**Regla para el próximo puente:** un resultado truncado no es un dato con menos filas, es un
dato **equivocado**. Si una fuente puede truncar, o agregás del lado del motor o chequeás el
tope explícitamente.

## Verificado antes de deployar

- Las dos consultas corridas **verbatim** contra el Asinfo real: el catálogo devuelve las
  43 tejedoras; la agregada devuelve kg y rollos por par en ~450 ms.
- Aritmética cruzada a mano: MQ 001 desde el 20/05 da 34.146,51 kg contra los 34.504 medidos
  con `>=`. La diferencia son los 357 kg del día del service, que quedan afuera a propósito
  (el desgaste cuenta *después* de la intervención).
- Guard de truncado: se dispara con el tope, no se dispara con 37 filas.
- Con Asinfo caído o truncado la pantalla avisa y **no pinta ningún estado**.
- Inyección SQL: fechas e IDs se validan contra `^\d{4}-\d{2}-\d{2}$` y `int()` antes de
  interpolarse. Un `'; DROP TABLE maquina--` queda neutralizado; una fecha basura se rechaza.

## Decisiones tomadas y por qué

- **Programa aparte, no una pestaña más.** Lo pidió así. Además el usuario es otro (el jefe
  de planta, no tintorería) y el dato viene de otro lado (Asinfo, no formulas_app).
- **No se usa el módulo de mantenimiento de Asinfo.** Existe completo — `plan_mantenimiento`,
  `orden_trabajo_mantenimiento`, `lectura_mantenimiento` y cinco tablas más — pero está en
  **cero filas**: nunca se usó. Es pesado y arrastra un modelo de órdenes de trabajo que no
  es lo que se pidió.
- **Se cachea sólo el éxito.** Si Asinfo no contesta, la pantalla muestra la última lectura
  buena y avisa que es vieja. Nunca un cero — un cero pondría todo en verde por un problema
  de red, que es justo el fallo que este programa existe para evitar.
- **Una contraseña compartida, sin usuarios.** "Muy muy fácil". Quién hizo el service se
  escribe a mano en el formulario, con autocompletado de los nombres ya usados.

## Lo que falta definir (input humano)

1. **La lista de services de una tejedora y cada cuánto va cada uno.** No sale de ningún
   sistema — lo tiene el mecánico. Se carga en `/tipos`.
2. **Si las 43 máquinas comparten plan o hay grupos** (por marca, galga, antigüedad). Hoy el
   plan es global: un tipo de service aplica a todas. Si hacen falta planes distintos, se
   agrega una columna de grupo.
3. **Si tiene que avisar solo** (mail con lo vencido) o alcanza con entrar a mirar.

## Notas de infraestructura

El detalle del server (capacidad, puertos, deuda técnica) NO vive acá a
propósito: es documentación operativa, no del programa. Está en las notas
internas.
