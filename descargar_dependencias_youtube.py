"""
descargar_dependencias_youtube.py
----------------------------------
Descarga (con "pip download") todas las dependencias del YouTube IA
Downloader a una carpeta local ("dependencias_offline", al lado de este
archivo), incluyendo sus dependencias transitivas. Una vez descargadas,
youtube_Mp3.pyw las instala DESDE esa carpeta la próxima vez
que haga falta, sin tocar internet para nada -- ver _intentar_instalar
en ese archivo.

Uso: correr este programa una vez (con internet), del lado de la
computadora que sí tiene internet, y después copiar la carpeta
"dependencias_offline" completa a la máquina donde vaya a correr el
downloader (si es la misma máquina, no hace falta copiar nada).

No hace falta tener instalado nada más que Python para correr este
programa -- solo usa la librería estándar (tkinter, subprocess) más pip,
que ya viene con Python.

NOTA: esto cubre las dependencias que se instalan con pip. FFmpeg (la
carpeta "ffmpeg" del proyecto) y Firefox NO son paquetes de pip y no se
descargan acá -- se manejan aparte, como ya lo hacía el programa.
"""

import os
import sys
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

CREATIONFLAGS = 0x08000000 if os.name == "nt" else 0

CARPETA_BASE = Path(__file__).resolve().parent
CARPETA_CONFIG = CARPETA_BASE / "config"


def _cargar_lista_dependencias():
    # La lista vive en config/dependencias_youtube.py (a propósito adentro
    # de "config", para que no quede un .py suelto que se pueda llegar a
    # ejecutar por error) -- mismo mecanismo de carga por ruta directa que
    # usa youtube_Mp3.pyw, para leer siempre la misma lista.
    ruta = CARPETA_CONFIG / "dependencias_youtube.py"
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("dependencias_youtube", ruta)
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)
        return modulo.DEPENDENCIAS
    except Exception:
        return None


# Mismo respaldo que en youtube_Mp3.pyw, por si
# config/dependencias_youtube.py no está.
DEPENDENCIAS = _cargar_lista_dependencias() or [
    ("yt-dlp", "yt_dlp"),
    ("curl_cffi", "curl_cffi"),
    ("librosa", "librosa"),
    ("numpy", "numpy"),
    ("soundfile", "soundfile"),
    ("scipy", "scipy"),
    ("rapidfuzz", "rapidfuzz"),
    ("ytmusicapi", "ytmusicapi"),
]

ANCHO_VENTANA = 420


class VentanaDescarga(tk.Tk):
    def __init__(self):
        super().__init__()
        self.carpeta_destino = CARPETA_BASE / "dependencias_offline"
        self.descargando = False

        self.title("⬇️ Descargador de dependencias (YouTube Mp3)")
        self.resizable(False, False)
        self.configure(bg="#f8f9fa")
        self.attributes("-topmost", True)

        self._pending_after_ids = []
        self.labels_estado = {}
        self._construir_interfaz()
        self._centrar_ventana()
        self.protocol("WM_DELETE_WINDOW", self._al_cerrar)

    # ---- mismos helpers de thread-safety que VentanaVerificacion ----
    def _programar(self, ms, fn):
        id_after = self.after(ms, fn)
        self._pending_after_ids.append(id_after)
        return id_after

    def _cancelar_pendientes(self):
        for id_after in self._pending_after_ids:
            try:
                self.after_cancel(id_after)
            except Exception:
                pass
        self._pending_after_ids = []

    def _config_seguro(self, widget, **kwargs):
        if widget.winfo_exists():
            widget.config(**kwargs)

    def _al_cerrar(self):
        self._cancelar_pendientes()
        sys.exit(0)

    # ---------------------------- interfaz ----------------------------
    def _construir_interfaz(self):
        contenedor = tk.Frame(self, bg="#f8f9fa", padx=20, pady=15)
        contenedor.pack(fill="both", expand=True)

        tk.Label(contenedor, text="⬇️ Descargar dependencias para uso sin internet",
                 font=("Segoe UI", 13, "bold"), bg="#f8f9fa", fg="#2c3e50",
                 wraplength=380, justify="left").pack(anchor="w")
        tk.Label(contenedor,
                 text="Deja los instaladores en una carpeta local para que el "
                      "downloader no tenga que bajarlos de internet cada vez.",
                 font=("Segoe UI", 9), bg="#f8f9fa", fg="#7f8c8d",
                 wraplength=380, justify="left").pack(anchor="w", pady=(0, 12))

        for nombre, _import_name in DEPENDENCIAS:
            fila = tk.Frame(contenedor, bg="#f8f9fa")
            fila.pack(fill="x", pady=2)
            tk.Label(fila, text=nombre, font=("Segoe UI", 9, "bold"),
                     bg="#f8f9fa", fg="#2c3e50", width=14, anchor="w").pack(side="left")
            lbl_estado = tk.Label(fila, text="⏳ Pendiente", font=("Segoe UI", 9),
                                  bg="#f8f9fa", fg="#7f8c8d", anchor="w")
            lbl_estado.pack(side="left", fill="x", expand=True)
            self.labels_estado[nombre] = lbl_estado

        self.progreso = ttk.Progressbar(contenedor, mode="determinate", length=380)
        self.progreso.pack(fill="x", pady=(10, 6))

        self.lbl_estado_general = tk.Label(
            contenedor, text="Listo para descargar.", font=("Segoe UI", 9),
            bg="#f8f9fa", fg="#2c3e50", anchor="w", justify="left", wraplength=380)
        self.lbl_estado_general.pack(fill="x", pady=(0, 10))

        marco_carpeta = tk.LabelFrame(
            contenedor, text="📂 Carpeta de destino", font=("Segoe UI", 9, "bold"),
            bg="#f8f9fa", fg="#2c3e50", padx=10, pady=8)
        marco_carpeta.pack(fill="x", pady=(0, 10))
        self.lbl_carpeta = tk.Label(
            marco_carpeta, text=str(self.carpeta_destino), font=("Segoe UI", 8),
            bg="#f8f9fa", fg="#34495e", anchor="w", justify="left", wraplength=380)
        self.lbl_carpeta.pack(fill="x")
        tk.Button(marco_carpeta, text="Cambiar carpeta...", font=("Segoe UI", 8),
                  command=self._elegir_carpeta).pack(anchor="w", pady=(4, 0))

        frame_botones = tk.Frame(contenedor, bg="#f8f9fa")
        frame_botones.pack(fill="x")
        self.btn_descargar = tk.Button(
            frame_botones, text="⬇️ Descargar todo", font=("Segoe UI", 9, "bold"),
            bg="#2ecc71", fg="white", relief="flat", padx=10, pady=6,
            command=self._al_descargar)
        self.btn_descargar.pack(side="left", padx=(0, 8))
        self.btn_abrir_carpeta = tk.Button(
            frame_botones, text="📂 Abrir carpeta", font=("Segoe UI", 9),
            relief="flat", padx=10, pady=6, command=self._abrir_carpeta)
        self.btn_abrir_carpeta.pack(side="left")

        self.update_idletasks()
        alto = self.winfo_reqheight() + 16
        self.geometry(f"{ANCHO_VENTANA}x{alto}")

    def _centrar_ventana(self):
        self.update_idletasks()
        ancho = self.winfo_width()
        alto = self.winfo_height()
        x = (self.winfo_screenwidth() - ancho) // 2
        y = (self.winfo_screenheight() - alto) // 2
        self.geometry(f"{ancho}x{alto}+{x}+{y}")

    def _elegir_carpeta(self):
        if self.descargando:
            return
        elegida = filedialog.askdirectory(
            title="Elegí dónde guardar los instaladores",
            initialdir=str(self.carpeta_destino.parent))
        if elegida:
            self.carpeta_destino = Path(elegida)
            self.lbl_carpeta.config(text=str(self.carpeta_destino))

    def _abrir_carpeta(self):
        try:
            self.carpeta_destino.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(str(self.carpeta_destino))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(self.carpeta_destino)])
            else:
                subprocess.Popen(["xdg-open", str(self.carpeta_destino)])
        except Exception as e:
            messagebox.showerror("No se pudo abrir la carpeta", str(e))

    # ---------------------------- descarga ----------------------------
    def _al_descargar(self):
        if self.descargando:
            return
        self.descargando = True
        self.btn_descargar.config(state="disabled", bg="#95a5a6")
        for nombre, _ in DEPENDENCIAS:
            self._config_seguro(self.labels_estado[nombre], text="⏳ Pendiente", fg="#7f8c8d")
        self.progreso["value"] = 0
        self.lbl_estado_general.config(text="🔍 Preparando carpeta...")
        threading.Thread(target=self._descargar_todo, daemon=True).start()

    def _descargar_todo(self):
        try:
            self.carpeta_destino.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._programar(0, lambda: self._config_seguro(
                self.lbl_estado_general,
                text=f"❌ No se pudo crear la carpeta de destino: {e}", fg="#e74c3c"))
            self._programar(0, self._terminar)
            return

        total = len(DEPENDENCIAS)
        hubo_error = False
        for i, (nombre, _import_name) in enumerate(DEPENDENCIAS):
            self._programar(0, lambda n=nombre: self._config_seguro(
                self.labels_estado[n], text="⏳ Descargando...", fg="#e67e22"))
            self._programar(0, lambda n=nombre: self._config_seguro(
                self.lbl_estado_general, text=f"⬇️ Descargando {n}..."))
            try:
                # --no-deps NO se usa a propósito: "pip download" tiene que
                # bajar también las dependencias transitivas de cada
                # paquete (ej. las que necesita librosa por debajo), si no
                # la instalación offline después se queda a mitad de
                # camino pidiendo algo que no está en la carpeta.
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "download", "--quiet",
                     "-d", str(self.carpeta_destino), nombre],
                    creationflags=CREATIONFLAGS)
                self._programar(0, lambda n=nombre: self._config_seguro(
                    self.labels_estado[n], text="✅ Descargado", fg="#27ae60"))
            except subprocess.CalledProcessError as e:
                hubo_error = True
                self._programar(0, lambda n=nombre: self._config_seguro(
                    self.labels_estado[n], text="❌ Error (revisar conexión)", fg="#e74c3c"))
                print(f"[descargar_dependencias] Error descargando '{nombre}': {e}")
            self._programar(0, lambda v=(i + 1) / total * 100: self._config_seguro(self.progreso, value=v))

        if hubo_error:
            texto_final = ("⚠️ Terminó con algún error -- revisá que haya internet y "
                            "volvé a apretar 'Descargar todo' (lo ya descargado no se vuelve a bajar).")
        else:
            texto_final = ("✅ Listo. La carpeta ya tiene todo lo necesario -- el downloader "
                            "la va a usar solo la próxima vez que le falte instalar algo.")
        self._programar(0, lambda: self._config_seguro(
            self.lbl_estado_general, text=texto_final, fg="#e74c3c" if hubo_error else "#27ae60"))
        self._programar(0, self._terminar)

    def _terminar(self):
        self.descargando = False
        self._config_seguro(self.btn_descargar, state="normal", bg="#2ecc71")


if __name__ == "__main__":
    app = VentanaDescarga()
    app.mainloop()
