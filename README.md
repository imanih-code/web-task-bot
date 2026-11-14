# web-task-bot

Graba **una vez** lo que haces en una página web y repítelo automáticamente
para **cada fila de un Excel o CSV**.

Pensado para tareas mecánicas: dar de alta empleados en una intranet, cargar
facturas en un portal, actualizar fichas una por una. Abres el navegador, haces
la tarea con el primer registro y el bot aprende qué columna va en cada campo.

```
wtb record alta-empleados --url https://intranet/empleados/nuevo --data empleados.xlsx
wtb run    alta-empleados --data empleados.xlsx
```

```
  [  1/120] registro 1    OK        4.1s  Ana · López
  [  2/120] registro 2    OK        3.9s  Luis · Pérez
  [  3/120] registro 3    ERROR    20.4s  Marta · Gil
           paso 7 (marcar opción "Parcial"): no encontré opción "Parcial" después de 20 s
  ...
Resumen: 119 OK  1 con error
  Resultados: runs/alta-empleados/20261114-171104/resultados.xlsx
```

## Instalación

Necesitas Python 3.10 o superior y Google Chrome o Microsoft Edge.

```bash
python -m venv .venv
```

```bash
.venv\Scripts\activate
```

```bash
pip install -e .
```

Si no tienes ni Chrome ni Edge, instala el Chromium de Playwright:

```bash
python -m playwright install chromium
```

## Cómo se usa

### 1. Prepara el Excel

Una fila por registro y los nombres de columna en la primera fila. Si la tarea
sube archivos, ponlos en la misma carpeta del Excel y pon el nombre del
archivo en una columna (por ejemplo `CV` = `ana.pdf`).

| Nombre | Apellido | Email           | Departamento | Ingreso    | Jornada | CV       |
|--------|----------|-----------------|--------------|------------|---------|----------|
| Ana    | López    | ana@empresa.com | Tecnología   | 15/03/2024 | Parcial | ana.pdf  |
| Luis   | Pérez    | luis@empresa.com| Ventas       | 01/04/2024 | Completa| luis.pdf |

### 2. Graba la tarea con el primer registro

```bash
wtb record alta-empleados --url https://intranet/empleados/nuevo --data empleados.xlsx
```

Se abre el navegador. Haz la tarea **usando los datos de la primera fila**
(la terminal te los muestra). Abajo a la derecha hay una barra:

| Botón       | Para qué sirve |
|-------------|----------------|
| Esperar     | el siguiente clic marca algo que hay que esperar antes de seguir |
| Verificar   | el siguiente clic marca un texto que debe aparecer (p.ej. "Guardado correctamente"); si no aparece, el registro se marca como error |
| Extraer     | el siguiente clic guarda un dato de la página en los resultados (p.ej. el número de expediente generado) |
| Pausar      | deja de grabar mientras haces algo que no quieres que se repita, como iniciar sesión |
| Terminar    | guarda el bot |

Al terminar, cada valor que escribiste que coincide con una celda de la
primera fila queda conectado a su columna:

```
Valores conectados a columnas del Excel:
  paso   2: 'Ana' → {{Nombre}}
  paso   6: '2024-03-15' → {{Ingreso | date:%Y-%m-%d}}
  paso   7: 'opción "Parcial"' → elemento {{Jornada}}
  paso  10: 'ana.pdf' → {{CV}}
  paso  11: 'Alta de Ana' → Alta de {{Nombre}}
```

Fíjate que también se conectan los **clics** que dependen de los datos: si
elegiste la opción "Parcial", para Luis el bot elegirá "Completa".

### 3. Comprueba y prueba

```bash
wtb show alta-empleados
```

```bash
wtb check alta-empleados --data empleados.xlsx
```

`check` revisa sin abrir el navegador que existan todas las columnas y todos
los archivos a subir.

```bash
wtb run alta-empleados --data empleados.xlsx --rows 2 --step
```

`--step` ejecuta paso a paso: resalta cada elemento y espera a que pulses Enter.

### 4. Ejecuta todo

```bash
wtb run alta-empleados --data empleados.xlsx
```

- Si un registro falla, se reintenta (`--retries`) y si sigue fallando se
  guarda una captura de pantalla y se pasa al siguiente.
- Los resultados quedan en `runs/<bot>/<fecha>/resultados.xlsx`: tus columnas
  más `_estado`, `_error`, los datos extraídos y la ruta de la captura.
- Si lo paras (Ctrl+C) o hubo errores, sigue con lo pendiente:

```bash
wtb run alta-empleados --data empleados.xlsx --resume
```

## Qué hace para no romperse

- **Varias formas de encontrar cada elemento.** Por cada clic o campo se
  guardan varios localizadores: `data-testid`, id (solo si no parece generado
  automáticamente), rol + texto accesible ("botón Guardar"), etiqueta,
  placeholder, texto y ruta CSS. Si uno deja de funcionar, prueba el siguiente.
- **Espera a que las cosas aparezcan.** Cada paso espera hasta `timeout`
  segundos a que su elemento exista y se pueda usar.
- **Espera a que la página se calme** después de cada acción: sin peticiones
  de red en curso y sin spinners o máscaras de "cargando" visibles (Bootstrap,
  Material UI, Ant Design, Element, PrimeNG, Kendo, DataTables, `aria-busy`...).
  Puedes añadir los tuyos en `busy_selectors`.
- **Campos difíciles.** Si un campo con máscara no acepta el valor de golpe,
  lo vuelve a escribir tecla por tecla.
- **Iframes, pestañas nuevas y ventanas emergentes** (`alert`/`confirm`, que
  acepta por defecto).
- **Subida de archivos** aunque el sitio use un botón propio en lugar del
  `<input type=file>`.
- **Sesión guardada.** El navegador usa un perfil propio en `.wtb/profiles/`,
  así que inicias sesión una vez a mano y el bot ya entra con sesión.
- **Contraseñas.** Si escribes una contraseña al grabar, no se guarda: queda
  como `{{secret:CONTRASENA}}` y se pide al ejecutar, o se lee de la variable
  de entorno `WTB_SECRET_CONTRASENA`.

No resuelve CAPTCHAs ni verificaciones en dos pasos: si el sitio las pide,
inicia sesión a mano antes de ejecutar.

## Retocar un bot a mano

El bot es un archivo YAML legible en `bots/<nombre>/bot.yaml` (`wtb edit
<nombre>` lo abre). Cada paso tiene una `action` y, si actúa sobre un
elemento, un `target` con su lista de localizadores `find`:

```yaml
- action: fill
  target:
    desc: campo "Nombre"
    find:
    - id: nombre
    - role: textbox
      name: Nombre
    - label: Nombre
  value: "{{Nombre | upper}}"
  timeout: 30          # opcional: más tiempo para este paso
  optional: true       # opcional: si no aparece, se salta sin error
```

Acciones disponibles: `goto`, `click`, `dblclick`, `hover`, `fill`, `type`
(tecla a tecla), `select`, `check`, `uncheck`, `upload`, `press`, `wait_for`,
`assert_text`, `extract`, `download`, `back`, `reload`, `sleep`.

Filtros para las columnas: `upper`, `lower`, `title`, `strip`, `digits`,
`int`, `date:%Y-%m-%d`, `default:texto`.

Ajustes del bot (`settings`):

| Ajuste           | Por defecto  | Qué hace |
|------------------|--------------|----------|
| `timeout`        | `20`         | segundos máximos esperando cada elemento |
| `date_format`    | `%d/%m/%Y`   | cómo se escriben las fechas del Excel |
| `files_dir`      | carpeta del Excel | dónde buscar los archivos a subir |
| `on_dialog`      | `accept`     | `accept` o `dismiss` para alert/confirm |
| `settle_timeout` | `8`          | segundos máximos esperando a que la página se calme |
| `busy_selectors` | `[]`         | selectores CSS extra de indicadores de carga |

## Desarrollo

```bash
pip install -e ".[dev]"
```

```bash
pytest
```

Los tests de punta a punta levantan un sitio de demo local
(`tests/demo_site/`) con un formulario que carga tarde, un iframe, un
`confirm()`, una subida de archivos con botón propio y un guardado asíncrono
con spinner. Graban la tarea y la reproducen para tres registros. Para
probarlo a mano:

```bash
python tests/demo_server.py
```

y graba contra `http://127.0.0.1:8899/form.html`.
