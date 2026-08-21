# Cómo trabajar en este repo

Programa de mantenimiento de máquinas de tejeduría de Intela.
Ver `README.md` para qué hace y cómo está armado. Esto es cómo se trabaja.

## Antes de tocar nada

Cargar la skill **`maquinas-tejeduria`**: tiene el modelo de datos, el mapa de
puertos del server y las trampas que ya se pagaron. Pair con `intela-aws-deploy`.

## El ciclo

```bash
# 1. Cambiar código
# 2. Correr los tests SIEMPRE antes de commitear
python3 scripts/test_maquinas.py
# 3. Commit + push a main
# 4. El server se actualiza solo en <2 min (scripts/auto_update.ps1)
```

No hay deploy manual. **El server tira del repo**, no al revés: como el repo es
público no hace falta ninguna credencial en ningún lado. GitHub Actions sólo
corre los tests.

## Como se pushea

```bash
python3 scripts/test_maquinas.py      # 182 checks, un segundo
git push                              # el server se actualiza solo en <2 min
```

El token que sirve para este repo es el de `Programa Core/.gh_pat`. El de
`formulas_app/.git/gh-credentials` da 403 aca.

**No hace falta CloudShell.** El server tira del repo y el updater se actualiza
a si mismo. `/healthz` dice que commit esta corriendo.

## Reglas duras

**Este repo es PUBLICO.** Nunca `git add -A` sin mirar que entra. El 20/08/2026
se colaron las planillas de planta y hubo que reescribir el historial.

**El semaforo va por KILOS.** Los dias se muestran, no prenden nada.

**El semaforo es una fila por MAQUINA, no por (maquina, tipo).** El color lo
prenden los mantenimientos que tienen tope de kilos. El cambio de agujas NO
prende nada: no se hace por tiempo ni por desgaste, lo pide la tela, asi que va
al lado como fecha. Antes la misma maquina salia dos veces con dos colores
distintos.

**Nunca inventar un tope de kilos.** Los define el mecanico, y se cargan en la
ficha de cada maquina (dentro de «Editar la ficha y los kilos»), no en una
pantalla aparte: el numero es de la maquina.

**Los tests corren antes de cada commit.** Son 303 checks en 30 grupos y tardan
un segundo. Cubren lo que ya rompió producción: el pool sin inicializar, la
inyección en el SQL de Asinfo, la aritmética del semáforo, el arranque en lote,
la lectura de las dos planillas (mantenimiento y control de ajuste), el
historial que se recarga sin duplicar, los kilos entre paradas y que cada
pantalla del menú abra de verdad.

**El aviso naranja prende al 90%, no al 80%.** El número vive en `AVISO`
(app.py) y la barra del semáforo lo recibe como `aviso`: estaba escrito a mano
en la plantilla y entre el 80% y el 90% la fila decía «En regla» en verde con
la barra naranja.

**Un renglón de la planilla puede ser DOS mantenimientos.** «limpiesa de
cilindro · cambio de cilindro a galga 28» son dos cosas hechas el mismo día. Y
al revés: «limpiesa de cilindro» NO es un cambio de cilindro — tiene que decir
*cambio*. Por eso la fila 7 se guarda como orden 70, 71, 72: la clave
(hoja, orden) tiene que seguir siendo única.

**Los tipos los crea el mecánico en la pantalla de Tipos.** El lector busca cuál
de los que EXISTEN habla de agujas, cilindro o platinas (`excel._clasificar`).
Si uno no está cargado, esa frase cae en limpieza. Nunca crear tipos desde el
código.

**Recargar una planilla borra y rehace sus hojas.** Antes sólo actualizaba fila
por fila, así que una fila borrada del Excel se quedaba para siempre. Los
mantenimientos cargados a mano (`hoja` en nulo) no se tocan.

**Seis hojas tienen DOS fichas pegadas al lado.** La de la máquina y la copia de
la de al lado. La MQ 61 salía con el número de serie de la otra. Se corta en la
columna donde empieza el segundo encabezado (`excel._segunda_ficha`), y si a la
de la izquierda le borraron los títulos se leen con los de la copia
(`excel._espejo`).

**Un número escrito a mano va con `a_decimal_es`, no con `a_decimal`.** Acá el
punto es de miles: `a_decimal("1.410")` devuelve 1,41 y guardaba mal las rpm.

**Un número escrito a mano en la planilla viene con la unidad pegada.**
«1,80 kg/m», «138 g», «4,40 *KG». Se leen con `ajustes._medida`, que acepta el
número y su unidad y nada más: así entran los 180 kg/m y los 42 gramajes
terminados que se perdían, y siguen afuera las cuarenta longitudes de malla
(«28,2 LM») que alguien escribió en esa misma columna.

**El gramaje del CRUDO y el del TERMINADO son dos columnas.** La del terminado
dice sólo «G/m2» y el `startswith` del crudo se la llevaba: 43 celdas que no
entraban nunca. El crudo va primero en `_TITULOS_AJUSTE`, con su etiqueta
completa.

**Varias hojas de la planilla de ajuste tienen DOS tablas al lado.**
«INVENTARIO LEVAS» trae el inventario a la izquierda y, pegada a la derecha,
cuántas levas lleva cada tela — cuarenta renglones que no entraban a ningún
lado. «BANDAS» trae tres. Antes de dar una hoja por leída, mirar qué hay a la
derecha.

**Repuestos son TRES pestañas, no seis.** «AGUJAS» es por máquina (la de usar) y
«CODIGO DE AGUJAS» es por modelo con la marca (la de comprar): el mismo dato
para dos usos, así que van juntas. Las Memminger, las de motor y el stock son
las tres bandas.

**La MQ 12 tiene dos juegos de agujas**, uno por galga, y los dos son suyos: se
guardan juntos en la misma fila. La MQ 14 dice «VACIO EL LUGAR» y sale en los
descartes, no en silencio.

**Importar la app como la importa Waitress.** Los tests hacen `import app` sin
mockear `store` entero, a propósito. Un test que mockea todo no ve que la app
levanta pero devuelve 500 en cada pantalla.

**El menu es de siete: Semaforo · Cargar mantenimiento · Ajustes de tela ·
Produccion · Repuestos · Maquinas · Tipos.** `/archivos` y `/carga` siguen vivas pero NO van en el menu:
las planillas ya se cargaron y no se tocan todos los dias. No volver a
colgarlas.

**En pantalla se dice "mantenimiento", no "service".** Ver la skill
`textos-de-pantalla-intela`: castellano simple, una idea por linea.

**Validar los templates.** `py_compile` no ve los `.html`. Un `{% endif %}` de
menos sólo aparece en runtime, con la pantalla rota en producción. El workflow
y `deploy.sh` los parsean con Jinja.

**Nunca borrar antes de poder reemplazar.** Vale para el auto-updater y para
cualquier script que toque el server. Armar completo aparte, verificar, y recién
ahí renombrar.

**Sumar del lado del motor, no en Python.** Metabase corta en 2.000 filas sin
avisar. Un resultado truncado no es un dato con menos filas: es un dato
equivocado. Hay un guard que falla cerrado.

**El esquema corre UNA SENTENCIA POR TRANSACCION.** Antes iba todo en un solo
`execute`: si una sentencia fallaba, `init_pool` reventaba, `/healthz` devolvia
503 y el auto-update del server deshacia el deploy entero — sin dejar rastro de
cual habia fallado. Desde afuera parecia que pushear no hacia nada. Ahora la que
falla queda en `store.AVISOS_ESQUEMA` y `/healthz` la muestra. No volver a
juntarlas.

**Un `ON CONFLICT` contra un indice PARCIAL tiene que repetir el `WHERE` del
indice.** Si no, Postgres contesta «there is no unique or exclusion constraint
matching the ON CONFLICT specification». Paso con
`service (hoja, orden) WHERE hoja IS NOT NULL`.

**`excel.a_kilos` limpia todo lo que no sea digito y se lleva puesto el signo**:
un «-5» entra como 5. Para validar un numero que puede venir negativo, parsear a
mano.

## Estilo

Castellano en todo lo que ve un usuario y en los comentarios. Números en formato
español (punto para miles, coma para decimales) vía el filtro `num`. Los
comentarios explican **por qué**, no qué — el qué ya está en el código.

## Lo que NO va en este repo

Credenciales, y documentación operativa que describa puntos débiles del server
(capacidad, puertos abiertos, deuda de seguridad). Eso vive en las notas
internas y en las skills, no acá.
