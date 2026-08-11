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
import struct
import tempfile
import array
import subprocess
import shutil
import importlib.util

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext
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


def extract_dtpk_wav(path, outdir, log):
    data = open(path, 'rb').read()
    p = parse_dtpk(data)
    os.makedirs(outdir, exist_ok=True)
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
            result = subprocess.run(cmd, capture_output=True, text=True)
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


def extract_path(path, log):
    data = open(path, 'rb').read()
    base = os.path.splitext(os.path.basename(path))[0]
    outdir = os.path.join(os.path.dirname(path), base + '_extraido')
    if os.path.exists(outdir):
        raise ValueError('ya existe la carpeta %s (borrala o muevela)' % outdir)
    if data[0:4] == b'DTPK':
        extract_dtpk(path, outdir, log)
    else:
        kind = detect_stream(data)
        if kind:
            extract_stream(path, outdir, log)
        else:
            raise ValueError('no es un contenedor de audio reconocido (DTPK / ADX / MPEG-1 Layer 2)')


def repack_path(folder, log):
    if not os.path.isdir(folder):
        raise ValueError('la ruta no es una carpeta')
    mp = os.path.join(folder, '_manifest.json')
    if not os.path.isfile(mp):
        raise ValueError('la carpeta no tiene _manifest.json')
    manifest = json.load(open(mp, encoding='utf-8'))
    if manifest.get('type') == 'dtpk':
        repack_dtpk_folder(folder, log)
    elif manifest.get('type') == 'stream':
        repack_stream_folder(folder, log)
    else:
        raise ValueError('tipo de manifest desconocido')


def decode_all_to_wav(path, outdir):
    data = open(path, 'rb').read()
    os.makedirs(outdir, exist_ok=True)
    base = os.path.splitext(os.path.basename(path))[0]
    if data[0:4] == b'DTPK':
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
                        subprocess.run(cmd, check=True, capture_output=True)
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
                max_bytes=None, original_bytes=None):
        self.root = root
        self.rate = max(1, rate)
        self.stereo = stereo
        self.channels = 2 if stereo else 1
        self.fmt = fmt
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
        self.sel_start = 0
        self.sel_end = len(self.left)
        self.zoom = 1.0
        self.scroll = 0.0
        self.drag_mode = None
        self.playing = False
        self.play_after_id = None

        self.win = tk.Toplevel(root)
        self.win.title(title)
        self.win.geometry('820x460')
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
            % (self.fmt, self.rate, 'stereo' if self.stereo else 'mono', dur_ms, est))
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

    def _cmd_fade_in(self):
        s0, s1 = self._fade_range()
        if s1 <= s0:
            return
        self._push_undo()
        n = s1 - s0
        for i in range(n):
            g = i / n
            self.left[s0 + i] = int(self.left[s0 + i] * g)
            if self.right is not None:
                self.right[s0 + i] = int(self.right[s0 + i] * g)
        self._redraw()

    def _cmd_fade_out(self):
        s0, s1 = self._fade_range()
        if s1 <= s0:
            return
        self._push_undo()
        n = s1 - s0
        for i in range(n):
            g = 1 - (i / n)
            self.left[s0 + i] = int(self.left[s0 + i] * g)
            if self.right is not None:
                self.right[s0 + i] = int(self.right[s0 + i] * g)
        self._redraw()

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
        self.log_path = os.path.join(app_dir(), 'MVC2_AudioTool.log')
        self.session = None
        self.mode = tk.StringVar(value='libre')
        self.hybrid_flags = {}
        self.gif_frames = []
        self.gif_label = None
        self.gif_index = 0
        self.indicadores = {}
        root.title('MVC2 Audio Tool')
        root.geometry('880x620')
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
        if self.session is None or self.session['kind'] != 'dtpk':
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

    def load_bin(self, path):
        data = open(path, 'rb').read()
        name = os.path.basename(path)
        base = os.path.splitext(name)[0]
        self.session = None
        self.tree.delete(*self.tree.get_children())
        self._update_char_icon('')
        if data[0:4] == b'DTPK':
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
        outdir = os.path.splitext(path)[0] + '_extraido'
        modo = self._ask_extract_mode()
        if modo is None:
            return
        try:
            if modo in ('raw', 'both'):
                extract_path(path, self.log)
            if modo in ('wav', 'both'):
                extract_dtpk_wav(path, outdir + '_wav' if modo == 'both' else outdir, self.log)
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
        dlg.geometry('330x210')
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

    def cmd_replace(self):
        if self.session is None:
            return
        if self.session['kind'] == 'stream':
            ftype = [('Audio', '*.adx *.bin'), ('Todos', '*.*')]
            title = 'Reemplazar stream %s completo' % self.session['stream_kind'].upper()
        else:
            ftype = [('Audio', '*.yadpcm *.wav *.pcm8 *.pcm16'), ('Todos', '*.*')]
            title = 'Reemplazo para el sample seleccionado'
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
            else:
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
                        raw = raw + b'\x00' * (original_bytes - len(raw))
                        self.log('  MODO ESTRICTO: pad %d bytes con ceros (tamano exacto)' % (original_bytes - len(raw)))
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
        except Exception as ex:
            messagebox.showerror('MVC2 Audio Tool', str(ex))


def main():
    args = sys.argv[1:]
    if args and args[0] == '-w' and len(args) >= 2:
        decode_all_to_wav(os.path.abspath(args[1]),
                          os.path.abspath(args[1]) and (os.path.splitext(args[1])[0] + '_wav'))
        return
    if TK_OK and os.environ.get('MVC2_TOOL_CONSOLE') != '1':
        root = TkinterDnD.Tk() if HAVE_DND else tk.Tk()
        app = App(root)
        if args:
            root.after(600, lambda: [app.handle(os.path.abspath(a)) for a in args])
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