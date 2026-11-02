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
| **text** | "escribe los caracteres" (4–6) | **87%** | 91% por carácter | ~2,1 s | **1,2** |
| **grid** | "selecciona todas las imágenes con X" (3×3) | **87%** | 96% por celda | **5 ms** | **1,2** |
| **image** | "haz clic en la imagen con X" | 33% | 33% | 8 ms | 3,0 |

Lectura honesta de la tabla:

- **slider**: 40/40 exactos, **0 px de error**. Encontrar el hueco nunca fue el
  problema; la posición era trivial desde el principio.
- **text**: se rompe en ~1,2 intentos. El coste es de inferencia OCR (~2 s), no de
  ingeniería. Un bucle de reintentos lo pasa en menos de 3 segundos.
- **grid**: mismo resultado con 5 ms. Aquí el coste es la **aritmética**, no el
  modelo — y por eso los dos números se parecen: el atacante es barato, la
  selección múltiple no.
- **image**: 33%. El cuello de botella **no es la visión artificial**, es la
  *segmentación*. Es un resultado útil: señala dónde está el trabajo difícil.

### La aritmética de la selección múltiple

El resultado que sostiene el banco: el atacante mantiene su precisión por celda
(~93%) mientras el acierto **completo** se desploma.

| Celdas objetivo | Exacto | Por celda | Intentos |
|---|---|---|---|
| 1 | 87% | 96% | 1,2 |
| 2 | 55% | 93% | 1,8 |
| 3 | 28% | 91% | 2,7 |
| 4 | 9% | 88% | 3,9 |

Éste es el número que un proveedor de CAPTCHA necesita: **la diferencia entre
"por celda" y "por reto"**. Un atacante con 90% por celda parece mediocre;
con 9% de retos completos, es decorativo.

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
