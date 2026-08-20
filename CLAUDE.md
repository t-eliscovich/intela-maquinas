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

**Los tests corren antes de cada commit.** Son 182 checks en 19 grupos y tardan
un segundo. Cubren lo que ya rompió producción: el pool sin inicializar, la
inyección en el SQL de Asinfo, la aritmética del semáforo, el arranque en lote,
la lectura de las dos planillas (mantenimiento y control de ajuste), el
historial que se recarga sin duplicar, los kilos entre paradas y que cada
pantalla del menú abra de verdad.

**Importar la app como la importa Waitress.** Los tests hacen `import app` sin
mockear `store` entero, a propósito. Un test que mockea todo no ve que la app
levanta pero devuelve 500 en cada pantalla.

**El menu es de seis: Semaforo · Cargar mantenimiento · Ajustes · Repuestos ·
Maquinas · Tipos.** `/archivos` y `/carga` siguen vivas pero NO van en el menu:
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
