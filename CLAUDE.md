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

## Reglas duras

**Los tests corren antes de cada commit.** Son 13 y tardan un segundo. Cubren lo
que ya rompió producción: el pool sin inicializar, la inyección en el SQL de
Asinfo, la aritmética del semáforo, el arranque en lote.

**Importar la app como la importa Waitress.** Los tests hacen `import app` sin
mockear `store` entero, a propósito. Un test que mockea todo no ve que la app
levanta pero devuelve 500 en cada pantalla.

**Validar los templates.** `py_compile` no ve los `.html`. Un `{% endif %}` de
menos sólo aparece en runtime, con la pantalla rota en producción. El workflow
y `deploy.sh` los parsean con Jinja.

**Nunca inventar umbrales.** Los números de cada service los define el mecánico.
Un semáforo con umbrales inventados pinta rojos falsos y nadie lo mira más.

**Nunca borrar antes de poder reemplazar.** Vale para el auto-updater y para
cualquier script que toque el server. Armar completo aparte, verificar, y recién
ahí renombrar.

**Sumar del lado del motor, no en Python.** Metabase corta en 2.000 filas sin
avisar. Un resultado truncado no es un dato con menos filas: es un dato
equivocado. Hay un guard que falla cerrado.

## Estilo

Castellano en todo lo que ve un usuario y en los comentarios. Números en formato
español (punto para miles, coma para decimales) vía el filtro `num`. Los
comentarios explican **por qué**, no qué — el qué ya está en el código.

## Lo que NO va en este repo

Credenciales, y documentación operativa que describa puntos débiles del server
(capacidad, puertos abiertos, deuda de seguridad). Eso vive en las notas
internas y en las skills, no acá.
