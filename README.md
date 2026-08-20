# Máquinas · Mantenimiento de tejeduría

Programa chico para saber **qué máquina de tejeduría hay que parar a limpiar**.

El menú tiene seis pantallas:

| Ruta | Para qué |
|---|---|
| `/` | **Semáforo.** Una fila por máquina: qué pasó los kilos, qué falta poco, qué está en regla. |
| `/registrar` | **Cargar mantenimiento.** Qué se le hizo a una máquina, quién, cuánto llevó y qué repuesto se cambió. Al guardar, los kilos de esa máquina vuelven a cero. |
| `/ajustes` | Cómo se pone cada máquina para tejer cada tela, y cuánto hilo lleva cada tela. |
| `/repuestos` | Qué aguja, qué leva y qué banda lleva cada máquina, y cuántas hay. |
| `/maquinas` | Las 43 máquinas, con lo que le falta a cada una. Desde acá se entra a la ficha. |
| `/tipos` | Los tipos de mantenimiento. |

Hay dos pantallas más que **no van en el menú a propósito**, porque no se miran
todos los días: `/archivos` (subir la planilla, un manual, una foto) y `/carga`
(leer una planilla de Excel). Las planillas ya se cargaron; las rutas quedan
vivas para el día que haya que subir una corregida.

---

## La idea en una frase

**Los kilos no se cargan a mano.** Asinfo ya registra cada rollo de tela cruda que entra
a bodega con la máquina que lo tejió — desde julio de 2022. Este programa sólo guarda
*cuándo se paró cada máquina*, y resta.

```
kg desde el último mantenimiento  =  Σ (rollos a bodega 52 de esa máquina,
                                        posteriores a la fecha del mantenimiento)
```

## El semáforo: una fila por MÁQUINA

El color de la fila lo prenden **los mantenimientos que tienen tope de kilos** —
los que se vencen por desgaste. Manda el peor de todos.

**El cambio de agujas no prende nada.** No se hace porque pasó el tiempo ni
porque se gastó: lo pide la tela. Va al lado, como fecha, para saber cuándo fue
el último y cuántos kilos tejió desde entonces.

Antes el semáforo era una fila por (máquina, tipo) y la misma máquina salía dos
veces con dos colores distintos. Eso era justo lo que había que evitar: quien
mira la pantalla quiere saber **qué máquina parar**, no qué combinación.

## El tope de kilos va en la ficha de la máquina

Cada cuántos kilos se limpia se carga en **Máquinas → la máquina → *Editar la
ficha y los kilos***, no en una pantalla aparte. El número es de la máquina y el
mecánico lo decide mirándola a ella: la MQ 3 teje unos 139.000 kg al año y la
MQ 63 unos 7.400. Un número único para todas deja media planta en verde para
siempre.

Si la máquina no tiene número propio manda el del tipo (`tipo_service.cada_kg`),
que existe sólo como respaldo para no tener que cargar 43 filas antes de que el
semáforo sirva para algo.

## La ficha de la máquina: un renglón por DÍA

Si el mismo día se limpió la máquina y se le cambiaron las agujas, en la
planilla son dos filas — pero **para el mecánico es una sola parada**. La ficha
las junta en un renglón, con los dos tipos, quién trabajó y las notas.

Al lado van **los kilos que tejió desde la parada anterior**, que es la pregunta
de verdad: cuánto aguantó. La última parada cuenta hasta hoy.

---

## Qué guarda este programa (y qué no)

En el schema `mantenimiento`, todo con `CREATE ... IF NOT EXISTS`:

| Tabla | Qué es |
|---|---|
| `tipo_service` | Los tipos de mantenimiento, con el tope de kilos general (de respaldo). |
| `service` | Cada mantenimiento hecho: máquina, tipo, fecha, quién, horas, repuestos, nota. `hoja` y `orden` dicen de qué hoja y qué fila de la planilla salió. |
| `plan_maquina` | El tope de kilos **de esa máquina**. |
| `maquina_ficha` | Marca, modelo, galga, diámetro, alimentadores, agujas, año, serie. |
| `archivo` | Lo que se sube desde el programa. Va en la base y no en una carpeta del server: así sobrevive a una actualización, que reemplaza la carpeta entera. |
| `ajuste` | La puesta a punto de cada tela en cada máquina. |
| `aguja_maquina` | Qué aguja lleva cada máquina. |
| `aguja_modelo` | Qué aguja lleva cada modelo, con la marca de la aguja y de la platina. |
| `leva` | Las levas. Cuelgan de un modelo, no de una máquina: la misma sirve para varias. |
| `banda` | Las bandas Memminger y las de motor, por modelo y diámetro. |
| `banda_stock` | Cuántas bandas hay de cada medida. |
| `eficiencia` | Cuánto **tendría que** dar cada máquina en 12 y en 24 horas. Es un cálculo de la planilla, no lo que tejió: eso lo mide Asinfo. |
| `consumo_hilo` | Cuánto hilo lleva cada tela. |
| `gramaje` | El peso medido de la tela que salió de cada máquina. |

Los kilos, los rollos y el catálogo de máquinas **no se guardan**: se calculan al
vuelo contra Asinfo. **A Asinfo sólo se lo lee, nunca se le escribe.**

## Qué hay cargado hoy (20/08/2026)

| | |
|---|---|
| Mantenimientos | **1.366**, desde 2018 |
| Fichas de máquina | 36 |
| Ajustes de máquina | 1.260 |
| Agujas por máquina | 29 |
| Agujas por modelo | 12 |
| Levas | 11 |
| Bandas | 24 (Memminger y de motor) |
| Medidas en stock | 20 |
| Máquinas con producción calculada | 24 |
| Consumo de hilo | 56 filas |
| Gramajes | 15 |

Los 1.366 mantenimientos antes eran 66: el lector viejo de la planilla se
quedaba con **la última fecha de cada tipo** y tiraba el resto. Con el historial
entero la ficha de una máquina muestra de verdad cada cuánto se para.

**Lo que falta lo tiene que decir el mecánico**, y está detallado en
[`PARA-EL-MECANICO.md`](PARA-EL-MECANICO.md): **7 máquinas sin arrancar**
(no tienen ningún mantenimiento anotado, así que no hay desde cuándo contar) y
**13 sin tope de kilos** (cuentan, pero nunca se prenden).

## El punto cero

Una máquina **no aparece con color hasta que se le carga su primer
mantenimiento**. Antes de eso no hay desde cuándo contar, y un número inventado
sería peor que ninguno. Las que faltan arrancar se listan aparte, con un botón
para cargarlas.

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
MAQUINAS_PORT=5003
```

Las tablas **se crean solas** al arrancar. No hay migraciones.

El esquema corre **una sentencia por transacción**: la que falla no tira abajo
el arranque, queda anotada en `store.AVISOS_ESQUEMA` y `/healthz` la muestra.
Ver `CLAUDE.md` para por qué.

## Correr

```bash
pip install -r requirements.txt
python app.py                                  # desarrollo
python -m waitress --port=5003 app:app         # producción
```

`GET /healthz` devuelve qué commit está corriendo, si el puente a Asinfo está
configurado, cuántos tipos hay cargados y —si la hubo— qué sentencia del esquema
no pudo correr.

## Deploy

**No hay deploy manual.** Se pushea a `main` y el server tira del repo solo, en
menos de dos minutos (`scripts/auto_update.ps1`). Como el repo es público no
hace falta ninguna credencial en ningún lado; GitHub Actions sólo corre los
tests. `/healthz` dice qué commit está andando.

El auto-updater se prende **una sola vez** en el box con
`scripts/prender_auto_update.ps1`. Sin eso el server nunca tira del repo: se
pushea, el CI da verde, y la pantalla sigue vieja sin que nada avise. Pasó el
19/08/2026.

`deploy.sh` sigue existiendo para empujar desde afuera con awscli, pero es el
camino de excepción.

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
  con `>=`. La diferencia son los 357 kg del día del mantenimiento, que quedan afuera a
  propósito (el desgaste cuenta *después* de la intervención).
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
- **Una contraseña compartida, sin usuarios.** "Muy muy fácil". Quién hizo el mantenimiento
  se escribe a mano en el formulario, con autocompletado de los nombres ya usados.
- **La planilla se vuelve a cargar sin duplicar.** Cada fila guarda de qué hoja y de qué
  renglón salió, así que cargarla otra vez actualiza en vez de duplicar. Lo cargado a mano
  no tiene hoja y no se pisa nunca.
- **El consumo de hilo vive dentro de Ajustes.** Es la misma pregunta — cómo se teje una
  tela — y partirla en dos pantallas obligaba a cambiar de pantalla para contestar media.

## Lo que falta definir (input humano)

1. **Los kilos de las 13 máquinas que no tienen tope**, y el arranque de las 7 que no
   cuentan. Está en `PARA-EL-MECANICO.md`.
2. **Si el cambio de agujas también tiene que avisar por kilos.** Hoy va sólo como fecha.
   Si el mecánico quiere un tope, se carga en el mismo lugar que el de limpieza.
3. **Si tiene que avisar solo** (mail con lo vencido) o alcanza con entrar a mirar.

## Notas de infraestructura

El detalle del server (capacidad, puertos, deuda técnica) NO vive acá a
propósito: es documentación operativa, no del programa. Está en las notas
internas.
