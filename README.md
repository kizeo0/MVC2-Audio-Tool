# MVC2 Audio Tool

Herramienta con interfaz gráfica para **extraer, escuchar, editar y reemplazar** las voces/audios empaquetados en los archivos `.bin` de **Marvel vs. Capcom 2**, respetando el **orden real del secuenciador del juego** (el mismo orden que se usa para el doblaje).

Pensada para proyectos de doblaje/traducción: te muestra cada línea de voz en el orden correcto, con su formato, duración y personaje, para que sepas exactamente qué archivo de audio va en cada lugar.

---

## ¿Para qué sirve?

Los archivos `pl##_voi.bin` de MVC2 guardan las voces de cada personaje en un contenedor propio (`DTPK`), con cada sample comprimido en distintos formatos (PCM 8 bit, ADPCM 4 bit propio del juego, PCM 16 bit). El orden físico en el que están guardados **no es el mismo** en el que se reproducen en el juego — ese orden real lo define el secuenciador interno del `.bin`.

Esta herramienta:

- Lee el `.bin` y usa `DTPKDump.py` para reconstruir el **orden real de reproducción** (el que necesitás para armar/organizar un guion de doblaje).
- Te deja **escuchar** cada sample directamente desde el programa.
- Te deja **reemplazar** cada sample por tu propio archivo de audio (wav, mp3, etc.), convirtiéndolo automáticamente al formato, canal y sample rate que corresponde.
- Trae un **editor de audio integrado** (estilo Audacity) para recortar, hacer fade in/out, normalizar, etc., sin salir del programa.
- Vuelve a **empaquetar** todo en un `.bin` nuevo, listo para meter de vuelta en el juego.
- Detecta automáticamente **qué personaje** es cada `.bin` y muestra su retrato.

---

## Características principales

- **Orden correcto garantizado**: la lista en pantalla, la extracción y el reempaque usan siempre el mismo orden (el del secuenciador real), no un orden inventado.
- **Tres modos de trabajo**, pensados para no romper el juego al reemplazar audio:
  - 🟢 **Libre**: podés reemplazar cualquier sample por un audio de cualquier duración, sin restricciones.
  - 🔴 **Estricto**: el audio reemplazado tiene que entrar en el mismo tamaño en bytes que el original (necesario si vas a reinyectar el `.bin` sin recalcular offsets del juego). Si tu reemplazo es más largo, el programa te abre el editor para que lo recortes antes de continuar.
  - 🟡 **Híbrido**: mezcla de los dos anteriores — marcás sample por sample (con un click en el ícono verde/rojo de cada fila) si ese en particular debe respetar el límite estricto o no.
- **Editor de audio integrado** (doble click en "Recortar..." o automáticamente cuando hace falta en modo estricto):
  - Zoom con la rueda del mouse, selección arrastrando sobre la onda.
  - Recortar a selección / eliminar selección.
  - Fade in, fade out, normalizar volumen.
  - Deshacer (Ctrl+Z), reproducir selección o todo el audio.
- **Conversión automática** de tu archivo de reemplazo: si le das un WAV estéreo y el sample original es mono, lo mezcla correctamente; si el sample rate no coincide, lo re-muestrea; soporta 8/16/24 bit de entrada.
- **Detección de personaje**: al cargar un `.bin`, el programa identifica el personaje por su nombre de archivo y muestra su retrato (carpeta `assets/characters`).
- **Marcado visual**: las filas de samples ya reemplazados o editados se pintan de verde, para ver de un vistazo qué falta y qué no.
- **Extracción cruda o a WAV**: podés sacar todos los samples del `.bin` tal cual están comprimidos, o ya decodificados a `.wav` listos para escuchar en cualquier reproductor.
- **Log** de todo lo que hace el programa, tanto en pantalla como guardado en `MVC2_AudioTool.log`.




<img width="600" height="300" alt="image" src="https://github.com/user-attachments/assets/70c784f7-51d6-408c-a789-f6eec3e08b42" />


---

## Formatos de audio que entiende

| Formato interno | Descripción |
|---|---|
| `PCM 8 bit`  | PCM sin comprimir, 8 bits por muestra. |
| `ADPCM 4 bit` (yadpcm) | Formato ADPCM propio del motor de MVC2, comprime 4:1 aprox. |
| `PCM 16 bit` | PCM sin comprimir, 16 bits por muestra. |

Como entrada para reemplazar un sample, el programa acepta cualquier `.wav` común (8/16/24 bit, mono o estéreo, cualquier sample rate) y también archivos MS-ADPCM (usando `ffmpeg`, incluido).

---

## Requisitos

- **Windows** (usa `winsound` para reproducir audio).
- Si usás el **`.exe`**: nada más, ya viene todo empaquetado adentro (no hace falta tener Python instalado).
- Si corrés el **código fuente** (`.py`):
  - Python 3.12
  - [Pillow](https://pypi.org/project/Pillow/) (`pip install pillow`)
  - Opcional: [tkinterdnd2](https://pypi.org/project/tkinterdnd2/) para poder arrastrar y soltar el `.bin` sobre la ventana (`pip install tkinterdnd2`)
  - `ffmpeg.exe` y `DTPKDump.py` en la misma carpeta que `MVC2_AudioTool.py`

---

## Uso rápido

1. Abrí `MVC2_AudioTool.exe` (o `MVC2_AudioTool.py` si corrés desde código fuente).
2. **Cargar .bin...** → elegí el `pl##_voi.bin` del personaje que quieras.
3. La lista se llena en el **orden real de reproducción**, con: número de orden, nombre de archivo, formato, tamaño, sample rate, duración y estado.
4. Elegí el **modo** (Libre / Estricto / Híbrido) según lo que necesites.
5. Seleccioná un sample y usá:
   - **Reproducir** / **Detener** para escucharlo.
   - **Cargar reemplazo...** para reemplazarlo por tu propio audio.
   - Click derecho → **Recortar...** para editarlo con el editor integrado.
   - **Extraer crudo...** para sacar todos los samples tal cual (o en `.wav`).
6. Cuando termines de reemplazar todo lo que necesitás, guardá el `.bin` nuevo para reinyectarlo en el juego.

---

## Carpetas y archivos necesarios

El programa necesita esta estructura junto a sí mismo (ya sea el `.py` o el `.exe`):

```
MVC2_AudioTool.exe (o .py)
DTPKDump.py
ffmpeg.exe
app.ico
assets/
├── verde.png
├── rojo.png
├── lain.gif
└── characters/
    ├── Ryu.png
    ├── Zangief.png
    └── ... (un .png por personaje)
```

---

## Compilar tu propio .exe

El proyecto incluye `MVC2_AudioTool.spec` y `compilar.bat` para generar un ejecutable único con [PyInstaller](https://pyinstaller.org/), con `ffmpeg.exe`, `DTPKDump.py`, el ícono y toda la carpeta `assets/` empaquetados adentro (no incluye `ffplay.exe` ni `ffprobe.exe`, no se usan).

1. Tené Python 3.12 instalado.
2. Poné `compilar.bat` junto al resto de los archivos del proyecto.
3. Doble click en `compilar.bat`.
4. El resultado queda en `dist/MVC2 Audio Tool.exe`.

---

## Notas técnicas

- El orden de reproducción se obtiene parseando el secuenciador real del `.bin` mediante `DTPKDump.py`. Si por algún motivo ese módulo no puede cargarse, el programa avisa con una ventana emergente y usa un orden de respaldo simple (que puede no coincidir con el orden real del juego).
- En modo **Estricto**, si el audio de reemplazo pesa más que el original, se abre automáticamente el editor para recortarlo antes de aceptar el reemplazo — así nunca se genera un `.bin` con un tamaño que rompa el juego.
- Los archivos reemplazados o editados quedan marcados en verde en la lista hasta que se guarde o se revierta el cambio con "Quitar reemplazo".

---

## Licencia

_(Completar según corresponda — por ejemplo, uso personal/no comercial para modding, dado que trabaja con archivos protegidos de un juego de Capcom.)_
