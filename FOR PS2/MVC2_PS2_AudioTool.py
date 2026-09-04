# -*- coding: utf-8 -*-
"""MVC2 Audio Tool - estudio de doblaje para Marvel vs Capcom 2
(PS3 NPUB30068, formato original del Dreamcast).

GUI:
- Arrastra un .bin sobre la ventana -> se CARGA en el estudio:
  * DTPK (plXX_voi.bin, se_comn.bin, se_staf.bin, se_syuk.bin):
    lista de samples reproducibles en el ORDEN REAL del secuenciador
    (grupo y track). Incluye solo samples ÚNICOS (primera aparición).
    Doble clic (o boton) los escucha.
    Con "Cargar reemplazo..." eliges un .yadpcm / .wav / .pcm8 / .pcm16
    para ese sample. "Guardar como..." escribe el .bin nuevo.
  * ADX (adx_*.bin): stream completo reproducible (decodificador ADX v3
    incluido). Reemplazo = archivo .adx entero.
   * MPEG-1 Layer 2 (s18rm04.bin...): reemplazo completo; la reproduccion
     requiere ffmpeg (no instalado).
 - Arrastra CUALQUIER OTRO .bin del juego -> la herramienta te dira que es
   (texturas, codigo, animaciones, interfaz...) y por eso NO es audio.
 - IMPORTANTE: al reemplazar audio mas largo, el .bin crece de tamano y los
   offsets se desplazan. De momento NO sabemos si la PS3 acepta .bin con
   tamano distinto al original. Al guardar con cambio de tamano aparecera un
   AVISO: prueba primero en RPCS3 y, si funciona, en consola real.

Decodificadores incluidos (sin dependencias):
  YAMAHA ADPCM 4-bit (decodificador port de Sappharad, MIT, via DTPKDump;
  codificador AICA 4-bit portado de adpcm-master de superctr)
  CRI ADX v3 (formula de vgmstream)
  PCM 8 bit (xor 0x80) y PCM 16 bit LE
  MS ADPCM (via ffmpeg externo)

Linea de comandos (modo consola):
  py -3.12 MVC2_AudioTool.py -w <archivo.bin>   decodifica todo a WAV
  py -3.12 MVC2_AudioTool.py <archivo.bin>      extraccion pura a carpeta
  py -3.12 MVC2_AudioTool.py <carpeta>          reempaqueta carpeta

Ejecutar con Python 3.12:  py -3.12 MVC2_AudioTool.py
"""

import os
import re
import sys
import json
import wave
import time
import math
import struct
import tempfile
import array
import subprocess
import shutil
import importlib.util

# Evita el parpadeo de consolas al llamar ffmpeg/wav2vag en Windows
# (en el .exe sin consola propia cada llamada abría una ventana CMD).
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

def _run(*args, **kwargs):
    kwargs.setdefault('creationflags', _NO_WINDOW)
    return subprocess.run(*args, **kwargs)

# Arranque sigiloso: si no hay consola (pythonw / exe windowed), stdout y
# stderr son None y cualquier print() reventaría el arranque. Se redirigen
# a devnull; con consola normal no cambia nada.
try:
    if sys.stdout is None or sys.stderr is None:
        _devnull = open(os.devnull, 'w')
        if sys.stdout is None:
            sys.stdout = _devnull
        if sys.stderr is None:
            sys.stderr = _devnull
except:
    pass


def _install_crash_log():
    # Los errores no capturados (incluidos segfaults vía faulthandler) se
    # anexan a MVC2_AudioTool.log junto al .py / EXE. Así, si el programa
    # se cierra solo, el log dice por qué.
    try:
        import time as _t
        import traceback as _tb
        logp = os.path.join(app_dir(), 'MVC2_AudioTool.log')
        fh = open(logp, 'a', encoding='utf-8')
        fh.write('\n--- arranque %s ---\n' % _t.strftime('%Y-%m-%d %H:%M:%S'))
        fh.flush()
        try:
            import faulthandler as _fh
            _fh.enable(file=fh)
        except:
            pass

        def _hook(t, v, tb):
            try:
                fh.write('EXCEPCION NO CAPTURADA:\n')
                _tb.print_exception(t, v, tb, file=fh)
                fh.flush()
            except:
                pass
        try:
            sys.excepthook = _hook
        except:
            pass
    except:
        pass

def _center_win(win, w, h, parent=None):
    # Centra la ventana en el padre (o pantalla) en vez de arriba a la izquierda.
    try:
        if parent is not None:
            try:
                parent.update_idletasks()
            except:
                pass
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            if pw <= 1 or ph <= 1:
                parent = None
            else:
                win.geometry(f'{w}x{h}+{max(0, px + (pw - w)//2)}+{max(0, py + (ph - h)//2)}')
                return
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f'{w}x{h}+{max(0, (sw - w)//2)}+{max(0, (sh - h)//2)}')
    except:
        pass

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, simpledialog
    import tkinter.ttk as ttk
    TK_OK = True
except Exception:
    TK_OK = False

try:
    import winsound
except ImportError:
    winsound = None

HAVE_DND = False
if TK_OK:
    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD
        HAVE_DND = True
    except ImportError:
        pass

# ======================================================================
# RUTAS: funcionan tanto ejecutando el .py como compilado a .exe (PyInstaller)
# ======================================================================

def app_dir():
    """Carpeta del .exe (modo compilado) o del .py (modo desarrollo).
    Usar para archivos que el usuario debe poder ver/editar, como el log,
    ya que en un .exe 'onefile' los recursos internos se extraen a una
    carpeta temporal que se borra al cerrar el programa."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(*parts):
    """Carpeta de recursos empaquetados dentro del .exe (assets, ffmpeg.exe,
    DTPKDump.py, app.ico). En modo compilado viven en sys._MEIPASS (carpeta
    temporal que crea PyInstaller al arrancar); en modo desarrollo, junto
    al .py."""
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *parts)


# ======================================================================
# IMPORTAR DTPKDump.py para parsear el secuenciador correctamente
# ======================================================================

def load_dtpk_module():
    dtpk_path = resource_path("DTPKDump.py")
    if not os.path.isfile(dtpk_path):
        raise ImportError(f"No se encontró DTPKDump.py en {os.path.dirname(dtpk_path)}")
    spec = importlib.util.spec_from_file_location("DTPKDump", dtpk_path)
    dtpk_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dtpk_module)
    return dtpk_module

def safe_print(msg):
    """print() normal, pero sin romper si no hay consola (el .exe se
    compila con console=False -> sys.stdout puede ser None ahi)."""
    try:
        print(msg)
    except Exception:
        pass

DTPK_LOAD_ERROR = None  # si falla, la GUI muestra esto en un aviso al abrir

try:
    DTPK = load_dtpk_module()
    HAVE_DTPK = True
    safe_print("DTPKDump.py cargado correctamente.")
except Exception as e:
    HAVE_DTPK = False
    DTPK_LOAD_ERROR = f"{type(e).__name__}: {e}"
    safe_print(f"No se pudo cargar DTPKDump.py: {e}. Se usará parseo interno.")

# ======================================================================
# CONSTANTES Y FUNCIONES DE SOPORTE
# ======================================================================

RATE16_TO_HZ = {
    0xd61d: 4000, 0xdd1e: 6000, 0xde36: 6500, 0xe008: 7000, 0xe21d: 8000,
    0xe21e: 8012, 0xe320: 8500, 0xe41f: 9000, 0xe51c: 9500, 0xe70a: 10500,
    0xe800: 11025, 0xe91e: 12000, 0xea0b: 12500, 0xea36: 13000, 0xec08: 14000,
    0xed15: 15000, 0xee1d: 16000, 0xef20: 17000, 0xf01f: 18000, 0xf11c: 19000,
    0xf214: 20000, 0xf30a: 21000, 0xf400: 22050, 0xf42f: 23000, 0xf4f6: 24000,
    0xf600: 25000, 0xf71d: 26000, 0xf800: 28000, 0xf900: 30000, 0xfa13: 32000,
    0xfb00: 34000, 0xfc1d: 35000, 0xfd15: 38000, 0xfe1d: 40000, 0xff00: 42000,
}

RATE_LOW_KEYS = [(0x0500, 49000), (0x0400, 48000), (0x0300, 47000),
                 (0x0200, 46000), (0x0100, 45000), (0x0000, 44100)]

REGION_NAMES = {
    0x20: 'Combinacion', 0x24: 'Programa', 0x28: 'Desconocido',
    0x2c: 'Secuenciador', 0x30: 'Playback', 0x34: 'ICS',
    0x38: 'Efectos', 0x3c: 'Samples',
}

EXT_BY_FORMAT = {'adpcm': 'yadpcm', 'pcm8': 'pcm8', 'pcm16': 'pcm16'}
FORMAT_LABEL = {'adpcm': 'ADPCM 4bit', 'pcm8': 'PCM 8bit', 'pcm16': 'PCM 16bit'}

FLAG_ADPCM = 0x1000000 | 0x2000000
FLAG_PCM8 = 0x800000
FLAG_PCM16 = 0

CHARACTERS = {
    'pl00': 'Ryu', 'pl01': 'Zangief', 'pl02': 'Guile', 'pl03': 'Morrigan',
    'pl04': 'Anakaris', 'pl05': 'Strider Hiryu', 'pl06': 'Cyclops',
    'pl07': 'Wolverine', 'pl08': 'Psylocke', 'pl09': 'Iceman',
    'pl0a': 'Rogue', 'pl0b': 'Captain America', 'pl0c': 'Spider-Man',
    'pl0d': 'Hulk', 'pl0e': 'Venom', 'pl0f': 'Dr. Doom',
    'pl10': 'Tron Bonne', 'pl11': 'Jill', 'pl12': 'Hayato',
    'pl13': 'Ruby Heart', 'pl14': 'Son Son', 'pl15': 'Amingo',
    'pl16': 'Marrow', 'pl17': 'Cable', 'pl18': 'Abyss 1',
    'pl19': 'Abyss 2', 'pl1a': 'Abyss 3', 'pl1b': 'Chun-Li',
    'pl1c': 'Mega Man', 'pl1d': 'Roll', 'pl1e': 'Akuma',
    'pl1f': 'BB Hood', 'pl20': 'Felicia', 'pl21': 'Charlie',
    'pl22': 'Sakura', 'pl23': 'Dan', 'pl24': 'Cammy', 'pl25': 'Dhalsim',
    'pl26': 'M. Bison', 'pl27': 'Ken', 'pl28': 'Gambit',
    'pl29': 'Juggernaut', 'pl2a': 'Storm', 'pl2b': 'Sabretooth',
    'pl2c': 'Magneto', 'pl2d': 'Shuma-Gorath', 'pl2e': 'War Machine',
    'pl2f': 'Silver Samurai', 'pl30': 'Omega Red', 'pl31': 'Spiral',
    'pl32': 'Colossus', 'pl33': 'Iron Man', 'pl34': 'Sentinel',
    'pl35': 'Blackheart', 'pl36': 'Thanos', 'pl37': 'Jin',
    'pl38': 'Captain Commando', 'pl39': 'Bonerine',
    'pl3a': 'Kobun / Servbot',
}

PREVIEW_WAV = os.path.join(tempfile.gettempdir(), 'mvc2_preview.wav')

CHARACTER_ICON_OVERRIDES = {
    'bb hood': 'Bulleta',
    'captain commando': 'Cap_Commando',
    'bonerine': 'Wolverine_Bone',
    'kobun / servbot': 'Servbot',
}

# ----------------------------------------------------------------------
# FUNCIONES DE AUDIO
# ----------------------------------------------------------------------

def _normalize_char_name(s):
    return re.sub(r'[^a-z0-9]', '', s.lower())

def translate_rate(rate16):
    if rate16 < 0x600:
        for key, hz in RATE_LOW_KEYS:
            if rate16 <= key:
                return hz
        return 44100
    if rate16 > 0xD000:
        return RATE16_TO_HZ.get(rate16, 11025)
    return None

def rate_text(rate16):
    hz = translate_rate(rate16)
    if hz:
        return '%d Hz' % hz
    return '0x%04X' % rate16

def flag_for_format(fmt):
    return {'adpcm': FLAG_ADPCM, 'pcm8': FLAG_PCM8, 'pcm16': FLAG_PCM16}[fmt]

def signed_to_unsigned(n, byte_count):
    return int.from_bytes(n.to_bytes(byte_count, 'little', signed=True),
                          'little', signed=False)

def yadpcm_to_pcm16(data):
    diff_lookup = [1, 3, 5, 7, 9, 11, 13, 15, -1, -3, -5, -7, -9, -11, -13, -15]
    index_scale = [0x0e6, 0x0e6, 0x0e6, 0x0e6, 0x133, 0x199, 0x200, 0x266]
    out = bytearray(len(data) * 4)
    dst_loc = 0
    src = 0
    cur_quant = 0x7f
    cur_sample = 0
    high_nibble = False
    while dst_loc < len(out):
        delta = (data[src] >> (4 if high_nibble else 0)) & 0xf
        x = cur_quant * diff_lookup[delta & 15]
        x = int(cur_sample + ((x + (signed_to_unsigned(x, 4) >> 29)) >> 3))
        cur_sample = min(max(x, -32768), 32767)
        cur_quant = (cur_quant * index_scale[delta & 7]) >> 8
        cur_quant = min(max(cur_quant, 0x7F), 0x6000)
        out[dst_loc] = cur_sample & 0xFF
        out[dst_loc + 1] = (cur_sample >> 8) & 0xFF
        dst_loc += 2
        cur_sample = int(cur_sample * 254 / 256)
        high_nibble = not high_nibble
        if not high_nibble:
            src += 1
    return bytes(out)

def pcm16_to_yadpcm(pcm16):
    diff_lookup = [1, 3, 5, 7, 9, 11, 13, 15, -1, -3, -5, -7, -9, -11, -13, -15]
    index_scale = [0x0e6, 0x0e6, 0x0e6, 0x0e6, 0x133, 0x199, 0x200, 0x266]
    n = len(pcm16) // 2
    out = bytearray((n + 1) // 2)
    cur_quant = 0x7f
    cur_sample = 0
    pos = 0
    buf = 0
    for i in range(n):
        s = struct.unpack_from('<h', pcm16, i * 2)[0]
        best = 0
        best_err = 1 << 62
        for a in range(16):
            x = cur_quant * diff_lookup[a]
            dk = cur_sample + ((x + (signed_to_unsigned(x, 4) >> 29)) >> 3)
            if dk > 32767:
                dk = 32767
            elif dk < -32768:
                dk = -32768
            e = dk - s
            if e < 0:
                e = -e
            if e < best_err:
                best_err = e
                best = a
                if e == 0:
                    break
        dk = cur_sample + ((cur_quant * diff_lookup[best] + (signed_to_unsigned(cur_quant * diff_lookup[best], 4) >> 29)) >> 3)
        if dk > 32767:
            dk = 32767
        elif dk < -32768:
            dk = -32768
        if i & 1:
            out[pos] = (best << 4) | buf
            pos += 1
        else:
            buf = best
        cur_quant = (cur_quant * index_scale[best & 7]) >> 8
        cur_quant = min(max(cur_quant, 0x7F), 0x6000)
        cur_sample = int(dk * 254 / 256)
    return bytes(out)

def interleave_pcm16(left, right):
    if len(left) != len(right):
        n = min(len(left), len(right))
        left, right = left[:n], right[:n]
    out = bytearray(len(left) * 2)
    out[0::2] = left
    out[1::2] = right
    return bytes(out)

def decode_sample(fmt, stereo, raw):
    if fmt == 'adpcm':
        if stereo:
            n = len(raw) // 2
            l = yadpcm_to_pcm16(raw[:n])
            r = yadpcm_to_pcm16(raw[n:])
            return interleave_pcm16(l, r)
        return yadpcm_to_pcm16(raw)
    if fmt == 'pcm8':
        mono = array.array('h', ((b if b < 128 else b - 256) << 8 for b in raw)).tobytes()
        if stereo:
            n = len(mono) // 2
            return interleave_pcm16(mono[:n], mono[n:])
        return mono
    if stereo:
        return interleave_pcm16(raw[:len(raw) // 2], raw[len(raw) // 2:])
    return raw

def write_wav(path, pcm16, rate, stereo):
    w = wave.open(path, 'wb')
    w.setnchannels(2 if stereo else 1)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(pcm16)
    w.close()

# ======================================================================
# VAG PS2 (Sony ADPCM) - decode/encode para HD/BD PS2
# ======================================================================
VAG_COEF = [
    [0.0, 0.0],
    [60.0/64.0, 0.0],
    [115.0/64.0, -52.0/64.0],
    [98.0/64.0, -55.0/64.0],
    [122.0/64.0, -60.0/64.0],
]
# Coeficientes enteros estilo hardware/SPU (vgmstream decode_psx).
# El predictor flotante acumula error de redondeo en el feedback y
# satura a ±32768 en frames fuertes (sonido fino/chillón). Con enteros
# coincide con ffmpeg/juego.
VAG_COEF_I = [
    (0, 0),
    (60, 0),
    (115, -52),
    (98, -55),
    (122, -60),
]

def vag_to_pcm16(vag_data):
    """Decodifica VAG ADPCM raw (sin header VAGp, solo frames 16B) a PCM16 mono.
    Aritmética ENTERA estilo SPU/hardware (ref: vgmstream decode_psx)."""
    out = bytearray()
    s1 = 0
    s2 = 0
    # Cada frame 16B: byte0 predict<<4|shift, byte1 flags, 14B data (28 nibbles)
    for i in range(0, len(vag_data), 16):
        if i+16 > len(vag_data):
            break
        b0 = vag_data[i]
        flags = vag_data[i+1]
        predict = b0 >> 4
        shift = b0 & 0x0F
        if predict > 4:
            predict = 0
        c0, c1 = VAG_COEF_I[predict]
        # 14 bytes -> 28 muestras (nibble bajo primero, ref vag2wav.c)
        for j in range(14):
            b = vag_data[i+2+j]
            for nibble_idx in (0, 1):
                nib = (b & 0x0F) if nibble_idx==0 else ((b>>4)&0x0F)
                # Escala con extensión de signo a 16 bits (ref vag2wav.c)
                val = (nib << 12) & 0xFFFF
                if val & 0x8000:
                    val -= 0x10000
                val >>= shift
                # Predictor entero (>>6 como el hardware; en Python >> hace
                # floor igual que el shift aritmético de C/gcc)
                s = val + ((c0 * s1 + c1 * s2) >> 6)
                if s > 32767:
                    s = 32767
                elif s < -32768:
                    s = -32768
                out += struct.pack('<h', s)
                s2 = s1
                s1 = s
        # flags 0x07 es terminador; se decodifica igual (normalmente silencio)
    return bytes(out)

def pcm16_to_vag(pcm16):
    """Codifica PCM16 mono a VAG ADPCM raw (frames 16B, sin header). Usa algoritmo wav2vag."""
    # Necesita estado persistente entre bloques
    # Port de wav2vag.c find_predict/pack
    n = len(pcm16)//2
    samples = list(struct.unpack('<' + 'h'*n, pcm16))
    out = bytearray()
    # Estados estáticos
    _s1 = 0.0
    _s2 = 0.0
    s1 = 0.0
    s2 = 0.0
    # Precomputar f
    f = VAG_COEF
    idx = 0
    while idx < n:
        block = samples[idx:idx+28]
        if len(block) < 28:
            block = block + [0]*(28-len(block))
        # find_predict
        best_predict = 0
        best_shift = 0
        # Buffer para cada predict
        min_val = 1e10
        buffer = [[0.0]*5 for _ in range(28)]
        best_d = None
        # Probar 5 predictores
        for p in range(5):
            max_abs = 0.0
            s1_tmp = _s1
            s2_tmp = _s2
            for j in range(28):
                s0 = float(block[j])
                if s0 > 30719: s0 = 30719
                if s0 < -30720: s0 = -30720
                ds = s0 + s1_tmp * f[p][0] + s2_tmp * f[p][1]
                buffer[j][p] = ds
                if abs(ds) > max_abs:
                    max_abs = abs(ds)
                s2_tmp = s1_tmp
                s1_tmp = s0
            if max_abs < min_val:
                min_val = max_abs
                best_predict = p
                best_d = [buffer[j][p] for j in range(28)]
            if min_val <= 7:
                best_predict = 0
                best_d = [buffer[j][0] for j in range(28)]
                break
        # Actualizar _s1/_s2 para siguiente bloque (con best)
        # Recalcular con best para actualizar _s1/_s2 correctamente
        s1_tmp = _s1
        s2_tmp = _s2
        for j in range(28):
            s0 = float(block[j])
            if s0 > 30719: s0 = 30719
            if s0 < -30720: s0 = -30720
            # ds ya calculado como best_d[j], pero necesitamos actualizar s1/s2
            s2_tmp = s1_tmp
            s1_tmp = s0
        _s1 = s1_tmp
        _s2 = s2_tmp
        d_samples = best_d
        # Calcular shift
        min2 = int(min_val)
        shift_mask = 0x4000
        shift = 0
        while shift < 12:
            if shift_mask & (min2 + (shift_mask>>3)):
                break
            shift += 1
            shift_mask >>= 1
        # pack
        four_bit = [0]*28
        for j in range(28):
            s0 = d_samples[j] + s1 * f[best_predict][0] + s2 * f[best_predict][1]
            ds = s0 * (1 << shift)
            di = (int(ds + 0x800) & 0xFFFFF000)
            if di & 0x80000:
                di -= 0x100000
            if di > 32767: di = 32767
            if di < -32768: di = -32768
            four_bit[j] = di & 0xFFFF
            di_shifted = di >> shift
            # arithmetic shift: Python ya hace, pero para negativos con & 0xFFFF necesitamos signo
            if di & 0x8000 and di < 0:
                # di ya es signed, shift es aritmético
                pass
            # Actualizar s1/s2 para pack
            # di_shifted es con signo
            if di_shifted & 0x8000:
                di_shifted_signed = di_shifted - 0x10000 if di_shifted >= 0x8000 else di_shifted
            else:
                di_shifted_signed = di_shifted
            # Pero di ya es signed, di>>shift es aritmético, usar di
            di_signed = di
            if di & 0x8000 and di >= 0x8000:
                # convertir a signed 16?
                pass
            s2 = s1
            s1 = float(di >> shift) - s0
            # Corrección: usar di con signo
            # Si di es negativo, di>>shift mantiene signo en Python
            s1 = float(di >> shift) - s0
            # Ajuste por signo de di
            if di >= 0x80000:
                # no
                pass
        # Crear frame 16B
        flags = 0x00
        # Si es último bloque, usaremos flag 0x01 para data y luego terminador 0x07 aparte (lo maneja el caller)
        # Aquí solo data, el caller añadirá terminador
        b0 = (best_predict << 4) | shift
        out.append(b0 & 0xFF)
        out.append(flags & 0xFF)
        for k in range(0, 28, 2):
            # four_bit[k] y [k+1] son valores con low 12 bits cero, empaquetar nibbles
            # En wav2vag: d = ((four_bit[k+1]>>8)&0xF0) | ((four_bit[k]>>12)&0x0F)
            b = ((four_bit[k+1] >> 8) & 0xF0) | ((four_bit[k] >> 12) & 0x0F)
            out.append(b & 0xFF)
        idx += 28
    return bytes(out)

def vag_to_wav_bytes(vag_raw, rate):
    pcm = vag_to_pcm16(vag_raw)
    # Crear wav en memoria
    import io
    buf = io.BytesIO()
    w = wave.open(buf, 'wb')
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(pcm)
    w.close()
    return buf.getvalue()

def decode_adx_to_pcm16(data):
    import math
    if len(data) < 0x16 or data[0:2] != b'\x80\x00':
        raise ValueError('no es un stream ADX')
    if data[4] != 3:
        raise ValueError('ADX tipo %d no soportado (solo 3)' % data[4])
    frame_size = data[5]
    if frame_size != 0x12:
        raise ValueError('frame size ADX %d no soportado' % frame_size)
    if data[6] != 4:
        raise ValueError('ADX con %d bits no soportado' % data[6])
    channels = data[7]
    if channels not in (1, 2):
        raise ValueError('ADX con %d canales no soportado' % channels)
    sample_rate = struct.unpack('>i', data[8:12])[0]
    num_samples = struct.unpack('>i', data[0x0c:0x10])[0]
    cutoff = struct.unpack('>H', data[0x10:0x12])[0]
    version = struct.unpack('>H', data[0x12:0x14])[0]
    if version not in (0x0300, 0x0400):
        raise ValueError('version ADX 0x%04X no soportada' % version)
    start_offset = struct.unpack('>H', data[0x02:0x04])[0] + 4
    if start_offset + 6 > len(data):
        raise ValueError('offset de datos ADX fuera de rango')
    z = math.cos(2.0 * math.pi * cutoff / sample_rate)
    a = math.sqrt(2.0) - z
    b = math.sqrt(2.0) - 1.0
    c = (a - math.sqrt((a + b) * (a - b))) / b
    coef1 = int(c * 8192)
    coef2 = int(c * c * -4096)
    samples_per_frame = (frame_size - 2) * 2
    total_frames = (len(data) - start_offset) // frame_size
    n_per_ch = num_samples if num_samples > 0 else total_frames * samples_per_frame
    n_per_ch = min(n_per_ch, total_frames * samples_per_frame)
    out = bytearray(n_per_ch * 2 * channels)
    filled = 0
    for f in range(total_frames):
        fr = data[start_offset + f * frame_size: start_offset + (f + 1) * frame_size]
        if len(fr) < frame_size:
            break
        ch = f % channels
        fch = f // channels
        scale = struct.unpack('>h', fr[0:2])[0] + 1
        if fr[0] == 0x80 and fr[1] == 0x01:
            scale = 0
        h1 = h2 = 0
        base = fch * samples_per_frame
        if base >= n_per_ch:
            break
        for i in range(samples_per_frame):
            if base + i >= n_per_ch:
                break
            nib = fr[2 + i // 2]
            s = (nib >> 4) if (i & 1) == 0 else (nib & 0xf)
            if s & 8:
                s -= 16
            if version == 0x0300:
                v = s * scale + ((coef1 * h1) >> 12) + ((coef2 * h2) >> 12)
            else:
                v = s * scale + ((coef1 * h1 + coef2 * h2) >> 12)
            v = min(max(v, -32768), 32767)
            oi = ((base + i) * 2 * channels) + ch * 2
            out[oi] = v & 0xFF
            out[oi + 1] = (v >> 8) & 0xFF
            h2, h1 = h1, v
        filled += 1
    return bytes(out), sample_rate, channels

def resample_pcm16(pcm16, src_rate, dst_rate):
    if src_rate == dst_rate or not pcm16:
        return pcm16
    n = len(pcm16) // 2
    src = array.array('h')
    src.frombytes(pcm16)
    ratio = dst_rate / src_rate
    out_len = int(n * ratio)
    out = array.array('h', [0] * out_len)
    for i in range(out_len):
        s = i / ratio
        i0 = int(s)
        frac = s - i0
        if i0 + 1 < n:
            out[i] = int(src[i0] * (1 - frac) + src[i0 + 1] * frac)
        else:
            out[i] = src[-1] if n else 0
    return out.tobytes()

def make_mono(pcm16):
    arr = array.array('h')
    arr.frombytes(pcm16)
    if len(arr) < 2:
        return pcm16
    if len(arr) % 2:
        arr = arr[:-1]
    left = arr[0::2]
    right = arr[1::2]
    mono = array.array('h', ((l + r) // 2 for l, r in zip(left, right)))
    return mono.tobytes()

# ======================================================================
# PARSEO DTPK USANDO DTPKDump.py (con filtro de samples únicos)
# ======================================================================

def parse_dtpk(data):
    """Retorna diccionario con entries, playback_entries y play_order (orden real, samples únicos)."""
    if data[0:4] != b'DTPK':
        return None
    if len(data) < 0x40:
        raise ValueError('archivo DTPK demasiado corto')
    soff = struct.unpack('<I', data[0x3c:0x40])[0]
    if soff == 0:
        raise ValueError('DTPK sin tabla de samples (offset 0x3C = 0)')
    if soff + 4 > len(data):
        raise ValueError('offset del chunk de samples fuera de rango')
    count = struct.unpack('<I', data[soff:soff + 4])[0] + 1
    data_start = soff + 4 + 16 * count
    if data_start > len(data):
        raise ValueError('tabla de samples truncada')
    chunk = data[soff:]
    entries = []
    data_end = data_start
    for i in range(count):
        e = chunk[4 + 16 * i: 4 + 16 * (i + 1)]
        if len(e) < 16:
            raise ValueError('tabla de samples truncada en el sample %d' % i)
        v, ls, le, ms, ln = struct.unpack('<IHHII', e)
        off = v & 0x7FFFFF
        hi = v & ~0x7FFFFF
        if hi & 0x1000000:
            fmt = 'adpcm'
        elif hi & 0x800000:
            fmt = 'pcm8'
        elif hi in (0, 0x2000000):
            fmt = 'pcm16'
        else:
            raise ValueError('sample %d con tipo desconocido (flags 0x%X)' % (i, hi))
        stereo = ms != 0
        nbytes = ln * (2 if stereo else 1)
        if nbytes == len(chunk):
            rel = 0
        else:
            rel = off - soff
        if rel + nbytes > len(chunk):
            raise ValueError('sample %d fuera de rango (offset 0x%X len 0x%X)'
                             % (i, off, nbytes))
        entries.append(dict(index=i, offset=off, flags=hi, length=ln, bytes=nbytes,
                            format=fmt, stereo=stereo, loop_start=ls, loop_end=le))
        data_end = max(data_end, soff + rel + nbytes)

    # Parsear tabla de playback (SPDs)
    playback_entries = []
    poff = struct.unpack('<I', data[0x30:0x34])[0]
    if poff and poff + 0x50 <= len(data):
        pcount = struct.unpack('<H', data[poff + 0x10:poff + 0x12])[0] + 1
        for i in range(pcount):
            e = data[poff + 0x50 + 0x40 * i: poff + 0x50 + 0x40 * (i + 1)]
            if len(e) < 0x40:
                break
            sample = e[2]
            r16 = (e[0x0a] << 8) | e[0x0b]
            playback_entries.append({'playback_id': i, 'sample': sample, 'rate16': r16})

    # Obtener play_order usando DTPKDump.py (si está disponible)
    play_order = []
    if HAVE_DTPK:
        try:
            seq_off = struct.unpack('<I', data[0x2c:0x30])[0]
            if seq_off and seq_off + 4 <= len(data):
                seq_chunk_data = data[seq_off:]
                seq_chunk = DTPK.DTPKSequencerChunk(seq_off, seq_chunk_data)
                seen_samples = set()
                for group_idx, group in enumerate(seq_chunk.SequencerGroups):
                    for track_idx, track in enumerate(group.groupItems):
                        for entry in track.entryList:
                            if DTPK.IsTrackEntry(entry.entry_type):
                                spd_id = entry.SPD_used_or_event_id
                                if spd_id < len(playback_entries):
                                    sample = playback_entries[spd_id]['sample']
                                    if sample not in seen_samples:
                                        seen_samples.add(sample)
                                        rate16 = playback_entries[spd_id]['rate16']
                                        play_order.append({
                                            'group': group_idx,
                                            'track': track_idx,
                                            'playback_id': spd_id,
                                            'sample': sample,
                                            'rate16': rate16
                                        })
            if not play_order:
                print("Advertencia: DTPKDump no generó play_order, usando fallback.")
        except Exception as e:
            print(f"Error usando DTPKDump: {e}. Usando fallback.")

    # Fallback: si no se obtuvo play_order, usar orden de playback con filtro de sample único
    if not play_order:
        seen_samples = set()
        for p in playback_entries:
            sample = p['sample']
            if sample not in seen_samples:
                seen_samples.add(sample)
                play_order.append({
                    'group': 0,
                    'track': p['playback_id'],
                    'playback_id': p['playback_id'],
                    'sample': sample,
                    'rate16': p['rate16']
                })

    return dict(soff=soff, count=count, data_start=data_start, data_end=data_end,
                entries=entries, playback=playback_entries, play_order=play_order)

# ======================================================================
# PS2 HD/BD (IECS) - contenedor .BIN con HD/BD para voces PS2
# ======================================================================
def is_ps2_container(data):
    if len(data) < 0x20:
        return False
    try:
        hd_off = struct.unpack('<I', data[0x00:0x04])[0]
        hd_sz = struct.unpack('<I', data[0x04:0x08])[0]
        bd_off = struct.unpack('<I', data[0x08:0x0C])[0]
        bd_sz = struct.unpack('<I', data[0x0C:0x10])[0]
        if hd_off+hd_sz > len(data) or bd_off+bd_sz > len(data):
            return False
        hd = data[hd_off:hd_off+hd_sz]
        if len(hd) < 0x30:
            return False
        if hd[0:8] != b'IECSsreV':
            return False
        if hd[0x10:0x18] != b'IECSdaeH':
            return False
        return True
    except:
        return False

def parse_ps2_container(data):
    hd_off = struct.unpack('<I', data[0x00:0x04])[0]
    hd_sz = struct.unpack('<I', data[0x04:0x08])[0]
    bd_off = struct.unpack('<I', data[0x08:0x0C])[0]
    bd_sz = struct.unpack('<I', data[0x0C:0x10])[0]
    hd = data[hd_off:hd_off+hd_sz]
    bd = data[bd_off:bd_off+bd_sz]
    vagi_off = struct.unpack('<I', hd[0x30:0x34])[0]
    max_idx = struct.unpack('<I', hd[vagi_off+0x0C:vagi_off+0x10])[0]
    cnt = max_idx+1
    entries = []
    for i in range(cnt):
        param_off = struct.unpack('<I', hd[vagi_off+0x10+i*4:vagi_off+0x14+i*4])[0]
        abs_param = vagi_off + param_off
        vag_off = struct.unpack('<I', hd[abs_param:abs_param+4])[0]
        vag_rate = struct.unpack('<H', hd[abs_param+0x04:abs_param+0x06])[0]
        if i < max_idx:
            next_param = struct.unpack('<I', hd[vagi_off+0x10+(i+1)*4:vagi_off+0x14+(i+1)*4])[0]
            next_vag = struct.unpack('<I', hd[vagi_off+next_param:vagi_off+next_param+4])[0]
            sz = next_vag - vag_off
        else:
            sz = bd_sz - vag_off
        entries.append(dict(index=i, offset=vag_off, size=sz, rate=vag_rate, param_off=param_off))
    return dict(hd_off=hd_off, hd_sz=hd_sz, bd_off=bd_off, bd_sz=bd_sz, hd=hd, bd=bd, vagi_off=vagi_off, max_idx=max_idx, entries=entries)

def parse_ps2_simple(data):
    # Wrapper simple para GUI: retorna entries con info para lista
    p = parse_ps2_container(data)
    # Crear play_order similar a DTPK: cada VAG es un sample, orden por índice
    play_order=[]
    for e in p['entries']:
        # Filtrar slots vacíos (80 bytes = blank)
        # Pero mostramos todos para edición
        play_order.append(dict(index=e['index'], offset=e['offset'], size=e['size'], rate=e['rate']))
    return p

LATINO_PC_CANDIDATES = [r'F:\MVC2\LATINO PC\sound\se\mvc2']

# Puentes DTPK (mismo idioma original que los VAG PS2; sirven para ordenar
# los slots por CONTENIDO, sin usar duraciones). Se busca el .bin homónimo
# con magia DTPK: primero junto al .bin PS2, luego en estas carpetas.
DTPK_BRIDGE_CANDIDATES = [
    r'C:\Users\WinterOS\Downloads\Telegram Desktop\MARVEL VS CAPCOM 2 - MOD LATINO - 1.0 - NPUB30068\MARVEL VS CAPCOM 2 - MOD LATINO - 1.0 - NPUB30068\UP0102-NPUB30068_00-MARVELVCAPCOM2FG\USRDIR\gdrom',
]

# Tracks del bloque de gruñidos repetidos: existen en el secuenciador pero
# nunca tienen take latino (verificado en los 59 pjs). No se mapean.
GRUNT_TRACKS = {40, 41, 42, 43}

# Versión del formato de caché del puente (por entrada: si cambia, se
# reconstruye ese pid). Subir al cambiar criterios de match.
BRIDGE_CACHE_V = 9


def _sig_file(path):
    # Firma por CONTENIDO (tamaño + sha1): vale entre máquinas aunque
    # cambien las fechas (sirve para compartir la caché con el .exe).
    import hashlib
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        for blk in iter(lambda: f.read(65536), b''):
            h.update(blk)
    return '%d:%s' % (os.path.getsize(path), h.hexdigest()[:16])

# Umbral de correlación por contenido (mismo idioma, distinto codec).
# Pares verdaderos dan 0.75-1.00; fondo < 0.35.
BRIDGE_MIN_SCORE = 0.60


def _load_rutas_override():
    # rutas.txt opcional junto al .py / EXE (para compartir el tool):
    #   LATINO=F:\ruta\a\sound\se\mvc2
    #   GDROM=C:\ruta\a\gdrom
    # Esas carpetas se agregan al frente de los candidatos. Si no existe,
    # no cambia nada (sirven las rutas por defecto + junto al .bin).
    try:
        seen = set()
        dirs = [os.path.dirname(os.path.abspath(__file__))]
        try:
            dirs.append(os.path.dirname(os.path.abspath(sys.executable)))
            dirs.append(sys._MEIPASS)
        except:
            pass
        dirs.append(os.getcwd())
        for d in dirs:
            if d in seen:
                continue
            seen.add(d)
            pf = os.path.join(d, 'rutas.txt')
            if not os.path.isfile(pf):
                continue
            for ln in open(pf, encoding='utf-8'):
                ln = ln.split('#', 1)[0].strip()
                if not ln or '=' not in ln:
                    continue
                k, v = ln.split('=', 1)
                k, v = k.strip().upper(), v.strip().strip('"')
                if not v or not os.path.isdir(v):
                    continue
                if k == 'LATINO' and v not in LATINO_PC_CANDIDATES:
                    LATINO_PC_CANDIDATES.insert(0, v)
                elif k == 'GDROM' and v not in DTPK_BRIDGE_CANDIDATES:
                    DTPK_BRIDGE_CANDIDATES.insert(0, v)
            break
    except:
        pass


_load_rutas_override()

def _find_latino_wavs(bin_path):
    # Lista ordenada de wavs latinos (sin blank) para el pid del bin, o [].
    try:
        base = os.path.splitext(os.path.basename(bin_path))[0]
        pid = base.split('_')[0].lower()  # pl1b
        if not pid.startswith('pl'):
            return []
        roots = list(LATINO_PC_CANDIDATES) + [os.path.dirname(os.path.abspath(bin_path))]
        for root in roots:
            for cand in (os.path.join(root, f'snd_{pid}', 'wav'),
                         os.path.join(root, 'wav')):
                if os.path.isdir(cand):
                    import glob as _g
                    wavs = sorted(_g.glob(os.path.join(cand, '*.wav')))
                    return [w for w in wavs if os.path.basename(w).lower() != 'blank.wav']
        return []
    except:
        return []

def _span_cache_file(wavdir):
    return os.path.join(wavdir, '_span_cache.json')

def _read_span_cache(wavdir):
    try:
        return json.load(open(_span_cache_file(wavdir), encoding='utf-8'))
    except:
        return {}

def _write_span_cache(wavdir, cache):
    try:
        json.dump(cache, open(_span_cache_file(wavdir), 'w', encoding='utf-8'))
    except:
        pass

def span_pcm(pcm_bytes, rate, th=500):
    # Duración de voz (sin silencios) en ms. Para comparar líneas entre idiomas.
    try:
        import array as _arr
        n = len(pcm_bytes)//2
        if n <= 0:
            return 0
        a = _arr.array('h')
        a.frombytes(pcm_bytes)
        nz = [i for i, x in enumerate(a) if abs(x) > th]
        if not nz:
            return 0
        return int((nz[-1]-nz[0])/max(1, rate)*1000)
    except:
        return 0

def _wav_span_dur(wav):
    # (span_ms|None, dur_ms|None) con caché en disco junto a los wavs.
    # El 'Duration' del header miente ~10% en MS ADPCM: se decodifica y cuenta.
    try:
        wav = os.path.abspath(wav)
        wavdir = os.path.dirname(wav)
        st = os.stat(wav)
        sig = [st.st_size, int(st.st_mtime)]
        cache = _read_span_cache(wavdir)
        ent = cache.get(os.path.basename(wav))
        if ent and ent.get('sig') == sig and 'span' in ent and 'dur' in ent:
            return ent['span'], ent['dur']
        ff = resource_path('ffmpeg.exe')
        if not os.path.isfile(ff):
            ff = shutil.which('ffmpeg') or shutil.which('ffmpeg.exe')
        if not ff:
            return None, None
        r = _run([ff, '-i', wav], capture_output=True, text=True)
        txt = (r.stderr or '') + (r.stdout or '')
        mhz = re.search(r'(\d+)\s*Hz', txt)
        sr = int(mhz.group(1)) if mhz else 48000
        with tempfile.NamedTemporaryFile(delete=False, suffix='.raw') as t:
            tp = t.name
        try:
            r2 = _run([ff, '-y', '-v', 'error', '-i', wav, '-ac', '1',
                       '-ar', str(sr), '-f', 's16le', '-acodec', 'pcm_s16le', tp],
                      capture_output=True)
            if r2.returncode != 0 or not os.path.exists(tp):
                return None, None
            raw = open(tp, 'rb').read()
            n = len(raw)//2
            if n <= 0:
                return None, None
            span, dur = span_pcm(raw, sr), int(n/sr*1000)
        finally:
            try: os.remove(tp)
            except: pass
        cache[os.path.basename(wav)] = {'sig': sig, 'span': span, 'dur': dur}
        _write_span_cache(wavdir, cache)
        return span, dur
    except:
        return None, None

def _probe_wav_ms(path):
    # Duración EXACTA del audio vía ffmpeg (decodifica y cuenta samples).
    # El 'Duration' del header miente ~10% en MS ADPCM. None si falla.
    _, dur = _wav_span_dur(path)
    return dur

def _pcm_to_list(pcm16):
    # PCM16 mono -> lista de float para correlación.
    a = array.array('h')
    a.frombytes(pcm16)
    return [float(x) for x in a]


def _corr_best_lag(xa, xb, lagw, maxn):
    # Mejor correlación normalizada en ventana de lag. Retorna (score, lag).
    n = len(xa) if len(xa) < len(xb) else len(xb)
    if maxn and n > maxn:
        n = maxn
    if n < 200:
        return 0.0, 0
    xa = xa[:n]
    xb = xb[:n]
    ma = sum(xa) / n
    mb = sum(xb) / n
    sa = 0.0
    sb = 0.0
    for i in range(n):
        da = xa[i] - ma
        db = xb[i] - mb
        sa += da * da
        sb += db * db
    if sa <= 0.0 or sb <= 0.0:
        return 0.0, 0
    den = (sa * sb) ** 0.5
    best = -2.0
    best_lag = 0
    for lag in range(-lagw, lagw + 1):
        s = 0.0
        if lag >= 0:
            upper = n - lag
            for i in range(upper):
                s += (xa[i] - ma) * (xb[i + lag] - mb)
        else:
            upper = n + lag
            for i in range(upper):
                s += (xa[i - lag] - ma) * (xb[i] - mb)
        c = s / den
        if c > best:
            best = c
            best_lag = lag
    return best, best_lag


def _corr_screen(xa, xb):
    # Cribado rápido: diezmado /4, lag ±32 (= ±128 nativo), 1600 muestras.
    xs = xa[::4]
    ys = xb[::4]
    return _corr_best_lag(xs, ys, 32, 1600)


def _corr_around(xa, xb, lag0):
    n = len(xa) if len(xa) < len(xb) else len(xb)
    if n > 9000:
        n = 9000
    if n < 200:
        return 0.0
    xa = xa[:n]
    xb = xb[:n]
    ma = sum(xa) / n
    mb = sum(xb) / n
    sa = 0.0
    sb = 0.0
    for i in range(n):
        da = xa[i] - ma
        db = xb[i] - mb
        sa += da * da
        sb += db * db
    if sa <= 0.0 or sb <= 0.0:
        return 0.0
    den = (sa * sb) ** 0.5
    best = 0.0
    for lag in range(lag0 * 4 - 12, lag0 * 4 + 13):
        s = 0.0
        if lag >= 0:
            if lag >= n:
                continue
            upper = n - lag
            for i in range(upper):
                s += (xa[i] - ma) * (xb[i + lag] - mb)
        else:
            if -lag >= n:
                continue
            upper = n + lag
            for i in range(upper):
                s += (xa[i - lag] - ma) * (xb[i] - mb)
        c = s / den
        if c > best:
            best = c
    return best


def find_dtpk_bridge(bin_path):
    # Localiza el .bin homónimo con magia DTPK (puente del mismo idioma).
    try:
        name = os.path.basename(bin_path)
        cands = [os.path.join(os.path.dirname(os.path.abspath(bin_path)), name)]
        for root in DTPK_BRIDGE_CANDIDATES:
            cands.append(os.path.join(root, name))
        for c in cands:
            try:
                if os.path.isfile(c) and os.path.abspath(c) != os.path.abspath(bin_path):
                    with open(c, 'rb') as f:
                        magic = f.read(4)
                    if magic == b'DTPK':
                        return c
            except:
                pass
        return None
    except:
        return None


def dtpk_voice_tracks(dtpk_data):
    # {track: {'samples': [s...], 'rates': set(hz), 'spds': [...]}} grupo 0,
    # lista COMPLETA (sin filtro de únicos: importan joins y compartidos).
    # El grupo 1 es espejo (mismos tracks y samples), no se usa.
    tracks = {}
    try:
        if not HAVE_DTPK or dtpk_data[0:4] != b'DTPK':
            return tracks
        seq_off = struct.unpack('<I', dtpk_data[0x2c:0x30])[0]
        poff = struct.unpack('<I', dtpk_data[0x30:0x34])[0]
        pcount = struct.unpack('<H', dtpk_data[poff + 0x10:poff + 0x12])[0] + 1
        pb = []
        for i in range(pcount):
            e = dtpk_data[poff + 0x50 + i * 0x40: poff + 0x50 + (i + 1) * 0x40]
            if len(e) < 0x40:
                break
            pb.append((e[2], (e[0x0a] << 8) | e[0x0b]))
        seq = DTPK.DTPKSequencerChunk(seq_off, dtpk_data[seq_off:])
        if not seq.SequencerGroups:
            return tracks
        grp = seq.SequencerGroups[0]
        for track_idx, track in enumerate(grp.groupItems):
            for entry in track.entryList:
                if DTPK.IsTrackEntry(entry.entry_type):
                    spd = entry.SPD_used_or_event_id
                    if spd < len(pb):
                        smp, rate16 = pb[spd]
                        hz = translate_rate(rate16)
                        ent = tracks.setdefault(track_idx, {'samples': [], 'rates': set(), 'spds': []})
                        if smp not in ent['samples']:
                            ent['samples'].append(smp)
                        if hz:
                            ent['rates'].add(hz)
                        ent['spds'].append(spd)
    except:
        pass
    return tracks


def _bridge_cache_file():
    return os.path.join(app_dir(), 'ps2_bridge_cache.json')


def _digest_file():
    return os.path.join(app_dir(), 'ps2_dtpk_digest.json')


def _digest_load():
    # Digest track->sample/tasas (independencia del DTPK). Junto al .py /
    # EXE, o empaquetado. Retorna {} si no está.
    try:
        with open(_digest_file(), encoding='utf-8') as f:
            d = json.load(f)
        if isinstance(d, dict) and d:
            return d
    except:
        pass
    try:
        pf = resource_path('ps2_dtpk_digest.json')
        if os.path.isfile(pf) and os.path.abspath(pf) != os.path.abspath(_digest_file()):
            with open(pf, encoding='utf-8') as f:
                d = json.load(f)
            if isinstance(d, dict) and d:
                return d
    except:
        pass
    return {}


def _digest_tracks(bin_path):
    # {track: {'samples':[], 'rates':[]}} del pid (claves int). {} si no hay.
    try:
        base = os.path.splitext(os.path.basename(bin_path))[0]
        pid = base.split('_')[0].lower()
        dg = _digest_load()
        ent = dg.get(pid)
        if not isinstance(ent, dict):
            return {}
        tr = ent.get('tracks', {})
        return {int(t): v for t, v in tr.items() if isinstance(v, dict)}
    except:
        return {}


def _voice_missing(bin_path, entries, track):
    # ¿La voz del track no está en ningún slot? Estructural (digest +
    # mapa): el sample no lo contiene ningún slot mapeado. Sin DTPK/PCM.
    try:
        det = bridge_detail_for_bin(bin_path)
        if not det:
            return False
        dg = _digest_tracks(bin_path)
        tinfo = dg.get(track)
        if not tinfo or len(tinfo.get('samples', [])) != 1:
            return False
        smp = tinfo['samples'][0]
        for d in det.values():
            if d.get('sample') == smp:
                return False
            if track in (d.get('tracks') or []):
                return False
        return True
    except:
        return False


_BRIDGE_DUB_WARN = {}

def _bridge_is_dubbed(bin_path, vtracks, samp_pcm):
    # ¿Este "puente" DTPK ya está DOBLADO (contiene los takes latinos)?
    # Un puente doblado arruina el match: se rechaza con aviso.
    # Solo cuenta si TODAS las voces probadas matchean (un pack real
    # puede tener takes sin doblar, que matchean legítimamente).
    try:
        tests = []
        for tnum in sorted(vtracks):
            if tnum >= 30 or tnum in GRUNT_TRACKS:
                continue
            tinfo = vtracks[tnum]
            if not tinfo['samples'] or not tinfo['rates']:
                continue
            smp = tinfo['samples'][0]
            if smp not in samp_pcm:
                continue
            lat = None
            for cand in _find_latino_wavs(bin_path):
                m = re.search(r'_%02d\.wav$' % tnum, os.path.basename(cand), re.I)
                if m:
                    lat = cand
                    break
            if lat is None:
                continue
            tests.append((tnum, smp, sorted(tinfo['rates'])[0], lat))
            if len(tests) >= 4:
                break
        if len(tests) < 3:
            return False
        hits = 0
        for tnum, smp, rate, lat in tests:
            try:
                r = _wav_to_pcm_native(lat)
                if not r:
                    continue
                wl = _pcm_to_list(resample_pcm16(r[0], r[1], rate))
                sc, lag = _corr_screen(wl, samp_pcm[smp])
                v = _corr_around(wl, samp_pcm[smp], lag) if sc >= 0.50 else sc
                if v >= 0.85:
                    hits += 1
            except:
                pass
        return hits == len(tests)
    except:
        return False

def dtpk_sample_sharing(bin_path):
    # {track: [tracks hermanos que comparten sample]}. Primero digest
    # (sin DTPK); si no hay, parseo del puente.
    # Sirve para explicar takes sin slot propio.
    sharing = {}
    try:
        dg = _digest_tracks(bin_path)
        if dg:
            by_s = {}
            for tnum, tinfo in dg.items():
                for s in tinfo.get('samples', []):
                    by_s.setdefault(s, []).append(tnum)
            for tnum, tinfo in dg.items():
                sibs = set()
                for s in tinfo.get('samples', []):
                    for t in by_s.get(s, []):
                        if t != tnum:
                            sibs.add(t)
                if sibs:
                    sharing[tnum] = sorted(sibs)
            return sharing
    except:
        pass
    try:
        bridge = find_dtpk_bridge(bin_path)
        if not bridge:
            return sharing
        data = open(bridge, 'rb').read()
        vt = dtpk_voice_tracks(data)
        by_s = {}
        for tnum, tinfo in vt.items():
            for s in tinfo['samples']:
                by_s.setdefault(s, []).append(tnum)
        for tnum, tinfo in vt.items():
            sibs = set()
            for s in tinfo['samples']:
                for t in by_s.get(s, []):
                    if t != tnum:
                        sibs.add(t)
            if sibs:
                sharing[tnum] = sorted(sibs)
    except:
        pass
    return sharing


def _bridge_cache_load():
    try:
        with open(_bridge_cache_file(), encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except:
        pass
    try:
        # EXE empaquetado (onefile): la caché puede venir dentro del bundle.
        pf = resource_path('ps2_bridge_cache.json')
        if os.path.isfile(pf) and os.path.abspath(pf) != os.path.abspath(_bridge_cache_file()):
            with open(pf, encoding='utf-8') as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
    except:
        pass
    return {}


def _bridge_cache_save(cache):
    try:
        with open(_bridge_cache_file(), 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False)
    except:
        pass


def _wav_to_pcm_native(wav_path):
    # WAV latino -> (pcm16 mono, rate). PCM directo; MS ADPCM vía ffmpeg.
    # Se usa SOLO para detectar slots ya doblados (mismo origen digital),
    # nunca para ordenar.
    try:
        with open(wav_path, 'rb') as f:
            riff = f.read(12)
            if riff[:4] != b'RIFF' or riff[8:12] != b'WAVE':
                return None
            fmt_chunk = None
            while True:
                cid = f.read(4)
                if len(cid) < 4:
                    break
                csize = struct.unpack('<I', f.read(4))[0]
                if cid == b'fmt ':
                    fmt_chunk = f.read(csize)
                    break
                f.seek(csize, 1)
        if fmt_chunk is None:
            return None
        audio_format = struct.unpack('<H', fmt_chunk[0:2])[0]
        nch = struct.unpack('<H', fmt_chunk[2:4])[0]
        sample_rate = struct.unpack('<I', fmt_chunk[4:8])[0]
        if audio_format == 2:
            ff = resource_path('ffmpeg.exe')
            if not os.path.isfile(ff):
                ff = shutil.which('ffmpeg') or shutil.which('ffmpeg.exe')
            if not ff:
                return None
            with tempfile.NamedTemporaryFile(delete=False, suffix='.raw') as t:
                tp = t.name
            try:
                r = _run([ff, '-y', '-v', 'error', '-i', wav_path, '-ac', '1',
                          '-ar', str(sample_rate), '-f', 's16le', '-acodec', 'pcm_s16le', tp],
                         capture_output=True)
                if r.returncode != 0:
                    return None
                pcm = open(tp, 'rb').read()
            finally:
                try:
                    os.remove(tp)
                except:
                    pass
            return pcm, sample_rate
        w = wave.open(wav_path, 'rb')
        nch = w.getnchannels()
        sw = w.getsampwidth()
        nf = w.getnframes()
        sr = w.getframerate()
        frames = w.readframes(nf)
        w.close()
        if sw == 1:
            pcm = b''.join(struct.pack('<h', (b - 128) << 8) for b in frames)
        elif sw == 2:
            pcm = frames
        else:
            return None
        if nch == 2:
            pcm = make_mono(pcm)
        return pcm, sr
    except:
        return None


def _rank_slot_vs_samples(sl, e_rate, samp_pcm, samp_rates, tol=0.30):
    # [(score, eff, smp)] ordenado desc. score verificado si screen>=0.30.
    # tol = tolerancia relativa del filtro de tasas (solo poda; decide audio).
    cands = []
    effs = [e_rate, e_rate // 2] if e_rate else []
    for eff in effs:
        if not eff:
            continue
        for smp in samp_pcm:
            hit = False
            for r in samp_rates.get(smp, set()):
                if r and abs(eff - r) <= max(2, int(r * tol)):
                    hit = True
                    break
            if hit:
                cands.append((eff, smp))
    scored = []
    for eff, smp in cands:
        sc, lag = _corr_screen(sl, samp_pcm[smp])
        if sc >= 0.30:
            v = _corr_around(sl, samp_pcm[smp], lag)
        else:
            v = sc
        scored.append((v, eff, smp))
    scored.sort(reverse=True)
    return scored


def _bridge_entry_to_res(ent, res):
    try:
        for k, v in ent['slots'].items():
            sr = list(v[6]) if len(v) > 6 and isinstance(v[6], list) else []
            res[int(k)] = {'sample': v[0], 'track': v[1], 'tracks': v[2],
                           'score': v[3], 'eff': v[4], 'method': v[5],
                           'srates': sr}
        return True
    except:
        return False


def build_bridge_for_ps2(bin_path, entries, bd, log=None):
    # Puente por CONTENIDO slot PS2 <-> sample DTPK (mismo idioma).
    # Retorna {slot: {'sample':, 'track': (principal), 'tracks': [...],
    #                 'score':, 'eff':, 'method': 'dtpk'|'dub'}}.
    # La caché vale por CONTENIDO del .bin: si el .bin es idéntico al de
    # la caché distribuida con el .exe, ordena sin necesitar el DTPK.
    # Sin caché ni puente retorna {}. La DURACIÓN no interviene en nada.
    if log is None:
        def log(m):
            pass
    res = {}
    try:
        base = os.path.splitext(os.path.basename(bin_path))[0]
        pid = base.split('_')[0].lower()
        if not pid.startswith('pl'):
            return res
        try:
            sig_bin = f'{pid}|{_sig_file(bin_path)}'
        except:
            sig_bin = None
        bridge = find_dtpk_bridge(bin_path)
        cache = _bridge_cache_load()
        if bridge:
            try:
                sig = f'{sig_bin}|{_sig_file(bridge)}'
            except:
                try:
                    bst = os.stat(bridge)
                    dst = os.stat(bin_path)
                    sig = f'{pid}|{dst.st_size}|{dst.st_mtime_ns}|{bst.st_size}|{bst.st_mtime_ns}'
                except:
                    sig = None
            if sig:
                ent = cache.get(sig)
                if isinstance(ent, dict) and ent.get('v') == BRIDGE_CACHE_V and ent.get('slots'):
                    if _bridge_entry_to_res(ent, res):
                        return res
        elif sig_bin:
            # Sin DTPK local: buscar entrada compatible con este .bin
            # (caché distribuida con el .exe). Mismo contenido = mismo mapa.
            prefix = sig_bin + '|'
            for k, ent in cache.items():
                if not isinstance(ent, dict) or ent.get('v') != BRIDGE_CACHE_V or not ent.get('slots'):
                    continue
                if isinstance(k, str) and k.startswith(prefix):
                    if _bridge_entry_to_res(ent, res):
                        log('Mapa desde caché distribuida (sin DTPK local).')
                        return res
            log('Puente DTPK no encontrado (se usa solo lo verificado a oido).')
            return res
        else:
            return res
        if not bridge:
            return res
        dtpk_data = open(bridge, 'rb').read()
        vtracks = dtpk_voice_tracks(dtpk_data)
        if not vtracks:
            return res
        # samples del DTPK decodificados una vez
        p = parse_dtpk(dtpk_data)
        by_idx = {e['index']: e for e in p['entries']}
        samp_pcm = {}
        samp_rates = {}
        for tnum, tinfo in vtracks.items():
            for s in tinfo['samples']:
                samp_rates.setdefault(s, set()).update(tinfo['rates'])
                if s in samp_pcm or s not in by_idx:
                    continue
                e = by_idx[s]
                rel = 0 if e['bytes'] == len(dtpk_data) - p['soff'] else e['offset'] - p['soff']
                raw = dtpk_data[p['soff'] + rel: p['soff'] + rel + e['bytes']]
                try:
                    pcm = decode_sample(e['format'], e['stereo'], raw)
                except:
                    continue
                if e['stereo']:
                    try:
                        pcm = make_mono(pcm)
                    except:
                        pass
                samp_pcm[s] = _pcm_to_list(pcm)
        # Guardia: si el "puente" ya está doblado, rechazarlo (arruinaría
        # el match). Solo fijos a oído en ese caso.
        try:
            if _bridge_is_dubbed(bin_path, vtracks, samp_pcm):
                _BRIDGE_DUB_WARN[os.path.abspath(bin_path)] = True
                log('AVISO: el DTPK puente parece DOBLADO (trae los takes latinos), no original.')
                log('Se ignora el puente: solo pares verificados a oido. Usa DTPKs originales (DC/NAOMI/PS3 vanilla).')
                return res
        except:
            pass
        # sample -> tracks (para colisiones y joins)
        sample_tracks = {}
        for tnum, tinfo in vtracks.items():
            for s in tinfo['samples']:
                sample_tracks.setdefault(s, []).append(tnum)
        slot_list = [e['index'] for e in entries if e['size'] > 200]
        by_e = {e['index']: e for e in entries}
        total = len(slot_list)
        for pos, s in enumerate(slot_list):
            e = by_e[s]
            raw = bd[e['offset']:e['offset'] + e['size']]
            if len(raw) >= 16 and raw[-16:] == bytes.fromhex('00 07 77 77 77 77 77 77 77 77 77 77 77 77 77 77'):
                raw = raw[:-16]
            try:
                sl = _pcm_to_list(vag_to_pcm16(raw))
            except:
                continue
            scored = _rank_slot_vs_samples(sl, e['rate'], samp_pcm, samp_rates)
            best = (0.0, None, None, 0)
            for v, eff, smp in scored[:3]:
                if v > best[0]:
                    best = (v, eff, smp, 0)
            if best[1] is not None and best[0] >= BRIDGE_MIN_SCORE:
                smp = best[2]
                trks = [t for t in sample_tracks.get(smp, []) if t not in GRUNT_TRACKS]
                if not trks:
                    continue
                res[s] = {'sample': smp, 'track': trks[0], 'tracks': trks,
                          'score': round(best[0], 3), 'eff': best[1], 'method': 'dtpk',
                          'srates': sorted(samp_rates.get(smp, set()))}
                continue
            # 2ª oportunidad: el port a veces retima el pitch (ej slot a
            # 8000 de un SPD 11025). Se lleva el slot a la tasa del SPD
            # (solo ratios suaves 0.7-1.4) y se exige score alto.
            try:
                retry = []
                for eff in (e['rate'], e['rate'] // 2):
                    if not eff:
                        continue
                    for smp, spcm in samp_pcm.items():
                        for r in samp_rates.get(smp, set()):
                            if not r:
                                continue
                            ratio = r / eff
                            if 0.7 <= ratio <= 1.4 and abs(ratio - 1.0) > 0.04:
                                retry.append((eff, smp, r))
                best2 = (0.0, None, None, None)
                for eff, smp, r in retry:
                    try:
                        slr = _pcm_to_list(resample_pcm16(vag_to_pcm16(raw), eff, r))
                    except:
                        continue
                    sc, lag = _corr_screen(slr, samp_pcm[smp])
                    if sc >= 0.45:
                        v = _corr_around(slr, samp_pcm[smp], lag)
                        if v > best2[0]:
                            best2 = (v, eff, smp, r)
                if best2[1] is not None and best2[0] >= 0.80:
                    smp = best2[2]
                    trks = [t for t in sample_tracks.get(smp, []) if t not in GRUNT_TRACKS]
                    if trks:
                        res[s] = {'sample': smp, 'track': trks[0], 'tracks': trks,
                                  'score': round(best2[0], 3), 'eff': best2[3],
                                  'method': 'dtpk',
                                  'srates': sorted(samp_rates.get(smp, set()))}
            except:
                pass
            if log and (pos % 5 == 0 or pos + 1 == total):
                log(f'  puente DTPK: slot {s:02d} ({pos + 1}/{total})')
        # Fallback: slots sin match original -> ¿ya doblados? (mismo origen
        # digital que el take latino: MS ADPCM -> VAG). Se lleva el take a
        # la tasa del slot y se compara por contenido a resolución nativa.
        # Solo se decodifican los takes si queda algún slot sin match.
        pending = [s for s in slot_list if s not in res]
        wpcm = {}
        if pending:
            for w in _find_latino_wavs(bin_path):
                r = _wav_to_pcm_native(w)
                if r:
                    wpcm[w] = r
        for s in pending:
            e = by_e[s]
            raw = bd[e['offset']:e['offset'] + e['size']]
            if len(raw) >= 16 and raw[-16:] == bytes.fromhex('00 07 77 77 77 77 77 77 77 77 77 77 77 77 77 77'):
                raw = raw[:-16]
            try:
                sl = _pcm_to_list(vag_to_pcm16(raw))
            except:
                continue
            best = (0.0, None, None)
            for eff in (e['rate'], e['rate'] // 2):
                if not eff:
                    continue
                for w, (wraw, wrate) in wpcm.items():
                    try:
                        wl = _pcm_to_list(resample_pcm16(wraw, wrate, eff)) if wrate != eff else _pcm_to_list(wraw)
                    except:
                        continue
                    sc, lag = _corr_screen(sl, wl)
                    if sc >= 0.45:
                        v = _corr_around(sl, wl, lag)
                        if v > best[0]:
                            best = (v, w, eff)
            if best[1] is not None and best[0] >= 0.70:
                m = re.search(r'_(\d+)\.wav$', os.path.basename(best[1]), re.I)
                if m:
                    tnum = int(m.group(1))
                    sib = [tnum]
                    take_rates = set()
                    try:
                        ti = vtracks.get(tnum)
                        if ti:
                            for s2 in ti['samples']:
                                take_rates.update(samp_rates.get(s2, set()))
                                for t2 in sample_tracks.get(s2, []):
                                    if t2 not in sib:
                                        sib.append(t2)
                    except:
                        pass
                    res[s] = {'sample': None, 'track': tnum, 'tracks': sorted(sib),
                              'score': round(best[0], 3), 'eff': best[2], 'method': 'dub',
                              'srates': sorted(take_rates)}
        # Completado 1:1: si queda UN slot libre y UN take sin cubrir (teniendo
        # en cuenta también los fijos a oído), se emparejan por contenido
        # (sin filtro de tasas) con compuertas estrictas; si no, queda para
        # revisión a oído. Sin duraciones.
        try:
            by_num = {}
            for w in _find_latino_wavs(bin_path):
                m = re.search(r'_(\d+)\.wav$', os.path.basename(w), re.I)
                if m:
                    by_num[int(m.group(1))] = w
            covered = set()
            for b in res.values():
                for t in b.get('tracks', []) or []:
                    covered.add(t)
            fix_cov = _load_map_fix()
            fix_slots = set()
            for (fpid, fs) in fix_cov:
                if fpid != pid:
                    continue
                fix_slots.add(fs)
                m2 = re.search(r'_(\d+)\.wav$', fix_cov[(fpid, fs)], re.I)
                if m2:
                    covered.add(int(m2.group(1)))
            free_slots = [s for s in slot_list if s not in res and s not in fix_slots]
            missing = sorted(t for t in by_num if t not in covered)
            if len(free_slots) == 1 and len(missing) == 1:
                s = free_slots[0]
                tnum = missing[0]
                tinfo = vtracks.get(tnum)
                if tinfo and len(tinfo['samples']) == 1:
                    smp = tinfo['samples'][0]
                    e = by_e[s]
                    raw = bd[e['offset']:e['offset'] + e['size']]
                    if len(raw) >= 16 and raw[-16:] == bytes.fromhex('00 07 77 77 77 77 77 77 77 77 77 77 77 77 77 77'):
                        raw = raw[:-16]
                    sl = _pcm_to_list(vag_to_pcm16(raw))
                    ranked = _rank_slot_vs_samples(sl, e['rate'], samp_pcm, samp_rates, tol=10.0)
                    top = [x for x in ranked if x[2] == smp]
                    rest = [x for x in ranked if x[2] != smp]
                    if top and rest:
                        sc = top[0][0]
                        margin = sc - rest[0][0]
                        if sc >= BRIDGE_MIN_SCORE:
                            meth = 'dtpk'
                        elif sc >= 0.30 and margin >= 0.15:
                            meth = 'dudoso'
                        else:
                            meth = None
                        if meth:
                            res[s] = {'sample': smp, 'track': tnum, 'tracks': [tnum],
                                      'score': round(sc, 3), 'eff': top[0][1], 'method': meth,
                                      'srates': sorted(samp_rates.get(smp, set()))}
                            log(f'  puente DTPK: slot {s:02d} <- take {tnum:02d} [{meth}, completado 1:1]')
        except:
            pass
        cache[sig] = {'v': BRIDGE_CACHE_V,
                        'slots': {str(k): [v['sample'], v['track'], v['tracks'], v['score'], v['eff'], v['method'],
                                           v.get('srates', [])]
                                for k, v in res.items()}}
        _bridge_cache_save(cache)
    except Exception as ex:
        try:
            log(f'Puente DTPK falló: {ex}')
        except:
            pass
    return res

def latino_map_lines(bin_path, entries):
    """Líneas 'slot -> wav latino' con duraciones para _orden_latino.txt.
    El número del wav es el número de track DTPK (como en PS3); el slot PS2
    que lo contiene se halla por CONTENIDO (puente DTPK), nunca por duración.
    La duración solo indica si el take cabe (OK / 1/2 TASA / EXCEDE)."""
    wavs = _find_latino_wavs(bin_path)
    lines = []
    lines.append('Mapa slot PS2 -> wav latino (ORDEN POR VOZ: n. wav = n. track DTPK).')
    lines.append('Cada fila: voz <- slot que la contiene. Sin blanks.')
    lines.append('')
    if not wavs:
        lines.append('Sin carpeta latina encontrada (se buscó snd_<pid>\\wav).')
        return lines
    # Calentar caché (mide spans+duraciones una vez; próximas veces es instantáneo)
    for w in wavs:
        _wav_span_dur(w)
    mp = latino_map_for_ps2(bin_path, entries)
    if not mp:
        lines.append('Sin mapeo posible.')
        try:
            if find_dtpk_bridge(bin_path) is None:
                lines.append('No se halló el puente DTPK homónimo (misma carpeta o carpeta gdrom).')
        except:
            pass
        return lines
    try:
        _fix = _load_map_fix()
        _pid = os.path.splitext(os.path.basename(bin_path))[0].split('_')[0].lower()
    except:
        _fix, _pid = {}, ''
    det = bridge_detail_for_bin(bin_path)
    try:
        _br = find_dtpk_bridge(bin_path)
        lines.append(f'Puente DTPK: {os.path.basename(_br) if _br else "NO ENCONTRADO"}.')
        try:
            if _BRIDGE_DUB_WARN.get(os.path.abspath(bin_path)):
                lines.append('PUENTE RECHAZADO: parece DOBLADO (no original). Solo fijos a oido.')
        except:
            pass
        lines.append('')
    except:
        pass
    def _vnum(w):
        import re as _re
        m = _re.search(r'_(\d+)\.wav$', os.path.basename(w))
        return int(m.group(1)) if m else 9999
    # Orden POR VOZ (nº wav = nº track DTPK = orden del juego), no por slot.
    for s in sorted(mp, key=lambda x: (_vnum(mp[x]), x)):
        e = next((x for x in entries if x['index'] == s), None)
        _sp, dur = _wav_span_dur(mp[s])
        fit = _slot_fit(e, dur or 0)
        half = bool(_strong_half(e, dur or 0))
        cap_ms = _slot_cap_ms(e, half or fit == 'half')
        sw = ''
        d = det.get(s, {})
        meth = d.get('method')
        if (_pid, s) in _fix:
            sw = ' [FIJO: verificado a oido]' if _MAP_FIX_SRC.get((_pid, s)) == 'man' else ' [FIJO]'
        elif meth == 'dtpk':
            extra = [t for t in d.get('tracks', []) if t != d.get('track')]
            sw = f" [puente DTPK: track {d.get('track'):02d}, sample {d.get('sample')}, corr {d.get('score')}]"
            if extra:
                sw += f" [COMPARTE slot con takes {', '.join('%02d' % t for t in sorted(extra))}: elegir a oido]"
        elif meth == 'dub':
            sw = f" [DOBLADO: el slot ya trae este take (corr {d.get('score')})]"
        elif meth == 'dudoso':
            sw = f" [DUDOSO: corr {d.get('score')}, verificar a oido y fijar en ps2_map_fix.txt]"
        if half:
            sw += ' [1/2 TASA: solo cabe si el juego divide]'
        if dur is None or fit is None:
            lines.append(f"  slot {s:02d} ({e['rate'] if e else '?'}Hz, cap ~{cap_ms}ms) <- {os.path.basename(mp[s])}{sw}")
        elif fit == 'full':
            lines.append(f"  slot {s:02d} ({e['rate'] if e else '?'}Hz, cap ~{cap_ms}ms) <- {os.path.basename(mp[s])} (~{dur}ms) [OK]{sw}")
        elif fit == 'half' and half:
            lines.append(f"  slot {s:02d} ({e['rate'] if e else '?'}Hz, cap ~{cap_ms}ms) <- {os.path.basename(mp[s])} (~{dur}ms) [OK]{sw}")
        elif fit == 'half':
            lines.append(f"  slot {s:02d} ({e['rate'] if e else '?'}Hz, cap ~{cap_ms}ms) <- {os.path.basename(mp[s])} (~{dur}ms) [EXCEDE justo: bajar tasa o recortar]{sw}")
        else:
            lines.append(f"  slot {s:02d} ({e['rate'] if e else '?'}Hz, cap ~{cap_ms}ms) <- {os.path.basename(mp[s])} (~{dur}ms) [EXCEDE ni a mitad: bajar tasa o recortar]{sw}")
    # Sobrantes y huecos: lo que no se mapeó hay que revisarlo a oído uno a uno
    wavs = _find_latino_wavs(bin_path)
    slots = [e['index'] for e in entries if e['size'] > 200]
    mapped_wavs = set(mp.values())
    extra_wavs = [w for w in wavs if w not in mapped_wavs]
    free_slots = [s for s in slots if s not in mp]
    if extra_wavs:
        lines.append('')
        lines.append(f"SOBRAN {len(extra_wavs)} wavs (sin slot asignado):")
        try:
            _share = dtpk_sample_sharing(bin_path)
            _slot_of_take = {}
            for _s, _w in mp.items():
                _m = re.search(r'_(\d+)\.wav$', os.path.basename(_w), re.I)
                if _m:
                    _slot_of_take[int(_m.group(1))] = _s
        except:
            _share, _slot_of_take = {}, {}
        for w in extra_wavs:
            _m = re.search(r'_(\d+)\.wav$', os.path.basename(w), re.I)
            _t = int(_m.group(1)) if _m else None
            _sibs = _share.get(_t, []) if _t is not None else []
            _home = [f"take {_b:02d}->slot {_slot_of_take[_b]:02d}" for _b in _sibs if _b in _slot_of_take]
            if _home:
                lines.append(f"  + {os.path.basename(w)} [COMPARTE sample/slot con {', '.join(_home)}: mismo slot para ambos]")
            elif _t is not None and _t not in _slot_of_take and not free_slots and _voice_missing(bin_path, entries, _t):
                lines.append(f"  + {os.path.basename(w)} [SIN SLOT: la voz no está en este .bin PS2 (revisar original/DTPK)]")
            else:
                lines.append(f"  + {os.path.basename(w)} [revisar a oido]")
    if free_slots:
        lines.append('')
        lines.append(f"Slots SIN wav ({len(free_slots)}, se quedan originales):")
        for s in free_slots:
            lines.append(f"  - slot {s:02d}")
    return lines

# Pares verificados a oído {(pid, slot): wav_basename}. Mandan sobre todo.
# Ej pl04: slot11<-46, slot12<-45, slot13<-47 (mismo contenido que el puente).
PS2_MAP_FIX = {
    ('pl04', 11): 'mvc2_pl04_46.wav',
    ('pl04', 12): 'mvc2_pl04_45.wav',
    ('pl04', 13): 'mvc2_pl04_47.wav',
}

_MAP_FIX_SRC = {}
def _load_map_div():
    # {pid: {slot: bool}} División por slot (ps2_map_div.txt, puente DTPK).
    # True = el juego divide (dato a mitad). Se busca junto al .py / EXE / cwd.
    div = {}
    try:
        dirs = [os.path.dirname(os.path.abspath(__file__))]
        try:
            dirs.append(os.path.dirname(os.path.abspath(sys.executable)))
            dirs.append(sys._MEIPASS)
        except:
            pass
        dirs.append(os.getcwd())
        for d in dirs:
            pf = os.path.join(d, 'ps2_map_div.txt')
            if not os.path.isfile(pf):
                continue
            for ln in open(pf, encoding='utf-8'):
                ln = ln.split('#', 1)[0].strip()
                if not ln or '=' not in ln or ':' not in ln:
                    continue
                try:
                    left, val = ln.split('=', 1)
                    pid, slot = left.split(':', 1)
                    div.setdefault(pid.strip().lower(), {})[int(slot.strip())] = (val.strip() == '1')
                except:
                    pass
            break
    except:
        pass
    return div

_MAP_DIV = None
def _slot_divided(bin_path, slot):
    # True/False según ps2_map_div.txt; None si no hay dato.
    global _MAP_DIV
    try:
        if _MAP_DIV is None:
            _MAP_DIV = _load_map_div()
        pid = os.path.splitext(os.path.basename(bin_path))[0].split('_')[0].lower()
        return _MAP_DIV.get(pid, {}).get(slot)
    except:
        return None

def _hd_is_half_family(hd_rate):
    # Heurística histórica: HD 18000/24000 (±3%) se tocan a mitad.
    # Solo último recurso (sin div ni puente).
    try:
        if not hd_rate:
            return False
        if hd_rate in (18000, 24000):
            return True
        if abs(hd_rate - 18000) / 18000 < 0.03:
            return True
        if abs(hd_rate - 24000) / 24000 < 0.03:
            return True
        return False
    except:
        return False

def _rates_match(hd_rate, spd_rate, multiple=1, tol=0.15):
    try:
        if not hd_rate or not spd_rate:
            return False
        return abs(hd_rate - multiple * spd_rate) <= max(2, int(multiple * spd_rate * tol))
    except:
        return False

def _ps2_is_divided(bin_path, slot, hd_rate, sample_rates=None):
    # ¿El juego divide este slot (toca a HD/2)? Orden de evidencia:
    # 1) ps2_map_div.txt (dato explícito), 2) puente (HD ≈ 2×SPD del
    # sample), 3) heurística histórica. Retorna True/False/None.
    try:
        div = _slot_divided(bin_path, slot)
    except:
        div = None
    if div is not None:
        return div
    try:
        if sample_rates:
            half = any(_rates_match(hd_rate, r, 2) for r in sample_rates)
            full = any(_rates_match(hd_rate, r, 1) for r in sample_rates)
            if half and not full:
                return True
            if full and not half:
                return False
    except:
        pass
    return None

def _ps2_true_rate(bin_path, slot, hd_rate, sample_rates=None):
    # Tasa REAL a la que el juego toca el slot: HD/2 si divide, HD si no.
    # Sin esto las voces divididas suenan a ardilla (ej pl02: HD 16000,
    # juego a 8000). Sin evidencia, heurística (comportamiento heredado).
    try:
        div = _ps2_is_divided(bin_path, slot, hd_rate, sample_rates)
    except:
        div = None
    if div is None:
        div = _hd_is_half_family(hd_rate)
    try:
        return max(1, hd_rate // 2) if div else max(1, hd_rate)
    except:
        return hd_rate

def _load_map_fix():
    # {(pid, slot): wav_basename}. Defaults + manual verificado.
    # Gana el manual (ps2_map_fix.txt). Formato: pl04:11=46.
    # Sin auto-matching: cada línea es un par probado (oído o contenido).
    global _MAP_FIX_SRC
    fix = dict(PS2_MAP_FIX)
    _MAP_FIX_SRC = {k: 'def' for k in fix}
    try:
        dirs = [os.path.dirname(os.path.abspath(__file__))]
        try:  # frozen: junto al EXE / bundle
            dirs.append(os.path.dirname(os.path.abspath(sys.executable)))
            dirs.append(sys._MEIPASS)
        except:
            pass
        dirs.append(os.getcwd())
        for fn, src in (('ps2_map_fix.txt', 'man'),):
            for d in dirs:
                pf = os.path.join(d, fn)
                if not os.path.isfile(pf):
                    continue
                for ln in open(pf, encoding='utf-8'):
                    ln = ln.split('#', 1)[0].strip()
                    if not ln or '=' not in ln or ':' not in ln:
                        continue
                    left, num = ln.split('=', 1)
                    pid, slot = left.split(':', 1)
                    pid, slot, num = pid.strip().lower(), int(slot.strip()), num.strip()
                    if pid.startswith('pl'):
                        fix[(pid, slot)] = f'mvc2_{pid}_{int(num):02d}.wav'
                        _MAP_FIX_SRC[(pid, slot)] = src
                break
    except:
        pass
    return fix

def _slot_cap_ms(entry, half=False):
    # Capacidad del slot en ms (half=True: a mitad de tasa -> x2).
    try:
        if not entry or entry['size'] <= 16:
            return 0
        c = int(((entry['size']-16)//16*28 / max(1, entry['rate']) * 1000))
        return c*2 if half else c
    except:
        return 0

def _need_bytes(dur_ms, rate):
    # Bytes VAG que ocupa dur_ms a rate (misma fórmula que el diálogo de reemplazo).
    try:
        return -(-int(dur_ms*rate/1000)//28)*16 if dur_ms and rate else None
    except:
        return None

def _slot_fit(entry, dur_ms):
    # 'full': cabe a tasa HD. 'half': solo cabe si el juego divide (dato a mitad).
    # 'no': no cabe ni a mitad (pedirá bajar tasa o recortar). None si no se sabe.
    # La duración NUNCA reordena: solo dice si cabe (una traducción corta cabe igual).
    try:
        if not entry or entry['size'] <= 16 or not dur_ms:
            return None
        cap = entry['size']-16
        if _need_bytes(dur_ms, entry['rate']) <= cap:
            return 'full'
        # A mitad: la mitad de samples por ms -> bloques de 56 en la fórmula.
        if -(-int(dur_ms*entry['rate']/1000)//56)*16 <= cap:
            return 'half'
        return 'no'
    except:
        return None

def _strong_half(entry, dur_ms):
    # División CIERTA: el take desborda a tasa HD más allá del redondeo de
    # bloque (>64B) y cabe a mitad. Si no, el take no podría sonar bien.
    try:
        if _slot_fit(entry, dur_ms) != 'half':
            return False
        cap = entry['size']-16
        return (_need_bytes(dur_ms, entry['rate']) or 0) - cap > 64
    except:
        return False

def _is_half_rate(entry, dur_ms):
    return _strong_half(entry, dur_ms)

def latino_map_for_ps2(bin_path, entries):
    """Mapea slot_idx -> wav latino.
    1) pares verificados a oido (ps2_map_fix.txt + PS2_MAP_FIX),
    2) puente DTPK por CONTENIDO (mismo idioma original): slot -> sample
       -> track -> wav (el n. de wav es el n. de track, como en PS3).
    La DURACION no ordena ni decide nada: solo se muestra si el take cabe.
    Retorna {} si no halla la carpeta latina."""
    try:
        base = os.path.splitext(os.path.basename(bin_path))[0]
        pid = base.split('_')[0].lower()  # pl1b
        if not pid.startswith('pl'):
            return {}
        wavs = _find_latino_wavs(bin_path)
        if not wavs:
            return {}
        by_name = {os.path.basename(w): w for w in wavs}
        by_num = {}
        for w in wavs:
            m = re.search(r'_(\d+)\.wav$', os.path.basename(w), re.I)
            if m:
                by_num[int(m.group(1))] = w
        merged = {}
        detail = {}
        # 2) puente DTPK por contenido (se calcula primero para tener
        # sample/tracks incluso en slots con fijo a oido)
        bridge = {}
        try:
            data = open(bin_path, 'rb').read()
            bd = None
            if is_ps2_container(data):
                bd = data[struct.unpack('<I', data[0x08:0x0C])[0]:
                           struct.unpack('<I', data[0x08:0x0C])[0] + struct.unpack('<I', data[0x0C:0x10])[0]]
        except:
            bd = None
        if bd is not None:
            try:
                bridge = build_bridge_for_ps2(bin_path, entries, bd)
            except:
                bridge = {}
        # 1) fijos verificados a oido (mandan sobre el puente)
        fix = _load_map_fix()
        for s in [e['index'] for e in entries if e['size'] > 200]:
            key = (pid, s)
            if key in fix and fix[key] in by_name:
                merged[s] = by_name[fix[key]]
                m = re.search(r'_(\d+)\.wav$', fix[key], re.I)
                tnum = int(m.group(1)) if m else None
                b = bridge.get(s, {})
                detail[s] = {'track': tnum, 'sample': b.get('sample'),
                             'score': 1.0, 'method': 'fijo',
                             'tracks': b.get('tracks', [tnum] if tnum is not None else [])}
        # resto del puente
        for s, b in bridge.items():
            if s in merged:
                continue
            tnum = b.get('track')
            if tnum is not None and tnum in by_num:
                merged[s] = by_num[tnum]
                detail[s] = {'track': tnum, 'sample': b.get('sample'),
                             'score': b.get('score'), 'method': b.get('method'),
                             'tracks': b.get('tracks', [tnum])}
        _LAST_BRIDGE['bin'] = os.path.abspath(bin_path)
        _LAST_BRIDGE['detail'] = detail
        return merged
    except:
        return {}


_LAST_BRIDGE = {'bin': None, 'detail': {}}


def bridge_detail_for_bin(bin_path):
    # Detalle del último puente calculado para este .bin (método por slot).
    try:
        if _LAST_BRIDGE.get('bin') == os.path.abspath(bin_path):
            return _LAST_BRIDGE.get('detail', {})
    except:
        pass
    return {}

def extract_ps2(path, outdir, log):
    data = open(path, 'rb').read()
    p = parse_ps2_container(data)
    os.makedirs(outdir, exist_ok=True)
    # Guardar HD y BD separados + manifest
    open(os.path.join(outdir, '_hd.bin'), 'wb').write(p['hd'])
    open(os.path.join(outdir, '_bd.bin'), 'wb').write(p['bd'])
    # Guardar container header 0x20
    open(os.path.join(outdir, '_container_header.bin'), 'wb').write(data[:0x20])
    base_name = os.path.splitext(os.path.basename(path))[0]
    try:
        vmap = latino_map_for_ps2(path, p['entries'])
    except:
        vmap = {}
    for e in p['entries']:
        idx = e['index']
        rate = e['rate']
        # Extraer VAG raw del BD
        raw = p['bd'][e['offset']:e['offset']+e['size']]
        # Guardar como .vag con header VAGp para compatibilidad.
        # Tasa de cabecera = HD//2 como ps2-bankmod (el juego divide al
        # reproducir; así suenan bien en players PC y el reimporte conserva HD).
        header = bytearray(0x30)
        header[0:4]=b'VAGp'
        struct.pack_into('>I', header, 0x04, 0x20)
        struct.pack_into('>I', header, 0x0C, len(raw))
        struct.pack_into('>I', header, 0x10, max(1, rate//2))
        voz = _voz_of_latino(vmap[idx]) if idx in vmap else None
        if voz is not None:
            latbase = os.path.splitext(os.path.basename(vmap[idx]))[0]
            fname = f"V{voz:02d}_{latbase}_slot{idx:02d}.VAG"
        else:
            fname = f"{base_name}_{idx:02d}_Rate_{rate:04d}.VAG"
        open(os.path.join(outdir, fname), 'wb').write(header + raw)
        # Copia compatible con ps2-bankmod (lee el índice del número final: 00.VAG, 01.VAG...)
        try:
            open(os.path.join(outdir, f"{idx:02d}.VAG"), 'wb').write(header + raw)
        except:
            pass
    # Manifest
    manifest = dict(type='ps2hd', version=1, source=os.path.basename(path),
                    hd_off=p['hd_off'], hd_sz=p['hd_sz'], bd_off=p['bd_off'], bd_sz=p['bd_sz'],
                    vagi_off=p['vagi_off'], max_idx=p['max_idx'],
                    samples=[dict(index=e['index'], rate=e['rate'], offset=e['offset'], size=e['size']) for e in p['entries']])
    open(os.path.join(outdir, '_manifest.json'), 'w', encoding='utf-8').write(json.dumps(manifest, indent=2))
    log(f"PS2 HD/BD extraído: {len(p['entries'])} VAGs -> {outdir}")
    # Info
    lines=[]
    lines.append(f"Archivo: {os.path.basename(path)}")
    lines.append(f"Tipo: PS2 HD/BD (IECS) VAG ADPCM")
    lines.append(f"Tamaño: {len(data)} bytes (HD {p['hd_sz']} + BD {p['bd_sz']})")
    lines.append(f"Samples VAG: {len(p['entries'])}")
    for e in p['entries']:
        lines.append(f"  VAG {e['index']:02d} off 0x{e['offset']:04X} sz {e['size']:5d} rate {e['rate']:5d}")
    open(os.path.join(outdir, '_info.txt'), 'w', encoding='utf-8').write('\n'.join(lines))
    open(os.path.join(outdir, '_orden_latino.txt'), 'w', encoding='utf-8').write(
        '\n'.join(latino_map_lines(path, p['entries'])) + '\n')

def repack_ps2_folder(folder, log):
    mp = os.path.join(folder, '_manifest.json')
    if not os.path.isfile(mp):
        raise ValueError('Falta _manifest.json')
    manifest = json.load(open(mp, encoding='utf-8'))
    if manifest.get('type') not in ('ps2hd','ps2'):
        raise ValueError('Manifest no es PS2 HD')
    # Cargar HD/BD originales
    hd_path = os.path.join(folder, '_hd.bin')
    bd_path = os.path.join(folder, '_bd.bin')
    container_hdr_path = os.path.join(folder, '_container_header.bin')
    if not os.path.isfile(hd_path) or not os.path.isfile(bd_path):
        raise ValueError('Faltan _hd.bin/_bd.bin')
    hd = bytearray(open(hd_path,'rb').read())
    bd = bytearray(open(bd_path,'rb').read())
    # Reconstruir BD por slots fijos (como hace el script de tu amigo)
    # Cada VAG en carpeta reemplaza su slot
    import glob
    # Buscar VAGs
    vag_files = {}
    for fn in os.listdir(folder):
        if fn.lower().endswith('.vag'):
            # extraer índice del nombre: *_XX_*.VAG o XX.VAG
            m=re.search(r'(\d+)\.VAG$', fn, re.I)
            if m:
                idx=int(m.group(1))
                vag_files[idx]=os.path.join(folder, fn)
        elif fn.lower().endswith('.wav'):
            # Soporte WAV directo: convertir a VAG al vuelo
            m=re.search(r'(\d+)', fn)
            if m:
                idx=int(m.group(1))
                # Convertir WAV a VAG usando pcm16_to_vag? Usamos wav2vag si existe, sino via pcm16_to_vag
                vag_files[idx]=os.path.join(folder, fn)
    vagi_off = manifest['vagi_off']
    max_idx = manifest['max_idx']
    # Necesitamos HD para obtener offsets
    for idx, fpath in vag_files.items():
        if idx <0 or idx>max_idx:
            continue
        # Leer VAG o WAV
        ext=os.path.splitext(fpath)[1].lower()
        if ext=='.vag':
            data=open(fpath,'rb').read()
            if len(data)>=0x30 and data[0:4]==b'VAGp':
                rate=struct.unpack('>I', data[0x10:0x14])[0]
                adpcm=data[0x30:]
            else:
                # Raw VAG sin header
                rate=manifest['samples'][idx]['rate'] if idx < len(manifest['samples']) else 18000
                adpcm=data
        elif ext=='.wav':
            # WAV (incl. MS ADPCM latino) -> PCM16 (ffmpeg si hace falta) -> VAG
            try:
                w=wave.open(fpath,'rb')
                pcm=w.readframes(w.getnframes())
                ch=w.getnchannels(); sr=w.getframerate(); sw=w.getsampwidth()
                w.close()
                if sw != 2:
                    raise ValueError('no es PCM16')
            except Exception:
                ff = resource_path('ffmpeg.exe')
                if not os.path.isfile(ff):
                    ff = shutil.which('ffmpeg') or shutil.which('ffmpeg.exe')
                if not ff or not os.path.isfile(ff):
                    raise ValueError(f'no se pudo leer {fpath} (MS ADPCM necesita ffmpeg.exe)')
                import tempfile as _tf
                with _tf.NamedTemporaryFile(delete=False, suffix='.wav') as t: tp = t.name
                try:
                    r = _run([ff,'-y','-i',fpath,'-acodec','pcm_s16le',tp], capture_output=True)
                    if r.returncode != 0:
                        raise ValueError(f'ffmpeg falló con {os.path.basename(fpath)}')
                    w=wave.open(tp,'rb')
                    pcm=w.readframes(w.getnframes()); ch=w.getnchannels(); sr=w.getframerate()
                    w.close()
                finally:
                    try: os.remove(tp)
                    except: pass
            # Convertir a mono si needed (PS2 VAG es mono)
            if ch==2:
                pcm=make_mono(pcm)
            # Resample si rate distinto al slot? Usamos slot rate
            target_rate=manifest['samples'][idx]['rate'] if idx < len(manifest['samples']) else sr
            if sr!=target_rate:
                pcm=resample_pcm16(pcm, sr, target_rate)
            rate=target_rate
            # Codificar con wav2vag.exe si existe (verificado por roundtrip), si no pcm16_to_vag
            w2v = resource_path('wav2vag.exe')
            if not os.path.isfile(w2v):
                w2v = shutil.which('wav2vag.exe')
            if w2v and os.path.isfile(w2v):
                import tempfile as _tf2
                with _tf2.NamedTemporaryFile(delete=False, suffix='.raw') as t: tr = t.name
                with _tf2.NamedTemporaryFile(delete=False, suffix='.VAG') as t: tv = t.name
                try:
                    open(tr,'wb').write(pcm)
                    rr = _run([w2v, tr, tv, '-sraw16', f'-freq={rate}'], capture_output=True)
                    if rr.returncode != 0 or not os.path.exists(tv):
                        raise ValueError('wav2vag falló')
                    vd = open(tv,'rb').read()
                    adpcm = vd[0x30:] if len(vd)>=0x30 and vd[:4]==b'VAGp' else vd
                    if len(adpcm)>=16 and adpcm[-16:]==bytes.fromhex("00 07 77 77 77 77 77 77 77 77 77 77 77 77 77 77"):
                        adpcm = adpcm[:-16]
                finally:
                    try: os.remove(tr)
                    except: pass
                    try: os.remove(tv)
                    except: pass
            else:
                adpcm=pcm16_to_vag(pcm)
        else:
            continue
        # Obtener slot info
        param_off=struct.unpack('<I', hd[vagi_off+0x10+idx*4:vagi_off+0x14+idx*4])[0]
        vag_off=struct.unpack('<I', hd[vagi_off+param_off:vagi_off+param_off+4])[0]
        if idx < max_idx:
            next_param=struct.unpack('<I', hd[vagi_off+0x10+(idx+1)*4:vagi_off+0x14+(idx+1)*4])[0]
            next_off=struct.unpack('<I', hd[vagi_off+next_param:vagi_off+next_param+4])[0]
            slot_sz=next_off - vag_off
        else:
            slot_sz=len(bd) - vag_off
        # Aplicar lógica fixed slot del amigo: trim/pad con end marker
        VAG_END=bytes.fromhex("00 07 77 77 77 77 77 77 77 77 77 77 77 77 77 77")
        has_marker=len(adpcm)>=16 and adpcm[-16:]==VAG_END
        audio=adpcm[:-16] if has_marker else adpcm
        audio_slot=slot_sz-16
        max_audio=(audio_slot//16)*16
        if len(audio)>max_audio:
            audio=audio[:max_audio]
            log(f"  VAG {idx:02d} TRIMMED {len(adpcm)}->{len(audio)+16}")
        in_sz=len(audio)
        zero_pad=audio_slot-in_sz
        padded=audio + b"\x00"*zero_pad + VAG_END
        # Actualizar rate en HD
        struct.pack_into('<H', hd, vagi_off+param_off+0x04, rate)
        # Reconstruir BD
        new_bd=bytearray()
        new_bd.extend(bd[:vag_off])
        new_bd.extend(padded)
        new_bd.extend(bd[vag_off+slot_sz:])
        bd=new_bd
        log(f"  VAG {idx:02d} importado rate {rate} slot {slot_sz} audio {in_sz} pad {zero_pad}")
    # Reconstruir container
    hd_off_m = manifest['hd_off']; hd_sz_m = manifest['hd_sz']; bd_off_m = manifest['bd_off']; bd_sz_m = manifest['bd_sz']
    container_hdr_data = open(container_hdr_path,'rb').read() if os.path.isfile(container_hdr_path) else struct.pack('<IIII', hd_off_m, hd_sz_m, bd_off_m, bd_sz_m) + b"\x00"*16
    total_sz=max(hd_off_m+len(hd), bd_off_m+len(bd))
    container=bytearray(total_sz)
    container[0:0x20]=container_hdr_data[:0x20]
    container[hd_off_m:hd_off_m+len(hd)]=hd
    container[bd_off_m:bd_off_m+len(bd)]=bd
    out_path=os.path.join(folder, manifest['source'])
    open(out_path,'wb').write(container)
    log(f"PS2 BIN reconstruido -> {out_path}")

# ======================================================================
# AFS (archivo CRI del juego PS2; AFS01.AFS trae los plXX_voi.bin)
# ======================================================================
# El AFS01 verificado tiene 63 archivos (pl00..pl3a, se_comn, na_comn,
# se_staf, se_syuk) con tabla de nombres de 48B al final. La inyección
# con mismo tamaño es in-place (header y cola intactos, como ya se hizo
# con los 11 bins actuales del juego).
AFS_GAME_DIR = r'F:\MVC2\Marvel vs. Capcom 2 New Age of Heroes [NTSC-PAL]\SLES_511.74.Marvel vs Capcom 2 New Age of Heroes\PS2'

def parse_afs(path):
    data = open(path, 'rb').read()
    if len(data) < 16 or data[0:4] != b'AFS\x00':
        raise ValueError('no es un AFS (magia AFS\\0 no encontrada)')
    n = struct.unpack('<I', data[4:8])[0]
    if n <= 0 or n > 10000:
        raise ValueError(f'número de archivos AFS inválido: {n}')
    files = []
    for i in range(n):
        off, sz = struct.unpack('<II', data[8+i*8:16+i*8])
        if off < 0 or sz < 0 or off + sz > len(data):
            raise ValueError(f'entrada AFS {i} fuera de rango (off={off} size={sz})')
        files.append(dict(index=i, offset=off, size=sz))
    # Tabla de nombres al final: registros con stride uniforme (32/48/64)
    names = [None]*n
    data_end = max(f['offset']+f['size'] for f in files)
    tail = data[data_end:]
    hits = {}
    for m in re.finditer(rb'[A-Za-z0-9_][A-Za-z0-9_\-\.]{1,39}', tail):
        s = m.group()
        if b'.' in s:
            try:
                hits[m.start()] = s.decode('ascii')
            except:
                pass
    for stride in (32, 48, 64):
        for (p0, _) in list(hits.items())[:64]:
            seq = [hits.get(p0+k*stride) for k in range(n)]
            if all(seq):
                names = seq
                break
        if names[0] is not None:
            break
    return dict(path=path, size=len(data), nfiles=n, files=files,
                names=names, data_end=data_end)

def inject_afs(afs_path, file_index=None, file_name=None, new_data=None, log=None):
    """Inyecta new_data (bytes) en un archivo del AFS.
    Con mismo tamaño: escritura in-place (header y cola intactos).
    Con distinto tamaño: rebuild completo preservando orden y cola.
    Crea .bak solo si no existe. Retorna el índice inyectado."""
    if log is None:
        log = print
    if new_data is None:
        raise ValueError('sin datos nuevos')
    new_data = bytes(new_data)
    p = parse_afs(afs_path)
    n = p['nfiles']
    if file_index is None:
        if not file_name:
            raise ValueError('indica índice o nombre de archivo')
        lname = file_name.lower()
        cands = [i for i, nm in enumerate(p['names']) if nm and nm.lower() == lname]
        if not cands:
            known = ', '.join([nm for nm in p['names'][:8] if nm])
            raise ValueError(f'{file_name} no está en el AFS (ej: {known}...)')
        file_index = cands[0]
    if file_index < 0 or file_index >= n:
        raise ValueError(f'índice AFS {file_index} fuera de rango (0..{n-1})')
    e = p['files'][file_index]
    nm = p['names'][file_index] or f'#{file_index}'
    old = open(afs_path, 'rb').read()
    bak = afs_path + '.bak'
    if not os.path.exists(bak):
        open(bak, 'wb').write(old)
        log(f"Backup creado: {bak}")
    else:
        log(f"Backup existente, no se toca: {bak}")
    if len(new_data) == e['size']:
        out = bytearray(old)
        out[e['offset']:e['offset']+e['size']] = new_data
        open(afs_path, 'wb').write(out)
        log(f"Inyectado {nm} en {os.path.basename(afs_path)} idx={file_index} "
            f"off={e['offset']} ({e['size']}B, mismo tamaño, in-place)")
    else:
        # Rebuild: mismo orden, primer offset preservado, alineado a 2048
        blobs = [old[f['offset']:f['offset']+f['size']] for f in p['files']]
        blobs[file_index] = new_data
        tail = old[p['data_end']:]
        first_off = p['files'][0]['offset']
        hlen = 8 + 8*n
        if hlen > first_off:
            raise ValueError('cabecera AFS no cabe antes del primer archivo')
        entries = []
        cur = first_off
        for i, b in enumerate(blobs):
            if i > 0:
                cur = (cur + 2047)//2048*2048
            entries.append((cur, len(b)))
            cur += len(b)
        out = bytearray(cur + len(tail))
        out[0:4] = b'AFS\x00'
        struct.pack_into('<I', out, 4, n)
        for i, (o, s) in enumerate(entries):
            struct.pack_into('<II', out, 8+i*8, o, s)
        out[hlen:first_off] = old[hlen:first_off]
        for (o, _s), b in zip(entries, blobs):
            out[o:o+len(b)] = b
        out[cur:cur+len(tail)] = tail
        open(afs_path, 'wb').write(out)
        log(f"Inyectado {nm} idx={file_index} con REBUILD "
            f"({e['size']}->{len(new_data)}B, AFS {len(old)}->{len(out)}B)")
    # Verificar
    chk = open(afs_path, 'rb').read()
    q = parse_afs(afs_path)
    got = chk[q['files'][file_index]['offset']:q['files'][file_index]['offset']+len(new_data)]
    if got != new_data:
        raise ValueError('verificación post-inyección falló')
    log(f"Verificado: {nm} íntegro en el AFS")
    return file_index

# ======================================================================
# FUNCIONES DE EXTRACCIÓN Y REPACK (usando play_order)
# ======================================================================

def extract_dtpk(path, outdir, log):
    data = open(path, 'rb').read()
    p = parse_dtpk(data)
    os.makedirs(outdir, exist_ok=True)
    header = data[:p['soff']]
    open(os.path.join(outdir, '_header.bin'), 'wb').write(header)
    tail = data[p['data_end']:]
    if tail:
        open(os.path.join(outdir, '_tail_pad.bin'), 'wb').write(tail)
    base_name = os.path.splitext(os.path.basename(path))[0]
    for item in p['play_order']:
        group = item['group']
        track = item['track']
        playback_id = item['playback_id']
        sample_idx = item['sample']
        rate16 = item['rate16']
        e = p['entries'][sample_idx]
        rel = 0 if e['bytes'] == len(data) - p['soff'] else e['offset'] - p['soff']
        raw = data[p['soff'] + rel: p['soff'] + rel + e['bytes']]
        ext = EXT_BY_FORMAT[e['format']]
        fname = f"{base_name}_{group:02d}_{track:02d}_SPD_{playback_id:02X}_Sample_{sample_idx:02X}_Rate_{rate16:04X}.{ext}"
        open(os.path.join(outdir, fname), 'wb').write(raw)
    order_lines = []
    for item in p['play_order']:
        fname = f"{base_name}_{item['group']:02d}_{item['track']:02d}_SPD_{item['playback_id']:02X}_Sample_{item['sample']:02X}_Rate_{item['rate16']:04X}.{EXT_BY_FORMAT[p['entries'][item['sample']]['format']]}"
        order_lines.append(fname)
    open(os.path.join(outdir, '_playback_order.txt'), 'w', encoding='utf-8').write('\n'.join(order_lines) + '\n')
    lines = []
    lines.append('Archivo original : %s' % os.path.basename(path))
    lines.append('Formato          : DTPK (contenedor de audio del Dreamcast)')
    lines.append('ID del paquete   : 0x%08X' % struct.unpack('<I', data[4:8])[0])
    lines.append('Tamano           : %d bytes' % len(data))
    lines.append('Total de samples : %d' % len(p['play_order']))
    if tail:
        lines.append('Relleno final    : %d bytes (_tail_pad.bin)' % len(tail))
    lines.append('')
    lines.append('Regiones (offset de cada chunk, leidos de la cabecera):')
    for k, name in REGION_NAMES.items():
        lines.append('  %-12s 0x%02X -> 0x%08X' % (name, k, struct.unpack('<I', data[k:k + 4])[0]))
    lines.append('')
    lines.append('Orden de reproduccion (grupo, track, SPD, sample, rate):')
    for item in p['play_order']:
        lines.append('  G%02d T%02d SPD%02X Sample%02X Rate%04X' % (item['group'], item['track'], item['playback_id'], item['sample'], item['rate16']))
    open(os.path.join(outdir, '_info.txt'), 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
    manifest = dict(type='dtpk', version=3, source=os.path.basename(path),
                    tail_len=len(tail),
                    samples=[dict(index=e['index'],
                                  file='%03d.%s' % (e['index'], EXT_BY_FORMAT[e['format']]),
                                  format=e['format'], stereo=1 if e['stereo'] else 0,
                                  rate=translate_rate(item['rate16']) if item else 0,
                                  length=e['length'], bytes=e['bytes'],
                                  flags=e['flags'], loop_start=e['loop_start'],
                                  loop_end=e['loop_end']) for e in p['entries']])
    open(os.path.join(outdir, '_manifest.json'), 'w', encoding='utf-8').write(
        json.dumps(manifest, indent=2, ensure_ascii=False))
    log('DTPK extraido: %d samples (orden secuenciador, únicos) -> %s' % (len(p['play_order']), outdir))


def _voz_of_latino(latpath):
    # Número de voz desde el wav mapeado (ej _11) o None.
    try:
        mv = re.search(r'(\d+)\.wav$', os.path.basename(latpath), re.I)
        return int(mv.group(1)) if mv else None
    except:
        return None

def extract_dtpk_wav(path, outdir, log, half_rates=False):
    data = open(path, 'rb').read()
    base_name = os.path.splitext(os.path.basename(path))[0]
    if is_ps2_container(data):
        # PS2 HD/BD: cada VAG a WAV con su tasa (decoder entero verificado).
        # half_rates=True escribe 18000->9000 y 24000->12000 (test ardilla).
        # Nombre por VOZ latina si hay mapeo (ordena como la carpeta de
        # latinos); si no, por slot como antes.
        p = parse_ps2_container(data)
        os.makedirs(outdir, exist_ok=True)
        try:
            vmap = latino_map_for_ps2(path, p['entries'])
        except:
            vmap = {}
        for e in p['entries']:
            raw = p['bd'][e['offset']:e['offset']+e['size']]
            if len(raw)>=16 and raw[-16:]==bytes.fromhex("00 07 77 77 77 77 77 77 77 77 77 77 77 77 77 77"):
                raw = raw[:-16]
            pcm = vag_to_pcm16(raw)
            rate = e['rate']
            if half_rates and rate in (18000, 24000):
                rate = rate // 2
            voz = _voz_of_latino(vmap[e['index']]) if e['index'] in vmap else None
            if voz is not None:
                latbase = os.path.splitext(os.path.basename(vmap[e['index']]))[0]
                fname = f"V{voz:02d}_{latbase}_slot{e['index']:02d}.wav"
            else:
                fname = f"{base_name}_VAG_{e['index']:02d}_Rate_{rate:04d}.wav"
            write_wav(os.path.join(outdir, fname), pcm, rate, False)
        open(os.path.join(outdir, '_info.txt'), 'w', encoding='utf-8').write(
            'Archivo original: %s\nSamples: %d\nFormato: WAV (VAG ADPCM decodificado)\nOrden: índice VAG\nTasas mitad: %s\n' % (
                os.path.basename(path), len(p['entries']), 'SI (18000->9000, 24000->12000)' if half_rates else 'no'))
        open(os.path.join(outdir, '_orden_latino.txt'), 'w', encoding='utf-8').write(
            '\n'.join(latino_map_lines(path, p['entries'])) + '\n')
        log('PS2 extraido a WAV: %d VAGs%s -> %s (+_orden_latino.txt)' % (len(p['entries']), ' (tasas mitad)' if half_rates else '', outdir))
        return
    p = parse_dtpk(data)
    os.makedirs(outdir, exist_ok=True)
    for item in p['play_order']:
        group = item['group']
        track = item['track']
        playback_id = item['playback_id']
        sample_idx = item['sample']
        rate16 = item['rate16']
        e = p['entries'][sample_idx]
        rel = 0 if e['bytes'] == len(data) - p['soff'] else e['offset'] - p['soff']
        raw = data[p['soff'] + rel: p['soff'] + rel + e['bytes']]
        pcm = decode_sample(e['format'], e['stereo'], raw)
        rate = translate_rate(rate16)
        fname = f"{base_name}_{group:02d}_{track:02d}_SPD_{playback_id:02X}_Sample_{sample_idx:02X}_Rate_{rate16:04X}.wav"
        write_wav(os.path.join(outdir, fname), pcm, rate, e['stereo'])
    open(os.path.join(outdir, '_info.txt'), 'w', encoding='utf-8').write(
        'Archivo original: %s\nSamples: %d\nFormato: WAV (todo decodificado)\nOrden: secuenciador (samples únicos)\n' % (
            os.path.basename(path), len(p['play_order'])))
    log('DTPK extraido a WAV: %d samples (orden secuenciador, únicos) -> %s' % (len(p['play_order']), outdir))


def load_sample_file(fpath, item, target_rate=None):
    ext = os.path.splitext(fpath)[1].lower()
    if ext == '.yadpcm':
        return 'adpcm', item['stereo'], open(fpath, 'rb').read()
    if ext == '.pcm8':
        return 'pcm8', item['stereo'], open(fpath, 'rb').read()
    if ext == '.pcm16':
        return 'pcm16', item['stereo'], open(fpath, 'rb').read()
    if ext != '.wav':
        raise ValueError('extension no soportada (.yadpcm/.pcm8/.pcm16/.wav)')

    with open(fpath, 'rb') as f:
        riff = f.read(12)
        if riff[:4] != b'RIFF' or riff[8:12] != b'WAVE':
            raise ValueError('No es un archivo WAV válido')
        fmt_chunk = None
        while True:
            cid = f.read(4)
            if len(cid) < 4:
                break
            csize = struct.unpack('<I', f.read(4))[0]
            if cid == b'fmt ':
                fmt_chunk = f.read(csize)
                break
            else:
                f.seek(csize, 1)
    if fmt_chunk is None:
        raise ValueError('No se encontró el chunk fmt en el WAV')

    audio_format = struct.unpack('<H', fmt_chunk[0:2])[0]
    nch = struct.unpack('<H', fmt_chunk[2:4])[0]
    sample_rate = struct.unpack('<I', fmt_chunk[4:8])[0]
    block_align = struct.unpack('<H', fmt_chunk[12:14])[0]
    bits_per_sample = struct.unpack('<H', fmt_chunk[14:16])[0]

    if audio_format == 2:
        ffmpeg_path = resource_path('ffmpeg.exe')
        if not os.path.isfile(ffmpeg_path):
            ffmpeg_path = shutil.which('ffmpeg.exe')
        if not ffmpeg_path:
            raise ValueError(
                'Este WAV usa MS ADPCM (formato 0x0002). Para convertirlo automáticamente, '
                'coloca ffmpeg.exe en la misma carpeta que este script o en el PATH.\n'
                'Puedes descargarlo desde https://ffmpeg.org/download.html'
            )
        out_channels = 2 if item['stereo'] else 1
        out_rate = target_rate if target_rate is not None else sample_rate
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
            tmp_path = tmp.name
        cmd = [
            ffmpeg_path,
            '-i', fpath,
            '-acodec', 'pcm_s16le',
            '-ac', str(out_channels),
            '-ar', str(out_rate),
            '-y',
            tmp_path
        ]
        try:
            result = _run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise ValueError(f'ffmpeg falló al convertir:\n{result.stderr}')
            w = wave.open(tmp_path, 'rb')
            nch = w.getnchannels()
            sw = w.getsampwidth()
            nf = w.getnframes()
            src_rate = w.getframerate()
            frames = w.readframes(nf)
            w.close()
            os.unlink(tmp_path)
            if sw != 2:
                raise ValueError('ffmpeg no generó PCM 16-bit')
            pcm16 = frames
            src_rate = out_rate
            target_rate = out_rate
            nch = out_channels
        except Exception as e:
            try:
                os.unlink(tmp_path)
            except:
                pass
            raise ValueError(f'Error al convertir MS ADPCM con ffmpeg: {e}')
    else:
        w = wave.open(fpath, 'rb')
        nch = w.getnchannels()
        sw = w.getsampwidth()
        nf = w.getnframes()
        src_rate = w.getframerate()
        frames = w.readframes(nf)
        w.close()

    if sw == 1:
        pcm16 = b''.join(struct.pack('<h', (b - 128) << 8) for b in frames)
    elif sw == 2:
        pcm16 = frames
    elif sw == 3:
        pcm16 = bytearray()
        for i in range(0, len(frames), 3):
            sample = int.from_bytes(frames[i:i+3], 'little', signed=True)
            pcm16.extend(struct.pack('<h', sample >> 8))
        pcm16 = bytes(pcm16)
    elif sw == 4:
        pcm16 = bytearray()
        for i in range(0, len(frames), 4):
            sample = struct.unpack_from('<i', frames, i)[0]
            pcm16.extend(struct.pack('<h', max(-32768, min(32767, sample >> 16))))
        pcm16 = bytes(pcm16)
    else:
        raise ValueError('profundidad WAV no soportada (%d-bit)' % (sw * 8))

    if target_rate is not None and target_rate != src_rate:
        pcm16 = resample_pcm16(pcm16, src_rate, target_rate)
        src_rate = target_rate

    stereo_out = item['stereo']
    if nch == 2 and not stereo_out:
        pcm16 = make_mono(pcm16)
        stereo_out = False
    elif nch == 1 and stereo_out:
        arr = array.array('h')
        arr.frombytes(pcm16)
        stereo_arr = array.array('h')
        for v in arr:
            stereo_arr.append(v)
            stereo_arr.append(v)
        pcm16 = stereo_arr.tobytes()
        stereo_out = True
    elif nch == 2 and stereo_out:
        stereo_out = True
    else:
        stereo_out = False

    if item['format'] == 'adpcm':
        if stereo_out:
            arr = array.array('h')
            arr.frombytes(pcm16)
            if len(arr) % 2:
                arr = arr[:-1]
            left = arr[0::2].tobytes()
            right = arr[1::2].tobytes()
            raw_adpcm = pcm16_to_yadpcm(left) + pcm16_to_yadpcm(right)
        else:
            raw_adpcm = pcm16_to_yadpcm(pcm16)
        return 'adpcm', stereo_out, raw_adpcm
    elif item['format'] == 'pcm8':
        arr = array.array('h')
        arr.frombytes(pcm16)
        raw8 = bytes((v >> 8) & 0xFF for v in arr)
        return 'pcm8', stereo_out, raw8
    else:
        return 'pcm16', stereo_out, pcm16


def build_dtpk(header, samples, tail):
    count = len(samples)
    entries = []
    data = b''
    cum = 0
    for fmt, stereo, raw, flags, loop_start, loop_end in samples:
        if stereo and len(raw) % 2:
            raise ValueError('tamano impar para un sample stereo')
        ln = len(raw) // (2 if stereo else 1)
        off = len(header) + 4 + 16 * count + cum
        entries.append(struct.pack('<IHHII', off | flags, loop_start,
                                   loop_end, 0x80 if stereo else 0, ln))
        data += raw
        cum += len(raw)
    chunk = struct.pack('<I', count - 1) + b''.join(entries) + data + tail
    out = header + chunk
    return out[:0x08] + struct.pack('<I', len(out)) + out[0x0c:]


def repack_dtpk_folder(folder, log):
    mp = os.path.join(folder, '_manifest.json')
    hp = os.path.join(folder, '_header.bin')
    if not os.path.isfile(mp) or not os.path.isfile(hp):
        raise ValueError('la carpeta no tiene _manifest.json y _header.bin')
    manifest = json.load(open(mp, encoding='utf-8'))
    if manifest.get('type') != 'dtpk':
        raise ValueError('el manifest no es de tipo DTPK')
    header = open(hp, 'rb').read()
    if header[0:4] != b'DTPK':
        raise ValueError('_header.bin no empieza con "DTPK"')
    tail_file = os.path.join(folder, '_tail_pad.bin')
    tail = open(tail_file, 'rb').read() if os.path.isfile(tail_file) else b'\x00' * manifest.get('tail_len', 0)
    samples = []
    replaced = 0
    warned_stereo = False
    for item in manifest['samples']:
        base = os.path.splitext(item['file'])[0]
        wav_path = os.path.join(folder, base + '.wav')
        raw_path = os.path.join(folder, item['file'])
        if os.path.isfile(wav_path):
            fpath = wav_path
        elif os.path.isfile(raw_path):
            fpath = raw_path
        else:
            raise ValueError('falta el archivo %s (ni %s ni %s)' % (item['file'], item['file'], base + '.wav'))
        target_rate = item.get('rate')
        fmt, stereo, raw = load_sample_file(fpath, item, target_rate)
        if fmt == item['format']:
            flags = item['flags']
        else:
            flags = flag_for_format(fmt)
        samples.append((fmt, stereo, raw, flags, item['loop_start'], item['loop_end']))
        if len(raw) != item['bytes'] or fmt != item['format'] or stereo != item['stereo']:
            replaced += 1
        if stereo != item['stereo']:
            warned_stereo = True
    out = build_dtpk(header, samples, tail)
    src = manifest.get('source')
    if not src:
        raise ValueError('el manifest no indica el nombre original')
    outpath = os.path.join(folder, src)
    with open(outpath, 'wb') as f:
        f.write(out)
    log('DTPK reconstruido: %d samples (%d modificados) -> %s' % (len(samples), replaced, outpath))
    if warned_stereo:
        log('  ATENCION: algun sample cambio de mono/stereo; el playback espera el original.')


def repack_stream_folder(folder, log):
    mp = os.path.join(folder, '_manifest.json')
    manifest = json.load(open(mp, encoding='utf-8'))
    if manifest.get('type') != 'stream':
        raise ValueError('el manifest no es de tipo stream')
    kind = manifest['kind']
    src = manifest.get('source')
    if not src:
        raise ValueError('el manifest no indica el nombre original')
    cand = [f for f in os.listdir(folder) if not f.startswith('_') and not f.endswith('.bin')]
    if len(cand) != 1:
        raise ValueError('la carpeta debe contener exactamente 1 archivo de stream')
    data = open(os.path.join(folder, cand[0]), 'rb').read()
    if detect_stream(data) != kind:
        raise ValueError('el archivo %s no parece %s' % (cand[0], kind))
    outpath = os.path.join(folder, src)
    with open(outpath, 'wb') as f:
        f.write(data)
    log('Stream %s reconstruido (%d bytes) -> %s' % (kind.upper(), len(data), outpath))


def extract_stream(path, outdir, log):
    # Extracción cruda de streams ADX/MPEG (faltaba: se referenciaba pero no existía).
    data = open(path, 'rb').read()
    kind = detect_stream(data)
    if not kind:
        raise ValueError('no es un stream ADX/MPEG reconocido')
    os.makedirs(outdir, exist_ok=True)
    ext = '.adx' if kind == 'adx' else '.mp2'
    open(os.path.join(outdir, 'stream' + ext), 'wb').write(data)
    manifest = dict(type='stream', version=1, kind=kind,
                    source=os.path.basename(path), size=len(data))
    open(os.path.join(outdir, '_manifest.json'), 'w', encoding='utf-8').write(json.dumps(manifest, indent=2))
    log(f"Stream {kind.upper()} extraído ({len(data)} bytes) -> {outdir}")

def extract_path(path, log, outdir=None):
    data = open(path, 'rb').read()
    base = os.path.splitext(os.path.basename(path))[0]
    if outdir is None:
        outdir = os.path.join(os.path.dirname(path), base + '_extraido')
        # Carpeta única (antes fallaba si ya existía)
        k = 2
        while os.path.exists(outdir):
            outdir = os.path.join(os.path.dirname(path), base + f'_extraido_{k}')
            k += 1
    else:
        os.makedirs(outdir, exist_ok=True)
    if is_ps2_container(data):
        extract_ps2(path, outdir, log)
    elif data[0:4] == b'DTPK':
        extract_dtpk(path, outdir, log)
    else:
        kind = detect_stream(data)
        if kind:
            extract_stream(path, outdir, log)
        else:
            raise ValueError('no es un contenedor de audio reconocido (PS2 HD/BD / DTPK / ADX / MPEG-1 Layer 2)')
    return outdir


def repack_path(folder, log):
    if not os.path.isdir(folder):
        raise ValueError('la ruta no es una carpeta')
    mp = os.path.join(folder, '_manifest.json')
    if not os.path.isfile(mp):
        raise ValueError('la carpeta no tiene _manifest.json')
    manifest = json.load(open(mp, encoding='utf-8'))
    if manifest.get('type') == 'dtpk':
        repack_dtpk_folder(folder, log)
    elif manifest.get('type') in ('ps2hd','ps2'):
        repack_ps2_folder(folder, log)
    elif manifest.get('type') == 'stream':
        repack_stream_folder(folder, log)
    else:
        raise ValueError('tipo de manifest desconocido')


def decode_all_to_wav(path, outdir):
    data = open(path, 'rb').read()
    os.makedirs(outdir, exist_ok=True)
    base = os.path.splitext(os.path.basename(path))[0]
    if is_ps2_container(data):
        p = parse_ps2_container(data)
        try:
            vmap = latino_map_for_ps2(path, p['entries'])
        except:
            vmap = {}
        for e in p['entries']:
            raw = p['bd'][e['offset']:e['offset']+e['size']]
            # Quitar end marker para decodificar solo audio
            if len(raw)>=16 and raw[-16:]==bytes.fromhex("00 07 77 77 77 77 77 77 77 77 77 77 77 77 77 77"):
                raw = raw[:-16]
            pcm = vag_to_pcm16(raw)
            # Tasa REAL de oído: mapa de división por slot (puente DTPK);
            # si no hay dato, el take mapeado manda (si solo cabe a mitad);
            # si no, tasa HD.
            rate = e['rate']
            try:
                dv = _slot_divided(path, e['index'])
                if dv is True:
                    rate = max(1, e['rate']//2)
                elif dv is None and e['index'] in vmap:
                    _sp, _dd = _wav_span_dur(vmap[e['index']])
                    if _strong_half(e, _dd or 0):
                        rate = max(1, e['rate']//2)
            except:
                pass
            fname = f"{base}_VAG_{e['index']:02d}_Rate_{rate:04d}.wav"
            write_wav(os.path.join(outdir, fname), pcm, rate, False)
        print('%d VAGs decodificados en %s' % (len(p['entries']), outdir))
    elif data[0:4] == b'DTPK':
        p = parse_dtpk(data)
        extracted = set()
        for item in p['play_order']:
            idx = item['sample']
            if idx in extracted:
                continue
            extracted.add(idx)
            e = p['entries'][idx]
            rel = 0 if e['bytes'] == len(data) - p['soff'] else e['offset'] - p['soff']
            raw = data[p['soff'] + rel: p['soff'] + rel + e['bytes']]
            pcm = decode_sample(e['format'], e['stereo'], raw)
            rate = translate_rate(item['rate16'])
            fname = f"{base}_{item['group']:02d}_{item['track']:02d}_SPD_{item['playback_id']:02X}_Sample_{idx:02X}_Rate_{item['rate16']:04X}.wav"
            write_wav(os.path.join(outdir, fname), pcm, rate, e['stereo'])
        print('%d WAV generados en %s' % (len(extracted), outdir))
    else:
        kind = detect_stream(data)
        if kind == 'adx':
            pcm, rate, ch = decode_adx_to_pcm16(data)
            out = os.path.join(outdir, base + '.wav')
            write_wav(out, pcm, rate, ch == 2)
            print('ADX decodificado (%d Hz, %d ch) -> %s' % (rate, ch, out))
        elif kind == 'mpeg':
            print('MPEG Layer 2: sin decodificador (requiere ffmpeg); copiando tal cual')
            open(os.path.join(outdir, base + '.mp2'), 'wb').write(data)
        else:
            if data[:4] == b'RIFF' and data[8:12] == b'WAVE':
                ffmpeg_path = resource_path('ffmpeg.exe')
                if not os.path.isfile(ffmpeg_path):
                    ffmpeg_path = shutil.which('ffmpeg.exe')
                if ffmpeg_path:
                    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
                        tmp_path = tmp.name
                    try:
                        cmd = [ffmpeg_path, '-i', path, '-acodec', 'pcm_s16le', '-y', tmp_path]
                        _run(cmd, check=True, capture_output=True)
                        w = wave.open(tmp_path, 'rb')
                        pcm = w.readframes(w.getnframes())
                        rate = w.getframerate()
                        ch = w.getnchannels()
                        w.close()
                        os.unlink(tmp_path)
                        out = os.path.join(outdir, base + '.wav')
                        write_wav(out, pcm, rate, ch == 2)
                        print('WAV MS ADPCM convertido a PCM -> %s' % out)
                    except Exception as e:
                        print('Error al convertir MS ADPCM:', e)
                else:
                    raise ValueError('formato no reconocido (MS ADPCM necesita ffmpeg.exe)')
            else:
                raise ValueError('formato no reconocido')


def detect_stream(data):
    if len(data) > 5 and data[0:2] == b'\x80\x00' and data[4] == 0x03:
        return 'adx'
    if data[:4] == b'\x21\x21\x00\xff':
        return 'mpeg'
    return None


def classify_bin(name, data):
    n = name.lower()
    if data[:4] == b'DTPK':
        return None
    if len(data) > 5 and data[0:2] == b'\x80\x00' and data[4] == 0x03:
        return None
    if data[:4] == b'\x21\x21\x00\xff':
        return None
    if n in ('1st_read.bin', '2_dp.bin'):
        return ('codigo', 'codigo ejecutable del juego (no se debe modificar)')
    if n == 'aicadrv.bin':
        return ('driver', 'driver de sonido AICA del Dreamcast (no se debe modificar)')
    if n in ('ask.bin', 'debug.bin', 'maigo.bin', 'sg_dpldr.bin'):
        return ('libreria', 'libreria de codigo del juego (no se debe modificar)')
    if n.endswith('tex.bin') or n.endswith('pol.bin'):
        return ('texturas', 'texturas / geometria 3D de escenarios o efectos')
    if n.endswith('pak.bin'):
        return ('texturas', 'paquete de texturas de personaje')
    if n.startswith(('atck', 'lib', 'taiki', 'wari', 'yok')) and n.endswith('_b.bin'):
        return ('animaciones', 'datos de animacion de personaje (golpes, poses, movimientos)')
    if n.startswith('hit_') and n.endswith('_b.bin'):
        return ('animaciones', 'datos de animacion de impacto/contacto de personaje')
    if n.startswith('pl') and ('_fac' in n or '_tbl_b' in n or '_win' in n):
        return ('personaje', 'datos de personaje (retrato de seleccion, tabla de stats o pose de victoria)')
    if n.startswith(('sel', 'nowload')) or n in ('warning.bin', 'vs4.bin', 'font.bin', 'keyboard.bin', 'err_mes.bin'):
        return ('interfaz', 'datos de interfaz / pantallas del juego')
    if n in ('dm08cab.bin', 'dm08chr.bin'):
        return ('especial', 'datos especiales (camara/efectos de escenarios demo)')
    return ('datos', 'datos del juego (no audio)')


# ======================================================================
# GUI (WaveEditor y App)
# ======================================================================

class WaveEditor:
    MARKER_HIT_PX = 7

    def __init__(self, root, title, pcm16_interleaved, rate, stereo, fmt,
                max_bytes=None, original_bytes=None, fmt_label=None):
        self.root = root
        self.rate = max(1, rate)
        self.stereo = stereo
        self.channels = 2 if stereo else 1
        self.fmt = fmt
        # Etiqueta visible (el workspace interno puede ser PCM16 aunque el
        # destino final sea otro codec, ej VAG ADPCM 4bit en PS2).
        self.fmt_label = fmt_label or fmt
        self.max_bytes = max_bytes
        self.original_bytes = original_bytes or max_bytes
        self.result = None

        arr = array.array('h')
        arr.frombytes(pcm16_interleaved)
        if stereo:
            self.left = array.array('h', arr[0::2])
            self.right = array.array('h', arr[1::2])
        else:
            self.left = array.array('h', arr)
            self.right = None

        self.undo_stack = []
        # Curva de fade: 'suave' (coseno, sonoridad pareja) por defecto
        self.fade_curve = tk.StringVar(value='suave')
        self.fade_ms_var = tk.StringVar(value='100')
        self.sel_start = 0
        self.sel_end = len(self.left)
        self.zoom = 1.0
        self.scroll = 0.0
        self.drag_mode = None
        self.playing = False
        self.play_after_id = None

        self.win = tk.Toplevel(root)
        self.win.title(title)
        try:
            _ico = resource_path('app.ico')
            if os.path.isfile(_ico):
                self.win.iconbitmap(_ico)
        except Exception:
            pass
        _center_win(self.win, 820, 460, parent=root)
        self.win.minsize(680, 400)
        self.win.transient(root)
        self.win.grab_set()
        self.win.protocol('WM_DELETE_WINDOW', self._on_cancel)
        self._build_ui()
        self.win.bind('<space>', lambda e: self._toggle_play())
        self.win.bind('<Control-z>', lambda e: self._undo())
        self.win.bind('<plus>', lambda e: self._zoom(2))
        self.win.bind('<minus>', lambda e: self._zoom(0.5))
        self.win.after(80, self._redraw)

    def n_frames(self):
        return len(self.left)

    def _push_undo(self):
        right_copy = array.array('h', self.right) if self.right is not None else None
        self.undo_stack.append((array.array('h', self.left), right_copy))
        if len(self.undo_stack) > 20:
            self.undo_stack.pop(0)

    def _undo(self):
        if not self.undo_stack:
            return
        left, right = self.undo_stack.pop()
        self.left, self.right = left, right
        self.sel_start = 0
        self.sel_end = self.n_frames()
        self._redraw()
        self._update_info()

    def _encode_channel(self, arr):
        pcm16 = arr.tobytes()
        if self.fmt == 'adpcm':
            return pcm16_to_yadpcm(pcm16)
        if self.fmt == 'pcm8':
            return bytes((v >> 8) & 0xFF for v in arr)
        return pcm16

    def _encode_all(self):
        raw = self._encode_channel(self.left)
        if self.right is not None:
            raw += self._encode_channel(self.right)
        return raw

    def _estimate_bytes(self):
        n = self.n_frames()
        if self.fmt == 'adpcm':
            per_ch = (n + 1) // 2
        elif self.fmt == 'pcm8':
            per_ch = n
        else:
            per_ch = n * 2
        return per_ch * self.channels

    def _bytes_to_ms(self, nbytes):
        if self.rate <= 0:
            return 0
        return int(self.n_frames() / self.rate * 1000)

    def _build_ui(self):
        info = tk.Frame(self.win)
        info.pack(fill='x', padx=10, pady=(8, 2))
        self.info_label = tk.Label(info, font=('Consolas', 9), fg='#333', justify='left')
        self.info_label.pack(side='left')
        self.limit_label = tk.Label(info, font=('Consolas', 9, 'bold'), fg='red')
        self.limit_label.pack(side='right')

        self.canvas = tk.Canvas(self.win, bg='#1a1a2e', height=180, highlightthickness=0,
                                cursor='sb_h_double_arrow')
        self.canvas.pack(fill='both', expand=True, padx=10, pady=4)
        self.canvas.bind('<Configure>', lambda e: self._redraw())
        self.canvas.bind('<ButtonPress-1>', self._on_press)
        self.canvas.bind('<B1-Motion>', self._on_drag)
        self.canvas.bind('<ButtonRelease-1>', self._on_release)
        self.canvas.bind('<MouseWheel>', self._on_wheel)
        self.canvas.bind('<Button-4>', lambda e: self._zoom(1.4))
        self.canvas.bind('<Button-5>', lambda e: self._zoom(1 / 1.4))

        scrollf = tk.Frame(self.win)
        scrollf.pack(fill='x', padx=10)
        self.scroll_var = tk.DoubleVar(value=0.0)
        self.scroll_scale = tk.Scale(scrollf, from_=0, to=1, resolution=0.001, orient='horizontal',
                                     variable=self.scroll_var, showvalue=0,
                                     command=lambda v: self._on_scroll())
        self.scroll_scale.pack(fill='x')

        ctrl = tk.Frame(self.win)
        ctrl.pack(fill='x', padx=10, pady=4)
        self.btn_play = tk.Button(ctrl, text='Play (espacio)', width=13, command=self._toggle_play)
        self.btn_play.pack(side='left')
        tk.Button(ctrl, text='Zoom +', width=7, command=lambda: self._zoom(2)).pack(side='left', padx=(8, 0))
        tk.Button(ctrl, text='Zoom -', width=7, command=lambda: self._zoom(0.5)).pack(side='left', padx=2)
        tk.Button(ctrl, text='Ver todo', width=8, command=self._zoom_reset).pack(side='left', padx=(0, 8))
        tk.Button(ctrl, text='Deshacer (Ctrl+Z)', width=16, command=self._undo).pack(side='left')

        edit = tk.Frame(self.win)
        edit.pack(fill='x', padx=10, pady=(0, 4))
        tk.Button(edit, text='Recortar a seleccion', command=self._cmd_trim_to_selection).pack(side='left')
        tk.Button(edit, text='Eliminar seleccion', command=self._cmd_delete_selection).pack(side='left', padx=4)
        tk.Button(edit, text='Fade In', command=self._cmd_fade_in).pack(side='left', padx=4)
        tk.Button(edit, text='Fade Out', command=self._cmd_fade_out).pack(side='left', padx=4)
        tk.Label(edit, text='Curva:', font=('Segoe UI', 8)).pack(side='left', padx=(4, 0))
        tk.OptionMenu(edit, self.fade_curve, 'suave', 'lineal', 'exp').pack(side='left')
        tk.Button(edit, text='Normalizar', command=self._cmd_normalize).pack(side='left', padx=4)

        markers = tk.Frame(self.win)
        markers.pack(fill='x', padx=10, pady=(0, 4))
        tk.Label(markers, text='Sel. inicio (ms):', font=('Segoe UI', 9)).pack(side='left')
        self.ini_var = tk.StringVar(value='0')
        tk.Entry(markers, textvariable=self.ini_var, width=8).pack(side='left', padx=(2, 10))
        tk.Label(markers, text='Sel. fin (ms):', font=('Segoe UI', 9)).pack(side='left')
        self.fin_var = tk.StringVar(value='0')
        tk.Entry(markers, textvariable=self.fin_var, width=8).pack(side='left', padx=2)
        tk.Button(markers, text='Ir', width=4, command=self._set_selection_from_fields).pack(side='left', padx=6)
        tk.Label(markers, text='I:', font=('Segoe UI', 9)).pack(side='left', padx=(8, 0))
        tk.Button(markers, text='◀', width=3, command=lambda: self._nudge_sel('ini', -25)).pack(side='left')
        tk.Button(markers, text='▶', width=3, command=lambda: self._nudge_sel('ini', 25)).pack(side='left')
        tk.Label(markers, text='F:', font=('Segoe UI', 9)).pack(side='left', padx=(6, 0))
        tk.Button(markers, text='◀', width=3, command=lambda: self._nudge_sel('fin', -25)).pack(side='left')
        tk.Button(markers, text='▶', width=3, command=lambda: self._nudge_sel('fin', 25)).pack(side='left')
        tk.Label(markers, text='Zona(ms):', font=('Segoe UI', 9)).pack(side='left', padx=(8, 0))
        tk.Entry(markers, textvariable=self.fade_ms_var, width=5).pack(side='left')
        tk.Button(markers, text='Zona ini', width=8, command=lambda: self._sel_edge('start')).pack(side='left', padx=2)
        tk.Button(markers, text='Zona fin', width=8, command=lambda: self._sel_edge('end')).pack(side='left')

        btns = tk.Frame(self.win)
        btns.pack(fill='x', padx=10, pady=(4, 10))
        tk.Button(btns, text='Cancelar', width=10, command=self._on_cancel).pack(side='right', padx=4)
        tk.Button(btns, text='Aplicar cambios', width=16, command=self._on_apply,
                 bg='#4a9eff', fg='white').pack(side='right')

        self._update_info()

    def _view_range(self):
        n = self.n_frames()
        view = max(1, int(n / self.zoom))
        start = max(0, min(n, int(self.scroll * max(0, n - view))))
        end = min(n, start + view)
        return start, end

    def _frame_to_x(self, frame, start, end, w):
        span = max(1, end - start)
        return int((frame - start) * w / span)

    def _x_to_frame(self, x, start, end, w):
        span = max(1, end - start)
        return int(start + x * span / max(1, w))

    def _redraw(self):
        c = self.canvas
        c.delete('all')
        w = c.winfo_width() or 780
        h = c.winfo_height() or 180
        mid = h // 2
        n = self.n_frames()
        if n == 0:
            return
        start, end = self._view_range()
        step = max(1, (end - start) // max(1, w))
        c.create_line(0, mid, w, mid, fill='#333')
        left = self.left
        x = 0
        i = start
        while i < end and x < w:
            chunk_max = 0
            j = min(end, i + step)
            for k in range(i, j):
                v = abs(left[k])
                if v > chunk_max:
                    chunk_max = v
            hgt = min(mid - 4, chunk_max * (mid - 4) // 32768)
            c.create_line(x, mid - hgt, x, mid + hgt, fill='#4a9eff')
            x += 1
            i += step
        s0, s1 = self.sel_start, self.sel_end
        if s1 > s0:
            x0 = self._frame_to_x(max(s0, start), start, end, w)
            x1 = self._frame_to_x(min(s1, end), start, end, w)
            c.create_rectangle(x0, 0, x1, h, fill='#2255aa', outline='', stipple='gray25')
        if start <= s0 <= end:
            xa = self._frame_to_x(s0, start, end, w)
            c.create_line(xa, 0, xa, h, fill='#00ff00', width=2, tags='mk_start')
        if start <= s1 <= end:
            xb = self._frame_to_x(s1, start, end, w)
            c.create_line(xb, 0, xb, h, fill='#ff4444', width=2, tags='mk_end')
        if self.max_bytes is not None:
            max_frames = self._max_frames_allowed()
            if max_frames is not None and start <= max_frames <= end:
                xl = self._frame_to_x(max_frames, start, end, w)
                c.create_line(xl, 0, xl, h, fill='red', width=2, dash=(3, 2))
        sel_ms = int((s1 - s0) / self.rate * 1000) if s1 > s0 else 0
        c.create_text(w // 2, 10, text='Seleccion: %d ms  |  zoom x%.1f' % (sel_ms, self.zoom),
                     fill='#0f0', font=('Consolas', 9))
        self._update_info()

    def _max_frames_allowed(self):
        if self.max_bytes is None:
            return None
        if self.fmt == 'adpcm':
            return self.max_bytes // self.channels * 2
        if self.fmt == 'pcm8':
            return self.max_bytes // self.channels
        return self.max_bytes // self.channels // 2

    def _update_info(self):
        n = self.n_frames()
        dur_ms = int(n / self.rate * 1000)
        est = self._estimate_bytes()
        self.info_label.configure(
            text='Formato: %s | Rate: %d Hz | %s | Duracion: %d ms | Tamano estimado: %d B'
            % (self.fmt_label, self.rate, 'stereo' if self.stereo else 'mono', dur_ms, est))
        if self.max_bytes is not None:
            over = est > self.max_bytes
            self.limit_label.configure(
                text=('SUPERA EL LIMITE (%d / %d B)' if over else 'dentro del limite (%d / %d B)')
                % (est, self.max_bytes), fg='red' if over else '#1a5c1a')
        else:
            self.limit_label.configure(text='')

    def _on_press(self, event):
        w = self.canvas.winfo_width() or 780
        start, end = self._view_range()
        frame = self._x_to_frame(event.x, start, end, w)
        x0 = self._frame_to_x(self.sel_start, start, end, w)
        x1 = self._frame_to_x(self.sel_end, start, end, w)
        if self.sel_end > self.sel_start and abs(event.x - x0) <= self.MARKER_HIT_PX:
            self.drag_mode = 'start'
        elif self.sel_end > self.sel_start and abs(event.x - x1) <= self.MARKER_HIT_PX:
            self.drag_mode = 'end'
        else:
            self.drag_mode = 'new'
            self.sel_start = self.sel_end = max(0, min(self.n_frames(), frame))
        self._redraw()

    def _on_drag(self, event):
        w = self.canvas.winfo_width() or 780
        start, end = self._view_range()
        frame = max(0, min(self.n_frames(), self._x_to_frame(event.x, start, end, w)))
        if self.drag_mode == 'start':
            self.sel_start = min(frame, self.sel_end)
        elif self.drag_mode == 'end':
            self.sel_end = max(frame, self.sel_start)
        elif self.drag_mode == 'new':
            if frame >= self.sel_start:
                self.sel_end = frame
            else:
                self.sel_end = self.sel_start
                self.sel_start = frame
        self._redraw()

    def _on_release(self, event):
        self.drag_mode = None
        self.ini_var.set('%.0f' % (self.sel_start / self.rate * 1000))
        self.fin_var.set('%.0f' % (self.sel_end / self.rate * 1000))

    def _on_wheel(self, event):
        if event.delta > 0:
            self._zoom(1.4)
        else:
            self._zoom(1 / 1.4)

    def _on_scroll(self):
        self.scroll = self.scroll_var.get()
        self._redraw()

    def _zoom(self, factor):
        self.zoom = max(1.0, min(200.0, self.zoom * factor))
        self._redraw()

    def _zoom_reset(self):
        self.zoom = 1.0
        self.scroll = 0.0
        self.scroll_var.set(0.0)
        self._redraw()

    def _set_selection_from_fields(self):
        try:
            ini = max(0, int(float(self.ini_var.get()) / 1000 * self.rate))
            fin = min(self.n_frames(), int(float(self.fin_var.get()) / 1000 * self.rate))
            if fin > ini:
                self.sel_start, self.sel_end = ini, fin
                self._redraw()
        except ValueError:
            pass

    def _nudge_sel(self, which, delta_ms):
        # Mueve el borde inicio/fin de la selección con las flechas (paso en ms).
        try:
            if which == 'ini':
                v = max(0.0, float(self.ini_var.get()) + delta_ms)
                self.ini_var.set('%.0f' % v)
            else:
                v = max(0.0, float(self.fin_var.get()) + delta_ms)
                self.fin_var.set('%.0f' % v)
            self._set_selection_from_fields()
        except ValueError:
            pass

    def _sel_edge(self, edge):
        # Selecciona los primeros (ini) o últimos (fin) N ms para el fade.
        try:
            ms = max(1.0, float(self.fade_ms_var.get()))
        except ValueError:
            ms = 100.0
            self.fade_ms_var.set('100')
        fr = int(ms / 1000 * self.rate)
        n = self.n_frames()
        if edge == 'start':
            self.sel_start, self.sel_end = 0, min(n, fr)
        else:
            self.sel_start, self.sel_end = max(0, n - fr), n
        self.ini_var.set('%.0f' % (self.sel_start / self.rate * 1000))
        self.fin_var.set('%.0f' % (self.sel_end / self.rate * 1000))
        self._redraw()

    def _cmd_trim_to_selection(self):
        if self.sel_end <= self.sel_start:
            messagebox.showinfo('Editor', 'Primero arrastra sobre la onda para elegir una seleccion.')
            return
        self._push_undo()
        s0, s1 = self.sel_start, self.sel_end
        self.left = self.left[s0:s1]
        if self.right is not None:
            self.right = self.right[s0:s1]
        self.sel_start, self.sel_end = 0, self.n_frames()
        self._zoom_reset()

    def _cmd_delete_selection(self):
        if self.sel_end <= self.sel_start:
            messagebox.showinfo('Editor', 'Primero elegi una seleccion para eliminar.')
            return
        self._push_undo()
        s0, s1 = self.sel_start, self.sel_end
        self.left = self.left[:s0] + self.left[s1:]
        if self.right is not None:
            self.right = self.right[:s0] + self.right[s1:]
        self.sel_start = self.sel_end = s0
        self._redraw()

    def _fade_range(self):
        if self.sel_end > self.sel_start:
            return self.sel_start, self.sel_end
        return 0, self.n_frames()

    def _fade_gain(self, t, fade_in):
        # Curvas de sonoridad: lineal (brusca), suave=coseno (pareja),
        # exp (lenta al inicio). t en [0,1].
        c = self.fade_curve.get()
        x = t if fade_in else 1.0 - t
        if c == 'lineal':
            return x
        if c == 'exp':
            return x * x
        return math.sin(x * math.pi / 2)

    def _apply_fade(self, fade_in):
        s0, s1 = self._fade_range()
        if s1 <= s0:
            return
        self._push_undo()
        n = s1 - s0
        den = max(1, n - 1)  # llegar exacto a 0/1 en los bordes
        left = self.left
        right = self.right
        gain = self._fade_gain
        for i in range(n):
            g = gain(i / den, fade_in)
            left[s0 + i] = int(left[s0 + i] * g)
            if right is not None:
                right[s0 + i] = int(right[s0 + i] * g)
        self._redraw()

    def _cmd_fade_in(self):
        self._apply_fade(True)

    def _cmd_fade_out(self):
        self._apply_fade(False)

    def _cmd_normalize(self):
        if self.n_frames() == 0:
            return
        peak = max((abs(v) for v in self.left), default=0)
        if self.right is not None:
            peak = max(peak, max((abs(v) for v in self.right), default=0))
        if peak == 0:
            messagebox.showinfo('Editor', 'El audio esta en silencio, no hay nada que normalizar.')
            return
        target = 32000
        gain = target / peak
        if gain <= 1.0001:
            messagebox.showinfo('Editor', 'El audio ya esta cerca del maximo, no hace falta normalizar.')
            return
        self._push_undo()
        self.left = array.array('h', (max(-32768, min(32767, int(v * gain))) for v in self.left))
        if self.right is not None:
            self.right = array.array('h', (max(-32768, min(32767, int(v * gain))) for v in self.right))
        self._redraw()

    def _toggle_play(self):
        if self.playing:
            self._stop_play()
            return
        if not winsound:
            messagebox.showinfo('Editor', 'winsound no disponible en este Python.')
            return
        s0, s1 = (self.sel_start, self.sel_end) if self.sel_end > self.sel_start else (0, self.n_frames())
        if s1 <= s0:
            return
        seg_left = self.left[s0:s1]
        if self.right is not None:
            seg = interleave_pcm16(seg_left.tobytes(), self.right[s0:s1].tobytes())
        else:
            seg = seg_left.tobytes()
        tmp = os.path.join(tempfile.gettempdir(), 'mvc2_editor_preview.wav')
        write_wav(tmp, seg, self.rate, self.stereo)
        winsound.PlaySound(None, winsound.SND_PURGE)
        winsound.PlaySound(tmp, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        self.playing = True
        self.btn_play.configure(text='Stop (espacio)')
        dur_ms = max(50, int((s1 - s0) / self.rate * 1000))
        self.play_after_id = self.win.after(dur_ms, self._on_play_finished)

    def _on_play_finished(self):
        self.playing = False
        self.play_after_id = None
        try:
            self.btn_play.configure(text='Play (espacio)')
        except tk.TclError:
            pass

    def _stop_play(self):
        if winsound:
            winsound.PlaySound(None, winsound.SND_PURGE)
        self.playing = False
        if self.play_after_id:
            self.win.after_cancel(self.play_after_id)
            self.play_after_id = None
        self.btn_play.configure(text='Play (espacio)')

    def _on_apply(self):
        raw = self._encode_all()
        if self.max_bytes is not None:
            if len(raw) > self.max_bytes:
                messagebox.showwarning('Editor',
                    'El audio editado (%d B) sigue superando el limite estricto (%d B).\n'
                    'Recorta o elimina mas seleccion antes de aplicar.' % (len(raw), self.max_bytes))
                return
            if len(raw) < self.max_bytes:
                raw = raw + b'\x00' * (self.max_bytes - len(raw))
        self._stop_play()
        self.result = (self.fmt, self.stereo, raw)
        self.win.destroy()

    def _on_cancel(self):
        self._stop_play()
        self.result = None
        self.win.destroy()

    def run(self):
        self.win.wait_window()
        return self.result


class App:
    def __init__(self, root):
        self.root = root
        try:
            root.report_callback_exception = self._report_callback_exception
        except:
            pass
        self.log_path = os.path.join(app_dir(), 'MVC2_AudioTool.log')
        self.session = None
        self.mode = tk.StringVar(value='estricto')
        self.hybrid_flags = {}
        # Test ardilla: escucha previews a mitad de tasa sin tocar datos ni HD
        self.half_preview = tk.BooleanVar(value=True)
        # Vista de lista PS2: por voz ascendente (como latinos) o por slot
        self.order_voz = tk.BooleanVar(value=True)
        self.gif_frames = []
        self.gif_label = None
        self.gif_index = 0
        self.indicadores = {}
        root.title('MVC2 Audio Tool v2')
        _center_win(root, 880, 620)
        root.minsize(720, 520)
        self._set_icon(root)
        self._load_indicadores()

        logo_frame = tk.Frame(root)
        logo_frame.pack(fill='x', padx=8, pady=(8, 0))
        logo = [
            "███╗   ███╗██╗   ██╗ ██████╗██████╗ ████████╗ ██████╗  ██████╗ ██╗     ",
            "████╗ ████║██║   ██║██╔════╝╚════██╗╚══██╔══╝██╔═══██╗██╔═══██╗██║     ",
            "██╔████╔██║██║   ██║██║      █████╔╝   ██║   ██║   ██║██║   ██║██║     ",
            "██║╚██╔╝██║╚██╗ ██╔╝██║     ██╔═══╝    ██║   ██║   ██║██║   ██║██║     ",
            "██║ ╚═╝ ██║ ╚████╔╝ ╚██████╗███████╗   ██║   ╚██████╔╝╚██████╔╝███████╗",
            "╚═╝     ╚═╝  ╚═══╝   ╚═════╝╚══════╝   ╚═╝    ╚═════╝  ╚═════╝ ╚══════╝",
        ]
        tk.Label(logo_frame, text='\n'.join(logo), font=('Consolas', 6, 'bold'),
                 fg='#0a1a4f', justify='center').pack()
        tk.Label(logo_frame, text='by KiZeo', font=('Consolas', 9, 'bold'),
                 fg='#1a5c1a', justify='center').pack()
        self.ps2_label = tk.Label(logo_frame, text='PS2 EDITION', font=('Consolas', 18, 'bold'),
                                    fg='#b8860b', justify='center')
        self.ps2_label.pack()
        self._ps2_glow_colors = ['#8a6508', '#a67c00', '#c9971c', '#e6b800',
                                 '#ffd700', '#e6b800', '#c9971c', '#a67c00']
        self._ps2_glow_index = 0
        self._animate_ps2_label()
        self.info_label = tk.Label(root, text='Nada cargado aun', font=('Segoe UI', 9),
                                   fg='#555')
        self.info_label.pack(fill='x', padx=8, pady=(2, 0))

        self.char_frame = tk.Frame(root, bg='#d0d0d0', bd=1, relief='sunken', height=28)
        self.char_frame.pack(fill='x', padx=8, pady=(4, 0))
        self.char_frame.pack_propagate(False)
        self.char_label = tk.Label(self.char_frame, text='', font=('Consolas', 10, 'bold'),
                                   fg='#0a1a4f', bg='#d0d0d0')
        self.char_label.pack(fill='both', padx=4)

        modo_frame = tk.Frame(root)
        modo_frame.pack(fill='x', padx=8, pady=(2, 0))
        tk.Label(modo_frame, text='Modo:', font=('Segoe UI', 9, 'bold')).pack(side='right', padx=(8, 4))
        for val, txt, clr in (('estricto', 'ESTRICTO', '#8b0000'),
                              ('hibrido', 'HIBRIDO', '#7a5c00'),
                              ('libre', 'LIBRE', '#1a5c1a')):
            tk.Radiobutton(modo_frame, text=txt, variable=self.mode, value=val,
                           font=('Segoe UI', 8, 'bold'), fg=clr, selectcolor='#fff',
                           indicatoron=0, width=9, relief='raised', bd=1,
                           command=self._on_mode_change).pack(side='right', padx=2)
        # Casillas de vista en esta fila (la de botones iba saturada y
        # tapaba 'Salir' en ventanas estrechas).
        self.half_preview_cb = tk.Checkbutton(modo_frame, text='Oír ½ tasa', variable=self.half_preview)
        self.half_preview_cb.pack(side='left')
        self.order_voz_cb = tk.Checkbutton(modo_frame, text='Orden voz', variable=self.order_voz,
                                           command=self._toggle_order)
        self.order_voz_cb.pack(side='left', padx=4)

        btns = tk.Frame(root)
        btns.pack(fill='x', padx=8, pady=4)
        self.char_icon_photo = None
        self.char_icon_frame = tk.Frame(btns, width=40, height=40, bd=1, relief='groove',
                                        bg='#e8e8e8')
        self.char_icon_frame.pack(side='left', padx=(0, 6))
        self.char_icon_frame.pack_propagate(False)
        self.char_icon_label = tk.Label(self.char_icon_frame, bg='#e8e8e8')
        self.char_icon_label.pack(fill='both', expand=True)
        self.buttons = {}
        self.buttons['open'] = tk.Button(btns, text='Cargar .bin...', command=self.cmd_open)
        self.buttons['open'].pack(side='left')
        self.buttons['extract'] = tk.Button(btns, text='Extraer crudo...', command=self.cmd_extract, state='disabled')
        self.buttons['extract'].pack(side='left', padx=4)
        self.buttons['play'] = tk.Button(btns, text='Reproducir', command=self.cmd_play, state='disabled')
        self.buttons['play'].pack(side='left', padx=4)
        self.buttons['stop'] = tk.Button(btns, text='Detener', command=self.cmd_stop, state='disabled')
        self.buttons['stop'].pack(side='left')
        self.buttons['replace'] = tk.Button(btns, text='Cargar reemplazo...', command=self.cmd_replace, state='disabled')
        self.buttons['replace'].pack(side='left', padx=4)
        self.buttons['unreplace'] = tk.Button(btns, text='Quitar reemplazo', command=self.cmd_unreplace, state='disabled')
        self.buttons['unreplace'].pack(side='left')
        self.buttons['save'] = tk.Button(btns, text='Guardar como...', command=self.cmd_save, state='disabled')
        self.buttons['save'].pack(side='right')
        tk.Button(btns, text='Salir', command=root.destroy).pack(side='right', padx=4)

        mid = tk.Frame(root)
        mid.pack(fill='both', expand=True, padx=8)
        cols = ('idx', 'file', 'fmt', 'size', 'rate', 'dur', 'state')
        self.tree = ttk.Treeview(mid, columns=cols, show='tree headings', height=11)
        for c, w, t in (('idx', 50, 'Orden'), ('file', 110, 'Archivo'), ('fmt', 70, 'Formato'),
                        ('size', 70, 'Tamano'), ('rate', 60, 'Tasa'), ('dur', 60, 'Dur(ms)'),
                        ('state', 90, 'Estado')):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor='center' if c == 'idx' else ('w' if c in ('file', 'fmt', 'state') else 'e'))
        self.tree.column('#0', width=38, stretch=False, anchor='center')
        self.tree.tag_configure('repl_row', foreground='#0a8a0a')
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<Double-1>', lambda e: self.cmd_play())
        self.tree.bind('<<TreeviewSelect>>', self.on_select)
        self.tree.bind('<Button-3>', self._on_right_click)
        self.tree.bind('<Button-1>', self._toggle_hybrid)

        bottom = tk.Frame(root)
        bottom.pack(fill='x', padx=8, pady=(4, 8))
        self.logbox = scrolledtext.ScrolledText(bottom, state='disabled', wrap='word',
                                                font=('Consolas', 8), height=5)
        self.logbox.pack(side='left', fill='both', expand=True, padx=(0, 8))
        self.gif_label = tk.Label(bottom)
        self.gif_label.pack(side='right', anchor='se')
        gif_path = resource_path('assets', 'lain.gif')
        self._load_gif(gif_path)

        if HAVE_DND:
            for w in (root, self.tree, self.logbox):
                w.drop_target_register(DND_FILES)
                w.dnd_bind('<<Drop>>', self.on_drop)
            self.log('Arrastra y suelta activado (tkinterdnd2).')
        else:
            self.log('Arrastra y suelta no disponible: usa los botones.')
        self.ctx_menu = tk.Menu(root, tearoff=0)
        self.ctx_menu.add_command(label='Reemplazar...', command=self.cmd_replace)
        self.ctx_menu.add_command(label='Reproducir', command=self.cmd_play)
        self.ctx_menu.add_command(label='Recortar...', command=self._open_trim)

        if HAVE_DTPK:
            self.log('DTPKDump.py cargado correctamente (orden real del secuenciador activo).')
        else:
            self.log('AVISO: DTPKDump.py NO cargó (%s). Usando orden de respaldo '
                     '(puede no coincidir con el orden real del juego).' % DTPK_LOAD_ERROR)
            root.after(400, lambda: messagebox.showwarning(
                'MVC2 Audio Tool - DTPKDump.py no cargó',
                'No se pudo cargar DTPKDump.py, asi que el orden de samples que '
                'ves en pantalla es un orden de RESPALDO (simple, secuencial) y '
                'puede NO coincidir con el orden real de doblaje del juego.\n\n'
                'Detalle tecnico: %s\n\n'
                'Si esto pasa con el .exe compilado (y el .py suelto anda bien), '
                'es casi seguro un problema al empaquetar DTPKDump.py o sus '
                'dependencias (aifc/chunk/audioop) con PyInstaller.' % DTPK_LOAD_ERROR))

    def _on_right_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            self.on_select()
            self.ctx_menu.tk_popup(event.x_root, event.y_root)

    def _set_icon(self, root):
        try:
            icopath = resource_path('app.ico')
            if os.path.isfile(icopath):
                root.iconbitmap(icopath)
        except Exception:
            pass

    def _load_indicadores(self):
        try:
            from PIL import Image, ImageTk
            base = resource_path('assets')
            verde = Image.open(os.path.join(base, 'verde.png')).resize((20, 20), Image.LANCZOS)
            rojo = Image.open(os.path.join(base, 'rojo.png')).resize((20, 20), Image.LANCZOS)
            self.indicadores = {
                True: ImageTk.PhotoImage(rojo),
                False: ImageTk.PhotoImage(verde),
            }
        except Exception:
            self.indicadores = {}
        self._char_icon_cache = {}
        self._char_icon_files = None

    def _character_icon_path(self, who):
        if not who:
            return None
        key = who.strip().lower()
        override = CHARACTER_ICON_OVERRIDES.get(key)
        base_dir = resource_path('assets', 'characters')
        if self._char_icon_files is None:
            try:
                self._char_icon_files = os.listdir(base_dir)
            except OSError:
                self._char_icon_files = []
        if override:
            for fname in self._char_icon_files:
                if os.path.splitext(fname)[0].lower() == override.lower():
                    return os.path.join(base_dir, fname)
        target = _normalize_char_name(who)
        for fname in self._char_icon_files:
            stem = os.path.splitext(fname)[0]
            if _normalize_char_name(stem) == target:
                return os.path.join(base_dir, fname)
        return None

    def _update_char_icon(self, who):
        if not hasattr(self, 'char_icon_label'):
            return
        if not who:
            self.char_icon_label.configure(image='', text='')
            self.char_icon_photo = None
            return
        cache_key = who.strip().lower()
        photo = self._char_icon_cache.get(cache_key, False)
        if photo is False:
            path = self._character_icon_path(who)
            photo = None
            if path:
                try:
                    from PIL import Image, ImageTk
                    img = Image.open(path).convert('RGBA').resize((38, 38), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(img)
                except Exception:
                    photo = None
            self._char_icon_cache[cache_key] = photo
        if photo:
            self.char_icon_label.configure(image=photo, text='')
            self.char_icon_photo = photo
        else:
            self.char_icon_label.configure(image='', text='?')
            self.char_icon_photo = None

    def _load_gif(self, path):
        try:
            from PIL import Image, ImageTk
            img = Image.open(path)
            w, h = img.size
            nw, nh = max(1, w // 4), max(1, h // 4)
            frames = []
            try:
                while True:
                    frame = img.copy().resize((nw, nh), Image.LANCZOS)
                    frames.append(ImageTk.PhotoImage(frame))
                    img.seek(img.tell() + 1)
            except EOFError:
                pass
            if frames:
                self.gif_frames = frames
                self._animate_gif()
        except Exception:
            pass

    def _animate_gif(self):
        if not self.gif_frames or not self.gif_label:
            return
        self.gif_label.configure(image=self.gif_frames[self.gif_index])
        self.gif_index = (self.gif_index + 1) % len(self.gif_frames)
        self.root.after(80, self._animate_gif)

    def _animate_ps2_label(self):
        # Pulso dorado lento: se ilumina y se apaga (~2.5s por ciclo)
        try:
            if self.ps2_label and self.ps2_label.winfo_exists():
                self.ps2_label.configure(fg=self._ps2_glow_colors[self._ps2_glow_index])
                self._ps2_glow_index = (self._ps2_glow_index + 1) % len(self._ps2_glow_colors)
                self.root.after(300, self._animate_ps2_label)
        except tk.TclError:
            pass

    def _on_mode_change(self):
        modo = self.mode.get()
        self.log('Modo: ' + modo.upper())
        self._refresh_tree_display()

    def _refresh_tree_display(self):
        modo = self.mode.get()
        for iid in self.tree.get_children():
            if modo == 'hibrido' and self.indicadores:
                idx = int(iid)
                flag = self.hybrid_flags.get(idx, False)
                self.tree.item(iid, image=self.indicadores[flag])
            else:
                self.tree.item(iid, image='')

    def _toggle_hybrid(self, event):
        if self.mode.get() != 'hibrido' or self.session is None or not self.indicadores:
            return
        region = self.tree.identify_region(event.x, event.y)
        if region not in ('tree', 'cell'):
            return
        col = self.tree.identify_column(event.x)
        if col != '#0':
            return
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        idx = int(iid)
        self.hybrid_flags[idx] = not self.hybrid_flags.get(idx, False)
        flag = self.hybrid_flags[idx]
        self.tree.item(iid, image=self.indicadores[flag])
        estado = 'ESTRICTO' if flag else 'LIBRE'
        self.log('  sample %03d: %s' % (idx, estado))
        return 'break'

    def _open_trim(self):
        if self.session is None:
            return
        if self.session['kind'] == 'ps2':
            self._open_trim_ps2()
            return
        if self.session['kind'] != 'dtpk':
            return
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo('MVC2 Audio Tool', 'Selecciona un sample.')
            return
        idx = int(sel[0])
        order_item = self.session['play_order'][idx]
        sample_idx = order_item['sample']
        e = self.session['entries'][sample_idx]
        rate = translate_rate(order_item['rate16'])
        current = self.session['repl'][idx]
        if current:
            fmt, stereo, raw = current
        else:
            fmt, stereo, raw = e['format'], e['stereo'], self.session['raws'][sample_idx]
        pcm = decode_sample(fmt, stereo, raw)
        editor = WaveEditor(self.root, 'Editar sample %03d' % idx, pcm, rate, stereo, fmt,
                            max_bytes=None, original_bytes=e['bytes'])
        result = editor.run()
        if result is None:
            return
        out_fmt, out_stereo, out_raw = result
        dur_ms = self._bytes_to_ms(len(out_raw), out_fmt, rate)
        self.session['repl'][idx] = (out_fmt, out_stereo, out_raw)
        self.tree.set(str(idx), 'fmt', FORMAT_LABEL[out_fmt])
        self.tree.set(str(idx), 'size', '%d B' % len(out_raw))
        self.tree.set(str(idx), 'dur', '%d ms' % dur_ms)
        self._set_row_state(str(idx), 'editado')
        self.log('Sample %03d editado con el editor de onda (%s, %d B, %d ms)'
                 % (idx, FORMAT_LABEL[out_fmt], len(out_raw), dur_ms))
        self.on_select()

    def _open_trim_ps2(self):
        # Editor de onda para samples PS2 VAG (recortar/normalizar/fade).
        # Decodifica con vag_to_pcm16, edita como PCM16 y recodifica con wav2vag.
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo('MVC2 Audio Tool', 'Selecciona un sample.')
            return
        idx = int(sel[0])
        e = self.session['entries'][idx]
        rates = self.session.get('repl_rates') or [None]*len(self.session['entries'])
        hd_rate = rates[idx] or e['rate']
        # El editor trabaja con samples: se audita a tasa REAL de juego
        # (dividida si toca; si no, ardilla). La cabecera VAG sigue en HD.
        try:
            _sr = ((self.session.get('bridge') or {}).get(idx, {}) or {}).get('srates')
        except:
            _sr = None
        rate = _ps2_true_rate(self.session.get('path', ''), idx, hd_rate, _sr)
        current = self.session['repl'][idx]
        if current:
            _, _, raw_cur = current
        else:
            raw_cur = self.session['raws'][idx]
        try:
            pcm = vag_to_pcm16(raw_cur)
        except Exception as ex:
            messagebox.showerror('MVC2 Audio Tool', f'No se pudo decodificar el VAG: {ex}')
            return
        audio_slot = max(0, e['size'] - 16)
        editor = WaveEditor(self.root, 'Editar VAG %02d (%d Hz, slot %d B)' % (idx, rate, e['size']),
                            pcm, rate, False, 'pcm16',
                            max_bytes=None, original_bytes=audio_slot,
                            fmt_label='VAG ADPCM 4bit')
        result = editor.run()
        if result is None:
            return
        _, _, out_pcm = result
        # Recodificar PCM editado -> VAG con wav2vag a la tasa HD
        wav2vag_path = resource_path('wav2vag.exe')
        if not os.path.isfile(wav2vag_path):
            wav2vag_path = shutil.which('wav2vag.exe') or os.path.join(os.path.dirname(__file__), 'wav2vag.exe')
        if not os.path.isfile(wav2vag_path or ''):
            messagebox.showerror('MVC2 Audio Tool', 'Falta wav2vag.exe junto al script.')
            return
        import tempfile as tf
        with tf.NamedTemporaryFile(delete=False, suffix='.wav') as f:
            tmp_wav = f.name
        with tf.NamedTemporaryFile(delete=False, suffix='.VAG') as f:
            tmp_vag = f.name
        try:
            w = wave.open(tmp_wav, 'wb')
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(hd_rate)
            w.writeframes(out_pcm); w.close()
            # wav2vag convierte WAV PCM16 (no raw): sin -sraw16
            res = _run([wav2vag_path, tmp_wav, tmp_vag, f'-freq={hd_rate}'],
                                 capture_output=True, text=True)
            if res.returncode != 0 or not os.path.exists(tmp_vag):
                raise ValueError(f'wav2vag falló: {res.stdout} {res.stderr}')
            vag_data = open(tmp_vag, 'rb').read()
            raw = vag_data[0x30:] if len(vag_data) >= 0x30 and vag_data[0:4] == b'VAGp' else vag_data
            if len(raw) >= 16 and raw[-16:] == bytes.fromhex("00 07 77 77 77 77 77 77 77 77 77 77 77 77 77 77"):
                raw = raw[:-16]
        except Exception as ex:
            messagebox.showerror('MVC2 Audio Tool', str(ex))
            return
        finally:
            try: os.remove(tmp_wav)
            except: pass
            try: os.remove(tmp_vag)
            except: pass
        dur_ms = int(len(raw)/16*28 / max(1, rate) * 1000)
        self.session['repl'][idx] = ('vag', False, raw)
        if 'repl_rates' not in self.session or len(self.session['repl_rates']) != len(self.session['entries']):
            self.session['repl_rates'] = [None]*len(self.session['entries'])
        if 'repl_drate' not in self.session or len(self.session['repl_drate']) != len(self.session['entries']):
            self.session['repl_drate'] = [None]*len(self.session['entries'])
        # El trim no decide HD: conserva el ya elegido (o el original si es None)
        self.session['repl_rates'][idx] = hd_rate
        self.session['repl_drate'][idx] = rate
        _hr = self.session.get('repl_rates', [None]*len(self.session['entries']))[idx]
        self.tree.set(str(idx), 'size', '%d B' % len(raw))
        self.tree.set(str(idx), 'rate', '%d Hz' % rate if not _hr or _hr == rate else '%d Hz (HD %d)' % (rate, _hr))
        self.tree.set(str(idx), 'dur', '%d ms' % dur_ms)
        self._set_row_state(str(idx), 'editado')
        self.log('VAG %02d editado con el editor de onda (%d B, %d ms, datos %d Hz%s)' % (idx, len(raw), dur_ms, rate, f", HD {_hr}" if _hr and _hr != rate else ""))
        self.on_select()

    def _bytes_to_ms(self, nbytes, fmt, rate):
        if rate <= 0:
            return 0
        if fmt == 'adpcm':
            return int(nbytes * 2 / rate * 1000)
        if fmt == 'pcm8':
            return int(nbytes / rate * 1000)
        if fmt == 'pcm16':
            return int(nbytes / 2 / rate * 1000)
        return int(nbytes / rate * 1000)

    def _set_row_state(self, iid, state):
        """Actualiza la columna Estado y pinta la fila entera de verde
        cuando el sample fue reemplazado o editado (el Treeview no permite
        colorear solo una palabra/celda, asi que se resalta la fila)."""
        self.tree.set(iid, 'state', state)
        if state in ('reemplazado', 'editado'):
            self.tree.item(iid, tags=('repl_row',))
        else:
            self.tree.item(iid, tags=())

    def _open_trim_forced(self, idx, entry, rate, raw_loaded, fmt, stereo, max_bytes):
        pcm = decode_sample(fmt, stereo, raw_loaded)
        editor = WaveEditor(self.root, 'RECORTE ESTRICTO - sample %03d' % idx, pcm, rate,
                            stereo, fmt, max_bytes=max_bytes, original_bytes=max_bytes)
        result = editor.run()
        if result is None:
            return None
        _, _, out_raw = result
        return out_raw

    def log(self, msg):
        self.logbox.configure(state='normal')
        self.logbox.insert('end', msg + '\n')
        self.logbox.see('end')
        self.logbox.configure(state='disabled')
        self.root.update_idletasks()
        try:
            with open(self.log_path, 'a', encoding='utf-8') as f:
                f.write(msg + '\n')
        except Exception:
            pass

    def _report_callback_exception(self, exc, val, tb):
        # Errores en callbacks de Tk (drag&drop, botones, animaciones):
        # al log en vez de perderse en una stderr invisible.
        try:
            import traceback
            self.log('ERROR en evento Tk:\n' + ''.join(traceback.format_exception(exc, val, tb)))
        except:
            pass

    def set_buttons(self, state):
        for name in ('extract', 'play', 'stop', 'replace', 'unreplace', 'save'):
            b = self.buttons.get(name)
            if b:
                b.configure(state=state)

    def on_drop(self, event):
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        for p in paths:
            self.handle(os.path.abspath(p))

    def handle(self, path):
        if os.path.isdir(path):
            self.log('')
            self.log('REPACK: %s' % path)
            try:
                repack_path(path, self.log)
                messagebox.showinfo('MVC2 Audio Tool', 'Carpeta reempaquetada:\n%s' % path)
            except Exception as e:
                self.log('ERROR: %s' % e)
                messagebox.showerror('MVC2 Audio Tool', str(e))
        else:
            try:
                self.load_bin(path)
            except Exception as e:
                self.log('ERROR: %s' % e)
                messagebox.showerror('MVC2 Audio Tool', str(e))

    def _ps2_voz(self, idx):
        # Número de voz latino del slot (None si no hay mapeo).
        try:
            lat = (self.session.get('latino_map') or {}).get(idx)
            if not lat:
                return None
            mv = re.search(r'(\d+)\.wav$', os.path.basename(lat), re.I)
            return int(mv.group(1)) if mv else None
        except:
            return None

    def _ps2_tag(self, idx):
        # Etiqueta corta con slot y voz para logs: "07" o "07/v11".
        try:
            v = self._ps2_voz(idx)
            return f"{idx:02d}/v{v:02d}" if v is not None else f"{idx:02d}"
        except:
            return f"{idx:02d}"

    def _fill_ps2_rows(self, p=None):
        # (Re)llena la lista PS2. Por voz ascendente (defecto, como latinos)
        # o por slot (fiel al .bin, estilo bankmod). El iid = índice siempre.
        try:
            if p is None:
                p = dict(entries=self.session['entries'])
            lmap = (self.session.get('latino_map') or {})
            if self.order_voz.get():
                def _vkey(i):
                    v = _voz_of_latino(lmap.get(i)) if i in lmap else None
                    return (1, i) if v is None else (0, v, i)
                order = sorted(range(len(p['entries'])), key=_vkey)
            else:
                order = list(range(len(p['entries'])))
            self.tree.delete(*self.tree.get_children())
            bdet = (self.session.get('bridge') or {})
            for idx in order:
                e = p['entries'][idx]
                is_blank = e['size'] <= 200
                orden_txt, fname = self._ps2_disp(idx)
                try:
                    _sr = (bdet.get(idx, {}) or {}).get('srates')
                    _tr = _ps2_true_rate(self.session.get('path', ''), idx, e['rate'], _sr)
                except:
                    _tr = e['rate']
                display_rate = f"{_tr} Hz" if _tr == e['rate'] else f"{_tr} Hz (HD {e['rate']})"
                dur_ms = int(((e['size']-16)//16*28 / max(1,_tr) *1000) if e['size']>16 else 0)
                if is_blank:
                    est = 'blank'
                else:
                    meth = bdet.get(idx, {}).get('method')
                    est = {'fijo': 'fijo oído', 'dtpk': 'puente',
                           'dub': 'doblado', 'dudoso': 'revisar oído'}.get(meth, 'sin wav' if idx not in lmap else 'original')
                self.tree.insert('', 'end', iid=str(idx), values=(
                    orden_txt, fname,
                    'VAG ADPCM' + (' (blank)' if is_blank else ''),
                    f"{e['size']} B", display_rate, f"{dur_ms} ms",
                    est))
                try:
                    rp = self.session.get('repl')
                    if rp and rp[idx] is not None:
                        fmt, stereo, raw = rp[idx]
                        rr = (self.session.get('repl_rates') or [None]*len(p['entries']))[idx]
                        dr = (self.session.get('repl_drate') or [None]*len(p['entries']))[idx]
                        self.tree.set(str(idx), 'size', '%d B' % len(raw))
                        if rr and dr and rr != dr:
                            self.tree.set(str(idx), 'rate', '%d Hz (HD %d)' % (dr, rr))
                        elif dr:
                            self.tree.set(str(idx), 'rate', '%d Hz' % dr)
                        self.tree.set(str(idx), 'dur', '%d ms' % int(len(raw)/16*28/max(1, dr or e['rate'])*1000))
                        self.tree.set(str(idx), 'file', (self.session.get('repl_src') or {}).get(idx, 'reemplazado'))
                        self._set_row_state(str(idx), 'reemplazado')
                except:
                    pass
        except:
            pass

    def _toggle_order(self):
        if self.session is not None and self.session.get('kind') == 'ps2':
            sel = self.tree.selection()
            self._fill_ps2_rows()
            self._refresh_tree_display()
            for s in sel:
                try:
                    if self.tree.exists(s):
                        self.tree.selection_add(s)
                except:
                    pass

    def _ps2_disp(self, idx):
        # Textos de fila PS2 (orden=voz como el PS3, archivo=latino si hay mapa).
        e = self.session['entries'][idx]
        base = os.path.splitext(self.session['source'])[0]
        repl = self.session['repl'][idx]
        voz = self._ps2_voz(idx)
        orden_txt = f"{voz:02d}" if voz is not None else f"{idx:02d}"
        if repl is not None:
            fname = self.session.get('repl_src', {}).get(idx) or f"VAG_{idx:02d} (reemplazado)"
        elif voz is not None:
            fname = os.path.basename((self.session.get('latino_map') or {})[idx])
        else:
            fname = f"{base}_VAG_{idx:02d}_Rate_{e['rate']:04d}.VAG"
        return orden_txt, fname

    def load_bin(self, path):
        data = open(path, 'rb').read()
        name = os.path.basename(path)
        base = os.path.splitext(name)[0]
        self.session = None
        self.tree.delete(*self.tree.get_children())
        self._update_char_icon('')
        if is_ps2_container(data):
            p = parse_ps2_container(data)
            # Preparar raws y session para PS2 VAG
            raws = []
            # Para GUI usaremos entries directos (cada VAG es un sample)
            for e in p['entries']:
                raw = p['bd'][e['offset']:e['offset']+e['size']]
                # Quitar end marker para reproducción/edición (16B)
                if len(raw)>=16 and raw[-16:]==bytes.fromhex("00 07 77 77 77 77 77 77 77 77 77 77 77 77 77 77"):
                    raw = raw[:-16]
                raws.append(raw)
            self.session = dict(kind='ps2', path=path, source=name,
                                hd_off=p['hd_off'], hd_sz=p['hd_sz'], bd_off=p['bd_off'], bd_sz=p['bd_sz'],
                                hd=p['hd'], bd=p['bd'], vagi_off=p['vagi_off'], max_idx=p['max_idx'],
                                entries=p['entries'], raws=raws, repl=[None]*len(p['entries']),
                                repl_rates=[None]*len(p['entries']),
                                repl_drate=[None]*len(p['entries']),
                                repl_src={},
                                latino_map=latino_map_for_ps2(path, p['entries']),
                                original_size=len(data))
            lmap = self.session['latino_map']
            self.session['bridge'] = bridge_detail_for_bin(path)
            bdet = self.session['bridge']
            if lmap:
                self.log(f"Latino detectado: {len(lmap)} wavs mapeados (slot -> wav, n. wav = n. voz)")
                for s in sorted(lmap, key=lambda x: (os.path.basename(lmap[x]), x)):
                    d = bdet.get(s, {})
                    tag = {'fijo': 'fijo', 'dtpk': 'puente %.2f' % (d.get('score') or 0),
                           'dub': 'doblado'}.get(d.get('method'), '?')
                    self.log(f"  voz {os.path.basename(lmap[s])[-7:-4]} <- slot {s:02d} [{tag}]")
            # Filas en ORDEN POR VOZ ascendente (como la carpeta de latinos y
            # el PS3: 00, 03, 04...). Sin mapeo van al final por slot.
            # El iid sigue siendo el índice (la selección no cambia).
            self._fill_ps2_rows(p)
            who = CHARACTERS.get(base[:4], '') if len(base)>=4 else ''
            self.info_label.configure(text=f"{name} | PS2 HD/BD VAG {len(p['entries'])} samples")
            self.char_label.configure(text=who.upper() if who else name)
            self._update_char_icon(who)
            self.log(f"Cargado {name}: PS2 HD/BD con {len(p['entries'])} VAGs (IECS)")
            self.set_buttons('normal')
            self._refresh_tree_display()
            return
        elif data[0:4] == b'DTPK':
            p = parse_dtpk(data)
            raws = []
            for e in p['entries']:
                rel = 0 if e['bytes'] == len(data) - p['soff'] else e['offset'] - p['soff']
                raws.append(data[p['soff'] + rel: p['soff'] + rel + e['bytes']])
            self.session = dict(kind='dtpk', path=path, source=name,
                                header=data[:p['soff']], tail=data[p['data_end']:],
                                entries=p['entries'], playback=p['playback'],
                                play_order=p['play_order'],
                                raws=raws, repl=[None] * len(p['play_order']),
                                original_size=len(data))
            for idx, item in enumerate(p['play_order']):
                sample_idx = item['sample']
                e = p['entries'][sample_idx]
                rate = translate_rate(item['rate16'])
                fname = f"{base}_{item['group']:02d}_{item['track']:02d}_SPD_{item['playback_id']:02X}_Sample_{sample_idx:02X}_Rate_{item['rate16']:04X}.{EXT_BY_FORMAT[e['format']]}"
                self.tree.insert('', 'end', iid=str(idx), values=(
                    '%02d' % item['track'],
                    fname,
                    FORMAT_LABEL[e['format']],
                    '%d B' % len(raws[sample_idx]),
                    rate_text(item['rate16']),
                    '%d ms' % self._bytes_to_ms(len(raws[sample_idx]), e['format'], rate),
                    'original'
                ))
            who = CHARACTERS.get(base, '')
            if not who and len(base) >= 4:
                who = CHARACTERS.get(base[:4], '')
            self.info_label.configure(
                text='%s  |  %s  |  %d samples' % (name, ('%s - ' % who) if who else '', len(p['play_order'])))
            self.char_label.configure(text=who.upper() if who else name)
            self._update_char_icon(who)
            self.log('Cargado %s: DTPK con %d samples (orden secuenciador, únicos)' % (name, len(p['play_order'])))
            self.set_buttons('normal')
            self._refresh_tree_display()
            return
        kind = detect_stream(data)
        if kind:
            self.session = dict(kind='stream', path=path, source=name, stream_kind=kind, data=data,
                                original_size=len(data))
            if kind == 'adx':
                pcm, rate, ch = decode_adx_to_pcm16(data)
                self.session['pcm'] = pcm
                self.session['rate'] = rate
                self.session['stereo'] = ch == 2
                self.tree.insert('', 'end', iid='0', values=('---', name, 'ADX v3',
                                                             '%d B' % len(data),
                                                             '%d Hz' % rate, 'original'))
                self.info_label.configure(text='%s  |  ADX %d Hz %s' % (name, rate, 'stereo' if ch == 2 else 'mono'))
                self.log('Cargado %s: ADX v3, %d Hz, %d canales, %.1f s'
                         % (name, rate, ch, len(pcm) / (2 * rate)))
            else:
                self.tree.insert('', 'end', iid='0', values=('---', name, 'MPEG-1 L2',
                                                             '%d B' % len(data), '-', 'original'))
                self.info_label.configure(text='%s  |  MPEG-1 Layer 2 (sin reproducir: falta ffmpeg)' % name)
                self.log('Cargado %s: MPEG-1 Layer 2 (reproduccion requiere ffmpeg)' % name)
            self.set_buttons('normal')
            return
        cat = classify_bin(name, data)
        if cat:
            grupo, desc = cat
            self.log('NO ES AUDIO: %s -> %s (%s)' % (name, grupo, desc))
            messagebox.showinfo('MVC2 Audio Tool - No es audio',
                                '%s\n\nEste archivo NO es audio: %s\n\nPor eso no aparece en el estudio.\n'
                                '(Grupo: %s)' % (name, desc, grupo))
            return
        raise ValueError('no es un contenedor de audio reconocido (DTPK / ADX / MPEG-1 Layer 2)')

    def on_select(self, _event=None):
        if self.session is None:
            return
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        if self.session['kind'] == 'dtpk':
            idx = int(iid)
            self.buttons['unreplace'].configure(state='normal' if self.session['repl'][idx] else 'disabled')
        elif self.session['kind'] == 'ps2':
            idx = int(iid)
            has = bool(self.session['repl'][idx]) if idx < len(self.session['repl']) else False
            self.buttons['unreplace'].configure(state='normal' if has else 'disabled')
        else:
            self.buttons['unreplace'].configure(state='normal' if self.session.get('repl_data') else 'disabled')

    def cmd_open(self):
        path = filedialog.askopenfilename(
            title='Elegir .bin de audio (DTPK/ADX/MPEG)',
            filetypes=[('BIN', '*.bin'), ('Todos', '*.*')])
        if path:
            self.handle(path)

    def cmd_extract(self):
        if self.session is None:
            return
        path = self.session['path']
        base = os.path.splitext(os.path.basename(path))[0]
        modo = self._ask_extract_mode()
        if modo is None:
            return
        dest = filedialog.askdirectory(title='Carpeta destino de la extracción',
                                       initialdir=os.path.dirname(path))
        if not dest:
            return
        outdir = os.path.join(dest, base + '_extraido')
        k = 2
        while os.path.exists(outdir):
            outdir = os.path.join(dest, base + f'_extraido_{k}')
            k += 1
        try:
            if modo in ('raw', 'both'):
                outdir = extract_path(path, self.log, outdir)
            if modo in ('wav', 'both'):
                hr = bool(self.half_preview.get()) if self.session is not None and self.session.get('kind') == 'ps2' else False
                extract_dtpk_wav(path, outdir + '_wav' if modo == 'both' else outdir, self.log, half_rates=hr)
            if modo == 'raw':
                messagebox.showinfo('MVC2 Audio Tool', 'Originales extraidos en:\n%s' % outdir)
            elif modo == 'wav':
                messagebox.showinfo('MVC2 Audio Tool', 'WAV decodificados en:\n%s' % outdir)
            else:
                messagebox.showinfo('MVC2 Audio Tool',
                                    'Originales: %s\nWAV: %s' % (outdir, outdir + '_wav'))
        except Exception as ex:
            messagebox.showerror('MVC2 Audio Tool', str(ex))

    def _ask_extract_mode(self):
        dlg = tk.Toplevel(self.root)
        dlg.title('Formato de extraccion')
        _center_win(dlg, 330, 210, parent=self.root)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()
        tk.Label(dlg, text='¿Como quieres extraer?', font=('Segoe UI', 10, 'bold')).pack(pady=(12, 6))
        var = tk.StringVar(value='wav')
        for val, txt, desc in (
                ('wav', 'Todo a WAV',
                 'Cada sample como WAV reproducible (rate correcto).'),
                ('raw', 'Originales (crudo)',
                 'Archivos crudos .yadpcm/.pcm8/.pcm16 + manifest.'),
                ('both', 'Ambos', 'Carpeta cruda + carpeta WAV.')):
            f = tk.Frame(dlg)
            f.pack(fill='x', padx=18, pady=2)
            tk.Radiobutton(f, variable=var, value=val).pack(side='left')
            tk.Label(f, text=txt, font=('Segoe UI', 9, 'bold'), anchor='w').pack(side='left')
            tk.Label(f, text=desc, font=('Segoe UI', 8), fg='#555', anchor='w').pack(side='left', padx=(8, 0))
        resultado = [None]
        def ok():
            resultado[0] = var.get()
            dlg.destroy()
        tk.Button(dlg, text='Extraer', width=12, command=ok).pack(pady=14)
        dlg.wait_window()
        return resultado[0]

    def cmd_play(self):
        if self.session is None:
            return
        try:
            if self.session['kind'] == 'stream':
                if self.session['stream_kind'] == 'mpeg':
                    messagebox.showinfo('MVC2 Audio Tool',
                                        'No hay decodificador MPEG-1 Layer 2 (se necesita ffmpeg).\n'
                                        'Si instalas ffmpeg y lo anade a PATH, funcionara.')
                    return
                write_wav(PREVIEW_WAV, self.session['pcm'], self.session['rate'], self.session['stereo'])
            elif self.session['kind'] == 'ps2':
                sel = self.tree.selection()
                if not sel:
                    messagebox.showinfo('MVC2 Audio Tool', 'Selecciona un sample de la lista.')
                    return
                idx = int(sel[0])
                item = self.session['repl'][idx]
                e = self.session['entries'][idx]
                if item:
                    fmt, stereo, raw = item
                    # Preview a tasa de DATOS (lo que el juego toca: HD/2 en doblados)
                    rate = (self.session.get('repl_drate') or [None]*len(self.session['entries']))[idx] or e['rate']
                else:
                    raw = self.session['raws'][idx]
                    rate = e['rate']
                # Decodificar VAG con el decoder Python entero (idéntico a ffmpeg/juego,
                # SNR 59dB medido; evita depender de ffmpeg para VAG).
                import tempfile as tf
                pcm = b""
                via = ''
                try:
                    pcm = vag_to_pcm16(raw)
                    via = 'python(vag_to_pcm16-int)'
                except Exception as ex_py:
                    # Fallback ffmpeg por si el raw viene en variante rara
                    try:
                        with tf.NamedTemporaryFile(delete=False, suffix='.VAG') as tmp_vag:
                            hdr = bytearray(0x30)
                            hdr[0:4]=b'VAGp'
                            hdr[4:8]=struct.pack('>I',0x20)
                            hdr[8:12]=struct.pack('>I',0)
                            hdr[12:16]=struct.pack('>I', len(raw)+16)
                            hdr[16:20]=struct.pack('>I', rate)
                            hdr[0x20:0x30]=f"VAG{idx:02d}".encode()[:16].ljust(16,b'\x00')
                            tmp_vag.write(hdr + raw + bytes.fromhex("00 07 77 77 77 77 77 77 77 77 77 77 77 77 77 77"))
                            tmp_vag_path=tmp_vag.name
                        with tf.NamedTemporaryFile(delete=False, suffix='.wav') as tmp_wav:
                            tmp_wav_path=tmp_wav.name
                        ffmpeg_path = resource_path('ffmpeg.exe')
                        if not os.path.isfile(ffmpeg_path):
                            ffmpeg_path = shutil.which('ffmpeg') or shutil.which('ffmpeg.exe')
                        if ffmpeg_path and os.path.isfile(ffmpeg_path):
                            res = _run([ffmpeg_path, '-y', '-i', tmp_vag_path, tmp_wav_path], capture_output=True)
                            if res.returncode==0 and os.path.exists(tmp_wav_path) and os.path.getsize(tmp_wav_path)>44:
                                w=wave.open(tmp_wav_path,'rb')
                                pcm=w.readframes(w.getnframes())
                                if w.getnchannels()==2:
                                    pcm=make_mono(pcm)
                                if w.getframerate() != rate:
                                    pcm=resample_pcm16(pcm, w.getframerate(), rate)
                                w.close()
                                via = 'ffmpeg(fallback)'
                        try: os.remove(tmp_vag_path)
                        except: pass
                        try: os.remove(tmp_wav_path)
                        except: pass
                    except:
                        pass
                    if not pcm:
                        self.log(f"Play VAG {idx:02d}: decoder python falló ({ex_py}) y sin fallback")
                        pcm = b"\x00\x00"*1024
                        via = 'silencio'
                # Tasa de juego: el ORIGINAL dividido se toca a HD/2 (si no,
                # ardilla). Los reemplazos ya traen tasa de datos: tal cual.
                # La casilla 1/2 solo fuerza mitad en slots NO divididos de
                # la familia 18/24k (test manual heredado).
                is_repl = item is not None
                try:
                    _sr = ((self.session.get('bridge') or {}).get(idx, {}) or {}).get('srates')
                except:
                    _sr = None
                if is_repl:
                    play_rate = rate
                else:
                    play_rate = _ps2_true_rate(self.session.get('path', ''), idx, rate, _sr)
                half_note = ''
                try:
                    _div = _ps2_is_divided(self.session.get('path', ''), idx, rate, _sr)
                    if _div is None:
                        _div = _hd_is_half_family(rate)
                    if self.half_preview.get() and not _div and not is_repl and _hd_is_half_family(rate):
                        play_rate = rate // 2
                        half_note = ' (preview 1/2 tasa)'
                    elif play_rate != rate:
                        half_note = ' (tasa de juego)'
                except:
                    pass
                self.log(f"Play VAG {idx:02d}: slot {e['size']}B rate_hd={e['rate']} rate_play={play_rate} via={via} pcm={len(pcm)}B{half_note}")
                # Preview a 44100Hz para que winsound/drivers suenen con cuerpo
                # (mismo tono y duración, solo cambia la tasa de salida;
                # solo vive en el temporal, no deja archivos junto al .bin)
                try:
                    pcm_prev = resample_pcm16(pcm, play_rate, 44100) if play_rate != 44100 else pcm
                    write_wav(PREVIEW_WAV, pcm_prev, 44100, False)
                except:
                    write_wav(PREVIEW_WAV, pcm, rate, False)
            else:
                sel = self.tree.selection()
                if not sel:
                    messagebox.showinfo('MVC2 Audio Tool', 'Selecciona un sample de la lista.')
                    return
                idx = int(sel[0])
                item = self.session['repl'][idx]
                order_item = self.session['play_order'][idx]
                sample_idx = order_item['sample']
                e = self.session['entries'][sample_idx]
                if item:
                    fmt, stereo, raw = item
                else:
                    fmt, stereo, raw = e['format'], e['stereo'], self.session['raws'][sample_idx]
                pcm = decode_sample(fmt, stereo, raw)
                rate = translate_rate(order_item['rate16'])
                write_wav(PREVIEW_WAV, pcm, rate, stereo)
            if winsound:
                winsound.PlaySound(None, winsound.SND_PURGE)
                winsound.PlaySound(PREVIEW_WAV, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            else:
                messagebox.showinfo('MVC2 Audio Tool', 'winsound no disponible en este Python.')
        except Exception as ex:
            messagebox.showerror('MVC2 Audio Tool', str(ex))

    def cmd_stop(self):
        if winsound:
            winsound.PlaySound(None, winsound.SND_PURGE)

    def _probe_audio_ms(self, path):
        # Duración aprox del audio fuente vía ffmpeg (ms). None si falla.
        return _probe_wav_ms(path)

    def _ask_ps2_target_rate(self, path, entry, dbl_default=None, dbl_note=''):
        # Diálogo con botones rápidos (recomendado / original / otra / cancelar).
        # Devuelve (rate_datos, keep_double) o None (cancelar).
        # keep_double=True => HD = 2x (slots doblados: el juego divide).
        # dbl_default: True/False inferido del slot (mapeo), None = histórico (>=18k).
        orig_rate = entry['rate']
        cap_audio = max(0, entry['size'] - 16)
        cap_samples = cap_audio // 16 * 28
        dur_ms = self._probe_audio_ms(path)
        cands = [8000, 9000, 11000, 11025, 12000, 16000, 18000, 22050, 24000]
        if orig_rate not in cands:
            cands.append(orig_rate)
        cands = sorted(set(cands))
        def need(r):
            return -(-int(dur_ms * r / 1000) // 28) * 16 if dur_ms else None
        if dur_ms:
            fit = [r for r in cands if need(r) <= cap_audio]
            sug = max(fit) if fit else min(cands)
        else:
            sug = orig_rate
        res = {'v': 'cancel'}
        keep = {'double': dbl_default if dbl_default is not None else orig_rate >= 18000}
        dlg = tk.Toplevel(self.root)
        dlg.title('Tasa destino PS2')
        self._set_icon(dlg)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()
        info = [f"Slot: {entry['size']} B @ {orig_rate} Hz (cap ~{cap_samples/max(1,orig_rate)*1000:.0f} ms)",
                f"Fuente: {os.path.basename(path)}" + (f" ~{dur_ms} ms" if dur_ms else " (?)"),
                f"Sugerido datos: {sug} Hz"]
        if dbl_note:
            info.append(dbl_note)
        if dur_ms:
            for r in cands:
                nd = need(r)
                mark = 'OK' if nd <= cap_audio else 'excede'
                tag = ' (original)' if r == orig_rate else (' <= rec' if r == sug else '')
                info.append(f"  {r:5d} Hz ~{nd:5d}B {mark}{tag}")
        tk.Label(dlg, text='\n'.join(info), font=('Consolas', 9), justify='left').pack(padx=14, pady=(12, 6))
        dbl_var = tk.BooleanVar(value=keep['double'])
        tk.Checkbutton(dlg, text='Slot doblado (el juego divide: HD = 2× datos)',
                       variable=dbl_var, font=('Segoe UI', 9, 'bold')).pack(pady=(0, 4))
        btns = tk.Frame(dlg)
        btns.pack(pady=(0, 12))
        def done(v):
            res['v'] = (v, bool(dbl_var.get())) if isinstance(v, int) else None
            dlg.destroy()
        tk.Button(btns, text=f'Usar recomendado ({sug} Hz)', width=24,
                  fg='#8a6508', activeforeground='#8a6508',
                  command=lambda: done(sug)).pack(side='left', padx=4)
        tk.Button(btns, text=f'Original ({orig_rate} Hz)', width=20,
                  fg='black',
                  command=lambda: done(orig_rate)).pack(side='left', padx=4)
        def other():
            try:
                dlg.grab_release()
                v = simpledialog.askinteger('Tasa manual', 'Tasa en Hz (4000-48000):',
                                            initialvalue=sug, minvalue=4000, maxvalue=48000,
                                            parent=self.root)
            except:
                v = None
            done(v if isinstance(v, int) else None)
        tk.Button(btns, text='Otra…', width=8, fg='#8b0000', activeforeground='#8b0000',
                  command=other).pack(side='left', padx=4)
        tk.Button(btns, text='Cancelar', width=10, command=lambda: done(None)).pack(side='left', padx=4)
        # Centrar sobre la ventana principal (con widgets ya creados para medir bien)
        try:
            self.root.update_idletasks()
            dlg.update_idletasks()
            _rx, _ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            _rw, _rh = self.root.winfo_width(), self.root.winfo_height()
            _w, _h = dlg.winfo_width(), dlg.winfo_height()
            if _w > 1 and _h > 1:
                dlg.geometry(f'+{max(0, _rx + (_rw - _w)//2)}+{max(0, _ry + (_rh - _h)//2)}')
        except:
            pass
        dlg.protocol('WM_DELETE_WINDOW', lambda: done(None))
        dlg.wait_window()
        return res['v']

    def _ps2_pcm_to_vag(self, out_pcm, rate):
        # Recodifica PCM16 mono a VAG raw sin end marker. Lanza ValueError si falla.
        wav2vag_path = resource_path('wav2vag.exe')
        if not os.path.isfile(wav2vag_path):
            wav2vag_path = shutil.which('wav2vag.exe') or os.path.join(os.path.dirname(__file__), 'wav2vag.exe')
        if not os.path.isfile(wav2vag_path or ''):
            raise ValueError('Falta wav2vag.exe junto al script.')
        import tempfile as tf
        with tf.NamedTemporaryFile(delete=False, suffix='.wav') as f:
            tmp_wav = f.name
        with tf.NamedTemporaryFile(delete=False, suffix='.VAG') as f:
            tmp_vag = f.name
        try:
            w = wave.open(tmp_wav, 'wb')
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
            w.writeframes(out_pcm); w.close()
            res = _run([wav2vag_path, tmp_wav, tmp_vag, f'-freq={rate}'],
                                 capture_output=True, text=True)
            if res.returncode != 0 or not os.path.exists(tmp_vag):
                raise ValueError(f'wav2vag falló: {res.stdout} {res.stderr}')
            vag_data = open(tmp_vag, 'rb').read()
            raw = vag_data[0x30:] if len(vag_data) >= 0x30 and vag_data[0:4] == b'VAGp' else vag_data
            if len(raw) >= 16 and raw[-16:] == bytes.fromhex("00 07 77 77 77 77 77 77 77 77 77 77 77 77 77 77"):
                raw = raw[:-16]
            return raw
        finally:
            try: os.remove(tmp_wav)
            except: pass
            try: os.remove(tmp_vag)
            except: pass

    def cmd_replace(self):
        if self.session is None:
            return
        if self.session['kind'] == 'stream':
            ftype = [('Audio', '*.adx *.bin'), ('Todos', '*.*')]
            title = 'Reemplazar stream %s completo' % self.session['stream_kind'].upper()
        elif self.session['kind'] == 'ps2':
            ftype = [('Audio', '*.vag *.wav *.yadpcm'), ('Todos', '*.*')]
            title = 'Reemplazo VAG para el sample seleccionado (PS2)'
        else:
            ftype = [('Audio', '*.yadpcm *.wav *.pcm8 *.pcm16'), ('Todos', '*.*')]
            title = 'Reemplazo para el sample seleccionado'
        # PS2: preseleccionar el wav latino mapeado a este slot (el número
        # del wav NO es el slot: ej wav mvc2_pl1b_10 -> slot 07)
        initdir, initfile = None, None
        if self.session['kind'] == 'ps2':
            try:
                sel0 = self.tree.selection()
                if sel0:
                    lat = (self.session.get('latino_map') or {}).get(int(sel0[0]))
                    if lat and os.path.isfile(lat):
                        initdir = os.path.dirname(lat)
                        initfile = os.path.basename(lat)
            except:
                pass
        if initfile:
            path = filedialog.askopenfilename(title=title + f' (sugerido: {initfile})',
                                              filetypes=ftype, initialdir=initdir, initialfile=initfile)
        else:
            path = filedialog.askopenfilename(title=title, filetypes=ftype)
        if not path:
            return
        try:
            if self.session['kind'] == 'stream':
                data = open(path, 'rb').read()
                if detect_stream(data) != self.session['stream_kind']:
                    raise ValueError('el archivo elegido no es %s' % self.session['stream_kind'].upper())
                self.session['data'] = data
                self.session['repl_data'] = path
                self.session.pop('pcm', None)
                if self.session['stream_kind'] == 'adx':
                    pcm, rate, ch = decode_adx_to_pcm16(data)
                    self.session['pcm'] = pcm
                    self.session['rate'] = rate
                    self.session['stereo'] = ch == 2
                self._set_row_state('0', 'reemplazado')
                self.log('Stream reemplazado por: %s' % path)
            elif self.session['kind'] == 'ps2':
                sel = self.tree.selection()
                if not sel:
                    raise ValueError('selecciona primero el sample en la lista')
                idx = int(sel[0])
                keep_double = False
                e = self.session['entries'][idx]
                original_bytes = e['size'] - 16  # sin contar end marker, slot real para audio
                # Cargar archivo: soporta .vag (con header VAGp) y .wav
                ext = os.path.splitext(path)[1].lower()
                if ext == '.vag':
                    data = open(path, 'rb').read()
                    if len(data) >= 0x30 and data[0:4]==b'VAGp':
                        rate = struct.unpack('>I', data[0x10:0x14])[0]
                        raw = data[0x30:]
                        # Quitar end marker si existe para edición
                        if len(raw)>=16 and raw[-16:]==bytes.fromhex("00 07 77 77 77 77 77 77 77 77 77 77 77 77 77 77"):
                            raw = raw[:-16]
                        fmt='vag'; stereo=False
                        # Convención bankmod: el VAG trae tasa de DATOS
                        # (al extraer se escribe HD//2). HD = datos x2:
                        # extraer->importar conserva el HD (round-trip seguro).
                        keep_double = True if rate else False
                    else:
                        raw = data
                        if len(raw)>=16 and raw[-16:]==bytes.fromhex("00 07 77 77 77 77 77 77 77 77 77 77 77 77 77 77"):
                            raw = raw[:-16]
                        fmt='vag'; stereo=False
                        rate = e['rate']
                    # Actualizar rate en HD si es distinto
                    if rate != e['rate']:
                        self.log(f"  VAG datos {rate} Hz -> HD {min(65535, rate*2) if keep_double else rate} Hz (original {e['rate']})")
                else:
                    # WAV MS ADPCM -> VAG: decodificar via ffmpeg a PCM raw y luego a VAG
                    # Si el latino cabe a tasa original, se usa directo sin preguntar.
                    # Solo se pregunta (para bajar tasa estilo bankmod) si excede el slot.
                    target_rate = e['rate']
                    keep_double = False
                    cap_audio = max(0, e['size'] - 16)
                    _dur = self._probe_audio_ms(path)
                    # División del slot: mapa DTPK primero; si no hay dato, el
                    # take mapeado (si solo cabe a mitad, el juego divide).
                    _dbl, _note = None, ''
                    try:
                        _dv = _slot_divided(self.session.get('path', ''), idx)
                        if _dv is True:
                            _dbl = True
                            _note = 'Slot doblado según mapa DTPK (ver ps2_map_div.txt)'
                        elif _dv is False:
                            _dbl = False
                            _note = 'Slot directo según mapa DTPK'
                    except:
                        pass
                    if _dbl is None:
                        try:
                            _lm = self.session.get('latino_map') or {}
                            _lw = _lm.get(idx)
                            if _lw and os.path.isfile(_lw):
                                _sp2, _dd = _wav_span_dur(_lw)
                                if _strong_half(e, _dd or 0):
                                    _dbl = True
                                    _note = 'Mapeo: el latino solo cabe a mitad (slot doblado)'
                        except:
                            pass
                    if _dur:
                        _need = -(-int(_dur * target_rate / 1000) // 28) * 16
                        if _need <= cap_audio and e['rate'] < 18000:
                            self.log(f"  cabe a {target_rate}Hz (~{_need}B de {cap_audio}B), sin preguntar tasa")
                        else:
                            _ans = self._ask_ps2_target_rate(path, e, _dbl, _note)
                            if _ans is None:
                                return
                            target_rate, keep_double = _ans
                    else:
                        _ans = self._ask_ps2_target_rate(path, e, _dbl, _note)
                        if _ans is None:
                            return
                        target_rate, keep_double = _ans
                    ffmpeg_path = resource_path('ffmpeg.exe')
                    if not os.path.isfile(ffmpeg_path):
                        ffmpeg_path = shutil.which('ffmpeg') or shutil.which('ffmpeg.exe')
                    # Usar wav2vag.exe + ffmpeg para conversión precisa (como hace tu amigo con MFAudio)
                    # wav2vag espera raw PCM y produce VAG ADPCM correcto para PS2
                    wav2vag_path = resource_path('wav2vag.exe')
                    if not os.path.isfile(wav2vag_path):
                        wav2vag_path = shutil.which('wav2vag.exe') or os.path.join(os.path.dirname(__file__), 'wav2vag.exe')
                    ffmpeg_path2 = resource_path('ffmpeg.exe')
                    if not os.path.isfile(ffmpeg_path2):
                        ffmpeg_path2 = shutil.which('ffmpeg') or shutil.which('ffmpeg.exe')
                    # Decodificar WAV (MS ADPCM) a raw PCM via ffmpeg
                    with tempfile.NamedTemporaryFile(delete=False, suffix='.raw') as tmp_raw:
                        tmp_raw_path = tmp_raw.name
                    with tempfile.NamedTemporaryFile(delete=False, suffix='.VAG') as tmp_vag:
                        tmp_vag_path = tmp_vag.name
                    try:
                        # ffmpeg a raw
                        cmd = [ffmpeg_path2, '-y', '-i', path, '-ac', '1', '-ar', str(target_rate), '-f', 's16le', '-acodec', 'pcm_s16le', tmp_raw_path]
                        res = _run(cmd, capture_output=True)
                        if res.returncode != 0:
                            raise ValueError(f"ffmpeg falló: {res.stderr.decode()[:200]}")
                        # wav2vag raw -> VAG
                        cmd2 = [wav2vag_path, tmp_raw_path, tmp_vag_path, '-sraw16', f'-freq={target_rate}']
                        res2 = _run(cmd2, capture_output=True, text=True)
                        if res2.returncode != 0 or not os.path.exists(tmp_vag_path):
                            raise ValueError(f"wav2vag falló: {res2.stdout} {res2.stderr}")
                        # Leer VAG raw (sin header VAGp, wav2vag con -sraw16 produce raw sin header? Con -sraw16 y sin -raw produce con header, con -raw produce sin header)
                        # Necesitamos raw sin header para BD slot, así que usamos -sraw16 -raw? Pero wav2vag con -sraw16 -raw produce raw sin header, sin -raw produce con header
                        # Para PS2 BD necesitamos raw sin header, así que si usamos sin -raw, quitamos header
                        vag_data = open(tmp_vag_path, 'rb').read()
                        if len(vag_data) >= 0x30 and vag_data[0:4]==b'VAGp':
                            raw = vag_data[0x30:]
                        else:
                            raw = vag_data
                        # Quitar end marker de wav2vag para guardar raw sin marker (cmd_save lo re-agrega)
                        if len(raw)>=16 and raw[-16:]==bytes.fromhex("00 07 77 77 77 77 77 77 77 77 77 77 77 77 77 77"):
                            raw = raw[:-16]
                        fmt='vag'; stereo=False
                        rate = target_rate
                    finally:
                        try: os.remove(tmp_raw_path)
                        except: pass
                        try: os.remove(tmp_vag_path)
                        except: pass
                original_bytes = e['size'] - 16
                modo = self.mode.get()
                is_strict = (modo == 'estricto') or (modo == 'hibrido' and self.hybrid_flags.get(idx, False))
                if is_strict:
                    if len(raw) > original_bytes:
                        # Como en PS3: recortador forzado en bucle. No se inyecta
                        # nada que exceda: o cabe editando, o recorte duro elegido,
                        # o se cancela (sin reemplazo).
                        try:
                            pcm_cur = vag_to_pcm16(raw)
                        except Exception as ex:
                            raise ValueError(f'No se pudo decodificar para recortar: {ex}')
                        self.log('  MODO ESTRICTO: audio %d B > original %d B. Abriendo recortador...' % (len(raw), original_bytes))
                        while True:
                            editor = WaveEditor(self.root, 'RECORTE ESTRICTO VAG %02d (%d Hz, máx %d B)' % (idx, rate, original_bytes),
                                                pcm_cur, rate, False, 'pcm16',
                                                max_bytes=None, original_bytes=original_bytes,
                                                fmt_label='VAG ADPCM 4bit')
                            result = editor.run()
                            if result is None:
                                return
                            _, _, out_pcm = result
                            raw = self._ps2_pcm_to_vag(out_pcm, rate)
                            if len(raw) <= original_bytes:
                                break
                            over = len(raw) - original_bytes
                            choice = messagebox.askyesnocancel('MODO ESTRICTO',
                                f'Sigue excediendo por {over} B ({len(raw)} > {original_bytes}).\n\n'
                                'Sí = volver al editor a recortar más\n'
                                'No = recorte duro automático al slot\n'
                                'Cancelar = no reemplazar nada')
                            if choice is None:
                                return
                            if choice is False:
                                raw = raw[:original_bytes]
                                if len(raw) % 16 != 0:
                                    raw = raw[:len(raw)-len(raw)%16]
                                self.log('  MODO ESTRICTO: recorte duro elegido a %d B' % len(raw))
                                break
                            pcm_cur = out_pcm
                    if len(raw) > original_bytes:
                        raw = raw[:original_bytes]
                        if len(raw) % 16 != 0:
                            raw = raw[:len(raw)-len(raw)%16]
                        self.log('  MODO ESTRICTO: recorte final a %d B (slot fijo)' % len(raw))
                    elif len(raw) < original_bytes:
                        _pad = original_bytes - len(raw)
                        raw = raw + b'\x00' * _pad
                        self.log('  MODO ESTRICTO: pad %d bytes con ceros (tamano exacto)' % _pad)
                # Para PS2, guardar (tanto en estricto como no estricto)
                if self.session['kind'] == 'ps2':
                    dur_ms = int(len(raw)/16*28 / max(1,rate) *1000) if rate else 0
                    self.session['repl'][idx] = (fmt, stereo, raw)
                    if 'repl_rates' not in self.session or len(self.session['repl_rates']) != len(self.session['entries']):
                        self.session['repl_rates'] = [None]*len(self.session['entries'])
                    if 'repl_drate' not in self.session or len(self.session['repl_drate']) != len(self.session['entries']):
                        self.session['repl_drate'] = [None]*len(self.session['entries'])
                    # keep_double (diálogo WAV): HD = 2x datos; si no, HD = datos
                    hd_rate = min(65535, rate*2) if (keep_double and rate) else rate
                    self.session['repl_rates'][idx] = hd_rate
                    self.session['repl_drate'][idx] = rate
                    if 'repl_src' not in self.session:
                        self.session['repl_src'] = {}
                    self.session['repl_src'][idx] = os.path.basename(path)
                    self.tree.set(str(idx), 'size', '%d B' % len(raw))
                    self.tree.set(str(idx), 'rate', '%d Hz' % rate if hd_rate == rate else '%d Hz (HD %d)' % (rate, hd_rate))
                    self.tree.set(str(idx), 'dur', '%d ms' % dur_ms)
                    self.tree.set(str(idx), 'file', os.path.basename(path))
                    _mv = re.search(r'(\d+)\.(wav|vag|yadpcm)$', os.path.basename(path), re.I)
                    if _mv:
                        self.tree.set(str(idx), 'idx', '%02d' % (int(_mv.group(1)) % 100))
                    self._set_row_state(str(idx), 'reemplazado')
                    self.log('Sample PS2 %02d reemplazado: %s (%d B, %d ms, datos %d Hz, HD %d Hz)' % (idx, os.path.basename(path), len(raw), dur_ms, rate, hd_rate))
                    self.on_select()
                    return
                else:
                    # DTPK (igual que versión PS3)
                    sel = self.tree.selection()
                    if not sel:
                        raise ValueError('selecciona primero el sample en la lista')
                    idx = int(sel[0])
                    order_item = self.session['play_order'][idx]
                    sample_idx = order_item['sample']
                    item = self.session['entries'][sample_idx]
                    target_rate = translate_rate(order_item['rate16'])
                    load_item = {
                        'stereo': item['stereo'],
                        'format': item['format']
                    }
                    fmt, stereo, raw = load_sample_file(path, load_item, target_rate)
                    original_bytes = len(self.session['raws'][sample_idx])
                    modo = self.mode.get()
                    is_strict = (modo == 'estricto') or (modo == 'hibrido' and self.hybrid_flags.get(idx, False))
                    if is_strict:
                        if len(raw) > original_bytes:
                            self.log('  MODO ESTRICTO: audio %d B > original %d B. Abriendo recortador...' % (len(raw), original_bytes))
                            trimmed = self._open_trim_forced(idx, item, target_rate, raw, fmt, stereo, original_bytes)
                            if trimmed is None:
                                return
                            raw = trimmed
                        elif len(raw) < original_bytes:
                            _pad = original_bytes - len(raw)
                            raw = raw + b'\x00' * _pad
                            self.log('  MODO ESTRICTO: pad %d bytes con ceros (tamano exacto)' % _pad)
                    dur_ms = self._bytes_to_ms(len(raw), fmt, target_rate)
                    if stereo != item['stereo']:
                        self.log('  atencion: el reemplazo cambia mono/stereo del sample %d' % sample_idx)
                    self.session['repl'][idx] = (fmt, stereo, raw)
                    self.tree.set(str(idx), 'fmt', FORMAT_LABEL[fmt])
                    self.tree.set(str(idx), 'size', '%d B' % len(raw))
                    self.tree.set(str(idx), 'dur', '%d ms' % dur_ms)
                    self._set_row_state(str(idx), 'reemplazado')
                    self.log('Sample %03d reemplazado: %s (%s, %d B, %d ms)'
                             % (idx, os.path.basename(path), FORMAT_LABEL[fmt], len(raw), dur_ms))
            self.on_select()
        except Exception as ex:
            messagebox.showerror('MVC2 Audio Tool', str(ex))

    def cmd_unreplace(self):
        if self.session is None:
            return
        sel = self.tree.selection()
        if not sel:
            return
        if self.session['kind'] == 'stream':
            self.session.pop('repl_data', None)
            self.session['data'] = open(self.session['path'], 'rb').read()
            if self.session['stream_kind'] == 'adx':
                pcm, rate, ch = decode_adx_to_pcm16(self.session['data'])
                self.session['pcm'] = pcm
                self.session['rate'] = rate
                self.session['stereo'] = ch == 2
            self._set_row_state('0', 'original')
            self.log('Reemplazo de stream quitado (vuelve el original).')
        elif self.session['kind'] == 'ps2':
            idx = int(sel[0])
            self.session['repl'][idx] = None
            if 'repl_rates' in self.session and idx < len(self.session['repl_rates']):
                self.session['repl_rates'][idx] = None
            if 'repl_drate' in self.session and idx < len(self.session['repl_drate']):
                self.session['repl_drate'][idx] = None
            try:
                self.session.get('repl_src', {}).pop(idx, None)
            except:
                pass
            e = self.session['entries'][idx]
            _o, _f = self._ps2_disp(idx)
            self.tree.set(str(idx), 'file', _f)
            self.tree.set(str(idx), 'size', '%d B' % e['size'])
            self.tree.set(str(idx), 'rate', '%d Hz' % e['rate'])
            self.tree.set(str(idx), 'dur', '%d ms' % int((e['size']-16)//16*28 / max(1,e['rate'])*1000))
            self._set_row_state(str(idx), 'original')
            self.log('Sample PS2 %02d: reemplazo quitado.' % idx)
        else:
            idx = int(sel[0])
            self.session['repl'][idx] = None
            order_item = self.session['play_order'][idx]
            sample_idx = order_item['sample']
            e = self.session['entries'][sample_idx]
            self.tree.set(str(idx), 'fmt', FORMAT_LABEL[e['format']])
            self.tree.set(str(idx), 'size', '%d B' % self.session['raws'][sample_idx].__len__())
            self._set_row_state(str(idx), 'original')
            self.log('Sample %03d: reemplazo quitado (vuelve el original).' % idx)
        self.on_select()

    def cmd_save(self):
        if self.session is None:
            return
        initial = self.session['source']
        out = filedialog.asksaveasfilename(
            title='Guardar .bin modificado como...',
            initialfile=initial, defaultextension='.bin',
            filetypes=[('BIN', '*.bin'), ('Todos', '*.*')])
        if not out:
            return
        try:
            if self.session['kind'] == 'stream':
                data = self.session['data']
                self.session['repl_data'] = self.session.get('repl_data') or None
                with open(out, 'wb') as f:
                    f.write(data)
                self.log('Stream guardado: %s (%d bytes)' % (out, len(data)))
            elif self.session['kind'] == 'ps2':
                # Reconstruir PS2 HD/BD con fixed slots
                hd = bytearray(self.session['hd'])
                bd = bytearray(self.session['bd'])
                vagi_off = self.session['vagi_off']
                max_idx = self.session['max_idx']
                # Para cada VAG reemplazado, reconstruir BD por slots
                for idx, repl in enumerate(self.session['repl']):
                    if repl is None:
                        continue
                    fmt, stereo, raw = repl
                    # raw es VAG sin end marker (ya viene sin marker desde cmd_replace)
                    # Obtener slot info
                    param_off = struct.unpack('<I', hd[vagi_off+0x10+idx*4:vagi_off+0x14+idx*4])[0]
                    vag_off = struct.unpack('<I', hd[vagi_off+param_off:vagi_off+param_off+4])[0]
                    if idx < max_idx:
                        next_param = struct.unpack('<I', hd[vagi_off+0x10+(idx+1)*4:vagi_off+0x14+(idx+1)*4])[0]
                        next_off = struct.unpack('<I', hd[vagi_off+next_param:vagi_off+next_param+4])[0]
                        slot_sz = next_off - vag_off
                    else:
                        slot_sz = len(bd) - vag_off
                    # Aplicar lógica fixed slot del amigo
                    VAG_END = bytes.fromhex("00 07 77 77 77 77 77 77 77 77 77 77 77 77 77 77")
                    # raw ya viene sin marker, asegurar
                    if len(raw) >= 16 and raw[-16:] == VAG_END:
                        raw = raw[:-16]
                    audio_slot = slot_sz - 16
                    max_audio = (audio_slot//16)*16
                    if len(raw) > max_audio:
                        raw = raw[:max_audio]
                        self.log(f"  VAG {idx:02d} recortado {len(raw)}->{max_audio}")
                    in_sz = len(raw)
                    zero_pad = audio_slot - in_sz
                    padded = raw + b"\x00"*zero_pad + VAG_END
                    # Reconstruir BD
                    new_bd = bytearray()
                    new_bd.extend(bd[:vag_off])
                    new_bd.extend(padded)
                    new_bd.extend(bd[vag_off+slot_sz:])
                    bd = new_bd
                    # Actualizar rate en HD al rate del reemplazo (como bankmod put_vag_sample_rate)
                    # Si no, un VAG de 8000Hz en slot de 18000Hz suena a ardilla
                    new_rate = None
                    try:
                        rr = self.session.get('repl_rates')
                        if rr and idx < len(rr) and rr[idx]:
                            new_rate = int(rr[idx])
                    except:
                        new_rate = None
                    if new_rate:
                        old_rate = struct.unpack('<H', hd[vagi_off+param_off+4:vagi_off+param_off+6])[0]
                        if new_rate != old_rate:
                            struct.pack_into('<H', hd, vagi_off+param_off+4, new_rate)
                            self.log(f"  VAG {idx:02d} rate HD {old_rate}->{new_rate} Hz")
                # Reconstruir container
                hd_off = self.session['hd_off']
                bd_off = self.session['bd_off']
                # Leer container original para header 0x20
                orig_data = open(self.session['path'], 'rb').read()
                container = bytearray(orig_data)
                container[hd_off:hd_off+len(hd)] = hd
                container[bd_off:bd_off+len(bd)] = bd
                out_bytes = bytes(container)
                with open(out, 'wb') as f:
                    f.write(out_bytes)
                self.log('PS2 HD/BD guardado: %s (%d VAGs, %d modificados, %d bytes)' % (out, len(self.session['entries']), len([r for r in self.session['repl'] if r is not None]), len(out_bytes)))
            else:
                items = []
                sample_repl = {}
                for idx, repl in enumerate(self.session['repl']):
                    if repl is not None:
                        order_item = self.session['play_order'][idx]
                        sample_idx = order_item['sample']
                        sample_repl[sample_idx] = repl
                for i, e in enumerate(self.session['entries']):
                    if i in sample_repl:
                        fmt, stereo, raw = sample_repl[i]
                        flags = e['flags'] if fmt == e['format'] else flag_for_format(fmt)
                    else:
                        fmt, stereo, raw = e['format'], e['stereo'], self.session['raws'][i]
                        flags = e['flags']
                    items.append((fmt, stereo, raw, flags, e['loop_start'], e['loop_end']))
                out_bytes = build_dtpk(self.session['header'], items, self.session['tail'])
                with open(out, 'wb') as f:
                    f.write(out_bytes)
                self.log('DTPK guardado: %s (%d samples, %d modificados, %d bytes)'
                         % (out, len(items), len([r for r in self.session['repl'] if r is not None]), len(out_bytes)))
            orig_size = self.session.get('original_size')
            new_size = len(data) if self.session['kind'] == 'stream' else len(out_bytes)
            if orig_size and new_size != orig_size:
                self.log('  AVISO: tamano original=%d, nuevo=%d (diferencia %d bytes)'
                         % (orig_size, new_size, new_size - orig_size))
                messagebox.showwarning('MVC2 Audio Tool - AVISO de tamano',
                    'El .bin guardado tiene DISTINTO tamano que el original:\n'
                    '  original: %d bytes\n'
                    '  nuevo:    %d bytes\n'
                    '  diferencia: %+d bytes\n\n'
                    'Si el juego falla, puede que el .bin deba conservar su tamano exacto.' % (orig_size, new_size, new_size - orig_size))
            else:
                messagebox.showinfo('MVC2 Audio Tool', 'Guardado:\n%s\n(%d bytes)' % (out, new_size))
            # PS2: ofrecer inyección directa en el AFS del juego
            if self.session['kind'] == 'ps2':
                try:
                    if messagebox.askyesno('MVC2 Audio Tool - Inyectar en AFS',
                            'Bin guardado:\n%s\n\n¿Inyectarlo también en el AFS01.AFS del juego?\n'
                            '(crea .bak solo si no existe)' % out):
                        initdir = AFS_GAME_DIR if os.path.isdir(AFS_GAME_DIR) else os.path.dirname(out)
                        afs = filedialog.askopenfilename(title='Elegir AFS del juego (AFS01.AFS)',
                                                         initialdir=initdir, initialfile='AFS01.AFS',
                                                         filetypes=[('AFS', '*.AFS'), ('Todos', '*.*')])
                        if afs:
                            inject_afs(afs, file_name=os.path.basename(out), new_data=out_bytes, log=self.log)
                            messagebox.showinfo('MVC2 Audio Tool',
                                'Inyectado en:\n%s\n(%s)' % (afs, os.path.basename(out)))
                except Exception as ex2:
                    messagebox.showerror('MVC2 Audio Tool - AFS', str(ex2))
        except Exception as ex:
            messagebox.showerror('MVC2 Audio Tool', str(ex))


def main():
    args = sys.argv[1:]
    _install_crash_log()
    if args and args[0] == '-w' and len(args) >= 2:
        decode_all_to_wav(os.path.abspath(args[1]),
                          os.path.abspath(args[1]) and (os.path.splitext(args[1])[0] + '_wav'))
        return
    if TK_OK and os.environ.get('MVC2_TOOL_CONSOLE') != '1':
        root = TkinterDnD.Tk() if HAVE_DND else tk.Tk()
        # Arranque sin parpadeo: la ventana nace oculta y solo se muestra
        # cuando la interfaz ya está armada y centrada.
        try:
            root.withdraw()
        except:
            pass
        app = App(root)
        if args:
            root.after(600, lambda: [app.handle(os.path.abspath(a)) for a in args])
        try:
            root.update_idletasks()
            try:
                root.deiconify()
            except Exception as _de:
                try:
                    with open(os.path.join(app_dir(), 'MVC2_AudioTool.log'), 'a', encoding='utf-8') as _f:
                        _f.write('DEICONIFY FALLO: %r\n' % (_de,))
                except:
                    pass
            # Ventana lista: mostrar sin parpadeo.
            try:
                root.lift()
            except:
                pass
        except:
            pass
        root.mainloop()
    else:
        for p in args:
            print('PROCESANDO:', p)
            try:
                if os.path.isdir(p):
                    repack_path(os.path.abspath(p), print)
                else:
                    extract_path(os.path.abspath(p), print)
                print('OK')
            except Exception as e:
                print('ERROR:', e)
        if not args:
            print(__doc__)


if __name__ == '__main__':
    main()