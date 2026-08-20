# Lo que falta cargar

El programa de mantenimiento está andando en **maquinas.intela.com.ec**.
Los kilos de cada máquina los cuenta solo, leyendo Asinfo. Lo que el programa
NO puede saber lo tiene que decir el mecánico.

Todo se carga desde **Máquinas**, tocando la que falta. Cada fila dice qué le
falta.

---

## Primero: dos máquinas están pasadas

| Máquina | Última limpieza | Kilos | Se pasó |
|---|---|---|---|
| **MQ 27** | 16/06/2026 | 23.430 de 20.000 | +3.430 kg |
| **MQ 22** | 03/12/2025 | 15.898 de 15.000 | +898 kg |

Estas dos no aparecían antes porque sus hojas de la planilla no tenían la fila
de títulos y el programa las daba por vacías. Ya están.

---

## 1. Trece máquinas sin tope de kilos

**MQ 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62 y 63.**

Cuentan los kilos y se ve cuánto llevan desde la última limpieza —la MQ 53 ya
lleva 24.961 kg— pero nadie dijo cada cuántos toca, así que el semáforo no las
puede prender.

La pregunta es una sola: **cada cuántos kilos tejidos hay que limpiar esta
máquina.**

**Dónde se carga:** Máquinas → la máquina → *Editar la ficha y los kilos*.

El número es **de cada máquina**, no uno para todas: la MQ 3 teje unos
139.000 kg al año y la MQ 63 unos 7.400. Un número único deja media planta en
verde para siempre. En la ficha hay un cuadro *Cuánto teje por mes* para
chequear si el número tiene sentido: si con ese tope la máquina tarda tres años
en llegar, el tope está mal.

Las que ya tienen número (MQ 1 a 29) van de 4.500 a 20.000 kg. Si alguno está
mal, se corrige en el mismo lugar.

---

## 2. La MQ 30 no tiene nada

No tiene hoja en la planilla de planta, así que no tiene ficha, ni
mantenimientos, ni tope. Hay que cargarle la última limpieza para que arranque
a contar.

**Si no se sabe cuándo fue, poner hoy.** Nunca una fecha vieja estimada: pinta
un rojo que no existe y después nadie vuelve a mirar el semáforo.

---

## 3. Tres fechas de la planilla no se entienden

Están escritas a mano y ni mirando las filas de arriba y abajo se puede saber
cuál es. Conviene corregirlas en el Excel:

| Hoja | Fila | Dice |
|---|---|---|
| MAQ.9 | 3 | `6/8//2018` — ¿6 de agosto o 8 de junio? |
| MQ 22 | 4 | `11/12,/2019` — ¿11 de diciembre o 12 de noviembre? |
| M 62 | 6 | `10/30/2026` — es una fecha que todavía no pasó |

Son tres de 1.493. El resto de las tipeadas a mano el programa las resolvió
solo, porque el historial está en orden y la fecha tiene que caer entre la de
arriba y la de abajo.

---

## 4. Falta el kg del cambio de agujas

El cambio de agujas se muestra sólo como fecha: cuándo fue el último y cuántos
kilos tejió desde entonces. **No prende el semáforo**, porque no se hace por
tiempo — lo pide la tela.

Si el mecánico quiere que también avise por kilos, hay que decir cada cuántos, y
se carga en el mismo lugar que el de limpieza.
