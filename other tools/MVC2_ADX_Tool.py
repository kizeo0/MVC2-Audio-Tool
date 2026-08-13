# -*- coding: utf-8 -*-
"""MVC2 ADX Tool - herramienta APARTE para extraer y reempaquetar los
streams ADX de MVC2 (archivos tipo adx_staf.bin, adx_*.bin: musica,
locutor de staff, etc). No modifica ni depende de MVC2_AudioTool.py.

Como se usa:
  - Arrastra un .bin ADX sobre la ventana -> te pregunta:
      "Extraer crudo (.adx tal cual)"  -> copia el stream tal cual viene,
          byte a byte, listo para reempacar sin tocarlo.
      "Convertir a WAV (decodificado)" -> decodifica todo a un .wav de
          16 bits normal y corriente, que puedes editar con cualquier
          programa de audio.
    En ambos casos se crea una carpeta "<nombre>_adx_extraido" con el
    resultado y un "_adx_info.json" con los datos del stream original
    (necesarios para poder reempaquetar correctamente despues).

  - Arrastra esa carpeta (ya sea con el .adx crudo o con el .wav, editado
    o no) de vuelta sobre la ventana -> genera "<nombre>_mod.bin", un ADX
    nuevo listo para reemplazar al original en el juego.
      * Si venia del modo "crudo": simplemente lo empaqueta de vuelta tal
        cual (por si solo quieres inspeccionarlo/renombrarlo).
      * Si venia del modo WAV: vuelve a codificar el WAV (con tus cambios,
        si los hiciste) a ADX real usando el mismo filtro (cutoff/version)
        que tenia el original, para que suene igual de bien.

Codec ADX incluido (formato CRI ADX tipo 3, estandar, con historial de
prediccion PERSISTENTE entre frames -- asi es como decodifica un
reproductor ADX real; el codificador es la funcion inversa exacta).

Ejecutar con Python 3.12:  py -3.12 MVC2_ADX_Tool.py
"""

import os
import sys
import json
import math
import struct
import array

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


# ============================================================== codec ADX ==

def parse_adx_header(data):
    if len(data) < 0x20 or data[0:2] != b'\x80\x00':
        raise ValueError('no es un stream ADX valido (falta la firma 0x80 0x00)')
    if data[4] != 3:
        raise ValueError('tipo de ADX %d no soportado (solo el tipo 3, estandar)' % data[4])
    frame_size = data[5]
    if data[6] != 4:
        raise ValueError('ADX de %d bits no soportado (solo 4 bits)' % data[6])
    channels = data[7]
    if channels not in (1, 2):
        raise ValueError('ADX con %d canales no soportado' % channels)
    sample_rate = struct.unpack('>i', data[8:12])[0]
    num_samples = struct.unpack('>i', data[0x0c:0x10])[0]
    cutoff = struct.unpack('>H', data[0x10:0x12])[0]
    version = struct.unpack('>H', data[0x12:0x14])[0]
    if version not in (0x0300, 0x0400):
        raise ValueError('version de ADX 0x%04X no soportada' % version)
    start_offset = struct.unpack('>H', data[0x02:0x04])[0] + 4
    if start_offset + frame_size > len(data):
        raise ValueError('offset de datos ADX fuera de rango')
    return dict(frame_size=frame_size, channels=channels, sample_rate=sample_rate,
                num_samples=num_samples, cutoff=cutoff, version=version,
                start_offset=start_offset, header=data[:start_offset])


def calc_coeffs(cutoff, sample_rate):
    z = math.cos(2.0 * math.pi * cutoff / sample_rate)
    a = math.sqrt(2.0) - z
    b = math.sqrt(2.0) - 1.0
    c = (a - math.sqrt((a + b) * (a - b))) / b
    return int(c * 8192), int(c * c * -4096)


def adx_decode(data):
    """ADX -> PCM16 entrelazado. Historial de prediccion persistente entre
    frames (asi decodifica de verdad un reproductor ADX; resetear el
    historial en cada frame -- como hace un decodificador simplificado --
    produce ruido/perdida de agudos audible)."""
    h = parse_adx_header(data)
    fsz, ch, sr, ns, cutoff, ver, soff = (h['frame_size'], h['channels'], h['sample_rate'],
                                          h['num_samples'], h['cutoff'], h['version'], h['start_offset'])
    coef1, coef2 = calc_coeffs(cutoff, sr)
    samples_per_frame = (fsz - 2) * 2
    total_frames = (len(data) - soff) // fsz
    out = array.array('h', [0] * (ns * ch))
    hist = [[0, 0] for _ in range(ch)]
    for f in range(total_frames):
        fr = data[soff + f * fsz: soff + (f + 1) * fsz]
        if len(fr) < fsz:
            break
        c = f % ch
        fch = f // ch
        raws = struct.unpack('>h', fr[0:2])[0]
        scale = 0 if (fr[0] == 0x80 and fr[1] == 0x01) else raws + 1
        h1, h2 = hist[c]
        base = fch * samples_per_frame
        if base >= ns:
            continue
        for i in range(samples_per_frame):
            if base + i >= ns:
                break
            nib = fr[2 + i // 2]
            s = (nib >> 4) if (i & 1) == 0 else (nib & 0xf)
            if s & 8:
                s -= 16
            if ver == 0x0300:
                v = s * scale + ((coef1 * h1) >> 12) + ((coef2 * h2) >> 12)
            else:
                v = s * scale + ((coef1 * h1 + coef2 * h2) >> 12)
            v = min(max(v, -32768), 32767)
            out[(base + i) * ch + c] = v
            h2, h1 = h1, v
        hist[c] = [h1, h2]
    return out.tobytes(), sr, ch


def adx_encode(pcm16_bytes, sample_rate, channels, cutoff, version, frame_size):
    """PCM16 entrelazado -> frames ADX (sin cabecera). Es la funcion
    inversa exacta de adx_decode: usa el MISMO historial persistente, y en
    cada muestra actualiza el historial con el valor ya reconstruido
    (cuantizado), igual que hara el decodificador."""
    coef1, coef2 = calc_coeffs(cutoff, sample_rate)
    samples_per_frame = (frame_size - 2) * 2
    src = array.array('h')
    src.frombytes(pcm16_bytes)
    n_per_ch = len(src) // channels
    n_frames_per_ch = (n_per_ch + samples_per_frame - 1) // samples_per_frame
    frames_out = [None] * (n_frames_per_ch * channels)
    for ch in range(channels):
        h1 = h2 = 0
        for fr in range(n_frames_per_ch):
            base = fr * samples_per_frame
            block = []
            for i in range(samples_per_frame):
                idx = base + i
                block.append(src[idx * channels + ch] if idx < n_per_ch else 0)
            # pasada 1: estimar la escala necesaria para este frame
            sh1, sh2 = h1, h2
            maxabs = 1
            for s0 in block:
                if version == 0x0300:
                    pred = ((coef1 * sh1) >> 12) + ((coef2 * sh2) >> 12)
                else:
                    pred = (coef1 * sh1 + coef2 * sh2) >> 12
                d = s0 - pred
                if abs(d) > maxabs:
                    maxabs = abs(d)
                sh2, sh1 = sh1, s0
            scale = max(1, (maxabs + 6) // 7)
            if scale > 32767:
                scale = 32767
            # pasada 2: cuantizar de verdad, con el historial "real"
            # (el que va a usar el decodificador, no el simulado arriba)
            nibs = bytearray(frame_size - 2)
            for i, s0 in enumerate(block):
                if version == 0x0300:
                    pred = ((coef1 * h1) >> 12) + ((coef2 * h2) >> 12)
                else:
                    pred = (coef1 * h1 + coef2 * h2) >> 12
                d = s0 - pred
                nib = int(round(d / scale))
                nib = max(-8, min(7, nib))
                recon = nib * scale + pred
                recon = max(-32768, min(32767, recon))
                h2, h1 = h1, recon
                if i % 2 == 0:
                    nibs[i // 2] = (nib & 0xF) << 4
                else:
                    nibs[i // 2] |= (nib & 0xF)
            frames_out[fr * channels + ch] = struct.pack('>h', scale - 1) + bytes(nibs)
    return b''.join(frames_out), n_per_ch


def build_adx_file(header_template, pcm16_bytes, sample_rate, channels, cutoff, version, frame_size):
    """Reconstruye un .bin ADX completo: reusa la cabecera original tal
    cual (mismo offset de datos, mismo texto de copyright) y solo
    actualiza el numero de samples y, si hiciera falta, canales/tasa."""
    body, n_per_ch = adx_encode(pcm16_bytes, sample_rate, channels, cutoff, version, frame_size)
    header = bytearray(header_template)
    header[8:12] = struct.pack('>i', sample_rate)
    header[0x0c:0x10] = struct.pack('>i', n_per_ch)
    header[7] = channels
    return bytes(header) + body


def write_wav(path, pcm16_bytes, sample_rate, channels):
    import wave
    w = wave.open(path, 'wb')
    w.setnchannels(channels)
    w.setsampwidth(2)
    w.setframerate(sample_rate)
    w.writeframes(pcm16_bytes)
    w.close()


def read_wav(path):
    import wave
    w = wave.open(path, 'rb')
    channels = w.getnchannels()
    sample_rate = w.getframerate()
    sampwidth = w.getsampwidth()
    n = w.getnframes()
    frames = w.readframes(n)
    w.close()
    if sampwidth != 2:
        raise ValueError('el WAV debe ser de 16 bits (este es de %d bits)' % (sampwidth * 8))
    return frames, sample_rate, channels


# ======================================================== logica de alto nivel ==

MANIFEST_NAME = '_adx_info.json'


def extract_raw(path, outdir, log):
    data = open(path, 'rb').read()
    h = parse_adx_header(data)
    base = os.path.splitext(os.path.basename(path))[0]
    os.makedirs(outdir)
    open(os.path.join(outdir, base + '.adx'), 'wb').write(data)
    manifest = dict(mode='raw', source=os.path.basename(path),
                     sample_rate=h['sample_rate'], channels=h['channels'],
                     cutoff=h['cutoff'], version=h['version'],
                     frame_size=h['frame_size'], num_samples=h['num_samples'],
                     header_hex=h['header'].hex())
    open(os.path.join(outdir, MANIFEST_NAME), 'w', encoding='utf-8').write(
        json.dumps(manifest, indent=2))
    log('Extraido crudo (tal cual): %s -> %s' % (os.path.basename(path), outdir))


def extract_wav(path, outdir, log):
    data = open(path, 'rb').read()
    h = parse_adx_header(data)
    pcm, sr, ch = adx_decode(data)
    base = os.path.splitext(os.path.basename(path))[0]
    os.makedirs(outdir)
    write_wav(os.path.join(outdir, base + '.wav'), pcm, sr, ch)
    manifest = dict(mode='wav', source=os.path.basename(path),
                     sample_rate=h['sample_rate'], channels=h['channels'],
                     cutoff=h['cutoff'], version=h['version'],
                     frame_size=h['frame_size'], num_samples=h['num_samples'],
                     header_hex=h['header'].hex())
    open(os.path.join(outdir, MANIFEST_NAME), 'w', encoding='utf-8').write(
        json.dumps(manifest, indent=2))
    log('Extraido y decodificado a WAV: %s -> %s (%d Hz, %d canal%s, %.1f s)'
        % (os.path.basename(path), outdir, sr, ch, 'es' if ch != 1 else '',
           len(pcm) / 2 / ch / sr))


def repack_folder(folder, log):
    mpath = os.path.join(folder, MANIFEST_NAME)
    if not os.path.isfile(mpath):
        raise ValueError('la carpeta no tiene %s (no fue creada por esta herramienta)' % MANIFEST_NAME)
    manifest = json.load(open(mpath, encoding='utf-8'))
    base = os.path.splitext(manifest['source'])[0]
    header_template = bytes.fromhex(manifest['header_hex'])
    out_name = base + '_mod.bin'
    out_path = os.path.join(folder, out_name)

    wav_path = os.path.join(folder, base + '.wav')
    adx_path = os.path.join(folder, base + '.adx')

    if os.path.isfile(wav_path):
        frames, sr, ch = read_wav(wav_path)
        if ch != manifest['channels']:
            log('Aviso: el WAV tiene %d canal(es) y el original tenia %d. Se usa el del WAV.'
                % (ch, manifest['channels']))
        out_data = build_adx_file(header_template, frames, sr, ch,
                                   manifest['cutoff'], manifest['version'], manifest['frame_size'])
        open(out_path, 'wb').write(out_data)
        log('ADX recodificado desde WAV -> %s (%d Hz, %d canal%s)'
            % (out_path, sr, ch, 'es' if ch != 1 else ''))
    elif os.path.isfile(adx_path):
        data = open(adx_path, 'rb').read()
        parse_adx_header(data)  # valida que siga siendo un ADX real
        open(out_path, 'wb').write(data)
        log('ADX crudo reempacado (sin cambios de audio) -> %s' % out_path)
    else:
        raise ValueError('no se encontro ni "%s.wav" ni "%s.adx" dentro de la carpeta'
                          % (base, base))
    return out_path


def is_adx_file(path):
    try:
        with open(path, 'rb') as f:
            head = f.read(0x20)
        parse_adx_header(head + b'\x00' * 32)
        return True
    except Exception:
        return False


# ==================================================================== GUI ==
# (las clases de abajo solo se definen si tkinter esta disponible, para que
# el codec y el modo consola funcionen igual en un entorno sin GUI)

if TK_OK:
    class ChoiceDialog(tk.Toplevel):
        def __init__(self, master, title, message, options):
            super().__init__(master)
            self.title(title)
            self.resizable(False, False)
            self.result = None
            self.transient(master)
            self.grab_set()
            tk.Label(self, text=message, wraplength=380, justify='left',
                     padx=16, pady=12).pack()
            btns = tk.Frame(self)
            btns.pack(pady=(0, 14))
            for label, value in options:
                tk.Button(btns, text=label, width=28,
                          command=lambda v=value: self._choose(v)).pack(pady=3, padx=16)
            tk.Button(self, text='Cancelar', command=self._cancel).pack(pady=(0, 10))
            self.protocol('WM_DELETE_WINDOW', self._cancel)
            self.update_idletasks()
            x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
            y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
            self.geometry('+%d+%d' % (max(x, 0), max(y, 0)))

        def _choose(self, v):
            self.result = v
            self.destroy()

        def _cancel(self):
            self.result = None
            self.destroy()


    class App:
        def __init__(self, root):
            self.root = root
            root.title('MVC2 ADX Tool - extraccion / reempaquetado de streams ADX')
            root.geometry('640x420')

            tk.Label(root, text='Arrastra aqui un adx_*.bin (para extraer)\n'
                                 'o la carpeta ya extraida (para reempaquetar)',
                     font=('Segoe UI', 12), pady=20, justify='center').pack()

            btnf = tk.Frame(root)
            btnf.pack(pady=6)
            tk.Button(btnf, text='Abrir .bin...', width=18, command=self.cmd_open_file).pack(side='left', padx=6)
            tk.Button(btnf, text='Abrir carpeta...', width=18, command=self.cmd_open_folder).pack(side='left', padx=6)

            self.logbox = scrolledtext.ScrolledText(root, state='disabled', wrap='word',
                                                     font=('Consolas', 9), height=16)
            self.logbox.pack(fill='both', expand=True, padx=10, pady=10)

            if HAVE_DND:
                root.drop_target_register(DND_FILES)
                root.dnd_bind('<<Drop>>', self.on_drop)
                self.log('Arrastra y soltar activado.')
            else:
                self.log('Arrastra y soltar no disponible (falta tkinterdnd2): usa los botones de arriba.')
                self.log('Instalalo con:  pip install tkinterdnd2')

        def log(self, msg):
            self.logbox.configure(state='normal')
            self.logbox.insert('end', msg + '\n')
            self.logbox.see('end')
            self.logbox.configure(state='disabled')

        def on_drop(self, event):
            paths = self.root.tk.splitlist(event.data)
            for p in paths:
                self.handle_path(p)

        def cmd_open_file(self):
            path = filedialog.askopenfilename(title='Elegir .bin ADX', filetypes=[('BIN', '*.bin'), ('Todos', '*.*')])
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
                    repack_folder(path, self.log)
                elif os.path.isfile(path):
                    self.log('--- Archivo: %s ---' % path)
                    if not is_adx_file(path):
                        self.log('No parece un stream ADX valido (firma 0x80 0x00 / tipo 3 no encontrada).')
                        messagebox.showerror('No es ADX',
                                              'Este archivo no parece un stream ADX (tipo 3) valido.')
                        return
                    outdir = os.path.splitext(path)[0] + '_adx_extraido'
                    if os.path.exists(outdir):
                        if not messagebox.askyesno('Ya existe',
                                                    'Ya existe "%s". Borrar y volver a extraer?' % outdir):
                            return
                        import shutil
                        shutil.rmtree(outdir)
                    dlg = ChoiceDialog(self.root, 'Como extraer',
                                        '"%s" es un stream ADX. Como quieres extraerlo?'
                                        % os.path.basename(path),
                                        [('Extraer crudo (.adx tal cual)', 'raw'),
                                         ('Convertir a WAV (decodificado)', 'wav')])
                    self.root.wait_window(dlg)
                    if dlg.result == 'raw':
                        extract_raw(path, outdir, self.log)
                    elif dlg.result == 'wav':
                        extract_wav(path, outdir, self.log)
                    else:
                        self.log('Cancelado.')
                else:
                    self.log('Ruta no reconocida: %s' % path)
            except Exception as e:
                self.log('ERROR: %s' % e)
                messagebox.showerror('Error', str(e))


def main_gui():
    root = TkinterDnD.Tk() if HAVE_DND else tk.Tk()
    App(root)
    root.mainloop()


def main_cli(argv):
    """Modo consola, por si se ejecuta sin entorno grafico:
       py MVC2_ADX_Tool.py extraer_crudo archivo.bin
       py MVC2_ADX_Tool.py extraer_wav archivo.bin
       py MVC2_ADX_Tool.py reempacar carpeta
    """
    def log(msg):
        print(msg)
    if len(argv) < 3:
        print(__doc__)
        print('\nUso:')
        print('  py MVC2_ADX_Tool.py extraer_crudo archivo.bin')
        print('  py MVC2_ADX_Tool.py extraer_wav archivo.bin')
        print('  py MVC2_ADX_Tool.py reempacar carpeta')
        return
    cmd, target = argv[1], argv[2]
    if cmd == 'extraer_crudo':
        outdir = os.path.splitext(target)[0] + '_adx_extraido'
        extract_raw(target, outdir, log)
    elif cmd == 'extraer_wav':
        outdir = os.path.splitext(target)[0] + '_adx_extraido'
        extract_wav(target, outdir, log)
    elif cmd == 'reempacar':
        repack_folder(target, log)
    else:
        print('Comando no reconocido: %s' % cmd)


if __name__ == '__main__':
    if len(sys.argv) > 1 and TK_OK is False:
        main_cli(sys.argv)
    elif len(sys.argv) > 1 and sys.argv[1] in ('extraer_crudo', 'extraer_wav', 'reempacar'):
        main_cli(sys.argv)
    elif TK_OK:
        main_gui()
    else:
        print('tkinter no esta disponible en este entorno. Usa el modo consola:')
        main_cli(['', '', ''])
