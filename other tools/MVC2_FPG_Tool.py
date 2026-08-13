# -*- coding: utf-8 -*-
"""MVC2 FPG Tool - herramienta APARTE para extraer, ver y reemplazar las
imagenes empaquetadas dentro de archivos .fpg tipo flog_ps3.fpg (logos de
Capcom/Marvel, fondos de carga, iconos de personaje, fuentes de UI...).
No modifica ni depende de MVC2_AudioTool.py ni de MVC2_ADX_Tool.py.

Formato del contenedor (deducido por ingenieria inversa de flog_ps3.fpg):
  offset 0        : magic b'30GF' + contador de entradas (u32 LE)
  offset 8..2047  : reservado / relleno con ceros
  offset 2048     : tabla de 'count' entradas de 16 bytes cada una:
                       hash(u32 LE), offset_datos(u32 LE),
                       tamano_comprimido(u32 LE), tamano_descomprimido(u32 LE)
  (relleno hasta el siguiente multiplo de 2048)
  luego           : los bloques comprimidos con zlib, uno tras otro, en el
                    mismo orden que la tabla.

Cada bloque, ya descomprimido, es de dos tipos:
  - Recurso de imagen: empieza con el texto 'SG28PT01' (big-endian, nativo
    de PS3). Trae un nombre en texto (ej. 'marvel_logo', 'BGD_Capcom') y
    los pixeles comprimidos en DXT1, DXT5 o sin comprimir (RGBA8888).
  - Recurso de texto/otro (XML de efectos, UI, etc): se preserva tal cual,
    no se re-codifica.

Como se usa:
  - Arrastra un .fpg -> crea una carpeta "<nombre>_fpg_extraido" con un
    .png por cada imagen (nombrado "<indice>_<nombre>.png") y un .xml/.bin
    por cada recurso no-imagen, mas un "_fpg_info.json" con todo lo
    necesario para reconstruir el archivo.
  - Edita los .png que quieras con cualquier editor de imagenes (Gimp,
    Photoshop, Paint.NET...). Puedes cambiarles el tamano sin problema.
  - Arrastra esa carpeta de vuelta -> genera "<nombre>_mod.fpg", listo
    para reemplazar al original.

Ejecutar con Python 3.12 (necesita Pillow y numpy):
  py -3.12 -m pip install pillow numpy
  py -3.12 MVC2_FPG_Tool.py
"""

import os
import sys
import json
import struct
import zlib
import base64

try:
    import numpy as np
    from PIL import Image
    HAVE_IMG = True
except Exception:
    HAVE_IMG = False

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext
    import tkinter.ttk as ttk
    TK_OK = True
except Exception:
    TK_OK = False

HAVE_DND = False
if TK_OK:
    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD
        HAVE_DND = True
    except ImportError:
        pass


TABLE_OFFSET = 2048
MAGIC = b'30GF'
IMG_MAGIC = b'SG28PT01'
MANIFEST_NAME = '_fpg_info.json'


# ============================================================ contenedor ==

def parse_fpg(data):
    if data[0:4] != MAGIC:
        raise ValueError('no es un .fpg reconocido (falta la firma "30GF")')
    count = struct.unpack('<I', data[4:8])[0]
    entries = []
    for i in range(count):
        off = TABLE_OFFSET + i * 16
        h, doff, dsize, ddec = struct.unpack('<IIII', data[off:off + 16])
        entries.append(dict(index=i, hash=h, offset=doff, csize=dsize, dsize=ddec))
    return dict(count=count, entries=entries, header=data[0:TABLE_OFFSET])


def align_2048(n):
    return (n + 2047) // 2048 * 2048


# ==================================================== formato de imagen ==

def locate_image_fields(out):
    """Dado el payload descomprimido de un recurso SG28PT01, encuentra el
    nombre, ancho, alto, offset donde empiezan los pixeles y el formato
    (DXT1 / DXT5 / RGBA8888 / RAW16), usando el tamano exacto restante
    como comprobacion (ver hilo de analisis: esto es mas fiable que
    intentar interpretar cada campo del header, que varia de un recurso a
    otro)."""
    name_start = 20
    nul = out.index(0, name_start)
    name = out[name_start:nul].decode('latin1', 'replace')
    p = nul + 1
    if p % 2:
        p += 1
    total = len(out)
    for off in range(p, min(p + 60, total - 8), 2):
        w = struct.unpack('>H', out[off:off + 2])[0]
        h = struct.unpack('>H', out[off + 2:off + 4])[0]
        if not (4 <= w <= 4096 and 4 <= h <= 4096 and w % 4 == 0 and h % 4 == 0):
            continue
        for pstart in range(off + 4, min(off + 4 + 80, total), 2):
            remaining = total - pstart
            if remaining == (w * h) // 2:
                return name, w, h, pstart, 'DXT1'
            if remaining == w * h:
                return name, w, h, pstart, 'DXT5'
            if remaining == w * h * 4:
                return name, w, h, pstart, 'RGBA8888'
            if remaining == w * h * 2:
                return name, w, h, pstart, 'RAW16'
    return name, None, None, None, None


def unpack565(c):
    r = ((c >> 11) & 0x1F) * 255 // 31
    g = ((c >> 5) & 0x3F) * 255 // 63
    b = (c & 0x1F) * 255 // 31
    return r, g, b


def pack565(r, g, b):
    return ((r * 31 // 255) << 11) | ((g * 63 // 255) << 5) | (b * 31 // 255)


def decode_dxt1(buf, w, h):
    out = np.zeros((h, w, 4), dtype=np.uint8)
    bx_n, by_n = w // 4, h // 4
    idx = 0
    for by in range(by_n):
        for bx in range(bx_n):
            c0, c1 = struct.unpack_from('>HH', buf, idx)
            bits = struct.unpack_from('>I', buf, idx + 4)[0]
            idx += 8
            r0, g0, b0 = unpack565(c0)
            r1, g1, b1 = unpack565(c1)
            colors = [(r0, g0, b0, 255), (r1, g1, b1, 255)]
            if c0 > c1:
                colors.append(((2 * r0 + r1) // 3, (2 * g0 + g1) // 3, (2 * b0 + b1) // 3, 255))
                colors.append(((r0 + 2 * r1) // 3, (g0 + 2 * g1) // 3, (b0 + 2 * b1) // 3, 255))
            else:
                colors.append(((r0 + r1) // 2, (g0 + g1) // 2, (b0 + b1) // 2, 255))
                colors.append((0, 0, 0, 0))
            for py in range(4):
                for px in range(4):
                    ci = (bits >> ((py * 4 + px) * 2)) & 0x3
                    yy, xx = by * 4 + py, bx * 4 + px
                    if yy < h and xx < w:
                        out[yy, xx] = colors[ci]
    return out


def decode_dxt5(buf, w, h):
    out = np.zeros((h, w, 4), dtype=np.uint8)
    bx_n, by_n = w // 4, h // 4
    idx = 0
    for by in range(by_n):
        for bx in range(bx_n):
            a0, a1 = buf[idx], buf[idx + 1]
            abits = int.from_bytes(buf[idx + 2:idx + 8], 'big')
            c0, c1 = struct.unpack_from('>HH', buf, idx + 8)
            cbits = struct.unpack_from('>I', buf, idx + 12)[0]
            idx += 16
            alphas = [a0, a1]
            if a0 > a1:
                for k in range(1, 7):
                    alphas.append(((7 - k) * a0 + k * a1) // 7)
            else:
                for k in range(1, 5):
                    alphas.append(((5 - k) * a0 + k * a1) // 5)
                alphas += [0, 255]
            r0, g0, b0 = unpack565(c0)
            r1, g1, b1 = unpack565(c1)
            colors = [(r0, g0, b0), (r1, g1, b1),
                      ((2 * r0 + r1) // 3, (2 * g0 + g1) // 3, (2 * b0 + b1) // 3),
                      ((r0 + 2 * r1) // 3, (g0 + 2 * g1) // 3, (b0 + 2 * b1) // 3)]
            for py in range(4):
                for px in range(4):
                    pix = py * 4 + px
                    a_idx = (abits >> (pix * 3)) & 0x7
                    c_idx = (cbits >> (pix * 2)) & 0x3
                    yy, xx = by * 4 + py, bx * 4 + px
                    if yy < h and xx < w:
                        r, g, b = colors[c_idx]
                        out[yy, xx] = (r, g, b, alphas[a_idx])
    return out


def decode_rgba8888(buf, w, h):
    arr = np.frombuffer(buf[:w * h * 4], dtype=np.uint8).reshape(h, w, 4)
    return arr.copy()


def decode_image(buf, w, h, fmt):
    if fmt == 'DXT1':
        return decode_dxt1(buf, w, h)
    if fmt == 'DXT5':
        return decode_dxt5(buf, w, h)
    if fmt == 'RGBA8888':
        return decode_rgba8888(buf, w, h)
    raise ValueError('formato %s no soportado para decodificar' % fmt)


# --------------------------------------------------------------- encoder --

def _to_blocks(img, w, h):
    """(h,w,C) -> (nblocks_y, nblocks_x, 4, 4, C), rellenando si hace falta."""
    C = img.shape[2]
    ph, pw = (h + 3) // 4 * 4, (w + 3) // 4 * 4
    if (ph, pw) != (h, w):
        canvas = np.zeros((ph, pw, C), dtype=img.dtype)
        canvas[:h, :w] = img
        img = canvas
    by, bx = ph // 4, pw // 4
    blocks = img.reshape(by, 4, bx, 4, C).transpose(0, 2, 1, 3, 4)
    return blocks.reshape(by * bx, 16, C), by, bx


def _pca_endpoints(blocks):
    """Para cada bloque (N,16,C) encuentra los 2 colores extremos a lo
    largo del eje de mayor varianza real (component principal), en vez de
    solo tomar los pixeles de mayor/menor luminancia. Esto evita el
    'dithering' artificial que aparece al recomprimir zonas casi-planas
    con gradientes sutiles (tipico de fondos de logos)."""
    n = blocks.shape[0]
    mean = blocks.mean(axis=1, keepdims=True)  # (N,1,C)
    dev = (blocks - mean).astype(np.float64)  # (N,16,C)
    cov = np.einsum('nki,nkj->nij', dev, dev)  # (N,C,C)
    C = blocks.shape[2]
    v = np.ones((n, C), dtype=np.float64)
    for _ in range(6):
        v = np.einsum('nij,nj->ni', cov, v)
        norm = np.linalg.norm(v, axis=1, keepdims=True)
        norm = np.where(norm < 1e-9, 1.0, norm)
        v = v / norm
    proj = np.einsum('nki,ni->nk', dev, v)  # (N,16)
    imax = np.argmax(proj, axis=1)
    imin = np.argmin(proj, axis=1)
    idxN = np.arange(n)
    return blocks[idxN, imax], blocks[idxN, imin]


def encode_dxt1(img, w, h):
    """Codificador DXT1 vectorizado con numpy: elige los colores base de
    cada bloque por analisis de componente principal (PCA), no solo por
    min/max de luminancia -> mucha menos banda/ruido visible que un
    bounding-box simple. No es tan optimo como un compresor comercial
    pero es rapido y de buena calidad para modding."""
    rgb = img[:, :, :3].astype(np.int32)
    blocks, by, bx = _to_blocks(rgb, w, h)  # (N,16,3)
    n = blocks.shape[0]
    c0rgb, c1rgb = _pca_endpoints(blocks)
    c0 = ((c0rgb[:, 0] * 31 // 255) << 11) | ((c0rgb[:, 1] * 63 // 255) << 5) | (c0rgb[:, 2] * 31 // 255)
    c1 = ((c1rgb[:, 0] * 31 // 255) << 11) | ((c1rgb[:, 1] * 63 // 255) << 5) | (c1rgb[:, 2] * 31 // 255)
    same = c0 == c1
    c0 = np.where(same, np.where(c0 == 0, 1, c0 - 1), c0)
    swap = c0 < c1
    c0s = np.where(swap, c1, c0)
    c1s = np.where(swap, c0, c1)
    c0, c1 = c0s, c1s

    r0 = ((c0 >> 11) & 0x1F) * 255 // 31
    g0 = ((c0 >> 5) & 0x3F) * 255 // 63
    b0 = (c0 & 0x1F) * 255 // 31
    r1 = ((c1 >> 11) & 0x1F) * 255 // 31
    g1 = ((c1 >> 5) & 0x3F) * 255 // 63
    b1 = (c1 & 0x1F) * 255 // 31
    pal = np.stack([
        np.stack([r0, g0, b0], axis=1),
        np.stack([r1, g1, b1], axis=1),
        np.stack([(2 * r0 + r1) // 3, (2 * g0 + g1) // 3, (2 * b0 + b1) // 3], axis=1),
        np.stack([(r0 + 2 * r1) // 3, (g0 + 2 * g1) // 3, (b0 + 2 * b1) // 3], axis=1),
    ], axis=1).astype(np.int32)  # (N,4,3)

    d = ((pal[:, :, None, :] - blocks[:, None, :, :]) ** 2).sum(axis=3)  # (N,4,16)
    ci = np.argmin(d, axis=1).astype(np.uint32)  # (N,16)
    bits = np.zeros(n, dtype=np.uint32)
    for k in range(16):
        bits |= (ci[:, k].astype(np.uint32) << np.uint32(k * 2))

    out = np.empty((n, 8), dtype=np.uint8)
    out[:, 0:2] = c0.astype('>u2').view(np.uint8).reshape(n, 2)
    out[:, 2:4] = c1.astype('>u2').view(np.uint8).reshape(n, 2)
    out[:, 4:8] = bits.astype('>u4').view(np.uint8).reshape(n, 4)
    return out.tobytes()


def encode_dxt5(img, w, h):
    rgba = img[:, :, :4].astype(np.int32)
    blocks, by, bx = _to_blocks(rgba, w, h)  # (N,16,4)
    n = blocks.shape[0]
    rgb = blocks[:, :, :3]
    alpha = blocks[:, :, 3]

    c0rgb, c1rgb = _pca_endpoints(rgb)
    c0 = ((c0rgb[:, 0] * 31 // 255) << 11) | ((c0rgb[:, 1] * 63 // 255) << 5) | (c0rgb[:, 2] * 31 // 255)
    c1 = ((c1rgb[:, 0] * 31 // 255) << 11) | ((c1rgb[:, 1] * 63 // 255) << 5) | (c1rgb[:, 2] * 31 // 255)
    same = c0 == c1
    c0 = np.where(same, np.where(c0 < 0xFFFF, c0 + 1, c0 - 1), c0)
    swap = c0 < c1
    c0s = np.where(swap, c1, c0)
    c1s = np.where(swap, c0, c1)
    c0, c1 = c0s, c1s

    r0 = ((c0 >> 11) & 0x1F) * 255 // 31
    g0 = ((c0 >> 5) & 0x3F) * 255 // 63
    b0 = (c0 & 0x1F) * 255 // 31
    r1 = ((c1 >> 11) & 0x1F) * 255 // 31
    g1 = ((c1 >> 5) & 0x3F) * 255 // 63
    b1 = (c1 & 0x1F) * 255 // 31
    pal = np.stack([
        np.stack([r0, g0, b0], axis=1),
        np.stack([r1, g1, b1], axis=1),
        np.stack([(2 * r0 + r1) // 3, (2 * g0 + g1) // 3, (2 * b0 + b1) // 3], axis=1),
        np.stack([(r0 + 2 * r1) // 3, (g0 + 2 * g1) // 3, (b0 + 2 * b1) // 3], axis=1),
    ], axis=1).astype(np.int32)
    d = ((pal[:, :, None, :] - rgb[:, None, :, :]) ** 2).sum(axis=3)
    cidx = np.argmin(d, axis=1).astype(np.uint32)
    cbits = np.zeros(n, dtype=np.uint32)
    for k in range(16):
        cbits |= (cidx[:, k].astype(np.uint32) << np.uint32(k * 2))

    a0 = alpha.max(axis=1)
    a1 = alpha.min(axis=1)
    a0 = np.where(a0 == a1, np.minimum(255, a0 + 1), a0)
    avals = [a0, a1]
    for k in range(1, 7):
        avals.append(((7 - k) * a0 + k * a1) // 7)
    avals = np.stack(avals, axis=1)  # (N,8)
    ad = np.abs(avals[:, :, None] - alpha[:, None, :])  # (N,8,16)
    aidx = np.argmin(ad, axis=1).astype(np.uint64)  # (N,16)
    abits = np.zeros(n, dtype=np.uint64)
    for k in range(16):
        abits |= (aidx[:, k] << np.uint64(k * 3))

    out = np.empty((n, 16), dtype=np.uint8)
    out[:, 0] = a0.astype(np.uint8)
    out[:, 1] = a1.astype(np.uint8)
    abits_bytes = abits.astype('>u8').view(np.uint8).reshape(n, 8)[:, 2:8]  # 6 bytes bajos, big-endian
    out[:, 2:8] = abits_bytes
    out[:, 8:10] = c0.astype('>u2').view(np.uint8).reshape(n, 2)
    out[:, 10:12] = c1.astype('>u2').view(np.uint8).reshape(n, 2)
    out[:, 12:16] = cbits.astype('>u4').view(np.uint8).reshape(n, 4)
    return out.tobytes()


def encode_rgba8888(img, w, h):
    return img[:, :, :4].astype(np.uint8).tobytes()


def encode_image(img, w, h, fmt):
    if fmt == 'DXT1':
        return encode_dxt1(img, w, h)
    if fmt == 'DXT5':
        return encode_dxt5(img, w, h)
    if fmt == 'RGBA8888':
        return encode_rgba8888(img, w, h)
    raise ValueError('formato %s no se puede recodificar (solo DXT1/DXT5/RGBA8888)' % fmt)


# ==================================================== logica de alto nivel ==

def safe_name(s):
    keep = [c if (c.isalnum() or c in '-_') else '_' for c in s]
    return ''.join(keep) or 'sin_nombre'


def extract_fpg(path, outdir, log):
    if not HAVE_IMG:
        raise RuntimeError('faltan las librerias Pillow y/o numpy (pip install pillow numpy)')
    data = open(path, 'rb').read()
    fpg = parse_fpg(data)
    os.makedirs(outdir)

    manifest_entries = []
    n_img = n_other = n_fail = 0
    for e in fpg['entries']:
        blob = data[e['offset']:e['offset'] + e['csize']]
        try:
            out = zlib.decompress(blob)
        except Exception as ex:
            log('  [%03d] ERROR al descomprimir: %s' % (e['index'], ex))
            n_fail += 1
            manifest_entries.append(dict(index=e['index'], hash=e['hash'], kind='raw_undecoded',
                                          raw_b64=base64.b64encode(blob).decode('ascii')))
            continue

        if out[:8] == IMG_MAGIC:
            name, w, h, pstart, fmt = locate_image_fields(out)
            if w is None or fmt not in ('DXT1', 'DXT5', 'RGBA8888'):
                motivo = 'formato %s sin decodificador' % fmt if fmt else 'no se pudo ubicar ancho/alto'
                log('  [%03d] "%s": %s -> se preserva sin editar' % (e['index'], name, motivo))
                fname = '%03d_%s.bin' % (e['index'], safe_name(name))
                open(os.path.join(outdir, fname), 'wb').write(out)
                manifest_entries.append(dict(index=e['index'], hash=e['hash'], kind='image_unsupported',
                                              name=name, file=fname))
                n_fail += 1
                continue
            header_prefix = out[:pstart]
            pixels = out[pstart:]
            img = decode_image(pixels, w, h, fmt)
            fname = '%03d_%s.png' % (e['index'], safe_name(name))
            Image.fromarray(img, 'RGBA').save(os.path.join(outdir, fname))
            manifest_entries.append(dict(
                index=e['index'], hash=e['hash'], kind='image', name=name,
                file=fname, width=w, height=h, format=fmt,
                header_prefix_b64=base64.b64encode(header_prefix).decode('ascii'),
                orig_blob_b64=base64.b64encode(blob).decode('ascii')))
            n_img += 1
        else:
            fname = '%03d_res.bin' % e['index']
            open(os.path.join(outdir, fname), 'wb').write(out)
            manifest_entries.append(dict(index=e['index'], hash=e['hash'], kind='other', file=fname))
            n_other += 1

    manifest = dict(source=os.path.basename(path), count=fpg['count'],
                     header_b64=base64.b64encode(fpg['header']).decode('ascii'),
                     entries=manifest_entries)
    open(os.path.join(outdir, MANIFEST_NAME), 'w', encoding='utf-8').write(json.dumps(manifest, indent=1))
    log('Extraidas %d imagenes, %d recursos de otro tipo, %d sin soporte -> %s'
        % (n_img, n_other, n_fail, outdir))


def rewrite_wh_in_header(header_prefix, orig_w, orig_h, new_w, new_h):
    """Si el tamano de una imagen cambio, busca los campos ancho/alto
    dentro del prefijo de header (los mismos que localize_image_fields ya
    encontro) y los reescribe. Devuelve (header_prefix_nuevo, encontrado)."""
    hp = bytearray(header_prefix)
    if (new_w, new_h) == (orig_w, orig_h):
        return bytes(hp), True
    name_start = 20
    nul = hp.index(0, name_start)
    p = nul + 1
    if p % 2:
        p += 1
    for off in range(p, len(hp) - 4, 2):
        w0 = struct.unpack_from('>H', hp, off)[0]
        h0 = struct.unpack_from('>H', hp, off + 2)[0]
        if w0 == orig_w and h0 == orig_h:
            struct.pack_into('>H', hp, off, new_w)
            struct.pack_into('>H', hp, off + 2, new_h)
            return bytes(hp), True
    return bytes(hp), False


def build_image_payload(header_prefix, orig_w, orig_h, fmt, img, log=None, idx=None, name=''):
    """A partir del prefijo de header original (magic+nombre+campos) y una
    imagen RGBA (numpy array) ya del tamano final (multiplo de 4), arma el
    payload SG28PT01 completo (header + pixeles) listo para comprimir con
    zlib. Devuelve (payload_bytes, new_w, new_h)."""
    new_h, new_w = img.shape[0], img.shape[1]
    pixel_bytes = encode_image(img, new_w, new_h, fmt)
    hp, found = rewrite_wh_in_header(header_prefix, orig_w, orig_h, new_w, new_h)
    if (new_w, new_h) != (orig_w, orig_h):
        if not found and log:
            log('  [%s] AVISO: no se pudo reescribir ancho/alto en el header, '
                'se mantiene el original' % (idx if idx is not None else '?'))
        if log:
            log('  [%s] "%s" redimensionado %dx%d -> %dx%d' % (idx, name, orig_w, orig_h, new_w, new_h))
    payload = bytearray(hp) + bytearray(pixel_bytes)
    new_size = len(payload) - 16
    struct.pack_into('>I', payload, 12, new_size)
    struct.pack_into('>I', payload, 16, new_size)
    return bytes(payload), new_w, new_h


def pad_to_multiple_of_4(pil_img):
    w, h = pil_img.size
    if w % 4 == 0 and h % 4 == 0:
        return pil_img, w, h
    pad_w, pad_h = (w + 3) // 4 * 4, (h + 3) // 4 * 4
    canvas = Image.new('RGBA', (pad_w, pad_h))
    canvas.paste(pil_img, (0, 0))
    return canvas, pad_w, pad_h


def assemble_fpg(header, count, blobs, hashes):
    """Arma los bytes finales de un .fpg a partir de la cabecera original,
    la cantidad de entradas y una lista de blobs YA comprimidos con zlib
    (uno por entrada, en orden). hashes: dict indice->hash original."""
    if any(b is None for b in blobs):
        missing = [i for i, b in enumerate(blobs) if b is None]
        raise ValueError('faltan entradas al armar el .fpg (indices %s)' % missing)
    data_start = align_2048(TABLE_OFFSET + count * 16)
    table = bytearray()
    body = bytearray()
    cursor = data_start
    for i in range(count):
        blob = blobs[i]
        dec_len = len(zlib.decompress(blob))
        table += struct.pack('<IIII', hashes.get(i, 0), cursor, len(blob), dec_len)
        body += blob
        cursor += len(blob)
    final = bytearray(header)
    final += b'\x00' * (TABLE_OFFSET - len(final)) if len(final) < TABLE_OFFSET else b''
    final = final[:TABLE_OFFSET] + table
    final += b'\x00' * (data_start - len(final))
    final += body
    return bytes(final)


def repack_fpg(folder, log):
    if not HAVE_IMG:
        raise RuntimeError('faltan las librerias Pillow y/o numpy (pip install pillow numpy)')
    mpath = os.path.join(folder, MANIFEST_NAME)
    if not os.path.isfile(mpath):
        raise ValueError('la carpeta no tiene %s (no fue creada por esta herramienta)' % MANIFEST_NAME)
    manifest = json.load(open(mpath, encoding='utf-8'))
    header = base64.b64decode(manifest['header_b64'])
    count = manifest['count']

    blobs = [None] * count
    hashes = {ent['index']: ent['hash'] for ent in manifest['entries']}
    n_changed = 0
    for ent in manifest['entries']:
        i = ent['index']
        kind = ent['kind']
        fpath = os.path.join(folder, ent.get('file', ''))
        if kind == 'raw_undecoded':
            blobs[i] = base64.b64decode(ent['raw_b64'])  # ya viene comprimido tal cual
            continue
        elif kind in ('other', 'image_unsupported'):
            blobs[i] = zlib.compress(open(fpath, 'rb').read(), 9)
            continue
        elif kind != 'image':
            raise ValueError('tipo de entrada desconocido en el manifest: %s' % kind)

        header_prefix = base64.b64decode(ent['header_prefix_b64'])
        fmt = ent['format']
        orig_w, orig_h = ent['width'], ent['height']
        pil = Image.open(fpath).convert('RGBA')

        # comparar contra el original: si el PNG es pixel-a-pixel igual a lo
        # que ya habia en el .fpg, reusar el bloque comprimido ORIGINAL tal
        # cual (cero perdida de calidad). Solo recodificar si de verdad cambio.
        orig_blob = base64.b64decode(ent['orig_blob_b64']) if 'orig_blob_b64' in ent else None
        if orig_blob is not None and pil.size == (orig_w, orig_h):
            orig_out = zlib.decompress(orig_blob)
            orig_img = decode_image(orig_out[len(header_prefix):], orig_w, orig_h, fmt)
            if np.array_equal(np.array(pil), orig_img):
                blobs[i] = orig_blob
                continue

        pil, new_w, new_h = pad_to_multiple_of_4(pil)
        payload, new_w, new_h = build_image_payload(
            header_prefix, orig_w, orig_h, fmt, np.array(pil), log, i, ent['name'])
        blobs[i] = zlib.compress(payload, 9)
        n_changed += 1

    final = assemble_fpg(header, count, blobs, hashes)
    out_name = os.path.splitext(manifest['source'])[0] + '_mod.fpg'
    out_path = os.path.join(folder, out_name)
    open(out_path, 'wb').write(final)
    log('FPG reconstruido: %d entradas (%d re-codificadas) -> %s' % (count, n_changed, out_path))
    return out_path


def is_fpg_file(path):
    try:
        with open(path, 'rb') as f:
            head = f.read(8)
        return head[0:4] == MAGIC
    except Exception:
        return False


# ==================================================================== GUI ==

if TK_OK:
    try:
        from PIL import ImageTk
    except Exception:
        ImageTk = None

    class EditorApp:
        """Ventana unica: arrastras el .fpg, ves la lista de recursos con
        miniatura, elegis uno y lo reemplazas ahi mismo -- la vista previa
        ya muestra el resultado REAL tras comprimir (no el PNG sin tocar),
        para detectar problemas de calidad antes de guardar. Al final,
        'Guardar .fpg modificado' arma el archivo final."""

        def __init__(self, root):
            self.root = root
            root.title('MVC2 FPG Tool - editor visual')
            root.geometry('980x600')

            self.fpg_path = None
            self.header = None
            self.count = 0
            self.entries = {}   # indice -> dict con el estado de cada entrada
            self._tk_img = None

            paned = tk.PanedWindow(root, orient='horizontal', sashwidth=6)
            paned.pack(fill='both', expand=True)

            left = tk.Frame(paned)
            cols = ('idx', 'nombre', 'tipo', 'tam', 'estado')
            self.tree = ttk.Treeview(left, columns=cols, show='headings', height=30)
            headers = [('idx', '#', 40), ('nombre', 'Nombre', 170), ('tipo', 'Tipo', 55),
                       ('tam', 'Tamano', 80), ('estado', 'Estado', 95)]
            for c, label, w in headers:
                self.tree.heading(c, text=label)
                self.tree.column(c, width=w, anchor='w')
            vsb = ttk.Scrollbar(left, orient='vertical', command=self.tree.yview)
            self.tree.configure(yscrollcommand=vsb.set)
            self.tree.pack(side='left', fill='both', expand=True)
            vsb.pack(side='left', fill='y')
            self.tree.bind('<<TreeviewSelect>>', self.on_select)
            paned.add(left, width=420)

            right = tk.Frame(paned)
            self.preview = tk.Label(right, text='Arrastra un .fpg para empezar\n'
                                                  '(o usa "Abrir .fpg...")',
                                     bg='#2b2b2b', fg='white', font=('Segoe UI', 11))
            self.preview.pack(fill='both', expand=True, padx=8, pady=8)

            btnf = tk.Frame(right)
            btnf.pack(fill='x', padx=8)
            tk.Button(btnf, text='Abrir .fpg...', command=self.cmd_open).pack(side='left', padx=3)
            tk.Button(btnf, text='Reemplazar imagen...', command=self.cmd_replace).pack(side='left', padx=3)
            tk.Button(btnf, text='Restaurar original', command=self.cmd_restore).pack(side='left', padx=3)
            tk.Button(btnf, text='Guardar .fpg modificado...', command=self.cmd_save).pack(side='right', padx=3)

            self.logbox = scrolledtext.ScrolledText(right, state='disabled', wrap='word',
                                                      font=('Consolas', 9), height=10)
            self.logbox.pack(fill='both', expand=False, padx=8, pady=8)
            paned.add(right)

            if not HAVE_IMG:
                self.log('AVISO: falta Pillow y/o numpy. Instala con:  pip install pillow numpy')
            if ImageTk is None:
                self.log('AVISO: no se pudo cargar ImageTk (viene con Pillow); sin el, no hay miniaturas.')

            if HAVE_DND:
                root.drop_target_register(DND_FILES)
                root.dnd_bind('<<Drop>>', self.on_drop)
            else:
                self.log('Arrastrar y soltar no disponible (falta tkinterdnd2): usa "Abrir .fpg...".')

        def log(self, msg):
            self.logbox.configure(state='normal')
            self.logbox.insert('end', msg + '\n')
            self.logbox.see('end')
            self.logbox.configure(state='disabled')

        def on_drop(self, event):
            for p in self.root.tk.splitlist(event.data):
                if os.path.isfile(p):
                    self.load_fpg(p)
                    return

        def cmd_open(self):
            path = filedialog.askopenfilename(title='Elegir .fpg', filetypes=[('FPG', '*.fpg'), ('Todos', '*.*')])
            if path:
                self.load_fpg(path)

        # ---------------------------------------------------------- carga --
        def load_fpg(self, path):
            if not HAVE_IMG:
                messagebox.showerror('Faltan librerias', 'Instala Pillow y numpy: pip install pillow numpy')
                return
            if not is_fpg_file(path):
                messagebox.showerror('No es FPG', 'Este archivo no tiene la firma "30GF" esperada.')
                return
            try:
                data = open(path, 'rb').read()
                fpg = parse_fpg(data)
            except Exception as ex:
                messagebox.showerror('Error al abrir', str(ex))
                return

            self.fpg_path = path
            self.header = fpg['header']
            self.count = fpg['count']
            self.entries = {}
            self.tree.delete(*self.tree.get_children())

            n_img = n_other = n_unsup = 0
            for e in fpg['entries']:
                i = e['index']
                blob = data[e['offset']:e['offset'] + e['csize']]
                try:
                    out = zlib.decompress(blob)
                except Exception:
                    self.entries[i] = dict(kind='raw_undecoded', hash=e['hash'], raw=blob)
                    self.tree.insert('', 'end', iid=str(i), values=(i, '(sin descomprimir)', 'raw', '-', 'original'))
                    continue
                if out[:8] == IMG_MAGIC:
                    name, w, h, pstart, fmt = locate_image_fields(out)
                    if w is None or fmt not in ('DXT1', 'DXT5', 'RGBA8888'):
                        self.entries[i] = dict(kind='image_unsupported', hash=e['hash'], name=name, raw=out)
                        self.tree.insert('', 'end', iid=str(i), values=(i, name, fmt or '?', '-', 'sin soporte'))
                        n_unsup += 1
                        continue
                    header_prefix = out[:pstart]
                    img = decode_image(out[pstart:], w, h, fmt)
                    self.entries[i] = dict(kind='image', hash=e['hash'], name=name, format=fmt,
                                            width=w, height=h, header_prefix=header_prefix,
                                            orig_blob=blob, current_img=img, modified=False)
                    self.tree.insert('', 'end', iid=str(i), values=(i, name, fmt, '%dx%d' % (w, h), 'original'))
                    n_img += 1
                else:
                    self.entries[i] = dict(kind='other', hash=e['hash'], raw=out)
                    self.tree.insert('', 'end', iid=str(i), values=(i, '(recurso)', 'otro', '-', 'original'))
                    n_other += 1

            self.log('Cargado "%s": %d imagenes, %d recursos, %d sin soporte de %d entradas.'
                      % (os.path.basename(path), n_img, n_other, n_unsup, self.count))
            self.preview.configure(image='', text='Elegi una imagen de la lista\n'
                                                    'para verla y reemplazarla.')

        # ------------------------------------------------------- vista previa --
        def _checker_bg(self, size):
            w, h = size
            tile = 12
            arr = np.zeros((h, w, 3), dtype=np.uint8)
            for y in range(0, h, tile):
                for x in range(0, w, tile):
                    c = 90 if ((x // tile) + (y // tile)) % 2 == 0 else 70
                    arr[y:y + tile, x:x + tile] = c
            return Image.fromarray(arr, 'RGB')

        def on_select(self, event=None):
            sel = self.tree.selection()
            if not sel:
                return
            idx = int(sel[0])
            ent = self.entries.get(idx)
            if not ent or ent['kind'] != 'image' or ImageTk is None:
                self.preview.configure(image='', text='(sin vista previa para esta entrada)')
                self._tk_img = None
                return
            pil = Image.fromarray(ent['current_img'], 'RGBA')
            pil.thumbnail((560, 480))
            bg = self._checker_bg(pil.size).convert('RGBA')
            composed = Image.alpha_composite(bg, pil)
            tkimg = ImageTk.PhotoImage(composed)
            self._tk_img = tkimg  # mantener referencia viva
            estado = 'MODIFICADO (vista previa ya comprimida)' if ent.get('modified') else 'original'
            self.preview.configure(image=tkimg, text='', compound='top')

        # ------------------------------------------------------- reemplazar --
        def cmd_replace(self):
            sel = self.tree.selection()
            if not sel:
                messagebox.showinfo('Elegi una imagen', 'Selecciona una imagen de la lista primero.')
                return
            idx = int(sel[0])
            ent = self.entries.get(idx)
            if not ent or ent['kind'] != 'image':
                messagebox.showinfo('No editable', 'Esta entrada no se puede reemplazar '
                                                     '(no es una imagen con formato soportado).')
                return
            path = filedialog.askopenfilename(title='Elegir imagen de reemplazo',
                                               filetypes=[('Imagenes', '*.png;*.bmp;*.tga;*.jpg;*.jpeg'),
                                                          ('Todos', '*.*')])
            if not path:
                return
            try:
                pil = Image.open(path).convert('RGBA')
                pil, new_w, new_h = pad_to_multiple_of_4(pil)
                if (new_w, new_h) != pil.size:
                    pass
                img = np.array(pil)
                enc = encode_image(img, new_w, new_h, ent['format'])
                preview_img = decode_image(enc, new_w, new_h, ent['format'])  # resultado REAL post-compresion
            except Exception as ex:
                messagebox.showerror('Error al procesar la imagen', str(ex))
                return

            ent['current_img'] = preview_img
            ent['pending_img'] = img
            ent['new_w'], ent['new_h'] = new_w, new_h
            ent['modified'] = True
            self.tree.set(str(idx), 'tam', '%dx%d' % (new_w, new_h))
            self.tree.set(str(idx), 'estado', 'MODIFICADO')
            self.log('[%03d] "%s" reemplazado. La vista previa ya muestra el resultado '
                      'comprimido real (no el PNG original sin tocar).' % (idx, ent['name']))
            self.on_select()

        def cmd_restore(self):
            sel = self.tree.selection()
            if not sel:
                return
            idx = int(sel[0])
            ent = self.entries.get(idx)
            if not ent or ent['kind'] != 'image' or not ent.get('modified'):
                return
            out = zlib.decompress(ent['orig_blob'])
            name, w, h, pstart, fmt = locate_image_fields(out)
            ent['current_img'] = decode_image(out[pstart:], w, h, fmt)
            ent.pop('pending_img', None)
            ent.pop('new_w', None)
            ent.pop('new_h', None)
            ent['modified'] = False
            self.tree.set(str(idx), 'tam', '%dx%d' % (w, h))
            self.tree.set(str(idx), 'estado', 'original')
            self.log('[%03d] restaurado al original.' % idx)
            self.on_select()

        # ------------------------------------------------------------ guardar --
        def cmd_save(self):
            if not self.fpg_path:
                messagebox.showinfo('Nada para guardar', 'Primero abri un .fpg.')
                return
            out_name = os.path.splitext(os.path.basename(self.fpg_path))[0] + '_mod.fpg'
            out_path = filedialog.asksaveasfilename(defaultextension='.fpg', initialfile=out_name,
                                                      filetypes=[('FPG', '*.fpg')])
            if not out_path:
                return
            try:
                blobs = [None] * self.count
                hashes = {}
                n_changed = 0
                for idx, ent in self.entries.items():
                    hashes[idx] = ent['hash']
                    if ent['kind'] == 'raw_undecoded':
                        blobs[idx] = ent['raw']
                    elif ent['kind'] in ('other', 'image_unsupported'):
                        blobs[idx] = zlib.compress(ent['raw'], 9)
                    elif ent['kind'] == 'image':
                        if ent.get('modified'):
                            payload, nw, nh = build_image_payload(
                                ent['header_prefix'], ent['width'], ent['height'], ent['format'],
                                ent['pending_img'], self.log, idx, ent['name'])
                            blobs[idx] = zlib.compress(payload, 9)
                            n_changed += 1
                        else:
                            blobs[idx] = ent['orig_blob']
                final = assemble_fpg(self.header, self.count, blobs, hashes)
                open(out_path, 'wb').write(final)
            except Exception as ex:
                self.log('ERROR al guardar: %s' % ex)
                messagebox.showerror('Error al guardar', str(ex))
                return
            self.log('Guardado: %s (%d imagenes modificadas de %d entradas totales)'
                      % (out_path, n_changed, self.count))
            messagebox.showinfo('Listo', 'Archivo guardado:\n%s\n\n%d imagen(es) modificada(s).'
                                 % (out_path, n_changed))

    class App:
        def __init__(self, root):
            self.root = root
            root.title('MVC2 FPG Tool - extraccion / reemplazo de imagenes .fpg')
            root.geometry('680x440')

            tk.Label(root, text='Arrastra aqui un .fpg (para extraer a PNG)\n'
                                 'o la carpeta ya extraida (para reempaquetar)',
                     font=('Segoe UI', 12), pady=16, justify='center').pack()

            btnf = tk.Frame(root)
            btnf.pack(pady=6)
            tk.Button(btnf, text='Abrir .fpg...', width=18, command=self.cmd_open_file).pack(side='left', padx=6)
            tk.Button(btnf, text='Abrir carpeta...', width=18, command=self.cmd_open_folder).pack(side='left', padx=6)

            self.logbox = scrolledtext.ScrolledText(root, state='disabled', wrap='word',
                                                      font=('Consolas', 9), height=18)
            self.logbox.pack(fill='both', expand=True, padx=10, pady=10)

            if not HAVE_IMG:
                self.log('AVISO: falta Pillow y/o numpy. Instala con:  pip install pillow numpy')

            if HAVE_DND:
                root.drop_target_register(DND_FILES)
                root.dnd_bind('<<Drop>>', self.on_drop)
                self.log('Arrastrar y soltar activado.')
            else:
                self.log('Arrastrar y soltar no disponible (falta tkinterdnd2): usa los botones de arriba.')

        def log(self, msg):
            self.logbox.configure(state='normal')
            self.logbox.insert('end', msg + '\n')
            self.logbox.see('end')
            self.logbox.configure(state='disabled')

        def on_drop(self, event):
            for p in self.root.tk.splitlist(event.data):
                self.handle_path(p)

        def cmd_open_file(self):
            path = filedialog.askopenfilename(title='Elegir .fpg', filetypes=[('FPG', '*.fpg'), ('Todos', '*.*')])
            if path:
                self.handle_path(path)

        def cmd_open_folder(self):
            path = filedialog.askdirectory(title='Elegir carpeta extraida')
            if path:
                self.handle_path(path)

        def handle_path(self, path):
            try:
                if os.path.isdir(path):
                    self.log('--- Carpeta: %s ---' % path)
                    repack_fpg(path, self.log)
                elif os.path.isfile(path):
                    self.log('--- Archivo: %s ---' % path)
                    if not is_fpg_file(path):
                        self.log('No parece un .fpg valido (falta la firma "30GF").')
                        messagebox.showerror('No es FPG', 'Este archivo no tiene la firma esperada.')
                        return
                    outdir = os.path.splitext(path)[0] + '_fpg_extraido'
                    if os.path.exists(outdir):
                        if not messagebox.askyesno('Ya existe', 'Ya existe "%s". Borrar y volver a extraer?' % outdir):
                            return
                        import shutil
                        shutil.rmtree(outdir)
                    extract_fpg(path, outdir, self.log)
                else:
                    self.log('Ruta no reconocida: %s' % path)
            except Exception as ex:
                self.log('ERROR: %s' % ex)
                messagebox.showerror('Error', str(ex))


def main_gui():
    root = TkinterDnD.Tk() if HAVE_DND else tk.Tk()
    EditorApp(root)
    root.mainloop()


def main_cli(argv):
    def log(msg):
        print(msg)
    if len(argv) < 3:
        print(__doc__)
        print('\nUso:')
        print('  py MVC2_FPG_Tool.py extraer archivo.fpg')
        print('  py MVC2_FPG_Tool.py reempacar carpeta')
        return
    cmd, target = argv[1], argv[2]
    if cmd == 'extraer':
        outdir = os.path.splitext(target)[0] + '_fpg_extraido'
        extract_fpg(target, outdir, log)
    elif cmd == 'reempacar':
        repack_fpg(target, log)
    else:
        print('Comando no reconocido: %s' % cmd)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] in ('extraer', 'reempacar'):
        main_cli(sys.argv)
    elif TK_OK:
        main_gui()
    else:
        print('tkinter no esta disponible en este entorno. Usa el modo consola:')
        main_cli(['', '', ''])
