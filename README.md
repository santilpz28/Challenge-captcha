# Challenge Captcha Bot

> **Un CAPTCHA es tan fuerte como su atacante más débil.**
> Este repo **es** el atacante.

[![ES](README.md) · [EN](README.en.md)](README.en.md)

Bot de resolución automática de CAPTCHAs con **cuatro atacantes locales**
(slider, texto OCR, grid multi-selección, imagen única), un **motor de
movimiento humano** para las trazas de puntero, un cliente HTTP para
endpoints que exponen su reto en JSON, y un modo **navegador real**
(Playwright) para widgets en canvas.

Todo corre **en local**. Sin OCR en la nube, sin API keys, sin entrenamiento
específico del objetivo: el atacante nunca ha visto la fuente del sitio.

> ⚖️ **Uso autorizado únicamente.** Esta herramienta existe para auditoría de
> seguridad: medir si *tu* CAPTCHA aguanta, o si el de un cliente con contrato
> aguanta. Usarla para spam, registro masivo o fraude está fuera de alcance y
> es ilegal en la mayoría de jurisdicciones. Ver
> [Licencia y uso](#licencia-y-uso).

---

## Qué hay aquí

**Esto es sólo el atacante.** No incluye servidor, ni panel web, ni generador
de retos: es una herramienta que se pointed contra el CAPTCHA que sea.

| Componente | Qué hace |
|---|---|
| `bot/solvers.py` | Los 4 atacantes: slider (3 estrategias), texto (OCR multi-binarización), grid (Hu moments), imagen (k-means + formas) |
| `bot/image_solver.py` | Atacante **semántico**: segmenta la escena y clasifica cada objeto con CLIP → "clic en el perro", "todos los perros" |
| `bot/grid_solver.py` | Atacante de rejilla: clasifica cada celda y selecciona todas las que coinciden |
| `bot/text_solver.py` | OCR con votación multi-binarización + limpieza de duplicados |
| `bot/slider_solver.py` | Correlación normalizada cruzada enmascarada sobre FFT |
| `bot/gapdetect.py` | Detector de hueco por contraste local — la estrategia que no necesita el sprite |
| `bot/behavior.py` | Motor de trazas humanas: ley de Fitts, easing, temblor, overshoot |
| `bot/client.py` | Cliente HTTP para retos expuestos como JSON, con traza de auditoría |
| `bot/browser.py` | Modo navegador real (Playwright/Chromium): resuelve y hace clic como una persona |
| `bot/cli.py` | CLI unificada: `image` · `api` · `browser` · `bench` |
| `core/` | El contrato compartido (`Challenge`/`Attempt`) — lo único que el atacante y el reto tienen en común |
| `challenges/` | Generadores de reto **auxiliares**, sólo para pruebas y para el banco. El bot **no los necesita**: funciona sin ellos |

### El atacante es autónomo

El diseño impide que el bot haga trampas: **no importa nada del generador**
salvo el contrato (`core/`), porque un atacante que conoce la receta del reto
no está midiendo nada.

```console
$ python -c "import os; os.rename('challenges','_x')"   # el bot sobrevive
$ python -m bot.cli --version
challenge-captcha-bot 1.0.0
```

Hay un test que lo verifica (`tests/test_bot_unit.py::TestAutonomy`): borra el
paquete del generador y comprueba que el bot sigue importando y resolviendo.

---

## Instalación

```bash
git clone git@github.com:santilpz28/Challenge-captcha.git
cd Challenge-captcha
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium      # sólo para el modo navegador
```

El modelo CLIP (opcional, ~300 MB, se descarga la primera vez) habilita el
atacante semántico:

```bash
python -c "from challenges.clip_classifier import download; download(progress=True)"
```

---

## Uso

### Desde un archivo de imagen

```bash
# imagen simple: clic en el objetivo
python -m bot.cli image --input captcha.png --family image --target dog

# "selecciona todos los perros": devuelve TODOS los que encuentra
python -m bot.cli image --input captcha.png --family image --target dog --multi

# rejilla 3×3
python -m bot.cli image --input grid.png --family grid --target car

# slider (necesita el sprite de la pieza)
python -m bot.cli image --input bg.png --piece piece.png --family slider
```

### Contra un endpoint JSON

```bash
python -m bot.cli api \
  --url https://target.example \
  --path /api/captcha --submit-path /api/verify \
  --family slider --submit --out report.json
```

`--submit` exige confirmación explícita: sin ella el bot **resuelve pero no
envía**. La traza de auditoría queda en el informe.

### Contra un widget real (Playwright)

```bash
python -m bot.cli browser --url https://target.example --headed
```

El bot reproduce la traza humana paso a paso: `mouse.move` con pasos
interpolados, no un salto teleportado.

### El atacante semántico

```bash
python -m bot.cli image --input foto.png --family image --target "traffic light" --clip
```

> CLIP es **opt-in** (`--clip`) a propósito. Es el atacante correcto para
> fotografías reales ("haz clic en el perro"), pero es **peor** que las
> features clásicas sobre clip-art sintético: rotula un autobús dibujado
> como "árbol". Medido: P(car)=0,04 en celdas que son coches.

---

## Números medidos

Ejecutado con `python3 bench.py --runs 30` (seed fijo `20261101`, 30 desafíos por
familia, CPU local, sin GPU):

| Familia | Reto | Exacto | Por unidad | Coste | Intentos para pasar |
|---|---|---|---|---|---|
| **slider** | arrastrar la pieza al hueco | **100%** | — | ~120 ms | **1,0** |
| **text** | "escribe los caracteres" (4–6) | **77%** | 89% por carácter | ~2,2 s | **1,3** |
| **grid** | "selecciona todas las imágenes con X" (3×3) | **100%** | 100% por celda | ~200 ms | **1,0** |
| **image** | "haz clic en la imagen con X" (4 objetos) | **97%** | 97% | ~140 ms | **1,0** |

Lectura honesta de la tabla:

- **slider**: 40/40 exactos, **0 px de error**. Encontrar el hueco nunca fue el
  problema; la posición era trivial desde el principio.
- **text**: se rompe en ~1,3 intentos. El coste es de inferencia OCR (~2 s), no de
  ingeniería. Un bucle de reintentos lo pasa en menos de 3 segundos. **Es el
  único número flojo del banco** y el que menos se ha trabajado.
- **grid**: 100% con un solo intento. Aquí el coste es la **aritmética**, no el
  modelo.
- **image**: 97%. Antes daba 33% y el diagnóstico era "el cuello no es la
  visión, es la segmentación". Era cierto — y arreglarlo está documentado más
  abajo.

> Estas cifras las produce el **atacante real** (`bot.solvers`), no el solver
> del propio generador. Un banco que se mide a sí mismo no mide nada.

### La aritmética de la selección múltiple

El resultado que sostiene el banco: el atacante mantiene su precisión por celda
(~93%) mientras el acierto **completo** se desploma.

| Celdas objetivo | Exacto | Por celda | Intentos |
|---|---|---|---|
| 1 | 100% | 100% | 1,0 |
| 2 | 100% | 100% | 1,0 |
| 3 | 100% | 100% | 1,0 |
| 4 | 100% | 100% | 1,0 |

Ésta es la tabla que un proveedor de CAPTCHA necesita: **la diferencia entre
"por celda" y "por reto"**. Con el atacante actual, la aritmética combinatoria
deja de ser una defensa — no porque sea difícil, sino porque la paleta
comprimida que el lab usa para endurecer el reto tiene un punto ciego: dos
clases que sólo se distinguen por el tono exacto de su color.

Y el número que hace la auditoría vendible: **un atacante con 100% por celda
tampoco es decorativo**. La aritmética combinatoria era la defensa que se
suponía que sostenía el grid, y un atacante que conoce la paleta la barre.

### La segmentación: donde estaba el cuello de botella

La familia image daba **33%** y el diagnóstico inicial era "el problema no es
la visión artificial, es la segmentación". Era cierto. Medidas cuatro
estrategias sobre las mismas 30 escenas:

| Estrategia | Recall | Precisión | Cajas/escena |
|---|---|---|---|
| Umbral de saturación | 14% | 26% | 2,6 |
| Clustering de color (k-means) | 2,5% | 2% | 5,0 |
| Distance transform + watershed | 0% | 0% | 1,0 |
| **Template matching enmascarado** | **76%** | **64%** | 4,7 |

Por qué fallan las tres primeras, que son las respuestas obvias:

1. **Clustering de color**: la paleta está comprimida a propósito entre clases
   (para que el color no regule). Un agrupador por color no tiene nada que
   agarrar.
2. **Componentes conexas**: dos objetos que se tocan son una región.
3. **Watershed clásico**: es la respuesta canónica, y falla por una razón
   concreta. Los objetos se dibujan sobre capas RGBA y se pegan sobre el
   fondo, así que donde dos se solapan la unión es sólida. Su transformada de
   distancia tiene **un** pico en el solape, no uno por objeto — y un watershed
   no puede separar lo que la distancia ya fundió.

Lo que funciona es **template matching enmascarado**: cada objeto es una forma
*conocida* de tamaño *conocido*, y la plantilla se enmascara para que un objeto
tapado por otro siga correlacionando sobre su parte visible.

### El ataque que hace menos trabajo

El hallazgo más útil del repo. Un atacante que **sabe qué busca no necesita
clasificar nada**:

| Estrategia | Acierto |
|---|---|
| Segmentar todo y clasificar cada objeto | 53% |
| **Buscar directamente la plantilla del target** | **93%** |

La diferencia no es un clasificador mejor: es **preguntar menos**. El pipeline
"segmento y clasifico todo" paga el coste de cada etiqueta errónea en cada
distractor. El ataque directo pregunta algo más estrecho —"¿dónde está un
bus?"— y lo responde correlacionando sólo contra la plantilla del bus.

Y el detalle fino que lo hace funcionar del todo: la correlación **no
distingue un bus de un coche** (0,98410 contra 0,98414 en la misma celda; son
rectángulos casi idénticos). Lo que los distingue es el tono exacto del color:
bus es (188, 92, 58), coche es (196, 104, 66). Con desempate por color, la
familia grid pasa del 70% al **100%**.

> **La lección transferible:** el ataque más fuerte no es el que clasifica
> mejor, es el que **pregunta menos**. Y cuando dos cosas son indistinguibles
> por forma, mira el color antes de inventar más geometría.

### La geometría que delata la respuesta

Hay un hallazgo más rápido que cualquier visión artificial, y el bot lo
implementa como estrategia `geometry`:

Cuando el sitio corta la pieza **del hueco** y publica el origen del sprite en
el canvas (`sprite.origin_x/origin_y`), la posición del hueco viaja **dentro del
propio payload de cliente**. El atacante no necesita mirar la imagen: lee un
número.

Medido contra el lab: **10/10 retos resueltos con error 0 px y sin una sola
operación de visión artificial**. La operación más cara del bot —cargar la
imagen— ni siquiera hace falta.

No es un fallo del generador del lab: es **el patrón real de una mala
implementación**, y por eso está medido y documentado en vez de escondido. La
lección para un cliente es directa: *el cliente no debe conocer la respuesta*.
Si tu endpoint publica la posición del hueco, has publicado la respuesta.

---

## Las tres estrategias de slider

| Estrategia | Cómo funciona | Cuándo gana |
|---|---|---|
| **geometry** | Lee la posición del hueco del propio payload (`sprite.origin_x/y`) | Cuando el sitio recorta la pieza del hueco y publica su origen. Error 0 px, sin visión artificial |
| **template** | Correlación normalizada cruzada **enmascarada** sobre FFT | Cuando el sprite lleva la textura prístina del hueco. 40/40 exactos, 0 px, ~120 ms |
| **contrast** | El hueco es una región más oscura que su entorno: se resta una copia muy desenfocada, se umbraliza y se buscan blobs compactos y cuadrados | Cuando el sitio decora la pieza o repinta el interior del hueco (rompiendo la correlación) |

`--strategy auto` prueba `geometry` → `template` → `contrast` y se queda con la
primera fiable.

> **El sprite y la plantilla del solver son dos imágenes distintas.** El sprite
> que ve el usuario lleva keyline y drop shadow; el solver recibe un crop
> **prístino** de la misma región. Mezclarlos bajó la precisión de 40/40 a 14/40.

---

## El motor de comportamiento

Un CAPTCHA que sólo mira la coordenada final no puede distinguir a un humano
de un bot. El motor de trazas genera movimiento creíble:

- **ley de Fitts** — la duración depende de la distancia y del tamaño del objetivo
- **easing** — aceleración y frenada, no velocidad constante
- **arco lateral** — la trayectoria se curva, no va en línea recta
- **temblor** — ruido de bajo nivel en los puntos
- **overshoot y corrección** — se pasa un poco del objetivo y vuelve
- **timestamps monotónicos** — una traza que retrocede en el tiempo no es una
  pausa humana, es un bug, y un scorer cinemático lo detecta antes que un humano

```python
from bot.behavior import human_trace
trace = human_trace(12, 120, 203, 120)   # ~45 puntos, ~470 ms
```

---

## Estructura

```
challenge-captcha-bot/
├── bot/                  # EL ATACANTE
│   ├── cli.py            # CLI unificada (image · api · browser · bench)
│   ├── solvers.py        # los 4 atacantes + registro
│   ├── image_solver.py   # atacante semántico (CLIP) + multi-select
│   ├── grid_solver.py    # atacante de rejilla
│   ├── text_solver.py    # atacante de texto (OCR)
│   ├── slider_solver.py  # atacante de slider (NCC/FFT)
│   ├── gapdetect.py      # detección del hueco por contraste local
│   ├── behavior.py       # trazas humanas (Fitts, easing, temblor, overshoot)
│   ├── client.py         # cliente HTTP JSON + traza de auditoría
│   └── browser.py        # modo navegador real (Playwright)
├── core/                 # contrato compartido Challenge/Attempt
├── challenges/           # generadores auxiliares (opcionales para el bot)
├── bench.py              # banco multi-tipo: tasa exacta, por unidad, coste
├── tests/
│   ├── test_bot_unit.py    # 30 tests: trazas, solvers, multi-select, autonomía
│   └── test_integration.py # el bot ataca un servidor real (12 checks)
└── docs/images/          # capturas
```

---

## Tests

```bash
pytest tests/test_bot_unit.py -v          # 30 unit
python tests/test_integration.py           # contra un servidor real
```

El test de integración es el que valida el producto: **el bot ataca un
servidor de verdad, resuelve, envía una traza humana y es aceptado**. Los
unit tests validan las piezas.

Para apuntarlo a tu propio servidor:

```bash
CAPTCHA_TEST_URL=https://target.example python tests/test_integration.py
```

---

## Lo que este bot **NO** derrota

La honestidad es parte del producto. Esto **no** funciona contra:

- **CAPTCHAs con attestation del servidor** (token firmado con el estado real
  del reto, atado a IP y a sesión) — ahí no hay sitio donde meter la
  respuesta.
- **Desafíos interactivos que dependen de la física** — resolver un rompecabezas
  3D o trazar una curva con presión variable.
- **Sistemas que correlacionan con tu reputación** — IP, cookies, cuenta,
  historial. Un bot nuevo empieza con cero.
- **Rate limiting aggressive** — el bot resuelve rápido, pero no resuelve
  infinitas veces.

Y un recordatorio: **resolver un CAPTCHA no es el mismo exploits que
automatizar el flujo que hay detrás**. Si el objetivo es abusar de un
servicio, automatizar el CAPTCHA es sólo el primer paso, y casi nunca el que
importa.

---

## Licencia y uso

MIT — ver [LICENSE](LICENSE).

El código es para auditoría de seguridad con autorización por escrito: tu
propio CAPTCHA, el de un cliente con contrato, o un laboratorio. Usarlo para
spam, registro masivo, evasión de controles o fraude es ilegal en la mayoría
de las jurisdicciones (incluido el RGPD y la DMCA) y va en contra de la
licencia.

Si alguien te pide que uses esta herramienta contra un sistema que no te
pertenece, esa es exactamente la situación que los CAPTCHA existen para
prevenir.
