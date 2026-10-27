"""
config/dependencias_youtube.py
-------------------------------
Lista compartida de dependencias del YouTube Mp3.

Vive adentro de la carpeta "config" a propósito, para que no quede un
.py suelto al lado de youtube_Mp3.pyw / descargar_dependencias_youtube.py
que se pueda llegar a ejecutar por error con un doble click.

La usan dos programas distintos, que la cargan por ruta directa (no con
un "import" normal, porque este archivo no está en la misma carpeta que
ellos) -- ver _cargar_lista_dependencias() en cada uno:
  - youtube_Mp3.pyw, al arrancar (VentanaVerificacion), para
    verificar/instalar lo que falte (primero intenta desde la carpeta
    "dependencias_offline", después con internet -- ver
    _intentar_instalar en ese archivo).
  - descargar_dependencias_youtube.py, para precargar todas estas
    dependencias en la carpeta "dependencias_offline" y así no depender
    de internet la próxima vez que haga falta instalar algo.

Mantener esta lista en UN solo lugar evita que el downloader y el
descargador se desincronicen entre sí. Si el día de mañana el
downloader necesita una librería nueva, alcanza con agregarla acá --
ambos programas la ven automáticamente.

Cada entrada es (nombre_para_pip, nombre_para_import) -- casi siempre
son iguales, pero no siempre (por eso van separados).

FFmpeg y Firefox NO van en esta lista: no se instalan con pip (FFmpeg
viene como binario en la carpeta "ffmpeg" del proyecto, y Firefox se
instala aparte con instalar_firefox_silencioso) -- ver la fila "extra"
correspondiente en VentanaVerificacion, dentro de youtube_Mp3.pyw.
"""

DEPENDENCIAS = [
    ("yt-dlp", "yt_dlp"),
    # Resuelve los desafios de firma de YouTube (junto con deno). Sin esto
    # solo se ven "imagenes" y las descargas fallan.
    ("yt-dlp-ejs", "yt_dlp_ejs"),
    # curl_cffi firma el handshake TLS como un navegador real
    # (impersonate). Si no está, el programa sigue andando igual:
    # simplemente no usa la suplantación.
    ("curl_cffi", "curl_cffi"),
    ("librosa", "librosa"),
    ("numpy", "numpy"),
    ("soundfile", "soundfile"),
    ("scipy", "scipy"),
    ("rapidfuzz", "rapidfuzz"),
    ("ytmusicapi", "ytmusicapi"),
]

if __name__ == "__main__":
    print("Este archivo es solo una lista de datos que usan youtube_Mp3.pyw "
          "y descargar_dependencias_youtube.py -- no hace falta ejecutarlo directamente.")
