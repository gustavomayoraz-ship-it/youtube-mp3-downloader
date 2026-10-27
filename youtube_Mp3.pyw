"""
# version verificada
YouTube Mp3 - con auto-aprendizaje real.
Todo lo que aprende (qué cliente de YouTube funciona mejor, qué tan
rápido puede pedir sin que lo bloqueen, qué videos son irrecuperables,
qué navegador de cookies funciona) se guarda en memoria.json y se usa
la próxima vez que se abre el programa - mejora solo con el uso, sin
que el usuario tenga que tocar nada.
Estructura de ventana de verificación de dependencias e instalación de
Firefox: sigue la referencia de VentanaVerificacion de las preferencias de Gustavo.
"""
import subprocess
import sys
import os
import re
import json
import time
import random
import threading
import concurrent.futures
import tempfile
import urllib.request
import webbrowser
import unicodedata
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from datetime import datetime, timedelta
import hashlib
import queue
try:
    import ctypes
    myappid = 'taller.jotas.downloader.ia.v1'
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
except Exception:
    pass
# =====================================================================
# RUTAS Y PERSISTENCIA
#   - config/config_app.json  -> ajustes de instalación (firefox, cantidad, etc.)
#   - memoria.json             -> todo lo que el programa aprende solo
#   - descargas_hechas.json    -> videos ya bajados, para no volver a pedirlos
# =====================================================================
def ruta_base():
    return os.path.dirname(os.path.abspath(__file__))
def ruta_config(archivo):
    config_dir = os.path.join(ruta_base(), 'config')
    if not os.path.exists(config_dir):
        os.makedirs(config_dir)
    return os.path.join(config_dir, archivo)
def cargar_config_app():
    defecto = {
        "usar_firefox_exclusivo": True,
        "cantidad": 8,
        "duracion_max": 8,
        "simultaneas": 1,
        "descargar_toda_lista": True,
        "analizar_bpm": True,
        "normalizar_volumen": True,
        "buscar_mejores_temas": False,
        "busqueda_exacta": True,
        "solo_estudio": True,
    }
    archivo = ruta_config("config_app.json")
    if os.path.exists(archivo):
        try:
            with open(archivo, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return defecto
        # La configuración de proxy se quitó del programa (ya no se usa):
        # si el archivo guardado todavía la trae, se descarta acá y no se
        # vuelve a escribir cuando el programa guarde sus ajustes.
        for clave_vieja in ("proxy", "proxies", "proxy_activo", "proxy_inicial"):
            data.pop(clave_vieja, None)
        for k, v in defecto.items():
            data.setdefault(k, v)
        return data
    return defecto
def guardar_config_app(data):
    archivo = ruta_config("config_app.json")
    try:
        escribir_json_atomico(archivo, data, indent=4)
        return True
    except Exception:
        return False
def ruta_memoria():
    return os.path.join(ruta_base(), "memoria.json")
def memoria_por_defecto():
    return {
        "client_stats": {},
        "cookies_navegador_efectivo": None,
        "delay_base_aprendido": 12.0,
        "nivel_base_aprendido": 0,
        "buena_racha": 0,
        "videos_irrecuperables": [],
        "total_descargas_exitosas": 0,
        "total_descargas_fallidas": 0,
        "ultima_sesion": None,
        "historial_busquedas": [],
        # Todo lo que aprende el predictor de riesgo (ver PredictorRiesgo):
        # el ritmo que YouTube aguantó, las horas del día en que bloquea,
        # la antigüedad de las cookies. Es lo que le permite ANTICIPARSE
        # al bloqueo en vez de reaccionar cuando ya perdió el intento.
        "predictor": predictor_por_defecto(),
        "clientes_podados": [],
    }
def cargar_memoria():
    archivo = ruta_memoria()
    defecto = memoria_por_defecto()
    if os.path.exists(archivo):
        try:
            with open(archivo, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in defecto.items():
                    data.setdefault(k, v)
                data = _migrar_memoria(data)
                return data
        except Exception:
            return defecto
    return defecto
_lock_memoria = threading.Lock()
def guardar_memoria(memoria):
    with _lock_memoria:
        try:
            escribir_json_atomico(ruta_memoria(), memoria, indent=4)
            return True
        except Exception:
            return False
def _migrar_memoria(data):
    """Traduce una memoria.json de una versión anterior al formato nuevo
    sin perder nada de lo aprendido:
    - videos_irrecuperables pasó de lista de ids a fichas con fecha,
      confirmaciones y si el error era definitivo o dudoso, así los vetos
      pueden caducar en vez de ser para siempre.
    - client_stats ganó "ewma" (éxito ponderado por recencia): si no
      estaba, se inicializa con el promedio histórico para no perder lo
      aprendido.
    - "predictor" es nuevo: arranca vacío y se llena solo con el uso.
    """
    viejos = data.get("videos_irrecuperables") or []
    if viejos and isinstance(viejos[0], str):
        ahora = datetime.now().isoformat()
        data["videos_irrecuperables"] = [
            {"id": v, "fecha": ahora, "motivo": "formato viejo",
             "confirmaciones": 1, "definitivo": True}
            for v in viejos
        ]
    for s in (data.get("client_stats") or {}).values():
        if isinstance(s, dict) and "ewma" not in s:
            total = s.get("exitos", 0) + s.get("fallos", 0)
            s["ewma"] = (s.get("exitos", 0) / total) if total else 0.5
            s["ultima_actualizacion"] = None
    predictor = data.get("predictor")
    if not isinstance(predictor, dict):
        data["predictor"] = predictor_por_defecto()
    else:
        for clave, valor in predictor_por_defecto().items():
            predictor.setdefault(clave, valor)
    return data
# =====================================================================
# FUNCIONES PARA DETECTAR DUPLICADOS
# =====================================================================
def calcular_hash_archivo(ruta, bloque=8192):
    """Calcula el hash SHA-1 de un archivo"""
    sha1 = hashlib.sha1()
    try:
        with open(ruta, "rb") as f:
            while True:
                datos = f.read(bloque)
                if not datos:
                    break
                sha1.update(datos)
        return sha1.hexdigest()
    except Exception:
        return None
def limpiar_nombre_para_comparar(nombre):
    """
    Limpia el nombre de archivo para comparar duplicados por nombre.
    Elimina:
    - Números entre paréntesis al final: (1), (2), etc.
    - Números entre corchetes al final: [1], [2], etc.
    - Espacios extras
    """
    nombre_sin_ext, ext = os.path.splitext(nombre)

    # Eliminar (1), (2), (3) al final
    nombre_sin_ext = re.sub(r'\s*\((\d+)\)\s*$', '', nombre_sin_ext)
    # Eliminar [1], [2], [3] al final
    nombre_sin_ext = re.sub(r'\s*\[(\d+)\]\s*$', '', nombre_sin_ext)
    # Eliminar - copia, - copy, etc.
    nombre_sin_ext = re.sub(r'\s*[-–]\s*(copia|copy|duplicado|duplicate)\s*$', '', nombre_sin_ext, flags=re.IGNORECASE)
    # Eliminar espacios extras
    nombre_sin_ext = nombre_sin_ext.strip()

    return nombre_sin_ext + ext
HILOS_HASH_DUPLICADOS = 6  # cuántos archivos se hashean a la vez al buscar duplicados por contenido
def detectar_duplicados_por_contenido(carpeta):
    """
    Detecta archivos duplicados por contenido (hash).
    Retorna un diccionario donde la clave es el hash y el valor es una
    lista de rutas de archivos que comparten ese hash.
    El hasheo (leer el archivo entero del disco) es lo que tarda acá, así
    que se reparte entre varios hilos - lo típico es estar esperando al
    disco, no a la CPU, así que varios archivos a la vez terminan mucho
    antes que uno por uno.
    """
    extensiones_audio_video = ('.mp3', '.wav', '.flac', '.m4a', '.ogg', '.mp4', '.avi', '.mkv', '.mov', '.wmv', '.webm')
    duplicados = {}
    archivos_procesados = 0
    rutas = []
    for raiz, _dirs, nombres in os.walk(carpeta):
        for nombre in nombres:
            if nombre.lower().endswith(extensiones_audio_video):
                rutas.append(os.path.join(raiz, nombre))
    with concurrent.futures.ThreadPoolExecutor(max_workers=HILOS_HASH_DUPLICADOS) as executor:
        for ruta_completa, hash_archivo in zip(rutas, executor.map(calcular_hash_archivo, rutas)):
            if hash_archivo:
                if hash_archivo not in duplicados:
                    duplicados[hash_archivo] = []
                duplicados[hash_archivo].append(ruta_completa)
                archivos_procesados += 1

    # Filtrar solo los que tienen duplicados (más de 1 archivo con mismo hash)
    duplicados_reales = {h: rutas for h, rutas in duplicados.items() if len(rutas) > 1}

    return duplicados_reales, archivos_procesados
def detectar_duplicados_por_nombre(carpeta):
    """
    Detecta archivos duplicados por nombre (ignorando (1), (2), etc.)
    Retorna un diccionario donde la clave es el nombre limpio y el valor
    es una lista de rutas de archivos que comparten ese nombre base.
    """
    extensiones_audio_video = ('.mp3', '.wav', '.flac', '.m4a', '.ogg', '.mp4', '.avi', '.mkv', '.mov', '.wmv', '.webm')
    duplicados = {}
    archivos_procesados = 0

    for raiz, _dirs, nombres in os.walk(carpeta):
        for nombre in nombres:
            if nombre.lower().endswith(extensiones_audio_video):
                ruta_completa = os.path.join(raiz, nombre)
                nombre_limpio = limpiar_nombre_para_comparar(nombre)

                if nombre_limpio not in duplicados:
                    duplicados[nombre_limpio] = []
                duplicados[nombre_limpio].append(ruta_completa)
                archivos_procesados += 1

    # Filtrar solo los que tienen duplicados (más de 1 archivo con mismo nombre)
    duplicados_reales = {n: rutas for n, rutas in duplicados.items() if len(rutas) > 1}

    return duplicados_reales, archivos_procesados
def normalizar_titulo_para_similitud(nombre):
    """
    Normaliza un nombre de archivo para comparar por parecido de título:
    saca el tag [BPM], pasa a minúsculas, saca guiones/puntos/signos y
    espacios de más. No importa el orden de las palabras (eso lo maneja
    la comparación con token_sort_ratio).
    """
    nombre_sin_ext, _ext = os.path.splitext(nombre)
    texto = re.sub(r"^\s*\[\d+\s*BPM\]\s*", "", nombre_sin_ext, flags=re.IGNORECASE)
    texto = texto.lower()
    texto = re.sub(r"[^a-z0-9áéíóúñü\s]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto
def detectar_duplicados_por_similitud(carpeta, umbral=88):
    """
    Agrupa archivos cuyo título se parece mucho aunque esté escrito
    distinto (guiones, orden de palabras, puntuación, etc). Por ejemplo:
    "malagata pensando en ti", "malagata - pensando en ti" y
    "pensando en ti - malagata" caen en el mismo grupo.
    Retorna un diccionario {clave_de_grupo: [rutas...]} con solo los
    grupos que tienen más de un archivo.
    """
    from rapidfuzz import fuzz
    extensiones_audio_video = ('.mp3', '.wav', '.flac', '.m4a', '.ogg', '.mp4', '.avi', '.mkv', '.mov', '.wmv', '.webm')
    archivos = []
    for raiz, _dirs, nombres in os.walk(carpeta):
        for nombre in nombres:
            if nombre.lower().endswith(extensiones_audio_video):
                ruta_completa = os.path.join(raiz, nombre)
                archivos.append((ruta_completa, normalizar_titulo_para_similitud(nombre)))
    usados = set()
    grupos = {}
    grupo_id = 0
    for i in range(len(archivos)):
        if i in usados:
            continue
        ruta_i, texto_i = archivos[i]
        if not texto_i:
            continue
        grupo_actual = [ruta_i]
        for j in range(i + 1, len(archivos)):
            if j in usados:
                continue
            ruta_j, texto_j = archivos[j]
            if not texto_j:
                continue
            if fuzz.token_sort_ratio(texto_i, texto_j) >= umbral:
                grupo_actual.append(ruta_j)
                usados.add(j)
        if len(grupo_actual) > 1:
            usados.add(i)
            grupo_id += 1
            grupos[f"similar_{grupo_id}"] = grupo_actual
    return grupos, len(archivos)
_PATRON_SUFIJO_COPIA = re.compile(r"\s*[\(\[]\d+[\)\]]\s*$")
def elegir_mejor_del_grupo(rutas):
    """
    Entre varios archivos que se consideran duplicados/iguales, elige cuál
    conviene mantener, en este orden de prioridad:
    1) el que NO termina en un contador tipo "(1)", "(2)" - eso casi
       siempre indica que es una copia, nunca el original.
       Si TODOS los archivos del grupo tienen ese sufijo (por ejemplo
       porque el original ya se borró en otro momento y solo quedan
       copias), este criterio empata entre todos y se sigue al próximo,
       así que igual se elige uno - nunca se descarta el grupo entero.
    2) entre los que quedan, el que ya tiene el tag [BPM] puesto.
    3) entre los que quedan, el de mayor tamaño (mejor calidad).
    """
    def puntaje(ruta):
        nombre = os.path.basename(ruta)
        nombre_sin_ext, _ext = os.path.splitext(nombre)
        sin_sufijo_copia = not bool(_PATRON_SUFIJO_COPIA.search(nombre_sin_ext))
        tiene_bpm = bool(re.match(r"^\s*\[\d+\s*BPM\]", nombre, re.IGNORECASE))
        try:
            tamano = os.path.getsize(ruta)
        except OSError:
            tamano = 0
        return (sin_sufijo_copia, tiene_bpm, tamano)
    return max(rutas, key=puntaje)
def detectar_todos_duplicados(carpeta):
    """
    Detecta duplicados por contenido, por nombre y por similitud de título.
    Retorna un diccionario con las tres categorías.
    """
    duplicados_contenido, total_contenido = detectar_duplicados_por_contenido(carpeta)
    duplicados_nombre, total_nombre = detectar_duplicados_por_nombre(carpeta)
    duplicados_similitud, total_similitud = detectar_duplicados_por_similitud(carpeta)
    return {
        'contenido': duplicados_contenido,
        'nombre': duplicados_nombre,
        'similitud': duplicados_similitud,
        'total_contenido': total_contenido,
        'total_nombre': total_nombre,
        'total_similitud': total_similitud,
    }
# =====================================================================
# FIREFOX: única forma automática y confiable de usar cookies (Chrome,
# Edge, Brave y Opera cifran sus cookies con "app-bound encryption"
# desde 2024 y ningún programa externo puede leerlas ya; Firefox no
# encripta las suyas, así que es el único navegador viable para esto).
# =====================================================================
def ruta_firefox():
    candidatos = []
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            candidatos.append(os.path.join(base, "Mozilla Firefox", "firefox.exe"))
    for c in candidatos:
        if os.path.exists(c):
            return c
    return None
def _parsear_vistas(texto):
    """'12M views' / '345K views' / '1.2B' -> cantidad entera; 0 si no se puede."""
    if not texto:
        return 0
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*([KkMmBb])?", str(texto))
    if not m:
        return 0
    try:
        n = float(m.group(1).replace(",", "."))
    except ValueError:
        return 0
    return int(n * {"k": 1e3, "m": 1e6, "b": 1e9}.get((m.group(2) or "").lower(), 1))
def ruta_chrome():
    candidatos = []
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            candidatos.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
    for c in candidatos:
        if os.path.exists(c):
            return c
    return None
def abrir_en_navegador(url):
    """Abre la URL con Firefox si está instalado y, si no, con Chrome. Si
    no hay ninguno de los dos, cae al navegador predeterminado del sistema.
    Devuelve el nombre del navegador usado."""
    flags = 0x08000000 if os.name == "nt" else 0
    ff = ruta_firefox()
    if ff:
        subprocess.Popen([ff, "-new-tab", url], creationflags=flags)
        return "Firefox"
    ch = ruta_chrome()
    if ch:
        subprocess.Popen([ch, url], creationflags=flags)
        return "Chrome"
    webbrowser.open(url)
    return "el navegador predeterminado"
def _version_firefox_instalado():
    """Lee la versión REAL del Firefox instalado, del application.ini que
    está al lado de firefox.exe. Se usa para que el user-agent que
    mandamos diga lo mismo que el navegador del que sacamos las cookies:
    mandar un user-agent de Chrome mientras se usan cookies de Firefox es
    una incoherencia que YouTube castiga con el "sign in to confirm"."""
    ruta = ruta_firefox()
    if not ruta:
        return None
    try:
        with open(os.path.join(os.path.dirname(ruta), "application.ini"),
                  "r", encoding="utf-8", errors="ignore") as f:
            encontrado = re.search(r"^\s*Version\s*=\s*([0-9]+(?:\.[0-9]+)*)",
                                   f.read(), re.MULTILINE)
        if encontrado:
            return encontrado.group(1).split(".")[0] + ".0"
    except Exception:
        pass
    return None
def instalar_firefox_silencioso(callback_estado=None):
    def _log(msg):
        if callback_estado:
            callback_estado(msg)
    try:
        _log("Descargando instalador de Firefox...")
        url = "https://download.mozilla.org/?product=firefox-latest-ssl&os=win64&lang=es-AR"
        destino = os.path.join(tempfile.gettempdir(), "firefox_installer.exe")
        urllib.request.urlretrieve(url, destino)
        _log("Instalando Firefox en silencio...")
        subprocess.run([destino, "/S"], timeout=180)
        time.sleep(3)
        ok = ruta_firefox() is not None
        try:
            os.remove(destino)
        except Exception:
            pass
        _log("Firefox instalado correctamente" if ok else "No se pudo confirmar la instalación")
        return ok
    except Exception as e:
        _log(f"Error instalando Firefox: {e}")
        return False
def navegador_esta_corriendo(navegador):
    procesos = {"firefox": "firefox.exe"}
    exe = procesos.get(navegador)
    if not exe or sys.platform != "win32":
        return False
    try:
        resultado = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {exe}"],
            capture_output=True, text=True, timeout=5,
            creationflags=0x08000000
        )
        return exe.lower() in resultado.stdout.lower()
    except Exception:
        return False
def obtener_ruta_ffmpeg():
    base_dir = ruta_base()
    posibles = [
        os.path.join(base_dir, 'ffmpeg', 'bin'),
        os.path.join(base_dir, 'ffmpeg'),
        base_dir,
    ]
    for p in posibles:
        if os.path.exists(os.path.join(p, 'ffmpeg.exe')):
            return p
    return None
_CARACTERES_INVALIDOS_CARPETA = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
def _cachedir_ytdlp():
    """Carpeta de caché de yt-dlp (una sola para todo el programa).
    Reusarla evita que yt-dlp vuelva a bajarse el reproductor JS en cada
    intento y en cada búsqueda: menos pedidos es menos motivo para que
    YouTube frene por ritmo. Devuelve None si no se puede crear, para no
    romper nada."""
    ruta = os.path.join(ruta_base(), "cache_yt_dlp")
    try:
        os.makedirs(ruta, exist_ok=True)
        return ruta
    except Exception:
        return None
def nombre_carpeta_valido(nombre):
    """
    Convierte un texto de búsqueda en un nombre de carpeta válido en
    Windows: saca los caracteres que Windows no permite en nombres de
    carpeta y recorta espacios/puntos del final (tampoco están permitidos
    ahí). Se usa para que "descargar toda la lista" guarde cada autor en
    su propia carpeta, llamada igual que lo que se buscó.
    """
    limpio = _CARACTERES_INVALIDOS_CARPETA.sub("", nombre or "").strip()
    limpio = limpio.rstrip(". ")
    return limpio
def extraer_video_id(url):
    patrones = [r'v=([a-zA-Z0-9_-]{11})', r'youtu\.be/([a-zA-Z0-9_-]{11})', r'embed/([a-zA-Z0-9_-]{11})']
    for p in patrones:
        m = re.search(p, url or "")
        if m:
            return m.group(1)
    return url
def _normalizar_para_comparar_nombre(texto):
    """
    Normaliza un nombre (de búsqueda, canal o título) para compararlo sin
    que importen mayúsculas/minúsculas, acentos ni signos de puntuación.
    Se usa para el filtro de "autor exacto": buscar "Tito Pachanga" no debe
    traer cualquier video que solo tenga la palabra "pachanga" suelta - el
    nombre completo que se escribió tiene que aparecer tal cual (ya
    normalizado) en el título o en el canal del resultado.
    """
    texto = texto or ""
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.lower()
    texto = re.sub(r"[^a-z0-9\s]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto
def _parsear_duracion_texto(texto):
    """Convierte una duración en formato 'M:SS' o 'H:MM:SS' (como la que
    devuelve YouTube Music) a segundos. Devuelve 0 si no se puede parsear."""
    if not texto:
        return 0
    try:
        partes = [int(p) for p in str(texto).strip().split(":")]
    except Exception:
        return 0
    segundos = 0
    for parte in partes:
        segundos = segundos * 60 + parte
    return segundos
# =====================================================================
# ANÁLISIS DE BPM Y RENOMBRADO (mismo criterio que el programa original
# de Gustavo: analiza el tema ya descargado y sobreescribe el nombre
# agregando el BPM detectado)
#
# Soporte multi-formato: librosa puede decodificar wav/flac/ogg de forma
# directa (vía soundfile), pero para mp3/m4a depende de "audioread", que
# a su vez necesita encontrar un ffmpeg en el PATH del sistema operativo.
# Este programa trae su propio ffmpeg embebido en la carpeta del programa
# (ver obtener_ruta_ffmpeg), que NO está necesariamente en el PATH de
# Windows - por eso el análisis de BPM podía fallar en mp3/m4a aunque
# funcionara en wav. Para evitar depender del PATH, siempre se convierte
# primero el archivo a un WAV temporal usando ese ffmpeg embebido (ffmpeg
# decodifica cualquier formato de audio común: mp3, wav, flac, m4a, ogg,
# aac, wma, opus, etc.) y RECIÉN AHÍ se analiza con librosa - así el
# resultado es el mismo sin importar el formato original del archivo.
# =====================================================================
PATRON_BPM_INICIO = re.compile(r"^\s*\[(\d+)\s*BPM\]", re.IGNORECASE)
PATRON_BPM_CUALQUIER_LADO = re.compile(r"\[(\d+)\s*BPM\]", re.IGNORECASE)
def _flags_win():
    return 0x08000000 if sys.platform == "win32" else 0
def _ruta_ffmpeg_exe():
    ffmpeg_dir = obtener_ruta_ffmpeg()
    if ffmpeg_dir:
        candidato = os.path.join(ffmpeg_dir, "ffmpeg.exe")
        if os.path.exists(candidato):
            return candidato
    return "ffmpeg"
def _ruta_ffprobe():
    ffmpeg_dir = obtener_ruta_ffmpeg()
    if ffmpeg_dir:
        candidato = os.path.join(ffmpeg_dir, "ffprobe.exe")
        if os.path.exists(candidato):
            return candidato
    return "ffprobe"
def _convertir_a_wav_temporal(ruta_audio, duracion_maxima=None):
    """
    Convierte cualquier formato de audio soportado por ffmpeg (mp3, wav,
    flac, m4a, ogg, aac, wma, opus...) a un WAV temporal mono de 22050 Hz,
    usando el ffmpeg embebido del programa (no depende del PATH del
    sistema). Devuelve la ruta del WAV temporal - quien lo llama es
    responsable de borrarlo después con os.remove().
    Si se pasa duracion_maxima (segundos), solo se convierten los primeros
    segundos del archivo, para no tener que decodificar el tema entero
    solo para medir el BPM.
    """
    ffmpeg_exe = _ruta_ffmpeg_exe()
    fd, temporal = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    cmd = [ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error"]
    if duracion_maxima:
        cmd += ["-t", str(duracion_maxima)]
    cmd += ["-i", ruta_audio, "-ac", "1", "-ar", "22050", temporal]
    resultado = subprocess.run(cmd, capture_output=True, text=True, timeout=180, creationflags=_flags_win())
    if resultado.returncode != 0 or not os.path.exists(temporal) or os.path.getsize(temporal) == 0:
        try:
            os.remove(temporal)
        except Exception:
            pass
        error_texto = (resultado.stderr or "").strip().splitlines()
        razon = error_texto[-1] if error_texto else "ffmpeg no pudo decodificar el archivo"
        raise RuntimeError(f"No se pudo convertir {os.path.basename(ruta_audio)} para analizarlo: {razon}")
    return temporal
def extraer_bpm(ruta_archivo):
    """
    Analiza el BPM de un archivo de audio, sea cual sea su formato
    (mp3, wav, flac, m4a, ogg, etc.) - convierte primero a WAV con ffmpeg
    (ver _convertir_a_wav_temporal) y recién ahí corre librosa, así el
    resultado no depende de qué backend de decodificación tenga instalado
    librosa/audioread en esta PC.
    """
    import librosa
    wav_temporal = _convertir_a_wav_temporal(ruta_archivo, duracion_maxima=90)
    try:
        y, sr = librosa.load(wav_temporal, sr=None, mono=True, duration=90)
    finally:
        try:
            os.remove(wav_temporal)
        except Exception:
            pass
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    try:
        return int(round(float(tempo)))
    except Exception:
        return int(round(float(tempo[0]))) if hasattr(tempo, "__len__") else 0
# --- Tonalidad (clave) en notación Camelot, para mezclar armónicamente ---
# Método Krumhansl-Schmuckler: se compara el "perfil de notas" promedio del
# tema con los perfiles típicos de las 24 tonalidades. Es un estimado (acierta
# bastante, no siempre: sobre todo confunde relativas mayor/menor).
_PERFIL_MAYOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
_PERFIL_MENOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
_CAMELOT_MAYOR = [8, 3, 10, 5, 12, 7, 2, 9, 4, 11, 6, 1]    # C, C#, D, D#, E, F, F#, G, G#, A, A#, B
_CAMELOT_MENOR = [5, 12, 7, 2, 9, 4, 11, 6, 1, 8, 3, 10]
PATRON_CLAVE_CAMELOT = re.compile(r"\[(?:1[0-2]|[1-9])[AB]\]")
TONALIDAD_EN_NOMBRE = True   # "[128 BPM] [8A] Artista - Tema.mp3"
def _clave_camelot(chroma_medio):
    import numpy as np
    v = np.asarray(chroma_medio, dtype=float)
    if v.shape != (12,) or not np.any(v):
        return None
    mejor_corr, mejor_clave = -2.0, None
    for tonica in range(12):
        rotado = np.roll(v, -tonica)
        for perfil, tabla, letra in ((_PERFIL_MAYOR, _CAMELOT_MAYOR, "B"), (_PERFIL_MENOR, _CAMELOT_MENOR, "A")):
            c = np.corrcoef(rotado, perfil)[0, 1]
            if c > mejor_corr:
                mejor_corr, mejor_clave = c, f"{tabla[tonica]}{letra}"
    return mejor_clave
def analizar_audio(ruta_archivo):
    """Devuelve (bpm, clave_camelot) cargando el audio UNA sola vez. La
    clave puede ser None si no se pudo estimar. Es una función de módulo
    (no un método) para poder mandarla a un ProcessPoolExecutor."""
    import librosa
    wav_temporal = _convertir_a_wav_temporal(ruta_archivo, duracion_maxima=90)
    try:
        y, sr = librosa.load(wav_temporal, sr=None, mono=True, duration=90)
    finally:
        try:
            os.remove(wav_temporal)
        except Exception:
            pass
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    try:
        bpm = int(round(float(tempo)))
    except Exception:
        bpm = int(round(float(tempo[0]))) if hasattr(tempo, "__len__") else 0
    clave = None
    try:
        armonico = librosa.effects.harmonic(y)
        clave = _clave_camelot(librosa.feature.chroma_cqt(y=armonico, sr=sr).mean(axis=1))
    except Exception:
        clave = None
    return bpm, clave
def renombrar_con_bpm(ruta_original, bpm, clave=None):
    carpeta = os.path.dirname(ruta_original)
    nombre = os.path.basename(ruta_original)
    nombre_sin_ext, ext = os.path.splitext(nombre)
    nombre_sin_ext = re.sub(r"^\s*\[\d+\s*BPM\]\s*", "", nombre_sin_ext, flags=re.IGNORECASE)
    nombre_sin_ext = re.sub(r"\s*\[\d+\s*BPM\]\s*$", "", nombre_sin_ext, flags=re.IGNORECASE)
    nombre_sin_ext = re.sub(r"^\s*\[(?:1[0-2]|[1-9])[AB]\]\s*", "", nombre_sin_ext)
    etiqueta = f"[{bpm} BPM] " + (f"[{clave}] " if clave else "")
    candidato = f"{etiqueta}{nombre_sin_ext}{ext}"
    nueva_ruta = os.path.join(carpeta, candidato)
    contador = 1
    while os.path.exists(nueva_ruta) and nueva_ruta != ruta_original:
        candidato = f"{etiqueta}{nombre_sin_ext} ({contador}){ext}"
        nueva_ruta = os.path.join(carpeta, candidato)
        contador += 1
    if nueva_ruta != ruta_original:
        os.rename(ruta_original, nueva_ruta)
    return nueva_ruta
# --- Etiquetas ID3 + carátula, sin recodificar el audio (ffmpeg -c copy) ---
def escribir_etiquetas_mp3(ruta, etiquetas, portada=None):
    """Reescribe las etiquetas del mp3 (title, artist, album, TBPM, TKEY...) y
    le pone la carátula, copiando el audio tal cual (cero pérdida de calidad)."""
    if (not etiquetas and not portada) or not ruta.lower().endswith(".mp3"):
        return False
    fd, temporal = tempfile.mkstemp(prefix=PREFIJO_TEMPORAL, suffix=".mp3", dir=os.path.dirname(ruta))
    os.close(fd)
    try:
        cmd = [_ruta_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", ruta]
        if portada:
            cmd += ["-i", portada]
        cmd += ["-map", "0:a"]
        if portada:
            cmd += ["-map", "1:v"]
        cmd += ["-c:a", "copy"]
        if portada:
            # recorte cuadrado centrado (los cuadros de video son 16:9)
            cmd += ["-c:v", "mjpeg", "-q:v", "2", "-vf", "crop='min(iw,ih)':'min(iw,ih)'",
                    "-metadata:s:v", "title=Cover", "-metadata:s:v", "comment=Cover (front)"]
        cmd += ["-map_metadata", "0", "-id3v2_version", "3"]
        for clave, valor in (etiquetas or {}).items():
            if valor:
                cmd += ["-metadata", f"{clave}={valor}"]
        cmd.append(temporal)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, creationflags=_flags_win())
        if r.returncode != 0 or not os.path.exists(temporal) or os.path.getsize(temporal) == 0:
            return False
        os.replace(temporal, ruta)
        return True
    finally:
        if os.path.exists(temporal):
            try:
                os.remove(temporal)
            except Exception:
                pass
def descargar_portada(item):
    """Baja la carátula a un archivo temporal (None si no se puede). Usa la
    del álbum que informa YouTube Music y, si no hay, la miniatura del video."""
    urls = []
    if item.get("portada_url"):
        urls.append(item["portada_url"])
    vid = item.get("id")
    if vid:
        urls += [f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg", f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"]
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                datos = resp.read(3_000_000)
            if len(datos) < 5000:
                continue
            fd, ruta = tempfile.mkstemp(prefix="portada_", suffix=".img")
            with os.fdopen(fd, "wb") as f:
                f.write(datos)
            return ruta
        except Exception:
            continue
    return None
# --- Nombres limpios ---
_PATRON_PARENTESIS_RUIDO = re.compile(
    r"\s*[\(\[\{][^\)\]\}]*?\b(?:official|oficial|video|videoclip|audio|lyrics?|letra|visuali[sz]er|clip|"
    r"hd|hq|4k|1080p|720p|mv|m/v|con letra|subtitulado)\b[^\)\]\}]*?[\)\]\}]", re.IGNORECASE)
def limpiar_titulo_video(titulo):
    t = titulo or ""
    t = _PATRON_PARENTESIS_RUIDO.sub("", t)
    t = re.sub(r"\s*\|.*$", "", t)                      # "Tema | Canal | 2024"
    t = re.sub(r"\s*-\s*Topic\s*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip(" -_.")
    return t or (titulo or "").strip()
def _canal_artista(canal):
    """Nombre limpio del artista si el canal es de los 'limpios' (Topic/VEVO),
    donde el canal ES el artista; si no, ''."""
    c = (canal or "").strip()
    if re.search(r"-\s*Topic$", c, re.IGNORECASE):
        return re.sub(r"\s*-\s*Topic$", "", c, flags=re.IGNORECASE).strip()
    if re.search(r"vevo$", c, re.IGNORECASE):
        return re.sub(r"\s*vevo$", "", c, flags=re.IGNORECASE).strip()
    return ""
def datos_etiquetas(item):
    """(titulo, artista) limpios de un resultado de búsqueda."""
    if item.get("artista"):
        titulo = limpiar_titulo_video(item.get("titulo_limpio") or item.get("title", ""))
        return titulo, ", ".join([a.strip() for a in item["artista"].split(",") if a.strip()][:2])
    titulo = limpiar_titulo_video(item.get("title", ""))
    artista = _canal_artista(item.get("channel"))
    if " - " in titulo:
        izq, der = titulo.split(" - ", 1)
        if izq.strip() and der.strip():
            return der.strip(), izq.strip()
    return titulo, artista
def nombre_archivo_limpio(item):
    """Nombre de archivo (sin extensión): 'Artista - Título'."""
    titulo, artista = datos_etiquetas(item)
    if not titulo:
        return ""
    nombre = titulo
    if artista and _normalizar_para_comparar_nombre(artista) not in _normalizar_para_comparar_nombre(titulo):
        nombre = f"{artista} - {titulo}"
    nombre = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", nombre)
    nombre = re.sub(r"\s+", " ", nombre).strip(" .")
    return nombre[:120].strip(" .")
# --- Versiones de estudio vs. "caseras" ---
_PATRON_DUDOSA = re.compile(
    r"\b(?:sped ?up|speed ?up|slowed|reverb|8d|nightcore|bass ?boost(?:ed)?|boosted|covers?|karaoke|remake|"
    r"mashup|a ?capella|instrumental|tutorial|reacci[a-z]*|reaction|tiktok|casera|casero|piano cover|"
    r"enganchad[a-z]*|megamix|mix|album completo|full album|playlist)\b")
def es_version_dudosa(titulo, query_norm="", tipo=None):
    """True si el título huele a versión modificada/casera. Lo que el usuario
    escribió en la búsqueda no cuenta (si busca 'remix', no se descarta)."""
    t = _normalizar_para_comparar_nombre(titulo)
    for m in _PATRON_DUDOSA.finditer(t):
        if m.group(0) not in (query_norm or ""):
            return True
    if tipo != "MUSIC_VIDEO_TYPE_ATV" and re.search(r"\bremix\b", t) and "remix" not in (query_norm or ""):
        return True
    return False
def puntaje_canal_estudio(canal):
    c = _normalizar_para_comparar_nombre(canal)
    if c.endswith(" topic"):
        return 2.0
    if "vevo" in c:
        return 1.5
    if "oficial" in c or "official" in c:
        return 1.0
    return 0.0
def escribir_json_atomico(ruta, datos, indent=4):
    """Escribe a un temporal y recién después reemplaza: un corte a mitad de
    la escritura (luz, cierre forzado) no deja el archivo a medias."""
    temporal = ruta + ".tmp"
    with open(temporal, "w", encoding="utf-8") as f:
        json.dump(datos, f, indent=indent, ensure_ascii=False)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
    os.replace(temporal, ruta)
# =====================================================================
# NORMALIZADOR DE VOLUMEN (loudness) - usa el filtro loudnorm de ffmpeg,
# el mismo estándar (EBU R128) que usan Spotify/YouTube Music para que
# todos los temas suenen parejo. Se aplica en una sola pasada (loudnorm
# dinámico, sin medición previa): casi el doble de rápido que medir y
# después aplicar, a costa de un margen de error de hasta ~1 LUFS
# respecto al objetivo exacto (aceptable para este uso).
#
# Soporte multi-formato: el códec de salida de ffmpeg se elige según la
# EXTENSIÓN real del archivo (mp3 -> libmp3lame, wav -> pcm_s16le,
# flac -> flac, m4a/aac -> aac, ogg -> libvorbis). Antes se forzaba
# siempre libmp3lame sin importar la extensión del archivo de entrada,
# lo que dejaba un archivo con contenido MP3 pero extensión .wav/.m4a/etc
# (corrupto para cualquier reproductor). Los formatos que no están en el
# mapa se recodifican como mp3 por compatibilidad (mismo comportamiento
# de antes) pero avisando por consola.
# =====================================================================
VOLUMEN_TARGET_I = -14.0   # LUFS integrado objetivo (estándar streaming)
VOLUMEN_TARGET_TP = -1.5   # true peak máximo, para evitar clipping
VOLUMEN_TARGET_LRA = 11.0  # rango de loudness objetivo
_MARCA_VOLUMEN_NORMALIZADO = "ia_downloader_normalizado_v1"
class NormalizacionCancelada(Exception):
    """Se levanta cuando el usuario cancela la normalización de volumen
    (con el botón "Cancelar normalización") mientras un tema se está
    procesando, para distinguirlo de un error real de ffmpeg."""
    pass
CODEC_POR_EXTENSION = {
    ".mp3": ("libmp3lame", ["-q:a", "0"]),  # VBR V0 (~245 kbps): al recodificar para normalizar no se pierde calidad
    ".wav": ("pcm_s16le", []),
    ".flac": ("flac", []),
    ".m4a": ("aac", ["-b:a", "192k"]),
    ".aac": ("aac", ["-b:a", "192k"]),
    ".ogg": ("libvorbis", ["-q:a", "5"]),
}
def _codec_para_extension(ext):
    """Devuelve (codec, args_extra) para la extensión dada. Si el formato
    no está mapeado, recodifica a mp3 en la máxima calidad VBR (V0)."""
    return CODEC_POR_EXTENSION.get(ext.lower(), ("libmp3lame", ["-q:a", "0"]))
def volumen_ya_normalizado(ruta_audio):
    """Revisa si el archivo ya fue normalizado antes, para no repetirlo en cada pasada masiva."""
    try:
        resultado = subprocess.run(
            [_ruta_ffprobe(), "-v", "quiet", "-print_format", "json", "-show_format", ruta_audio],
            capture_output=True, text=True, timeout=30, creationflags=_flags_win()
        )
        datos = json.loads(resultado.stdout or "{}")
        comentario = datos.get("format", {}).get("tags", {}).get("comment", "") or ""
        return _MARCA_VOLUMEN_NORMALIZADO in comentario
    except Exception:
        return False
def medir_volumen_detallado(ruta_audio, duracion_maxima=None):
    """
    Corre la primera pasada de loudnorm (solo mide, no toca el archivo) y
    devuelve las métricas medidas: input_i (loudness integrado en LUFS),
    input_tp, input_lra, input_thresh y target_offset.
    Sirve para cualquier formato que ffmpeg pueda decodificar (mp3, wav,
    flac, m4a, ogg, etc.) - no depende de la extensión, ffmpeg detecta el
    formato real del contenido.
    Si se pasa duracion_maxima (en segundos), solo analiza esa cantidad de
    segundos desde el arranque del tema en vez del archivo completo - mucho
    más rápido para un escaneo masivo, a costa de algo de precisión. Para
    normalizar de verdad (normalizar_volumen) siempre se mide el archivo
    completo.
    """
    ffmpeg_exe = _ruta_ffmpeg_exe()
    cmd_medir = [ffmpeg_exe, "-hide_banner", "-i", ruta_audio]
    if duracion_maxima:
        cmd_medir += ["-t", str(duracion_maxima)]
    cmd_medir += [
        "-af", f"loudnorm=I={VOLUMEN_TARGET_I}:TP={VOLUMEN_TARGET_TP}:LRA={VOLUMEN_TARGET_LRA}:print_format=json",
        "-f", "null", "-",
    ]
    medicion = subprocess.run(cmd_medir, capture_output=True, text=True, timeout=180, creationflags=_flags_win())
    # A veces ffmpeg no devuelve nada en stderr (queda en None) si el proceso
    # se corta de forma rara bajo mucha carga en paralelo; sin este resguardo
    # el re.search de abajo explota con un TypeError feo en vez de avisar bien.
    stderr_texto = medicion.stderr or ""
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr_texto)
    if not match:
        # Incluye la razón real que dio ffmpeg (archivo dañado, formato raro,
        # ruta no encontrada, etc.) en vez de un mensaje genérico.
        razon = stderr_texto.strip().splitlines()[-1] if stderr_texto.strip() else "ffmpeg no devolvió información (archivo dañado o vacío)"
        raise RuntimeError(razon)
    return json.loads(match.group(0))
def medir_volumen(ruta_audio, duracion_maxima=None):
    """Devuelve solo el loudness integrado medido del archivo, en LUFS."""
    return float(medir_volumen_detallado(ruta_audio, duracion_maxima=duracion_maxima)["input_i"])
def _duracion_audio_segundos(ruta_audio):
    """Duración total del audio en segundos (lectura rápida de metadata, no decodifica)."""
    try:
        resultado = subprocess.run(
            [_ruta_ffprobe(), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", ruta_audio],
            capture_output=True, text=True, timeout=30, creationflags=_flags_win()
        )
        return float(resultado.stdout.strip())
    except Exception:
        return None
def _parsear_out_time(valor):
    """Convierte 'HH:MM:SS.ffffff' (formato de -progress) a segundos (float)."""
    try:
        h, m, s = valor.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception:
        return None
def _correr_ffmpeg_con_progreso(cmd, duracion_total, callback_progreso, evento_cancelar=None):
    """
    Corre un comando de ffmpeg leyendo su progreso en vivo (-progress pipe:1)
    y llama a callback_progreso(pct) con el % de avance (0-100) a medida que
    procesa.
    Si se pasa evento_cancelar (threading.Event) y se activa mientras ffmpeg
    está corriendo, se mata el proceso enseguida (proceso.kill()) en vez de
    esperar a que termine solo - así "Cancelar" corta también al tema que ya
    está a mitad de normalizarse, no solo a los que todavía no arrancaron.
    Devuelve (codigo_salida, texto_error_si_hubo, fue_cancelado).
    """
    fd_err, ruta_err = tempfile.mkstemp(suffix=".log")
    os.close(fd_err)
    cancelado = False
    try:
        with open(ruta_err, "w", encoding="utf-8", errors="ignore") as f_err:
            proceso = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=f_err, text=True, creationflags=_flags_win()
            )
            try:
                for linea in proceso.stdout:
                    if evento_cancelar is not None and evento_cancelar.is_set():
                        cancelado = True
                        try:
                            proceso.kill()
                        except Exception:
                            pass
                        break
                    if not duracion_total or not callback_progreso:
                        continue
                    linea = linea.strip()
                    if linea.startswith("out_time="):
                        segundos = _parsear_out_time(linea.split("=", 1)[1])
                        if segundos is not None:
                            pct = max(0.0, min(100.0, (segundos / duracion_total) * 100))
                            callback_progreso(pct)
            finally:
                proceso.wait()
        codigo = proceso.returncode
        error_texto = ""
        if codigo != 0 and not cancelado:
            try:
                with open(ruta_err, "r", encoding="utf-8", errors="ignore") as f:
                    error_texto = f.read()[-300:]
            except Exception:
                pass
        return codigo, error_texto, cancelado
    finally:
        try:
            os.remove(ruta_err)
        except Exception:
            pass
def normalizar_volumen(ruta_audio, callback_progreso=None, evento_cancelar=None):
    """
    Pareja el volumen del tema al estándar de loudness (-14 LUFS). Sobrescribe
    el archivo original con la versión normalizada, conservando sus
    etiquetas (título, artista, etc.) y agrega una marca en el comentario
    para no volver a procesarlo si ya está normalizado.
    Funciona con cualquier formato soportado (mp3, wav, flac, m4a, ogg): el
    códec de salida se elige según la extensión real del archivo (ver
    CODEC_POR_EXTENSION), así el archivo resultante queda con el mismo
    formato que tenía, en vez de quedar siempre forzado a mp3.
    Usa loudnorm de una sola pasada (sin medir el archivo antes): la mitad
    de trabajo de ffmpeg por archivo que el modo de dos pasadas, a cambio
    de quedar con un margen de error de hasta ~1 LUFS del objetivo exacto
    (el valor final igual se vuelve a medir y mostrar en la tabla).
    Si se pasa callback_progreso, se llama con el % de avance (0-100) de la
    pasada de corrección (la que realmente reescribe el archivo), para poder
    mostrarlo en pantalla mientras se procesa.
    Si se pasa evento_cancelar (threading.Event) y está activado - antes de
    arrancar, o en cualquier momento mientras ffmpeg está corriendo - se
    corta el proceso y se levanta NormalizacionCancelada, dejando el archivo
    original intacto (el temporal a medio escribir se borra en el finally).
    """
    if evento_cancelar is not None and evento_cancelar.is_set():
        raise NormalizacionCancelada("Cancelado por el usuario")
    ffmpeg_exe = _ruta_ffmpeg_exe()
    duracion_total = _duracion_audio_segundos(ruta_audio)
    _, ext = os.path.splitext(ruta_audio)
    codec, args_codec = _codec_para_extension(ext)
    # --- Aplica la corrección en una sola pasada (loudnorm dinámico) ---
    carpeta = os.path.dirname(ruta_audio)
    # El temporal se crea al lado del archivo (así el reemplazo final es
    # instantáneo, sin depender de que sea el mismo disco), pero con un
    # nombre reconocible: si el programa se cierra a la fuerza en medio de
    # la normalización, ese archivo queda y después se limpia solo (ver
    # _limpiar_temporales_huerfanos).
    fd, temporal = tempfile.mkstemp(prefix=PREFIJO_TEMPORAL, suffix=ext or ".mp3", dir=carpeta)
    os.close(fd)
    try:
        filtro = (
            f"loudnorm=I={VOLUMEN_TARGET_I}:TP={VOLUMEN_TARGET_TP}:LRA={VOLUMEN_TARGET_LRA}:"
            f"print_format=summary"
        )
        cmd_aplicar = [
            ffmpeg_exe, "-y", "-hide_banner", "-i", ruta_audio,
            "-af", filtro,
            "-codec:a", codec, *args_codec,
            "-map_metadata", "0",
        ]
        # id3v2 solo aplica a contenedores tipo mp3; en otros formatos ffmpeg
        # lo ignora o falla, así que solo se agrega cuando corresponde.
        if codec == "libmp3lame":
            cmd_aplicar += ["-id3v2_version", "3"]
        cmd_aplicar += [
            "-metadata", f"comment={_MARCA_VOLUMEN_NORMALIZADO}",
            "-progress", "pipe:1", "-nostats",
            temporal,
        ]
        codigo, error_texto, cancelado = _correr_ffmpeg_con_progreso(
            cmd_aplicar, duracion_total, callback_progreso, evento_cancelar=evento_cancelar)
        if cancelado:
            raise NormalizacionCancelada("Cancelado por el usuario")
        if codigo != 0 or not os.path.exists(temporal) or os.path.getsize(temporal) == 0:
            raise RuntimeError(f"ffmpeg falló al normalizar: {error_texto}")
        os.replace(temporal, ruta_audio)
        if callback_progreso:
            callback_progreso(100.0)
    finally:
        if os.path.exists(temporal):
            try:
                os.remove(temporal)
            except Exception:
                pass
    return True
# =====================================================================
# PREDICTOR DE RIESGO: EL APRENDIZAJE QUE SE ANTICIPA AL BLOQUEO
#   El AntiBloqueoManager de más abajo es REACTIVO: espera a que YouTube
#   conteste con un bloqueo para recién ahí subir el nivel de cautela.
#   Este predictor va un paso adelante. Con lo que aprendió en sesiones
#   anteriores (a qué hora lo bloquean, a qué ritmo lo bloquean, qué
#   cliente se está degradando, cuánto le duran las cookies de Firefox)
#   calcula un puntaje de riesgo ANTES de cada pedido y decide si
#   conviene ir normal, más lento o frenar un rato - o sea, se anticipa
#   al bloqueo en vez de sufrirlo.
#   Todo lo que aprende queda en memoria.json, dentro de la clave
#   "predictor", así que la próxima sesión arranca sabiendo cómo le fue
#   a esta última.
# =====================================================================
def predictor_por_defecto():
    return {
        "eventos": [],                  # [[fecha iso, "ok"|"bloqueo"|"otro"|"irrecuperable"], ...]
        "perfil_horario": {},           # "0".."23" -> {"ok": n, "bloqueos": n}
        "ritmo_seguro": {               # techo de descargas por ventana que todavía no dio bloqueo
            "ventana_min": 10,
            "max_ok_sin_bloqueo": 12,   # solo un punto de partida: se aprende con el uso
            "muestras": 0,
            "ultima_subida": None,
        },
        "cookies_desde": None,          # desde cuándo se usa la sesión de cookies actual
        "cookies_primer_bloqueo": None, # primera señal de bloqueo con esas cookies
        "racha_ok": 0,                  # descargas seguidas sin ninguna señal
        "ultima_sonda_ok": None,
        "ultima_sonda_fallo": None,
        "ultima_actualizacion": None,
    }
def _fecha_iso_valida(evento, desde):
    try:
        return datetime.fromisoformat(evento[0]) >= desde
    except Exception:
        return False
class PredictorRiesgo:
    """Calcula el riesgo de que el PRÓXIMO pedido salga bloqueado, con
    todo lo que el programa ya aprendió, y decide cuánto conviene esperar
    antes de intentarlo."""
    VENTANA_RECIENTE_MIN = 10       # ventana para medir "a qué ritmo vengo"
    VENTANA_BLOQUEOS_MIN = 15       # ventana para mirar señales de bloqueo frescas
    MIN_MUESTRAS_HORA = 8           # sin datos suficientes, la hora no opina
    PAUSA_ANTICIPADA_MIN = 10       # minutos: no repetir la pausa preventiva antes de este tiempo
    CACHE_SEGUNDOS = 2.0            # el riesgo se recalcula como mucho cada 2s
    PESO_RITMO = 35
    PESO_BLOQUEOS = 30
    PESO_HORA = 20
    PESO_CLIENTE = 15
    PESO_COOKIES = 10
    PESO_RACHA = 10
    RITMO_TECHO_INICIAL = 12        # descargas por ventana que se asumen seguras al arrancar
    RITMO_SOBREPASO = 0.25          # cuánto hay que pasarse del techo para que suene la alarma
    def __init__(self, memoria, consola=None):
        self.memoria = memoria
        self.consola = consola
        self.datos = memoria.setdefault("predictor", predictor_por_defecto())
        for clave, valor in predictor_por_defecto().items():
            self.datos.setdefault(clave, valor)
        self._lock = threading.Lock()
        self._cache = None
        self._ultima_pausa_anticipada = 0.0
        self._ultimo_aviso = None
        # El techo de ritmo arranca calibrado con lo que YouTube ya aguantó
        # en las últimas 48h (ver _calibrar_techo_con_historial).
        self._calibrar_techo_con_historial()
    # --- utilidades internas ---
    def _log(self, mensaje, tipo="debug"):
        if self.consola:
            metodo = getattr(self.consola, tipo, None)
            if metodo:
                metodo(mensaje)
    def _fechas(self):
        salida = []
        for ev in self.datos.get("eventos", []):
            try:
                salida.append((datetime.fromisoformat(ev[0]), ev[1]))
            except Exception:
                continue
        return salida
    def _cuenta(self, tipo, minutos):
        """Cuántos eventos de ese tipo pasaron en los últimos N minutos."""
        limite = datetime.now() - timedelta(minutes=minutos)
        return sum(1 for f, t in self._fechas() if t == tipo and f >= limite)
    def _podar_eventos(self):
        """Se guardan 48h de historia (tope duro de 600 eventos): es lo
        único que hace falta para medir ritmo y señales recientes, y evita
        que memoria.json crezca sin control."""
        limite = datetime.now() - timedelta(hours=48)
        eventos = [ev for ev in self.datos.get("eventos", []) if _fecha_iso_valida(ev, limite)]
        self.datos["eventos"] = eventos[-600:]
    def _max_ok_en_ventana(self, minutos):
        """El máximo de descargas exitosas que entraron en cualquier ventana
        de N minutos del historial guardado (ventana corrediza)."""
        limite = datetime.now() - timedelta(hours=48)
        fechas = sorted(f for f, t in self._fechas() if t == "ok" and f >= limite)
        mejor = 0
        inicio = 0
        for fin in range(len(fechas)):
            while (fechas[fin] - fechas[inicio]).total_seconds() > minutos * 60:
                inicio += 1
            mejor = max(mejor, fin - inicio + 1)
        return mejor
    def _calibrar_techo_con_historial(self):
        """Al abrir el programa, el techo arranca sabiendo lo que YouTube YA
        aguantó: si en la sesión anterior se bajaron 40 temas en 10 minutos
        sin un solo bloqueo, no tiene sentido arrancar creyendo que el techo
        es 4 y marcando "ritmo alto" en cada descarga. Si en el historial sí
        hubo bloqueos, el techo guardado es un límite real y no se toca."""
        if not self.datos.get("eventos"):
            return
        if self._cuenta("bloqueo", 48 * 60) > 0:
            return
        ritmo = self.datos.get("ritmo_seguro")
        if not isinstance(ritmo, dict):
            return
        ventana = int(ritmo.get("ventana_min", self.VENTANA_RECIENTE_MIN) or self.VENTANA_RECIENTE_MIN)
        techo = int(ritmo.get("max_ok_sin_bloqueo", self.RITMO_TECHO_INICIAL) or self.RITMO_TECHO_INICIAL)
        historico = self._max_ok_en_ventana(ventana)
        if historico > techo:
            ritmo["max_ok_sin_bloqueo"] = historico
    # --- aprendizaje: cada cosa que pasa deja un dato ---
    def registrar_evento(self, tipo, sonda=False):
        """Anota lo que pasó (ok / bloqueo / otro / irrecuperable) con su
        hora. Es la materia prima del aprendizaje: el perfil horario, el
        techo de ritmo y las señales recientes salen de acá."""
        with self._lock:
            ahora = datetime.now()
            self.datos.setdefault("eventos", []).append([ahora.isoformat(), tipo])
            perfil = self.datos.setdefault("perfil_horario", {}).setdefault(
                str(ahora.hour), {"ok": 0, "bloqueos": 0})
            perfil.setdefault("ok", 0)
            perfil.setdefault("bloqueos", 0)
            ritmo = self.datos.setdefault("ritmo_seguro", {
                "ventana_min": self.VENTANA_RECIENTE_MIN,
                "max_ok_sin_bloqueo": self.RITMO_TECHO_INICIAL, "muestras": 0})
            ritmo.setdefault("ventana_min", self.VENTANA_RECIENTE_MIN)
            ritmo.setdefault("max_ok_sin_bloqueo", self.RITMO_TECHO_INICIAL)
            techo = max(1, int(ritmo.get("max_ok_sin_bloqueo", self.RITMO_TECHO_INICIAL)))
            if tipo == "ok":
                perfil["ok"] += 1
                self.datos["racha_ok"] = int(self.datos.get("racha_ok", 0)) + 1
                if sonda:
                    self.datos["ultima_sonda_ok"] = ahora.isoformat()
                # Si la sesión de cookies vuelve a andar después de haber
                # fallado, Firefox se renovó solo: se reinicia el reloj de
                # "cuánto hace que no revalido la sesión".
                if self.memoria.get("cookies_navegador_efectivo"):
                    if not self.datos.get("cookies_desde") or self.datos.get("cookies_primer_bloqueo"):
                        self.datos["cookies_desde"] = ahora.isoformat()
                        self.datos["cookies_primer_bloqueo"] = None
                # APRENDIZAJE HACIA ARRIBA, en dos velocidades:
                # - Si NUNCA se vio un bloqueo (ni en las últimas 48h) no
                #   hay ninguna evidencia de que este ritmo tenga techo, así
                #   que el techo es simplemente el máximo ritmo observado y
                #   se ajusta al toque y en silencio. Antes se exigía haber
                #   visto un bloqueo para poder subirlo, y con el techo
                #   inicial puesto a mano eso dejaba el aviso de "ritmo
                #   alto" encendido para siempre en un uso normal: en una
                #   sesión real de 46 descargas en 10 minutos, sin un solo
                #   fallo, el predictor creía estar 11 veces por encima del
                #   techo y estiraba el ritmo en cada descarga.
                # - Una vez que hubo un bloqueo, el techo es un límite de
                #   verdad: se sube de a un paso cada media hora limpia.
                venia = self._cuenta("ok", int(ritmo.get("ventana_min", self.VENTANA_RECIENTE_MIN)))
                if venia > techo:
                    if self._cuenta("bloqueo", 48 * 60) == 0:
                        ritmo["max_ok_sin_bloqueo"] = venia
                    elif int(ritmo.get("muestras", 0)) > 0 and self._cuenta("bloqueo", 30) == 0:
                        puede_subir = True
                        ultima_subida = ritmo.get("ultima_subida")
                        if ultima_subida:
                            try:
                                minutos = (ahora - datetime.fromisoformat(ultima_subida)).total_seconds() / 60
                                puede_subir = minutos >= 30
                            except Exception:
                                puede_subir = True
                        if puede_subir:
                            ritmo["max_ok_sin_bloqueo"] = venia
                            ritmo["ultima_subida"] = ahora.isoformat()
                            self._log(f"📚 Anticipación: media hora sin señales con {venia} descargas en "
                                      f"{ritmo.get('ventana_min')} min - subo el techo seguro a {venia}")
            elif tipo == "bloqueo":
                perfil["bloqueos"] += 1
                self.datos["racha_ok"] = 0
                if sonda:
                    self.datos["ultima_sonda_fallo"] = ahora.isoformat()
                if self.memoria.get("cookies_navegador_efectivo") and not self.datos.get("cookies_primer_bloqueo"):
                    self.datos["cookies_primer_bloqueo"] = ahora.isoformat()
                # APRENDIZAJE HACIA ABAJO: si venía haciendo N descargas sin
                # problemas y justo ahora saltó el bloqueo, el techo seguro
                # está por debajo de N. Se baja con margen para no volver a
                # pisarlo (y ese margen es justamente la anticipación: la
                # próxima vez frena ANTES de llegar a N).
                venia = self._cuenta("ok", int(ritmo.get("ventana_min", self.VENTANA_RECIENTE_MIN)))
                if venia > 0 and (venia - 1) < techo:
                    ritmo["max_ok_sin_bloqueo"] = max(1, venia - 1)
                    ritmo["muestras"] = int(ritmo.get("muestras", 0)) + 1
                    self._log(f"📚 Anticipación: el bloqueo apareció después de {venia} descargas en "
                              f"{ritmo.get('ventana_min')} min - bajo el techo seguro a "
                              f"{ritmo['max_ok_sin_bloqueo']} y la próxima vez freno antes", "warning")
                else:
                    ritmo["muestras"] = int(ritmo.get("muestras", 0)) + 1
            else:
                self.datos["racha_ok"] = 0
            self._podar_eventos()
            self.datos["ultima_actualizacion"] = ahora.isoformat()
            self._cache = None
    def _horas_cookies(self):
        desde = self.datos.get("cookies_desde")
        if not desde:
            return 0.0
        try:
            return (datetime.now() - datetime.fromisoformat(desde)).total_seconds() / 3600
        except Exception:
            return 0.0
    def _ewma_peor_candidato(self, clientes):
        """De los clientes que se van a usar, el que peor viene: es el que
        puede romper el intento y hacer creer que hay bloqueo."""
        if not clientes:
            return None
        stats = self.memoria.get("client_stats", {}) or {}
        valores = []
        for c in clientes[:2]:
            s = stats.get(c)
            if isinstance(s, dict) and s.get("ewma") is not None:
                valores.append(float(s["ewma"]))
        if not valores:
            return None
        return min(valores)
    # --- el corazón: cuánto riesgo hay AHORA ---
    def riesgo_actual(self, clientes_candidatos=None):
        """Devuelve {"score": 0-100, "sugerencia": ..., "motivos": [...]}.
        Cada componente mira algo distinto que el programa ya aprendió."""
        ahora = time.time()
        if self._cache and (ahora - self._cache[0]) < self.CACHE_SEGUNDOS:
            return self._cache[1]
        with self._lock:
            motivos = []
            puntaje = 0.0
            ritmo = self.datos.get("ritmo_seguro", {})
            ventana = int(ritmo.get("ventana_min", self.VENTANA_RECIENTE_MIN) or self.VENTANA_RECIENTE_MIN)
            techo = max(1, int(ritmo.get("max_ok_sin_bloqueo", self.RITMO_TECHO_INICIAL)
                              or self.RITMO_TECHO_INICIAL))
            ok_recientes = self._cuenta("ok", ventana)
            ratio = ok_recientes / techo
            # El aviso suena cuando se PASA del techo, no cuando lo alcanza:
            # si el techo es lo máximo que YouTube ya aguantó, estar justo en
            # el techo es lo normal. Se pide un sobrepaso del 25% para no
            # alarmarse por el ruido de la ventana.
            if ratio > 1 + self.RITMO_SOBREPASO:
                puntaje += self.PESO_RITMO * min(1.0, (ratio - 1 - self.RITMO_SOBREPASO) / 0.75)
                motivos.append(f"ritmo {ok_recientes} en {ventana} min, "
                               f"por encima del techo aprendido ({techo})")
            bloqueos_recientes = self._cuenta("bloqueo", self.VENTANA_BLOQUEOS_MIN)
            if bloqueos_recientes:
                # Con UNA sola señal ya se suma bastante: es la señal más
                # fresca y la que antes avisa que viene el bloqueo.
                puntaje += min(self.PESO_BLOQUEOS, 15 * bloqueos_recientes)
                motivos.append(f"{bloqueos_recientes} señal(es) de bloqueo en {self.VENTANA_BLOQUEOS_MIN} min")
            hora = self.datos.get("perfil_horario", {}).get(str(datetime.now().hour), {})
            total_hora = hora.get("ok", 0) + hora.get("bloqueos", 0)
            if total_hora >= self.MIN_MUESTRAS_HORA:
                pb = hora.get("bloqueos", 0) / total_hora
                if pb > 0.05:
                    puntaje += self.PESO_HORA * min(1.0, (pb - 0.05) / 0.25)
                    motivos.append(f"a esta hora te bloquea el {pb * 100:.0f}% de las veces")
            ewma = self._ewma_peor_candidato(clientes_candidatos)
            if ewma is not None and ewma < 0.5:
                puntaje += self.PESO_CLIENTE * min(1.0, (0.5 - ewma) / 0.5)
                motivos.append(f"el cliente disponible viene con {ewma * 100:.0f}% de éxito reciente")
            horas_cookies = self._horas_cookies()
            # Las cookies "viejas" solo importan si ya hubo una señal con
            # ellas: si vienen descargando bien, cada descarga ES una
            # revalidación y sumar riesgo por la antigüedad sería un
            # falso positivo permanente (en esta máquina: 550 h => +10
            # puntos fijos, sin ningún bloqueo).
            if horas_cookies > 24 and (self.datos.get("cookies_primer_bloqueo") or bloqueos_recientes):
                puntaje += self.PESO_COOKIES * min(1.0, (horas_cookies - 24) / 72)
                motivos.append(f"cookies de Firefox con {horas_cookies:.0f} h sin revalidar")
            racha = int(self.datos.get("racha_ok", 0))
            limite_racha = techo * (1 + self.RITMO_SOBREPASO)
            if racha > limite_racha:
                puntaje += self.PESO_RACHA * min(1.0, (racha - limite_racha) / 3)
                motivos.append(f"racha de {racha} descargas seguidas (techo aprendido: {techo})")
            puntaje = max(0.0, min(100.0, puntaje))
            resultado = {
                "score": round(puntaje),
                "sugerencia": self._sugerencia_para(puntaje),
                "motivos": motivos,
                "techo_ritmo": techo,
                "ok_recientes": ok_recientes,
            }
            self._cache = (ahora, resultado)
            return resultado
    def _sugerencia_para(self, puntaje):
        if puntaje >= 80:
            return "pausa_larga"
        if puntaje >= 60:
            return "pausa_corta"
        if puntaje >= 30:
            return "lento"
        return "normal"
    # --- el uso: qué hace el programa con ese riesgo ---
    def plan_antes_de_descargar(self, anti_bloqueo=None, clientes_candidatos=None,
                                debe_abortar=None, dormir=None):
        """Se llama ANTES de cada descarga. Devuelve el plan anticipado y,
        si el riesgo lo justifica, aplica una pausa preventiva: una sola
        vez cada PAUSA_ANTICIPADA_MIN minutos, para que la anticipación no
        se convierta en una lentitud permanente."""
        estado = self.riesgo_actual(clientes_candidatos)
        plan = dict(estado)
        plan["pausa_s"] = 0.0
        sugerencia = estado["sugerencia"]
        if sugerencia == "lento":
            # No frena la descarga, pero estira el ritmo: se le avisa al
            # AntiBloqueoManager para que la pausa entre descargas ya salga
            # más larga antes de que haya un solo fallo.
            if anti_bloqueo:
                anti_bloqueo.estirar_ritmo(1.15)
        elif sugerencia in ("pausa_corta", "pausa_larga"):
            if (time.time() - self._ultima_pausa_anticipada) >= self.PAUSA_ANTICIPADA_MIN * 60:
                self._ultima_pausa_anticipada = time.time()
                plan["pausa_s"] = (random.uniform(20, 40) if sugerencia == "pausa_corta"
                                   else random.uniform(90, 150))
                if anti_bloqueo:
                    anti_bloqueo.subir_nivel_preventivo("; ".join(estado["motivos"]) or "riesgo alto")
                self._log(f"🛡️ Anticipación: riesgo {estado['score']}% "
                          f"({'; '.join(estado['motivos'])}) - freno {plan['pausa_s']:.0f}s ANTES "
                          f"de que YouTube conteste", "warning")
                if dormir and not (debe_abortar and debe_abortar()):
                    dormir(plan["pausa_s"])
            else:
                plan["nota"] = "pausa preventiva reciente: se sigue sin volver a frenar"
        self._avisar_cambio(estado)
        return plan
    def _avisar_cambio(self, estado):
        """Avisa en la consola solo cuando el nivel de riesgo CAMBIA, para
        no llenar el registro repitiendo lo mismo en cada descarga."""
        if estado["sugerencia"] == self._ultimo_aviso:
            return
        self._ultimo_aviso = estado["sugerencia"]
        if estado["sugerencia"] == "normal":
            self._log(f"🟢 Anticipación: riesgo {estado['score']}%, vuelvo a ritmo normal")
        elif estado["sugerencia"] == "lento":
            self._log(f"🟡 Anticipación: riesgo {estado['score']}% "
                      f"({'; '.join(estado['motivos'])}) - alargo la pausa entre descargas "
                      f"antes de que falle algo")
    def calibrar_con_sonda(self, ok, detalle=None, es_bloqueo=False):
        """La sonda de salud es un pedido barato que se hace antes de
        arrancar una tanda: si YouTube ya viene respondiendo mal, la tanda
        entera arranca en modo cauteloso en vez de descubrirlo perdiendo
        las primeras descargas de la cola. Se distingue un bloqueo de
        YouTube de un problema local (permisos, red): avisar "YouTube está
        frenando" cuando en realidad falló algo de la máquina confunde y
        hace subir la cautela sin motivo."""
        if ok:
            self.datos["ultima_sonda_ok"] = datetime.now().isoformat()
            self._cache = None
            self._log("🩺 Sonda de salud: YouTube responde normal, arranco con el ritmo aprendido")
        elif es_bloqueo:
            self.datos["ultima_sonda_fallo"] = datetime.now().isoformat()
            self._cache = None
            self._log(f"🩺 Sonda de salud: YouTube ya está frenando ({detalle or 'señal de bloqueo'}) - "
                      f"arranco la tanda en modo cauteloso", "warning")
        else:
            self._cache = None
            self._log(f"🩺 Sonda de salud: no se pudo medir el estado de YouTube "
                      f"({detalle or 'error'}) - sigo con la cautela que ya tenía aprendida")
    def avisar_estado_inicial(self):
        """Cuenta al abrir el programa con qué aprendido arranca."""
        estado = self.riesgo_actual()
        perfil = self.datos.get("perfil_horario", {}) or {}
        horas = len([h for h, v in perfil.items() if (v.get("ok", 0) + v.get("bloqueos", 0)) >= self.MIN_MUESTRAS_HORA])
        self._log(f"🧠 Anticipación: arranco con riesgo {estado['score']}% · techo aprendido "
                  f"{estado['techo_ritmo']} descargas/{self.datos.get('ritmo_seguro', {}).get('ventana_min', 10)} min · "
                  f"{horas} franja(s) horaria(s) con historial")
        if estado["motivos"]:
            self._log(f"🧠 Motivos: {'; '.join(estado['motivos'])}")
    def estado_texto(self):
        """Texto corto para el panel (el detalle va a la consola)."""
        estado = self.riesgo_actual()
        aviso = " ⚠" if estado["sugerencia"] in ("pausa_corta", "pausa_larga") else ""
        return f"riesgo {estado['score']}%{aviso}"
    def guardar_en_memoria(self):
        with self._lock:
            self.datos["ultima_actualizacion"] = datetime.now().isoformat()
            self.memoria["predictor"] = self.datos

# =====================================================================
# ANTI-BLOQUEO CON APRENDIZAJE PERSISTENTE
#   A diferencia de un anti-bloqueo "de sesión" que arranca de cero
#   cada vez que se abre el programa, este arranca del nivel/velocidad
#   que la última sesión dejó guardado en memoria.json - así que si
#   ayer YouTube estaba exigente, hoy arranca con más cuidado, y si
#   todo salió bien varias veces seguidas, arranca más rápido.
# =====================================================================
class AntiBloqueoManager:
    NIVEL_MAXIMO = 6
    FACTOR_ESCALADO = 1.5
    UMBRAL_COOLDOWN = 3
    COOLDOWN_BASE = 60
    # Con la PRIMERA señal de bloqueo ya se frena a todos los hilos este
    # ratito (antes había que juntar 3 fallos: las 2 descargas siguientes
    # se perdían golpeando una puerta que YouTube acababa de cerrar).
    PAUSA_PREVENTIVA = 25
    COOLDOWN_MAX = 240
    EXITOS_PARA_BAJAR_NIVEL = 3
    RACHA_PARA_ACELERAR = 4
    DELAY_MINIMO = 6.0
    DELAY_MAXIMO = 35.0
    # Señales de bloqueo: lo que YouTube usa cuando frena por ritmo o por
    # sospecha de bot. Van como expresiones regulares para no confundir un
    # código de estado con cualquier número que lo contenga: ojo con "\b",
    # que en "12.429" el punto cuenta como límite de palabra y matchearía
    # igual, así que se exige que no haya ni dígito ni punto pegado.
    SENALES_BLOQUEO = [
        r"(?<![\d.])429(?![\d])", r"too many requests", r"rate.?limit",
        r"(?<![\d.])403(?![\d])", r"not a bot", r"sign in to confirm",
        r"captcha", r"confirm you'?re not a bot",
    ]
    # "Sign in to confirm your age" es un video con restricción de edad, NO
    # una señal de que YouTube nos esté frenando: antes coincidía con
    # "sign in to confirm" y disparaba pausa global + subida de nivel por
    # un solo video.
    SENALES_EDAD = ["confirm your age", "age-restricted", "age restricted",
                    "inappropriate for some users"]
    # Muerte segura del video: no importa cuántas veces se reintente, no
    # hay audio que sacar. Una sola confirmación alcanza.
    SENALES_MUERTE_SEGURA = [
        "video unavailable", "private video", "this video is private",
        "has been removed", "account associated with this video has been terminated",
        "video is no longer available", "this video is not available",
        "members-only", "join this channel",
        "this video does not exist", "no longer exists", "drm protected",
    ]
    # Señales DUDOSAS: a veces significan "el video está muerto" y a veces
    # solo "este intento no sirvió". Antes estaban mezcladas con las
    # definitivas, así que un simple "unable to extract" (típico de un
    # cliente que dejó de andar) vetaba un video sano PARA SIEMPRE. Ahora
    # necesitan confirmarse dos veces y el veto caduca.
    SENALES_DUDOSAS = [
        "copyright", "unable to extract", "video is protected",
        "premieres in", "live event will begin", "unsupported url",
    ]
    # Se mantiene el nombre viejo por compatibilidad (cualquier código o
    # memoria que lo mencione sigue funcionando).
    SENALES_IRRECUPERABLES = SENALES_MUERTE_SEGURA + SENALES_DUDOSAS
    def __init__(self, memoria, consola=None, predictor=None):
        self.memoria = memoria
        self.consola = consola
        # El predictor anticipado (ver PredictorRiesgo): este manager
        # reacciona a lo que YA pasó, el predictor se adelanta a lo que
        # puede pasar. Se le avisa cada éxito y cada fallo para que siga
        # aprendiendo.
        self.predictor = predictor
        # Barrera de pausa GLOBAL: cuando aparece una señal de bloqueo,
        # frena a TODOS los hilos de descarga a la vez. Antes solo se
        # dormía el hilo que había fallado, así que en modo paralelo los
        # demás seguían golpeando a YouTube justo durante el bloqueo.
        # Evento "set" = vía libre.
        self._via_libre = threading.Event()
        self._via_libre.set()
        self._lock_pausa = threading.Lock()
        nivel_guardado = min(int(memoria.get("nivel_base_aprendido", 0)), 2)
        delay_guardado = float(memoria.get("delay_base_aprendido", 12.0))
        # Los bloqueos de YouTube son por horas, no para siempre. Si la
        # última sesión fue hace rato, se relaja lo aprendido en vez de
        # arrancar siempre tan cauteloso como quedó la vez anterior -
        # si no, un mal rato un día deja el programa lento para siempre.
        horas_desde_ultima = None
        ultima = memoria.get("ultima_sesion")
        if ultima:
            try:
                horas_desde_ultima = (datetime.now() - datetime.fromisoformat(ultima)).total_seconds() / 3600
            except Exception:
                horas_desde_ultima = None
        if horas_desde_ultima is None or horas_desde_ultima >= 6:
            nivel_guardado = 0
            delay_guardado = min(delay_guardado, 12.0)
            self._log("🔄 Hace rato que no se usa (o es la primera vez): arranca con ritmo normal, sin cautela heredada.")
        elif horas_desde_ultima >= 2:
            nivel_guardado = nivel_guardado // 2
            delay_guardado = round((delay_guardado + 12.0) / 2, 1)
            self._log(f"🔄 Pasaron {horas_desde_ultima:.1f}h desde la última vez: relajando un poco la cautela heredada.")
        # Arranca desde lo aprendido (ya ajustado por el tiempo), no
        # siempre en 0 - esto es lo que hace que el programa "recuerde"
        # cómo le fue la última vez.
        self.nivel = nivel_guardado
        self.delay_base = delay_guardado
        # Tope de la ANTICIPACIÓN sobre el ritmo: el predictor puede
        # estirarlo como mucho un 50% de lo que valía al abrir el programa
        # (ver estirar_ritmo). La subida por fallos reales no tiene tope.
        self.delay_tope_anticipado = min(self.DELAY_MAXIMO, round(delay_guardado * 1.5, 1))
        self.buena_racha = int(memoria.get("buena_racha", 0))
        self.errores_consecutivos_bloqueo = 0
        self.exitos_consecutivos = 0
        # Protege el estado interno cuando hay varias descargas simultáneas
        # (cola con más de 1 worker) tocando el mismo AntiBloqueoManager.
        self._lock = threading.Lock()
        self._lock_turno = threading.Lock()
        self._proximo_turno = 0.0
        self.cooldowns_seguidos = 0
    def _log(self, mensaje, tipo="debug"):
        if self.consola:
            metodo = getattr(self.consola, tipo, None)
            if metodo:
                metodo(mensaje)
    def esperar_turno(self, workers_activos=1, debe_abortar=None):
        """Separa los ARRANQUES de descarga de todos los hilos con el ritmo
        aprendido (delay_actual) repartido entre los workers: con 1 worker
        es la pausa de siempre, con 4 arranca una descarga cada cuarto de
        esa pausa. Antes el modo paralelo ignoraba por completo el ritmo
        aprendido (solo escalonaba 1.5-3 s al encolar), así que "Ritmo
        aprendido" y el estirar_ritmo del predictor no frenaban nada ahí.
        Con el ritmo mínimo (6 s) y 4 workers da 1.5 s: la misma velocidad
        de antes, pero ahora sube sola si aparece precaución."""
        separacion = self.delay_actual() / max(1, workers_activos)
        with self._lock_turno:
            ahora = time.time()
            mi_turno = max(ahora, self._proximo_turno)
            self._proximo_turno = mi_turno + separacion * random.uniform(0.85, 1.15)
        while True:
            falta = mi_turno - time.time()
            if falta <= 0 or (debe_abortar and debe_abortar()):
                return
            time.sleep(min(0.5, falta))
    def clasificar_error(self, error_msg):
        """Traduce el texto del error de yt-dlp a un tipo de problema.
        Orden: primero muerte segura del video (es definitivo), después
        las señales de bloqueo y por último las dudosas, que necesitan
        confirmarse dos veces antes de vetar un video (ver
        registrar_dudoso)."""
        e = (error_msg or "").lower()
        for señal in self.SENALES_MUERTE_SEGURA:
            if señal in e:
                return "irrecuperable"
        for señal in self.SENALES_EDAD:
            if señal in e:
                return "otro"
        for patron in self.SENALES_BLOQUEO:
            if re.search(patron, e):
                return "bloqueo"
        for señal in self.SENALES_DUDOSAS:
            if señal in e:
                return "dudoso"
        return "otro"
    def registrar_exito(self):
        with self._lock:
            self.errores_consecutivos_bloqueo = 0
            self.cooldowns_seguidos = 0
            self.exitos_consecutivos += 1
            self.buena_racha += 1
            if self.exitos_consecutivos >= self.EXITOS_PARA_BAJAR_NIVEL and self.nivel > 0:
                self.nivel -= 1
                self.exitos_consecutivos = 0
                self._log(f"📉 Anti-bloqueo: bajando nivel de precaución a {self.nivel}/{self.NIVEL_MAXIMO}")
            if self.buena_racha >= self.RACHA_PARA_ACELERAR and self.delay_base > self.DELAY_MINIMO:
                self.delay_base = max(self.DELAY_MINIMO, round(self.delay_base * 0.9, 1))
                self.buena_racha = 0
                self._log(f"🚀 Aprendizaje: todo viene bien, acelerando el ritmo base a {self.delay_base}s entre descargas")
        if self.predictor:
            self.predictor.registrar_evento("ok")
    def registrar_error(self, error_msg):
        tipo = self.clasificar_error(error_msg)
        if tipo == "irrecuperable":
            if self.predictor:
                self.predictor.registrar_evento("irrecuperable")
            return tipo
        primera_señal = False
        with self._lock:
            self.exitos_consecutivos = 0
            self.buena_racha = 0
            if tipo == "bloqueo":
                self.errores_consecutivos_bloqueo += 1
                if self.nivel < self.NIVEL_MAXIMO:
                    self.nivel += 1
                if self.delay_base < self.DELAY_MAXIMO:
                    self.delay_base = min(self.DELAY_MAXIMO, round(self.delay_base * 1.3, 1))
                self._log(f"⚠️ Anti-bloqueo: señal de bloqueo, subiendo nivel a {self.nivel}/{self.NIVEL_MAXIMO} y ritmo base a {self.delay_base}s", "warning")
                primera_señal = self.errores_consecutivos_bloqueo == 1
        if self.predictor:
            self.predictor.registrar_evento(tipo)
        if primera_señal:
            self.pausar_todos(self.PAUSA_PREVENTIVA, "primer aviso de bloqueo")
        return tipo
    def necesita_cooldown(self):
        with self._lock:
            return self.errores_consecutivos_bloqueo >= self.UMBRAL_COOLDOWN
    def duracion_cooldown(self):
        with self._lock:
            extra = min(self.errores_consecutivos_bloqueo - self.UMBRAL_COOLDOWN, 3)
            return int(min(self.COOLDOWN_BASE + extra * 30, self.COOLDOWN_MAX))
    def registrar_cooldown(self):
        """Cuenta cooldowns SEGUIDOS sin un solo éxito en el medio. Con 3,
        seguir golpeando solo empeora la reputación de la IP: el programa
        corta la tanda y lo avisa (ver _descargar_uno)."""
        with self._lock:
            self.cooldowns_seguidos += 1
            return self.cooldowns_seguidos
    def pausa_entre_pedidos(self):
        """Segundos que yt-dlp espera entre los pedidos internos de UNA
        descarga. Se adapta: con YouTube tranquilo (nivel 0, riesgo bajo y
        ningún bloqueo en 48 h) baja a 1 s para ganar velocidad; con
        cualquier precaución sube."""
        with self._lock:
            nivel = self.nivel
        if nivel > 0:
            return min(6, 2 + nivel)
        if self.predictor:
            try:
                score = self.predictor.riesgo_actual()["score"]
                if score >= 30:
                    return 3
                if score < 10 and self.predictor._cuenta("bloqueo", 48 * 60) == 0:
                    return 1
            except Exception:
                pass
        return 2
    def reset_cooldown(self):
        with self._lock:
            self.errores_consecutivos_bloqueo = 0
    def delay_actual(self):
        with self._lock:
            return round(self.delay_base * (self.FACTOR_ESCALADO ** self.nivel), 1)
    def estado_texto(self):
        if self.nivel == 0:
            return "🟢 Normal"
        elif self.nivel <= 2:
            return f"🟡 Precaución (nivel {self.nivel})"
        else:
            return f"🔴 Alerta (nivel {self.nivel})"
    # --- ANTICIPACIÓN: frenar antes de que YouTube conteste ---
    def pausar_todos(self, segundos, motivo=""):
        """Pausa GLOBAL: deja esperando a todos los hilos de descarga a la
        vez (ver _via_libre). Si ya hay una pausa en curso, no la pisa."""
        with self._lock_pausa:
            if not self._via_libre.is_set():
                return
            self._via_libre.clear()
            self._log(f"🛡️ Pausa preventiva GLOBAL de {segundos:.0f}s para todos los hilos "
                      f"({motivo}) - mejor frenar ahora que perder varias descargas", "warning")
            threading.Thread(target=self._terminar_pausa, args=(segundos,), daemon=True).start()
    def _terminar_pausa(self, segundos):
        try:
            time.sleep(segundos)
        finally:
            self._via_libre.set()
    def esperar_si_pausado(self, debe_abortar=None, timeout=1.0):
        """Se llama antes de cada intento: si hay una pausa global activa,
        espera acá (sin trabar el cierre del programa)."""
        while not self._via_libre.is_set():
            if debe_abortar and debe_abortar():
                return
            self._via_libre.wait(timeout=timeout)
    def subir_nivel_preventivo(self, motivo=""):
        """Sube la cautela ANTES de que falle nada, cuando el predictor ve
        riesgo alto (por ejemplo, si la sonda de salud ya vino mal)."""
        with self._lock:
            if self.nivel < self.NIVEL_MAXIMO:
                self.nivel += 1
            # Mismo tope que estirar_ritmo: la anticipación nunca pasa del
            # 50% de lo que valía el ritmo al abrir el programa.
            self.delay_base = max(self.delay_base, min(
                self.DELAY_MAXIMO, self.delay_tope_anticipado, round(self.delay_base * 1.15, 1)))
            self._log(f"🛡️ Anticipación: subo a nivel {self.nivel}/{self.NIVEL_MAXIMO} con ritmo "
                      f"{self.delay_base}s sin haber fallado todavía ({motivo})", "warning")
    def estirar_ritmo(self, factor=1.15):
        """Estira la pausa entre descargas sin tocar el nivel: es la
        reacción suave del predictor cuando el riesgo está en amarillo.
        Tiene tope: la anticipación puede alargar el ritmo hasta un 50% de
        lo que valía al abrir el programa, nunca más. Sin ese tope, con el
        predictor en amarillo cada descarga multiplicaba por 1.15 y en una
        sesión larga el ritmo pasaba de 12s a 35s sin que hubiera fallado
        nada (medido en una sesión real de 46 descargas)."""
        with self._lock:
            nuevo = min(self.DELAY_MAXIMO, self.delay_tope_anticipado,
                        round(self.delay_base * factor, 1))
            if nuevo > self.delay_base:
                self.delay_base = nuevo
    def guardar_en_memoria(self):
        # Solo persiste el nivel "de fondo" (0-2) para no arrancar la
        # próxima sesión ya en alerta por un problema puntual de hoy.
        self.memoria["nivel_base_aprendido"] = min(self.nivel, 2)
        self.memoria["delay_base_aprendido"] = self.delay_base
        self.memoria["buena_racha"] = self.buena_racha
        if self.predictor:
            self.predictor.guardar_en_memoria()
# =====================================================================
# CONCURRENCIA ADAPTATIVA PARA LA COLA PARALELA
#   El usuario elige "Simultáneas" como techo (cuántas descargas como
#   máximo quiere en paralelo), pero el número REAL de workers activos
#   se achica solo apenas el AntiBloqueoManager empieza a ver señales de
#   bloqueo - así la velocidad extra de la cola paralela nunca empeora
#   un bloqueo que ya está empezando. Se vuelve a abrir solo en cuanto
#   el nivel de precaución baja de nuevo (sin que el usuario haga nada).
# =====================================================================
class ControlConcurrenciaAdaptativo:
    def __init__(self, anti_bloqueo, maximo, consola=None, predictor=None):
        self.anti_bloqueo = anti_bloqueo
        self.maximo = maximo
        self.consola = consola
        self.predictor = predictor
        self._condicion = threading.Condition()
        self._activos = 0
        self._ultimo_limite_avisado = None
    def _limite_actual(self):
        nivel = self.anti_bloqueo.nivel
        if nivel >= 4:
            limite = 1
        elif nivel >= 2:
            limite = min(self.maximo, 2)
        else:
            limite = self.maximo
        # ANTICIPACIÓN: con riesgo alto se achica la concurrencia aunque no
        # haya fallado nada todavía (el nivel sigue en 0 porque el nivel
        # reacciona a fallos, no a riesgo).
        if self.predictor:
            try:
                score = self.predictor.riesgo_actual()["score"]
            except Exception:
                score = 0
            if score >= 60:
                limite = 1
            elif score >= 40:
                limite = min(limite, 2)
        if limite != self._ultimo_limite_avisado:
            self._ultimo_limite_avisado = limite
            if self.consola and limite < self.maximo:
                self.consola.warning(
                    f"🧠 Anti-bloqueo adaptativo: bajando a {limite} descarga(s) simultánea(s) "
                    f"(de {self.maximo}) por precaución - nivel {nivel}"
                )
            elif self.consola and limite == self.maximo and self._ultimo_limite_avisado is not None:
                pass
        return limite
    def adquirir(self):
        with self._condicion:
            while self._activos >= self._limite_actual():
                self._condicion.wait(timeout=2)
            self._activos += 1
    def liberar(self):
        with self._condicion:
            self._activos -= 1
            self._condicion.notify_all()
# =====================================================================
# APRENDIZAJE DE CLIENTES DE YOUTUBE Y CACHE DE VIDEOS IRRECUPERABLES
# =====================================================================
CLIENTES_YOUTUBE = ["android", "ios", "web_creator", "mweb", "web", "tv"]
# User-agents para los intentos SIN cookies (cuando sí hay cookies se usa
# el del navegador real, ver user_agent_para). Van lo más al día posible:
# un user-agent viejo es de las primeras cosas que delatan a un script.
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36',
]
# Cuántos fallos SIN un solo éxito hacen falta para dar un cliente por
# muerto. YouTube va retirando clientes con el tiempo: en la memoria de
# este equipo "ios" acumulaba 55 fallos y "web_creator" 52, con cero
# éxitos, y seguir probándolos gastaba intentos y disparaba falsas
# subidas de nivel (que cuestan pausas de hasta 4 minutos).
FALLOS_PARA_PODAR = 8
def _puntaje_cliente(stats_cliente):
    """Éxito ponderado por RECENCIA (EWMA). El promedio de por vida no
    alcanza: un cliente que el mes pasado andaba bien y esta semana falla
    tiene que perder el primer puesto, y eso solo se ve mirando lo último
    que pasó."""
    s = stats_cliente or {}
    total = s.get("exitos", 0) + s.get("fallos", 0)
    if not total:
        return 0.55  # sin datos: neutral, se sigue probando
    ewma = s.get("ewma")
    if ewma is None:
        ewma = s.get("exitos", 0) / total
    # Si hace mucho que no se lo prueba, se lo relaja hacia el medio:
    # YouTube cambia seguido y una foto vieja no debería decidir sola.
    try:
        ultima = s.get("ultima_actualizacion")
        if ultima:
            dias = (datetime.now() - datetime.fromisoformat(ultima)).total_seconds() / 86400
            if dias > 7:
                ewma = (ewma + 0.5) / 2
    except Exception:
        pass
    return ewma
def _cliente_podado(stats_cliente):
    s = stats_cliente or {}
    return s.get("fallos", 0) >= FALLOS_PARA_PODAR and s.get("exitos", 0) == 0
def ranking_clientes(memoria):
    """Ordena los clientes por éxito reciente (ver _puntaje_cliente). Los
    que ya se dieron por muertos quedan AL FINAL de la lista: así los
    intentos normales no los gastan, pero el último intento permisivo
    (que ya es gratis porque se probó todo lo demás) los sigue sondeando
    por si alguna actualización de yt-dlp los revivió."""
    stats = memoria.setdefault("client_stats", {})
    ordenados = sorted(CLIENTES_YOUTUBE, key=lambda c: _puntaje_cliente(stats.get(c)), reverse=True)
    vivos = [c for c in ordenados if not _cliente_podado(stats.get(c))]
    muertos = [c for c in ordenados if _cliente_podado(stats.get(c))]
    memoria["clientes_podados"] = muertos
    return vivos + muertos
def registrar_resultado_cliente(memoria, clientes, exito):
    stats = memoria.setdefault("client_stats", {})
    ahora = datetime.now().isoformat()
    for c in clientes:
        s = stats.setdefault(c, {"exitos": 0, "fallos": 0})
        if exito:
            s["exitos"] += 1
        else:
            s["fallos"] += 1
        # EWMA con peso 0.2 hacia el último resultado: el ranking reacciona
        # a lo que pasa HOY en vez de arrastrar un promedio viejo.
        base = s.get("ewma")
        if base is None:
            total = s["exitos"] + s["fallos"]
            base = (s["exitos"] / total) if total else 0.5
        s["ewma"] = round(base * 0.8 + (1.0 if exito else 0.0) * 0.2, 4)
        s["ultima_actualizacion"] = ahora
# =====================================================================
# IMPERSONATE (huella TLS de navegador real)
#   YouTube no solo mira el user-agent: también mira cómo se firma el
#   handshake TLS/HTTP2. curl_cffi puede imitar la firma exacta de un
#   navegador real, y yt-dlp lo usa cuando se le pasa un objetivo de
#   "impersonate". Con eso, el pedido deja de parecer el de una librería
#   de Python. Si curl_cffi no está instalado (o la versión de yt-dlp no
#   lo soporta) simplemente no se usa y todo sigue funcionando igual: el
#   programa nunca depende de esto para descargar.
# =====================================================================
_IMPERSONATE_APAGADO = False
_IMPERSONATE_CACHE = {}
def _objetivos_disponibles():
    """Lista de objetivos de impersonate que soportan curl_cffi + esta
    versión de yt-dlp (el nombre de la clase cambió entre versiones)."""
    import curl_cffi  # noqa: F401  (es la librería que firma el handshake)
    from yt_dlp.networking import _curlcffi
    handler = (getattr(_curlcffi, "CurlCFFIRH", None)
               or getattr(_curlcffi, "CurlCFFIImpersonateRequestHandler", None))
    if handler is None:
        return []
    soporte = getattr(handler, "supported_targets", ())
    return list(soporte() if callable(soporte) else soporte)
def _version_objetivo(objetivo):
    try:
        return float(objetivo.version or 0)
    except Exception:
        return 0.0
def target_impersonate(navegador=None):
    """Devuelve el objetivo de impersonate COHERENTE con las cookies que se
    mandan, o None si no se puede.
    - Con cookies de Firefox: un objetivo de Firefox (el más nuevo). Antes
      se firmaba como Chrome mientras se mandaban cookies de Firefox, que
      es justo la incoherencia que YouTube castiga. Si esta versión de
      curl_cffi/yt-dlp no trae ninguno de Firefox, devuelve None: es
      mejor no suplantar y mandar el user-agent real de Firefox (ver
      user_agent_para) que mezclar identidades.
    - Sin cookies: Chrome (el que curl_cffi mantiene más al día).
    Se resuelve una sola vez por sesión y por navegador."""
    if _IMPERSONATE_APAGADO:
        return None
    clave = "firefox" if navegador == "firefox" else "chrome"
    if clave in _IMPERSONATE_CACHE:
        return _IMPERSONATE_CACHE[clave]
    elegido = None
    try:
        disponibles = _objetivos_disponibles()
        candidatos = [o for o in disponibles if getattr(o, "client", None) == clave]
        if candidatos:
            elegido = max(candidatos, key=_version_objetivo)
        elif clave == "chrome" and disponibles:
            elegido = disponibles[-1]
    except Exception:
        elegido = None
    _IMPERSONATE_CACHE[clave] = elegido
    return elegido
def desactivar_impersonate():
    """Apaga impersonate por el resto de la sesión (se usa cuando falló en
    esta máquina, para no volver a intentarlo en cada descarga)."""
    global _IMPERSONATE_APAGADO
    _IMPERSONATE_APAGADO = True
def _aplicar_antisuplantacion(opts, navegador=None):
    """Le da identidad al pedido: impersonate si se puede, y si no el
    user-agent coherente con el navegador del que salen las cookies (ver
    user_agent_para). Con impersonate NO se fuerza user-agent: lo pone la
    propia librería, así la huella y el user-agent coinciden entre sí.
    Devuelve True si quedó impersonando."""
    objetivo = target_impersonate(navegador)
    if objetivo is not None:
        opts["impersonate"] = objetivo
        return True
    opts["user_agent"] = user_agent_para(navegador)
    return False
# =====================================================================
# MEMORIA DE DESCARGAS (no volver a pedir lo que ya está bajado)
#   Guarda qué videos ya se bajaron y con qué nombre de archivo, en
#   descargas_hechas.json. Antes de intentar algo, se fija si el archivo
#   sigue estando: si está, no le pide NADA a YouTube (ni metadata) - que
#   es el mayor ahorro de pedidos que hay. Si el archivo ya no está (lo
#   borraste a mano), se vuelve a bajar sin que tengas que hacer nada.
# =====================================================================
_DESCARGAS_HECHAS = None
_lock_descargas = threading.Lock()
def ruta_descargas_hechas():
    return os.path.join(ruta_base(), "descargas_hechas.json")
def cargar_descargas_hechas(forzar=False):
    """{id_de_video: {"archivo": nombre, "fecha": iso}}. Se mantiene en
    memoria y se relee del disco solo la primera vez (o si se pide)."""
    global _DESCARGAS_HECHAS
    with _lock_descargas:
        if _DESCARGAS_HECHAS is not None and not forzar:
            return _DESCARGAS_HECHAS
        datos = {}
        archivo = ruta_descargas_hechas()
        if os.path.exists(archivo):
            try:
                with open(archivo, "r", encoding="utf-8") as f:
                    cargado = json.load(f)
                if isinstance(cargado, dict):
                    datos = cargado
            except Exception:
                datos = {}
        _DESCARGAS_HECHAS = datos
        return datos
def guardar_descargas_hechas(datos=None):
    """Escribe el archivo de forma atómica (primero un temporal y después
    os.replace), para que un corte a mitad de la escritura no lo rompa."""
    global _DESCARGAS_HECHAS
    with _lock_descargas:
        if datos is None:
            datos = _DESCARGAS_HECHAS or {}
        if len(datos) > 5000:
            ordenados = sorted(datos.items(),
                               key=lambda kv: (kv[1] or {}).get("fecha") or "", reverse=True)
            datos = dict(ordenados[:5000])
        _DESCARGAS_HECHAS = datos
        archivo = ruta_descargas_hechas()
        temporal = archivo + ".tmp"
        try:
            with open(temporal, "w", encoding="utf-8") as f:
                json.dump(datos, f, indent=1, ensure_ascii=False)
            os.replace(temporal, archivo)
            return True
        except Exception:
            return False
def registrar_descarga_hecha(video_id, nombre_archivo):
    if not video_id:
        return
    datos = cargar_descargas_hechas()
    with _lock_descargas:
        datos[video_id] = {"archivo": nombre_archivo or "", "fecha": datetime.now().isoformat()}
    guardar_descargas_hechas(datos)
def ya_descargado(video_id, carpeta):
    """Devuelve el nombre del archivo si ese video ya está bajado y el
    archivo sigue estando en la carpeta; si no, None."""
    if not video_id:
        return None
    ficha = cargar_descargas_hechas().get(video_id) or {}
    nombre = ficha.get("archivo") or ""
    if not nombre:
        return None
    if os.path.exists(os.path.join(carpeta, nombre)):
        return nombre
    # El post-procesado renombra el archivo (le agrega el BPM adelante), así
    # que si el nombre exacto ya no está, se busca por el nombre sin la
    # extensión: si aparece en algún archivo de la carpeta, el tema está.
    base = os.path.splitext(nombre)[0]
    if not base:
        return None
    try:
        for actual in os.listdir(carpeta):
            if base in actual:
                return actual
    except Exception:
        pass
    return None
# =====================================================================
# TEMPORALES DE LA NORMALIZACIÓN
#   La normalización escribe el resultado en un archivo temporal al lado
#   del tema (para que el reemplazo sea instantáneo) y después lo borra.
#   Si el programa se cierra a la fuerza en medio de una normalización, ese
#   temporal queda tirado en la carpeta de la música: por eso ahora lleva
#   un nombre reconocible y, cuando volvés a abrir esa carpeta, se limpia.
# =====================================================================
PREFIJO_TEMPORAL = ".dsh_normalizando_"
_PATRON_TEMPORAL_VIEJO = re.compile(
    r"^tmp[A-Za-z0-9_]{8}\.(mp3|wav|flac|m4a|ogg|aac|wma|opus)$", re.IGNORECASE)
def _limpiar_temporales_huerfanos(carpeta, minutos_viejo=60):
    """Borra los temporales que dejó una normalización interrumpida a la
    fuerza. Devuelve la lista de nombres borrados."""
    borrados = []
    ahora = time.time()
    try:
        nombres = os.listdir(carpeta)
    except Exception:
        return borrados
    for nombre in nombres:
        ruta = os.path.join(carpeta, nombre)
        if not os.path.isfile(ruta):
            continue
        try:
            edad_min = (ahora - os.path.getmtime(ruta)) / 60
        except Exception:
            continue
        propio = nombre.startswith(PREFIJO_TEMPORAL)
        # Los del prefijo nuevo son del programa sin ninguna duda; los del
        # formato viejo (mkstemp sin prefijo) solo se tocan si ya son viejos,
        # para no borrar nada que se esté escribiendo ahora mismo.
        if propio and edad_min < 5:
            continue
        if not propio and not _PATRON_TEMPORAL_VIEJO.match(nombre):
            continue
        if not propio and edad_min < minutos_viejo:
            continue
        try:
            os.remove(ruta)
            borrados.append(nombre)
        except Exception:
            pass
    return borrados
_PATRON_PARCIAL = re.compile(
    r"(\.part(-Frag\d+)?|\.ytdl|\.temp(\.\w+)?|\.f\d+\.\w+)$", re.IGNORECASE)
def limpiar_parciales(carpetas):
    """Borra lo que deja una descarga cortada: archivos .part/.ytdl,
    temporales de conversión, fragmentos .fNNN, los temporales propios de la
    normalización y mp3 vacíos. Se llama al cerrar el programa. Si algún
    archivo sigue abierto por un hilo (Windows no deja borrarlo) se ignora."""
    borrados = []
    for carpeta in set(c for c in carpetas if c):
        try:
            nombres = os.listdir(carpeta)
        except Exception:
            continue
        for nombre in nombres:
            ruta = os.path.join(carpeta, nombre)
            try:
                if not os.path.isfile(ruta):
                    continue
                vacio = nombre.lower().endswith(".mp3") and os.path.getsize(ruta) == 0
                if (nombre.startswith(PREFIJO_TEMPORAL) or _PATRON_PARCIAL.search(nombre) or vacio):
                    os.remove(ruta)
                    borrados.append(nombre)
            except Exception:
                pass
    return borrados
def user_agent_para(navegador=None):
    """User-agent COHERENTE con el navegador del que salen las cookies.
    Rotar un user-agent de Chrome al azar mientras se mandan cookies de
    Firefox es una incoherencia que YouTube castiga con el "sign in to
    confirm you're not a bot": lo que dice el user-agent tiene que
    coincidir con las cookies que se mandan."""
    if navegador == "firefox":
        version = _version_firefox_instalado() or "142.0"
        return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{version}) "
                f"Gecko/20100101 Firefox/{version}")
    return random.choice(USER_AGENTS)
# Un video vetado se vuelve a intentar después de este plazo: un
# copyright que después liberan, un estreno que ya salió o un bloqueo
# temporal no tienen por qué dejar el video vetado para siempre (antes
# el veto era eterno, y encima se aplicaba a errores que no eran
# definitivos).
TTL_IRRECUPERABLE_DIAS = 30
def _fichas_irrecuperables(memoria):
    lista = memoria.setdefault("videos_irrecuperables", [])
    # Compatibilidad con memorias viejas, que guardaban el id suelto
    # como texto en vez de una ficha con fecha y confirmaciones. Se
    # respeta lo que decidió la versión anterior (definitivo = True),
    # pero ahora igual caduca a los 30 días.
    if lista and isinstance(lista[0], str):
        ahora = datetime.now().isoformat()
        lista = [{"id": v, "fecha": ahora, "motivo": "formato viejo",
                  "confirmaciones": 1, "definitivo": True}
                 for v in lista]
        memoria["videos_irrecuperables"] = lista
    return lista
def es_irrecuperable_conocido(memoria, video_id):
    """True solo si el video sigue dentro de su plazo de veto. Los vetos
    de un error DEFINITIVO valen con una sola vez; los de un error
    DUDOSO necesitan dos confirmaciones (ver registrar_dudoso). De paso
    limpia los vetos vencidos: así un video vetado por error (o que dejó
    de estar bloqueado) vuelve a intentarse solo, sin tocar nada."""
    lista = _fichas_irrecuperables(memoria)
    limite = datetime.now() - timedelta(days=TTL_IRRECUPERABLE_DIAS)
    vigentes = []
    encontrado = False
    for ficha in lista:
        try:
            fecha = datetime.fromisoformat(ficha.get("fecha"))
        except Exception:
            fecha = datetime.now()
        if fecha < limite:
            continue
        vigentes.append(ficha)
        if ficha.get("id") == video_id:
            if ficha.get("definitivo") or int(ficha.get("confirmaciones", 1)) >= 2:
                encontrado = True
    if len(vigentes) != len(lista):
        memoria["videos_irrecuperables"] = vigentes
    return encontrado
def registrar_irrecuperable(memoria, video_id, motivo=""):
    lista = _fichas_irrecuperables(memoria)
    for ficha in lista:
        if ficha.get("id") == video_id:
            ficha["confirmaciones"] = int(ficha.get("confirmaciones", 1)) + 1
            ficha["fecha"] = datetime.now().isoformat()
            ficha["definitivo"] = True
            return
    lista.append({"id": video_id, "fecha": datetime.now().isoformat(),
                  "motivo": (motivo or "")[:120], "confirmaciones": 1, "definitivo": True})
    if len(lista) > 2000:
        del lista[:len(lista) - 2000]
def registrar_dudoso(memoria, video_id, motivo=""):
    """Un fallo DUDOSO (no se sabe si el video está muerto o si el intento
    no sirvió, por ejemplo "unable to extract") no veta nada al primer
    intento: recién al segundo fallo igual se lo da por no disponible.
    Devuelve True si el video quedó vetado."""
    lista = _fichas_irrecuperables(memoria)
    for ficha in lista:
        if ficha.get("id") == video_id:
            ficha["confirmaciones"] = int(ficha.get("confirmaciones", 1)) + 1
            ficha["fecha"] = datetime.now().isoformat()
            return ficha.get("definitivo") or ficha["confirmaciones"] >= 2
    lista.append({"id": video_id, "fecha": datetime.now().isoformat(),
                  "motivo": (motivo or "")[:120], "confirmaciones": 1, "definitivo": False})
    return False
# =====================================================================
# COOKIES DE FIREFOX 100% AUTOMÁTICAS
#   El usuario no tiene que hacer nada: el programa encuentra solo el
#   perfil de Firefox con la sesión de YouTube, lee sus cookies AUNQUE
#   Firefox esté abierto (copia cookies.sqlite junto con su archivo -wal,
#   donde Firefox guarda lo más reciente) y las entrega a yt-dlp en un
#   archivo temporal. Antes, con Firefox abierto el programa descartaba
#   las cookies en silencio y descargaba "pelado" (mucho más riesgo de
#   bloqueo). Si Firefox no tiene sesión de YouTube, lo abre en la página
#   de inicio de sesión (una sola vez) y espera; si las cookies están
#   viejas, las renueva abriendo Firefox en segundo plano (ver
#   DownloaderIA._hilo_sesion_firefox).
# =====================================================================
_COOKIES_SESION = ("SAPISID", "__Secure-3PSID", "__Secure-1PSID", "LOGIN_INFO", "SID")
_cache_cookies_firefox = {"hora": 0.0, "filas": None, "perfil": None}
_lock_cookies_firefox = threading.Lock()
def _perfiles_firefox():
    """Perfiles de Firefox que tienen cookies.sqlite, el de uso más reciente primero."""
    base = os.environ.get("APPDATA")
    if not base:
        return []
    raiz = os.path.join(base, "Mozilla", "Firefox")
    if not os.path.isdir(raiz):
        return []
    perfiles = []
    try:
        import configparser
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(os.path.join(raiz, "profiles.ini"), encoding="utf-8")
        for sec in cp.sections():
            if sec.startswith("Install"):
                ruta, relativo = cp.get(sec, "Default", fallback=None), True
            elif sec.startswith("Profile"):
                ruta, relativo = cp.get(sec, "Path", fallback=None), cp.get(sec, "IsRelative", fallback="1") == "1"
            else:
                continue
            if ruta:
                perfiles.append(os.path.normpath(os.path.join(raiz, ruta) if relativo else ruta))
    except Exception:
        pass
    carpeta = os.path.join(raiz, "Profiles")
    if os.path.isdir(carpeta):
        for n in os.listdir(carpeta):
            perfiles.append(os.path.join(carpeta, n))
    unicos = []
    for pf in perfiles:
        if pf not in unicos and os.path.exists(os.path.join(pf, "cookies.sqlite")):
            unicos.append(pf)
    def _reciente(pf):
        mejor = 0.0
        for suf in ("", "-wal"):
            try:
                mejor = max(mejor, os.path.getmtime(os.path.join(pf, "cookies.sqlite" + suf)))
            except OSError:
                pass
        return mejor
    unicos.sort(key=_reciente, reverse=True)
    return unicos
def _leer_cookies_firefox(perfil):
    """Lee las cookies de YouTube/Google de un perfil, desde una copia
    temporal (no molesta a Firefox ni depende de que esté cerrado)."""
    import sqlite3
    import shutil
    tmp = tempfile.mkdtemp(prefix="ytcookies_")
    try:
        for suf in ("", "-wal", "-shm"):
            origen = os.path.join(perfil, "cookies.sqlite" + suf)
            if os.path.exists(origen):
                shutil.copy2(origen, os.path.join(tmp, "cookies.sqlite" + suf))
        con = sqlite3.connect(os.path.join(tmp, "cookies.sqlite"))
        try:
            consulta = ("SELECT host, path, isSecure, expiry, name, value, isHttpOnly FROM moz_cookies "
                        "WHERE (host LIKE '%youtube.com' OR host LIKE '%google.com' "
                        "OR host LIKE '%googlevideo.com' OR host LIKE '%ytimg.com')")
            try:
                return con.execute(consulta + " AND (originAttributes IS NULL OR originAttributes = '')").fetchall()
            except Exception:
                return con.execute(consulta).fetchall()
        finally:
            con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
def _filas_cookies_sesion(max_edad_s=60):
    """Cookies del perfil de Firefox que tiene la sesión de YouTube (con un
    caché corto, para no copiar la base en cada descarga). None si ningún
    perfil tiene sesión iniciada."""
    with _lock_cookies_firefox:
        if time.time() - _cache_cookies_firefox["hora"] < max_edad_s:
            return _cache_cookies_firefox["filas"]
        elegido = None
        for perfil in _perfiles_firefox():
            try:
                filas = _leer_cookies_firefox(perfil)
            except Exception:
                continue
            if any(f[4] in _COOKIES_SESION for f in filas):
                elegido = filas
                break
        _cache_cookies_firefox.update({"hora": time.time(), "filas": elegido})
        return elegido
def invalidar_cache_cookies_firefox():
    with _lock_cookies_firefox:
        _cache_cookies_firefox["hora"] = 0.0
def firefox_tiene_sesion_youtube():
    return bool(_filas_cookies_sesion(max_edad_s=0))
def exportar_cookies_firefox():
    """Escribe las cookies en un archivo temporal formato Netscape (el que
    entiende yt-dlp) y devuelve su ruta; None si no hay sesión."""
    filas = _filas_cookies_sesion()
    if not filas:
        return None
    fd, ruta = tempfile.mkstemp(prefix="ytcookies_", suffix=".txt")
    ahora = int(time.time())
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("# Netscape HTTP Cookie File\n")
        for host, path, seguro, expiry, nombre, valor, solo_http in filas:
            exp = int(expiry or 0)
            if exp > 10**11:          # algunas versiones guardan milisegundos
                exp //= 1000
            if exp == 0:              # cookie de sesión: vence "mañana" para que no se descarte
                exp = ahora + 86400
            elif exp <= ahora:        # ya vencida
                continue
            f.write("\t".join([("#HttpOnly_" if solo_http else "") + host,
                               "TRUE" if host.startswith(".") else "FALSE", path or "/",
                               "TRUE" if seguro else "FALSE", str(exp), nombre, valor]) + "\n")
    return ruta
def aplicar_cookies(ydl_opts, navegador):
    """Le pone las cookies a las opciones de yt-dlp. Para Firefox usa la
    copia propia (funciona con Firefox abierto) y, si no se puede, cae al
    método de yt-dlp. Devuelve la ruta del archivo temporal a borrar luego
    (o None)."""
    if navegador == "firefox":
        try:
            ruta = exportar_cookies_firefox()
        except Exception:
            ruta = None
        if ruta:
            ydl_opts["cookiefile"] = ruta
            return ruta
    ydl_opts["cookiesfrombrowser"] = (navegador,)
    return None
def borrar_temporal(ruta):
    if ruta:
        try:
            os.remove(ruta)
        except OSError:
            pass
def navegadores_disponibles_para_cookies(memoria):
    ff = ruta_firefox()
    if not ff:
        return []
    # Con Firefox abierto antes no se usaban cookies. Ahora sí, mientras se
    # puedan leer desde el perfil (ver aplicar_cookies).
    if navegador_esta_corriendo("firefox") and not _perfiles_firefox():
        return []
    return ["firefox"]
def registrar_navegador_efectivo(memoria, navegador):
    if navegador and memoria.get("cookies_navegador_efectivo") != navegador:
        memoria["cookies_navegador_efectivo"] = navegador
# =====================================================================
# INSTALACIÓN OFFLINE DE DEPENDENCIAS (16/09/2026, a pedido de Gustavo)
# La lista vive en config/dependencias_youtube.py (compartida con
# descargar_dependencias_youtube.py, para que no se desincronicen) y se
# carga por ruta directa porque config/ no es carpeta hermana de este
# archivo. Al instalar algo que falte, primero se intenta SIN internet
# desde la carpeta "dependencias_offline" (si existe y tiene algo
# adentro); si eso falla o la carpeta no está, recién ahí se cae al pip
# install de siempre con internet -- así nunca se rompe el arranque si
# la carpeta offline no está armada o está incompleta.
# =====================================================================
CREATIONFLAGS = 0x08000000 if os.name == "nt" else 0
def _cargar_lista_dependencias():
    ruta = os.path.join(ruta_base(), "config", "dependencias_youtube.py")
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("dependencias_youtube", ruta)
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)
        return modulo.DEPENDENCIAS
    except Exception:
        return None
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
CARPETA_DEPENDENCIAS_OFFLINE = os.path.join(ruta_base(), "dependencias_offline")
def _intentar_instalar(nombre):
    hay_carpeta_offline = (
        os.path.isdir(CARPETA_DEPENDENCIAS_OFFLINE)
        and len(os.listdir(CARPETA_DEPENDENCIAS_OFFLINE)) > 0)
    if hay_carpeta_offline:
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--quiet",
                 "--no-index", "--find-links", CARPETA_DEPENDENCIAS_OFFLINE, nombre],
                creationflags=CREATIONFLAGS)
            return "offline"
        except subprocess.CalledProcessError:
            pass
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", nombre],
            creationflags=CREATIONFLAGS)
        return "online"
    except subprocess.CalledProcessError:
        return None
# =====================================================================
# yt-dlp AL DÍA
#   La causa Nº1 de descargas que "de golpe" dejan de andar es un yt-dlp
#   viejo: YouTube cambia seguido y cada versión nueva trae el arreglo.
#   Su versión es una fecha (2026.08.19), así que se sabe cuántos días
#   tiene sin importarlo. Si pasan de MAX_DIAS se intenta actualizar UNA
#   vez por día, siempre ANTES de importarlo (así no se mezclan módulos
#   viejos y nuevos en memoria). Sin internet no pasa nada: se sigue con
#   la versión instalada.
# =====================================================================
def dias_desde_version_ytdlp():
    try:
        from importlib.metadata import version
        m = re.match(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", version("yt-dlp"))
        if not m:
            return None
        return (datetime.now() - datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))).days
    except Exception:
        return None
def actualizar_ytdlp_si_esta_vieja(max_dias=14, cada_horas=24):
    """Devuelve (dias, resultado) si intentó actualizar ("ok"/"fallo"), o
    None si no hacía falta o ya se intentó hoy."""
    dias = dias_desde_version_ytdlp()
    if dias is None or dias < max_dias:
        return None
    marca = ruta_config("ultimo_chequeo_ytdlp.txt")
    try:
        if os.path.exists(marca) and (time.time() - os.path.getmtime(marca)) < cada_horas * 3600:
            return None
        with open(marca, "w", encoding="utf-8") as f:
            f.write(datetime.now().isoformat())
    except Exception:
        return None
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade",
             "--retries", "1", "--timeout", "15", "yt-dlp"],
            capture_output=True, timeout=180,
            creationflags=0x08000000 if os.name == "nt" else 0)
        return (dias, "ok" if r.returncode == 0 else "fallo")
    except Exception:
        return (dias, "fallo")
# =====================================================================
# VENTANA DE VERIFICACIÓN DE DEPENDENCIAS
# (estructura según referencia interna de VentanaVerificacion)
# =====================================================================
class VentanaVerificacion:
    def __init__(self):
        self.config_data = cargar_config_app()
        self._pending_after_ids = []
        self.labels_estado = {}
        self._dependencias_con_error = []
        self._firefox_listo = False
        self.ventana = tk.Tk()
        self.ventana.title("🔍 Verificación de dependencias")
        self.ventana.resizable(False, False)
        self.ventana.configure(bg="#f8f9fa")
        self.ventana.attributes('-topmost', True)
        self.ventana.protocol("WM_DELETE_WINDOW", self.cerrar_ventana)
        self._construir_ui()
        # El alto se calcula según lo que realmente ocupa el contenido (no un
        # número fijo), así no queda espacio en blanco de más abajo del
        # botón "Iniciar aplicación" ni se corta nada si el contenido crece.
        self._centrar()
        self.ventana.after(300, self._iniciar_verificacion)
        self.ventana.mainloop()
    def _centrar(self):
        w = 400  # ancho fijo de la ventana
        self.ventana.update_idletasks()
        h = self.ventana.winfo_reqheight() + 16  # margen inferior
        sw = self.ventana.winfo_screenwidth()
        sh = self.ventana.winfo_screenheight()
        x = (sw // 2) - (w // 2)
        y = (sh // 2) - (h // 2)
        self.ventana.geometry(f"{w}x{h}+{x}+{y}")
    def _programar(self, ms, fn):
        aid = self.ventana.after(ms, fn)
        self._pending_after_ids.append(aid)
        return aid
    def _cancelar_pendientes(self):
        for aid in self._pending_after_ids:
            try:
                self.ventana.after_cancel(aid)
            except Exception:
                pass
        self._pending_after_ids.clear()
    def _config_seguro(self, widget, **kwargs):
        try:
            if widget.winfo_exists():
                widget.config(**kwargs)
        except Exception:
            pass
    def _construir_ui(self):
        tk.Label(self.ventana, text="📦 Verificando dependencias", font=("Segoe UI", 14, "bold"),
                  bg="#f8f9fa", fg="#2c3e50").pack(pady=(18, 2))
        tk.Label(self.ventana, text="Asegurando que todo esté listo para comenzar", font=("Segoe UI", 9),
                  bg="#f8f9fa", fg="#7f8c8d").pack(pady=(0, 14))
        self.frame_lista = tk.Frame(self.ventana, bg="#f8f9fa")
        self.frame_lista.pack(fill="x", padx=30)
        for nombre, _import in DEPENDENCIAS:
            fila = tk.Frame(self.frame_lista, bg="#f8f9fa")
            fila.pack(fill="x", pady=3)
            tk.Label(fila, text=nombre, font=("Segoe UI", 10, "bold"), width=14, anchor="w",
                      bg="#f8f9fa", fg="#2c3e50").pack(side="left")
            lbl = tk.Label(fila, text="⏳ Pendiente", font=("Segoe UI", 10), bg="#f8f9fa", fg="#7f8c8d")
            lbl.pack(side="left")
            self.labels_estado[nombre] = lbl
        fila_ff = tk.Frame(self.frame_lista, bg="#f8f9fa")
        fila_ff.pack(fill="x", pady=3)
        tk.Label(fila_ff, text="Firefox (opcional)", font=("Segoe UI", 10, "bold"), width=14, anchor="w",
                  bg="#f8f9fa", fg="#2c3e50").pack(side="left")
        self.lbl_estado_firefox = tk.Label(fila_ff, text="⏳ Verificando...", font=("Segoe UI", 10),
                                            bg="#f8f9fa", fg="#7f8c8d")
        self.lbl_estado_firefox.pack(side="left")
        self.progress_bar = ttk.Progressbar(self.ventana, mode="determinate", length=380)
        self.progress_bar.pack(pady=14)
        self.lbl_estado_general = tk.Label(self.ventana, text="🔍 Preparando verificación...",
                                            font=("Segoe UI", 9), bg="#f8f9fa", fg="#34495e")
        self.lbl_estado_general.pack(pady=(0, 8))
        # --- BOTONES EN LA MISMA LÍNEA ---
        frame_botones = tk.Frame(self.ventana, bg="#f8f9fa")
        frame_botones.pack(pady=14)
        self.btn_iniciar = tk.Button(frame_botones, text="🚀 Iniciar aplicación", command=self.iniciar_app,
                                       bg="#95a5a6", fg="white", font=("Segoe UI", 11, "bold"), relief="flat",
                                       padx=10, pady=8, state="disabled")
        self.btn_iniciar.pack(side="left", padx=(0, 10))
        self.btn_instalar_firefox = tk.Button(frame_botones, text="🦊 Instalar Firefox automáticamente",
                                               command=self.instalar_firefox, bg="#e67e22", fg="white",
                                               font=("Segoe UI", 9, "bold"), relief="flat", padx=8, pady=4)
        self.btn_instalar_firefox.pack(side="left")
    def instalar_firefox(self):
        self._config_seguro(self.btn_instalar_firefox, state="disabled", text="Instalando...")
        threading.Thread(target=self._hilo_instalar_firefox, daemon=True).start()
    def _hilo_instalar_firefox(self):
        def estado(msg):
            self._programar(0, lambda: self._config_seguro(self.lbl_estado_firefox, text=f"⏳ {msg}"))
        ok = instalar_firefox_silencioso(callback_estado=estado)
        def _final():
            if ok:
                self._firefox_listo = True
                self._config_seguro(self.lbl_estado_firefox, text="✅ Instalado", fg="#27ae60")
                self._config_seguro(self.btn_instalar_firefox, text="🦊 Instalado", state="disabled")
                self._config_seguro(self.lbl_estado_general, text="✅ Todo listo para comenzar", fg="#27ae60")
                if not self._dependencias_con_error:
                    self._programar(700, self.iniciar_app)
            else:
                self._config_seguro(self.lbl_estado_firefox, text="❌ Error al instalar", fg="#e74c3c")
                self._config_seguro(self.lbl_estado_general,
                                    text="⚠️ No se pudo instalar Firefox: reintentá con el botón", fg="#e74c3c")
                self._config_seguro(self.btn_instalar_firefox, state="normal", text="🦊 Reintentar instalación")
        self._programar(0, _final)
    def _iniciar_verificacion(self):
        threading.Thread(target=self._hilo_verificacion, daemon=True).start()
    def _hilo_verificacion(self):
        total = len(DEPENDENCIAS)
        # yt-dlp viejo = descargas que fallan de golpe: se actualiza ANTES
        # de importarlo (ver actualizar_ytdlp_si_esta_vieja).
        self._programar(0, lambda: self._config_seguro(
            self.lbl_estado_general, text="🔍 Revisando la versión de yt-dlp..."))
        resultado_ytdlp = actualizar_ytdlp_si_esta_vieja()
        if resultado_ytdlp:
            msg = ("⬆️ yt-dlp actualizado" if resultado_ytdlp[1] == "ok"
                   else "⚠️ yt-dlp viejo y no se pudo actualizar (sin internet?)")
            self._programar(0, lambda m=msg: self._config_seguro(self.lbl_estado_general, text=m))
        for idx, (nombre, import_name) in enumerate(DEPENDENCIAS):
            self._programar(0, lambda n=nombre: self._config_seguro(
                self.lbl_estado_general, text=f"🔍 Verificando {n}..."))
            self._programar(0, lambda n=nombre: self._config_seguro(
                self.labels_estado[n], text="🔍 Verificando...", fg="#f39c12"))
            try:
                __import__(import_name)
                self._programar(0, lambda n=nombre: self._config_seguro(
                    self.labels_estado[n], text="✅ Instalado", fg="#27ae60"))
            except ImportError:
                self._programar(0, lambda n=nombre: self._config_seguro(
                    self.labels_estado[n], text="⏳ Instalando...", fg="#f39c12"))
                self._programar(0, lambda n=nombre: self._config_seguro(
                    self.lbl_estado_general, text=f"📦 Instalando {n}..."))
                origen = _intentar_instalar(nombre)
                if origen == "offline":
                    self._programar(0, lambda n=nombre: self._config_seguro(
                        self.labels_estado[n], text="✅ Instalado (sin internet)", fg="#27ae60"))
                elif origen == "online":
                    self._programar(0, lambda n=nombre: self._config_seguro(
                        self.labels_estado[n], text="✅ Instalado", fg="#27ae60"))
                else:
                    self._dependencias_con_error.append(nombre)
                    self._programar(0, lambda n=nombre: self._config_seguro(
                        self.labels_estado[n], text="❌ Error", fg="#e74c3c"))
            pct = ((idx + 1) / total) * 100
            self._programar(0, lambda p=pct: self._config_seguro(self.progress_bar, value=p))
        firefox_instalado = ruta_firefox() is not None
        self._firefox_listo = firefox_instalado
        if firefox_instalado:
            self._programar(0, lambda: self._config_seguro(self.lbl_estado_firefox, text="✅ Instalado", fg="#27ae60"))
            self._programar(0, lambda: self._config_seguro(self.btn_instalar_firefox, text="🦊 Instalado", state="disabled"))
        else:
            self._programar(0, lambda: self._config_seguro(self.lbl_estado_firefox, text="❌ No instalado", fg="#e74c3c"))
            # Se instala SOLO (sin esperar el click). Si falla, el botón
            # queda habilitado para reintentar (ver _hilo_instalar_firefox).
            self._programar(0, self.instalar_firefox)
        self._programar(0, self._verificacion_completa)
    def _verificacion_completa(self):
        if not self._firefox_listo:
            self._config_seguro(self.lbl_estado_general, text="🦊 Instalando Firefox automáticamente...", fg="#e67e22")
        else:
            self._config_seguro(self.lbl_estado_general, text="✅ Todo listo para comenzar", fg="#27ae60")
        self._config_seguro(self.btn_iniciar, state="normal", bg="#27ae60")
        # Si TODO quedó verificado (ninguna dependencia con error) y Firefox
        # está instalado, no hace falta esperar el click: arranca solo, con
        # una pausa corta para alcanzar a ver el "Todo listo". Si algo falló
        # o falta Firefox, la ventana se queda esperando como siempre.
        if not self._dependencias_con_error and self._firefox_listo:
            self._programar(700, self.iniciar_app)
    def iniciar_app(self):
        guardar_config_app(self.config_data)
        self._cancelar_pendientes()
        self.ventana.quit()
        self.ventana.destroy()
    def cerrar_ventana(self):
        self._cancelar_pendientes()
        sys.exit(0)
# =====================================================================
# TABLA DE MÚSICA: una sola ventana, tipo planilla, para ver de un
# vistazo el volumen de cada tema (real vs. el que debería tener), el
# formato del archivo y manejar los duplicados de la carpeta que elijas -
# todo en un solo lugar. El análisis de BPM y de volumen soporta
# cualquiera de los formatos listados en EXTENSIONES_AUDIO (ver
# extraer_bpm / medir_volumen_detallado / normalizar_volumen, que
# convierten/leen el audio vía ffmpeg en vez de asumir un formato fijo).
# =====================================================================
class VentanaTablaMusica:
    UMBRAL_DESVIO_VOLUMEN = 1.5  # LUFS de tolerancia antes de considerar que "hay que normalizar"
    EXTENSIONES_AUDIO = (".mp3", ".wav", ".flac", ".m4a", ".ogg")
    DURACION_ESCANEO_RAPIDO = 45  # segundos que se miden al cargar la tabla (no el tema entero)
    HILOS_ESCANEO = max(8, min(32, (os.cpu_count() or 8) * 2))  # cuántos temas se miden en paralelo (al máximo que aguante la PC)
    HILOS_NORMALIZAR = 8          # cuántas normalizaciones de volumen (ffmpeg) corren a la vez
    HILOS_HASH = 6                # cuántos archivos se hashean a la vez al buscar duplicados
    PROCESOS_BPM = max(1, min(4, (os.cpu_count() or 4)))  # núcleos para analizar BPM en paralelo
    # proporción de ancho que le toca a cada columna (sobre el ancho total
    # disponible de la tabla en cada momento)
    PROPORCION_COLUMNAS = {
        "#0": 0.28,
        "formato": 0.08,
        "bpm": 0.07,
        "volumen_actual": 0.16,
        "volumen_objetivo": 0.16,
        "estado": 0.12,
        "progreso": 0.13,
    }
    def __init__(self, parent):
        self.parent = parent
        self.carpeta = None
        self.filas = {}              # iid de la tabla -> {"ruta", "volumen", "bpm", "formato"}
        self.grupos_duplicados = {}  # clave de grupo -> [rutas]
        self._cancelar_normalizacion = threading.Event()  # lo activa el botón "Cancelar normalización"
        self.ventana = tk.Toplevel(parent)
        self.ventana.title("📊 Tabla de música - duplicados y volumen")
        self.ventana.geometry("940x600")
        self.ventana.minsize(760, 460)
        self.ventana.configure(bg="#f4f6f7")
        # Modal: queda siempre por encima de la ventana principal y bloquea
        # la interacción con ella mientras está abierta (transient la ata a
        # la ventana principal, grab_set le saca el foco a todo lo demás).
        self.ventana.transient(parent)
        self.ventana.grab_set()
        self.ventana.protocol("WM_DELETE_WINDOW", self.cerrar_ventana)
        self._construir_ui()
    def cerrar_ventana(self):
        try:
            self.ventana.grab_release()
        except Exception:
            pass
        self.ventana.destroy()
    def _construir_ui(self):
        top = tk.Frame(self.ventana, bg="#f4f6f7")
        top.pack(fill="x", padx=14, pady=(14, 6))
        tk.Label(top, text="📊 Tabla de música", font=("Segoe UI", 13, "bold"),
                  bg="#f4f6f7", fg="#2c3e50").pack(side="left")
        self.lbl_carpeta = tk.Label(top, text="(ninguna carpeta seleccionada)", bg="#f4f6f7",
                                      fg="#7f8c8d", font=("Segoe UI", 8))
        self.lbl_carpeta.pack(side="left", padx=(16, 0), fill="x", expand=True)
        tk.Button(top, text="📁 Buscar carpeta", command=self._elegir_carpeta, bg="#2980b9", fg="white",
                   font=("Segoe UI", 9, "bold"), relief="flat", padx=10, pady=4).pack(side="right")
        # --- Tabla tipo planilla ---
        frame_tabla = tk.Frame(self.ventana, bg="#f4f6f7")
        frame_tabla.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        columnas = ("formato", "bpm", "volumen_actual", "volumen_objetivo", "estado", "progreso")
        self.tabla = ttk.Treeview(frame_tabla, columns=columnas, show="tree headings", selectmode="extended")
        self.tabla.heading("#0", text="Tema")
        self.tabla.heading("formato", text="Formato")
        self.tabla.heading("bpm", text="BPM")
        self.tabla.heading("volumen_actual", text="Volumen actual (LUFS)")
        self.tabla.heading("volumen_objetivo", text="Debería estar (LUFS)")
        self.tabla.heading("estado", text="Estado")
        self.tabla.heading("progreso", text="Normalizando...")
        self.tabla.column("#0", width=300, anchor="w")
        self.tabla.column("formato", width=70, anchor="center")
        self.tabla.column("bpm", width=60, anchor="center")
        self.tabla.column("volumen_actual", width=130, anchor="center")
        self.tabla.column("volumen_objetivo", width=130, anchor="center")
        self.tabla.column("estado", width=150, anchor="center")
        self.tabla.column("progreso", width=140, anchor="center")
        sb_y = ttk.Scrollbar(frame_tabla, orient="vertical", command=self.tabla.yview)
        self.tabla.configure(yscrollcommand=sb_y.set)
        self.tabla.pack(side="left", fill="both", expand=True)
        sb_y.pack(side="right", fill="y")
        # Las columnas se reparten en proporción al ancho disponible, así
        # siempre entran todas (incluida la barrita de progreso) sin importar
        # el tamaño de la ventana - no hace falta scrollear para el costado.
        self.tabla.bind("<Configure>", self._ajustar_columnas)
        self.tabla.tag_configure("dup", background="#fce4ec", foreground="#c0392b")
        self.tabla.tag_configure("mantener", background="#eafaf1", foreground="#1e8449")
        self.tabla.tag_configure("bajo", background="#fff3e0", foreground="#e67e22")
        self.tabla.tag_configure("alto", background="#fff3e0", foreground="#e67e22")
        self.tabla.tag_configure("ok", background="white", foreground="#27ae60")
        self.tabla.tag_configure("pendiente", background="white", foreground="#7f8c8d")
        tk.Label(self.ventana,
                  text="Tip: click / ctrl+click / shift+click en la tabla para ajustar qué archivos "
                       "quedan seleccionados antes de eliminar duplicados.",
                  bg="#f4f6f7", fg="#7f8c8d", font=("Segoe UI", 8)).pack(anchor="w", padx=14)
        # --- Estado y progreso ---
        estado_frame = tk.Frame(self.ventana, bg="#f4f6f7")
        estado_frame.pack(fill="x", padx=14, pady=(6, 0))
        self.progress = ttk.Progressbar(estado_frame, mode="determinate")
        self.progress.pack(fill="x", pady=(0, 4))
        self.lbl_estado = tk.Label(estado_frame, text="Elegí una carpeta para empezar", bg="#f4f6f7",
                                     fg="#7f8c8d", font=("Segoe UI", 8), anchor="w")
        self.lbl_estado.pack(fill="x")
        # --- Botones de acción ---
        botones = tk.Frame(self.ventana, bg="#f4f6f7")
        botones.pack(fill="x", padx=14, pady=(8, 14))
        self.btn_bpm = tk.Button(
            botones, text="🎯 Analizar BPM", command=self._analizar_bpm,
            bg="#27ae60", fg="white", font=("Segoe UI", 9, "bold"), relief="flat",
            padx=10, pady=6, state="disabled")
        self.btn_bpm.pack(side="left", padx=(0, 6))
        self.btn_detectar_dup = tk.Button(
            botones, text="🔍 Detectar duplicados", command=self._detectar_duplicados,
            bg="#e67e22", fg="white", font=("Segoe UI", 9, "bold"), relief="flat",
            padx=10, pady=6, state="disabled")
        self.btn_detectar_dup.pack(side="left", padx=(0, 6))
        self.btn_eliminar_dup = tk.Button(
            botones, text="🗑️ Eliminar duplicados seleccionados", command=self._eliminar_duplicados_seleccionados,
            bg="#c0392b", fg="white", font=("Segoe UI", 9, "bold"), relief="flat",
            padx=10, pady=6, state="disabled")
        self.btn_eliminar_dup.pack(side="left", padx=(0, 6))
        self.btn_normalizar = tk.Button(
            botones, text="🔊 Normalizar volumen", command=self._normalizar_volumen_necesarios,
            bg="#8e44ad", fg="white", font=("Segoe UI", 9, "bold"), relief="flat",
            padx=10, pady=6, state="disabled")
        self.btn_normalizar.pack(side="left")
        # Habilitado solo mientras hay una normalización en curso - es la
        # operación más pesada (ffmpeg reprocesando el audio entero de cada
        # tema), así que conviene poder cortarla sin esperar a que termine
        # la tanda completa.
        self.btn_cancelar_normalizar = tk.Button(
            botones, text="⏹ Cancelar normalización", command=self._cancelar_normalizacion_click,
            bg="#95a5a6", fg="white", font=("Segoe UI", 9, "bold"), relief="flat",
            padx=10, pady=6, state="disabled")
        self.btn_cancelar_normalizar.pack(side="left", padx=(6, 0))
    def _ajustar_columnas(self, event=None):
        """Reparte el ancho disponible de la tabla entre las columnas, para
        que entren todas (incluida la de progreso) sin importar el tamaño
        de la ventana."""
        try:
            ancho_total = self.tabla.winfo_width()
            if ancho_total < 100:
                return
            # deja un margen para el borde/scrollbar
            ancho_util = ancho_total - 20
            for col, proporcion in self.PROPORCION_COLUMNAS.items():
                nuevo_ancho = max(45, int(ancho_util * proporcion))
                self.tabla.column(col, width=nuevo_ancho, stretch=True)
        except tk.TclError:
            pass
    # ------------------------------------------------------------
    # CARGA DE LA CARPETA (nombre + formato + volumen real de cada tema)
    # ------------------------------------------------------------
    def _elegir_carpeta(self):
        carpeta = filedialog.askdirectory(title="Elegí la carpeta de música")
        if not carpeta:
            return
        self.carpeta = carpeta
        self.lbl_carpeta.config(text=carpeta)
        self.tabla.delete(*self.tabla.get_children())
        self.filas = {}
        self.grupos_duplicados = {}
        self.btn_detectar_dup.config(state="disabled")
        self.btn_eliminar_dup.config(state="disabled")
        self.btn_normalizar.config(state="disabled")
        threading.Thread(target=self._hilo_cargar_carpeta, args=(carpeta,), daemon=True).start()
    def _hilo_cargar_carpeta(self, carpeta):
        if not hasattr(self, '_carpetas_trabajo'):
            self._carpetas_trabajo = set()
        self._carpetas_trabajo.add(carpeta)
        # Antes de listar, se limpian los temporales que haya dejado una
        # normalización cortada a la fuerza (ver _limpiar_temporales_huerfanos).
        huerfanos = _limpiar_temporales_huerfanos(carpeta)
        if huerfanos:
            self._set_estado(f"🧹 Se limpiaron {len(huerfanos)} temporal(es) de una "
                             f"normalización interrumpida: {', '.join(huerfanos[:3])}")
        archivos = []
        for raiz, _dirs, nombres in os.walk(carpeta):
            for nombre in nombres:
                if nombre.lower().endswith(self.EXTENSIONES_AUDIO):
                    archivos.append(os.path.join(raiz, nombre))
        total = len(archivos)
        self._set_estado(f"🔍 Encontrados {total} temas. Midiendo volumen ({self.HILOS_ESCANEO} a la vez, "
                           f"primeros {self.DURACION_ESCANEO_RAPIDO}s de cada uno)...")
        self._set_progreso(0)
        def _medir(ruta):
            try:
                return ruta, medir_volumen(ruta, duracion_maxima=self.DURACION_ESCANEO_RAPIDO), None
            except Exception as e:
                # str(e) puede venir vacío según el tipo de excepción; si pasa,
                # al menos mostramos el tipo de error para que no quede en blanco.
                mensaje = str(e).strip() or f"{type(e).__name__} sin detalle"
                return ruta, None, mensaje
        completados = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.HILOS_ESCANEO) as executor:
            futuros = [executor.submit(_medir, ruta) for ruta in archivos]
            for futuro in concurrent.futures.as_completed(futuros):
                ruta, volumen, error_msg = futuro.result()
                nombre = os.path.basename(ruta)
                completados += 1
                self._set_estado(f"Midiendo {completados}/{total}: {nombre}")
                self.parent.after(0, lambda r=ruta, n=nombre, v=volumen, err=error_msg: self._agregar_fila(r, n, v, err))
                self._set_progreso((completados / total) * 100 if total else 100)
        self._set_estado(f"✅ {total} temas cargados. Ahora podés analizar BPM, detectar duplicados o normalizar volumen.")
        self.parent.after(0, lambda: self.btn_bpm.config(state="normal" if total else "disabled"))
        self.parent.after(0, lambda: self.btn_detectar_dup.config(state="normal" if total else "disabled"))
        self.parent.after(0, lambda: self.btn_normalizar.config(state="normal" if total else "disabled"))
    @staticmethod
    def _resumir_error(error_msg):
        """Traduce las razones más comunes de ffmpeg a algo entendible."""
        if not error_msg:
            return "motivo desconocido"
        e = error_msg.lower()
        if "timeoutexpired" in e or "timed out" in e:
            return "tardó demasiado (archivo muy pesado o dañado)"
        if "no such file" in e or "cannot find" in e or "system cannot find" in e:
            return "no se encontró el archivo (¿ruta con caracteres raros?)"
        if ("invalid data found" in e or "moov atom not found" in e or "could not find codec" in e
                or "invalid argument" in e or "end of file" in e or "eof" in e):
            return "archivo dañado, vacío o incompleto"
        if "permission denied" in e:
            return "sin permiso para abrir el archivo (¿está abierto en otro programa?)"
        if "ffmpeg no está disponible" in e or "not found" in e or "no se pudo iniciar" in e:
            return "no se encontró ffmpeg"
        return error_msg[:80]
    def _agregar_fila(self, ruta, nombre, volumen, error_msg=None):
        if volumen is None:
            texto_vol, tag = "❓ error", "pendiente"
            motivo = self._resumir_error(error_msg)
            estado = f"❓ No se pudo medir: {motivo}" if motivo else "❓ No se pudo medir"
        else:
            desvio = volumen - VOLUMEN_TARGET_I
            texto_vol = f"{volumen:.1f}"
            if desvio < -self.UMBRAL_DESVIO_VOLUMEN:
                tag, estado = "bajo", "🔉 Bajo"
            elif desvio > self.UMBRAL_DESVIO_VOLUMEN:
                tag, estado = "alto", "🔊 Alto"
            else:
                tag, estado = "ok", "✅ OK"
        match_bpm = PATRON_BPM_INICIO.match(nombre)
        bpm = int(match_bpm.group(1)) if match_bpm else None
        texto_bpm = str(bpm) if bpm else "❓"
        _nombre_sin_ext, ext = os.path.splitext(nombre)
        texto_formato = ext.lstrip(".").upper() if ext else "❓"
        try:
            iid = self.tabla.insert("", "end", text=nombre,
                                      values=(texto_formato, texto_bpm, texto_vol, f"{VOLUMEN_TARGET_I:.1f}", estado, ""),
                                      tags=(tag,))
            self.filas[iid] = {"ruta": ruta, "volumen": volumen, "bpm": bpm, "formato": texto_formato}
        except tk.TclError:
            pass
    def _set_estado(self, texto):
        self.parent.after(0, lambda: self._set_estado_seguro(texto))
    def _set_estado_seguro(self, texto):
        try:
            if self.ventana.winfo_exists():
                self.lbl_estado.config(text=texto)
        except tk.TclError:
            pass
    def _set_progreso(self, pct):
        self.parent.after(0, lambda p=pct: self._set_progreso_seguro(p))
    def _set_progreso_seguro(self, pct):
        try:
            if self.ventana.winfo_exists():
                self.progress.config(value=pct)
        except tk.TclError:
            pass
    # ------------------------------------------------------------
    # BPM (analiza y renombra solo los temas que todavía no lo tienen bien
    # puesto - funciona con cualquier formato de EXTENSIONES_AUDIO, ver
    # extraer_bpm más arriba)
    # ------------------------------------------------------------
    def _analizar_bpm(self):
        a_procesar = [(iid, datos) for iid, datos in self.filas.items() if not datos.get("bpm")]
        if not a_procesar:
            messagebox.showinfo("Nada que hacer", "Todos los temas ya tienen el BPM analizado.")
            return
        self.btn_bpm.config(state="disabled", text="⏳ Analizando BPM...")
        threading.Thread(target=self._hilo_analizar_bpm, args=(a_procesar,), daemon=True).start()
    def _hilo_analizar_bpm(self, items):
        """
        Separa los temas en dos grupos: los que ya tienen el tag [BPM] en el
        nombre (solo hay que reordenarlo, no cuesta nada) y los que de
        verdad necesitan analizar el audio. Solo el segundo grupo - el
        lento, con librosa - se manda a un ProcessPoolExecutor para usar
        varios núcleos del procesador a la vez en vez de analizar un tema
        por vez. extraer_bpm convierte cada archivo a WAV con ffmpeg antes
        de analizarlo, así que esto funciona igual sea mp3, wav, flac, m4a
        u ogg.
        """
        total = len(items)
        progreso_lock = threading.Lock()
        completados = {"n": 0}
        def _avanzar(nombre):
            with progreso_lock:
                completados["n"] += 1
                n = completados["n"]
            self._set_estado(f"🎯 Analizando BPM {n}/{total}: {nombre}")
            self._set_progreso((n / total) * 100 if total else 100)
        pendientes_audio = []
        for iid, datos in items:
            ruta = datos["ruta"]
            nombre = os.path.basename(ruta)
            match_bpm = PATRON_BPM_CUALQUIER_LADO.search(nombre)
            match_clave = PATRON_CLAVE_CAMELOT.search(nombre)
            # Con BPM Y tonalidad en el nombre solo hay que reordenarlo; si
            # falta la tonalidad, el tema se analiza igual (una sola vez).
            if match_bpm and match_clave:
                bpm = int(match_bpm.group(1))
                try:
                    nueva_ruta = renombrar_con_bpm(ruta, bpm, match_clave.group(0)[1:-1]) if bpm else ruta
                    self.parent.after(0, lambda i=iid, r=nueva_ruta, b=bpm: self._actualizar_fila_bpm(i, r, b))
                except Exception as e:
                    self._set_estado(f"❌ Error renombrando {nombre}: {e}")
                _avanzar(nombre)
            else:
                pendientes_audio.append((iid, ruta, nombre))
        if pendientes_audio:
            max_workers = min(self.PROCESOS_BPM, len(pendientes_audio))
            self._set_estado(f"🎯 Analizando audio de {len(pendientes_audio)} temas "
                              f"({max_workers} núcleos a la vez)...")
            with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
                futuros = {executor.submit(analizar_audio, ruta): (iid, ruta, nombre)
                           for iid, ruta, nombre in pendientes_audio}
                for futuro in concurrent.futures.as_completed(futuros):
                    iid, ruta, nombre = futuros[futuro]
                    try:
                        bpm, clave = futuro.result()
                        nueva_ruta = (renombrar_con_bpm(ruta, bpm, clave if TONALIDAD_EN_NOMBRE else None)
                                      if bpm else ruta)
                        if bpm:
                            try:
                                etiquetas = {"TBPM": str(bpm)}
                                if clave:
                                    etiquetas["TKEY"] = clave
                                escribir_etiquetas_mp3(nueva_ruta, etiquetas)
                            except Exception:
                                pass
                        self.parent.after(0, lambda i=iid, r=nueva_ruta, b=bpm: self._actualizar_fila_bpm(i, r, b))
                    except Exception as e:
                        self._set_estado(f"❌ Error analizando BPM de {nombre}: {e}")
                    _avanzar(nombre)
        self._set_estado(f"✅ BPM analizado en {total} temas.")
        self.parent.after(0, lambda: self.btn_bpm.config(state="normal", text="🎯 Analizar BPM"))
    def _actualizar_fila_bpm(self, iid, nueva_ruta, bpm):
        try:
            if iid not in self.filas:
                return
            self.filas[iid]["ruta"] = nueva_ruta
            self.filas[iid]["bpm"] = bpm
            valores = list(self.tabla.item(iid, "values"))
            # values = (formato, bpm, volumen_actual, volumen_objetivo, estado, progreso)
            valores[1] = str(bpm) if bpm else "❓"
            self.tabla.item(iid, text=os.path.basename(nueva_ruta), values=valores)
        except tk.TclError:
            pass
    # ------------------------------------------------------------
    # DUPLICADOS (combina contenido + nombre + similitud en un solo botón)
    # ------------------------------------------------------------
    def _detectar_duplicados(self):
        self.btn_detectar_dup.config(state="disabled", text="⏳ Buscando...")
        threading.Thread(target=self._hilo_detectar_duplicados, daemon=True).start()
    def _hilo_detectar_duplicados(self):
        try:
            todos = detectar_todos_duplicados(self.carpeta)
        except Exception as e:
            self._set_estado(f"❌ Error buscando duplicados: {e}")
            self.parent.after(0, lambda: self.btn_detectar_dup.config(state="normal", text="🔍 Detectar duplicados"))
            return
        # Combina los 3 métodos de detección en un solo set de grupos,
        # sin repetir un archivo que ya haya quedado agrupado por otro método.
        ruta_a_clave = {}
        grupos_combinados = {}
        contador = 0
        for tipo in ("contenido", "nombre", "similitud"):
            for rutas in todos[tipo].values():
                rutas_nuevas = [r for r in rutas if r not in ruta_a_clave]
                if len(rutas_nuevas) < 2:
                    continue
                contador += 1
                clave_grupo = f"{tipo}_{contador}"
                grupos_combinados[clave_grupo] = rutas_nuevas
                for r in rutas_nuevas:
                    ruta_a_clave[r] = clave_grupo
        self.grupos_duplicados = grupos_combinados
        total_marcados = 0
        indice_destino = 0
        for rutas in grupos_combinados.values():
            mejor = elegir_mejor_del_grupo(rutas)
            # El que se mantiene va primero (en verde) y justo debajo, en
            # rosa, sus duplicados - así cada grupo queda junto y arriba de
            # todo lo demás en la tabla, para poder revisar de un vistazo
            # si lo que se va a borrar es realmente el mismo tema antes de
            # tocar nada.
            orden_del_grupo = [mejor] + [r for r in rutas if r != mejor]
            for ruta in orden_del_grupo:
                iid = self._iid_de_ruta(ruta)
                if iid is None:
                    continue
                marcar_para_borrar = (ruta != mejor)
                if marcar_para_borrar:
                    total_marcados += 1
                idx = indice_destino
                indice_destino += 1
                self.parent.after(0, lambda i=iid, m=marcar_para_borrar, ix=idx: self._marcar_fila_duplicado(i, m, ix))
        self._set_estado(f"🔁 {len(grupos_combinados)} grupos de duplicados encontrados. "
                           f"{total_marcados} archivos pre-seleccionados para eliminar (revisá antes de borrar).")
        self.parent.after(0, lambda: self.btn_detectar_dup.config(state="normal", text="🔍 Detectar duplicados"))
        self.parent.after(0, lambda: self.btn_eliminar_dup.config(state="normal" if total_marcados else "disabled"))
    def _iid_de_ruta(self, ruta):
        for iid, datos in self.filas.items():
            if datos["ruta"] == ruta:
                return iid
        return None
    def _marcar_fila_duplicado(self, iid, marcar_para_borrar, indice_destino):
        try:
            valores = list(self.tabla.item(iid, "values"))
            # values = (formato, bpm, volumen_actual, volumen_objetivo, estado, progreso)
            if marcar_para_borrar:
                valores[4] = "🔁 Duplicado (a borrar)"
                tag = "dup"
            else:
                valores[4] = "✅ Se mantiene (original)"
                tag = "mantener"
            self.tabla.item(iid, values=valores, tags=(tag,))
            # Sube el grupo entero al principio de la tabla, en el orden en
            # que se van procesando los grupos - así quedan todos juntos y
            # visibles sin tener que scrollear entre el resto de los temas.
            self.tabla.move(iid, "", indice_destino)
            if marcar_para_borrar:
                self.tabla.selection_add(iid)
        except tk.TclError:
            pass
    def _eliminar_duplicados_seleccionados(self):
        seleccion = self.tabla.selection()
        rutas_seleccionadas = [(iid, self.filas[iid]["ruta"]) for iid in seleccion if iid in self.filas]
        if not rutas_seleccionadas:
            messagebox.showinfo("Sin selección", "No hay archivos seleccionados para eliminar.")
            return
        respuesta = messagebox.askyesno(
            "Confirmar eliminación",
            f"¿Estás seguro de que querés eliminar {len(rutas_seleccionadas)} archivos?\n\n"
            "Esta acción no se puede deshacer."
        )
        if not respuesta:
            return
        eliminados, errores = 0, 0
        for iid, ruta in rutas_seleccionadas:
            try:
                os.remove(ruta)
                eliminados += 1
                self.tabla.delete(iid)
                del self.filas[iid]
            except Exception as e:
                errores += 1
                print(f"Error eliminando {ruta}: {e}")
        self.btn_eliminar_dup.config(state="disabled")
        if errores == 0:
            messagebox.showinfo("Eliminación completada", f"✅ Se eliminaron {eliminados} archivos duplicados.")
        else:
            messagebox.showwarning("Eliminación parcial",
                                    f"⚠️ Se eliminaron {eliminados} archivos, pero hubo {errores} errores.")
    # ------------------------------------------------------------
    # NORMALIZAR VOLUMEN (solo los que estén lejos del objetivo - respeta
    # el formato original de cada archivo, ver normalizar_volumen más
    # arriba). Es la operación más pesada de la ventana (ffmpeg reprocesa
    # el audio entero de cada tema), así que se puede cortar a mitad de
    # camino con el botón "Cancelar normalización".
    # ------------------------------------------------------------
    def _normalizar_volumen_necesarios(self):
        a_procesar = [
            (iid, datos) for iid, datos in self.filas.items()
            if datos.get("volumen") is not None and abs(datos["volumen"] - VOLUMEN_TARGET_I) > self.UMBRAL_DESVIO_VOLUMEN
        ]
        if not a_procesar:
            messagebox.showinfo("Nada que hacer", "Todos los temas ya están dentro del rango de volumen esperado.")
            return
        self._cancelar_normalizacion.clear()
        self.btn_normalizar.config(state="disabled", text="⏳ Normalizando...")
        self.btn_cancelar_normalizar.config(state="normal", text="⏹ Cancelar normalización")
        threading.Thread(target=self._hilo_normalizar, args=(a_procesar,), daemon=True).start()
    def _cancelar_normalizacion_click(self):
        """
        Activa el evento de cancelación: los temas que todavía no arrancaron
        se saltan directo (no llegan a abrir ffmpeg), y al que está
        procesándose en este momento se le mata el proceso de ffmpeg ya
        mismo (ver evento_cancelar en normalizar_volumen /
        _correr_ffmpeg_con_progreso) - no hace falta esperar a que termine
        toda la tanda para cortar.
        """
        self._cancelar_normalizacion.set()
        self._config_seguro_boton(self.btn_cancelar_normalizar, state="disabled", text="⏳ Cancelando...")
        self._set_estado("⏹ Cancelando normalización (corta el tema actual y no arranca los que faltan)...")
    def _config_seguro_boton(self, widget, **kwargs):
        try:
            if widget.winfo_exists():
                widget.config(**kwargs)
        except tk.TclError:
            pass
    def _hilo_normalizar(self, items):
        """
        Antes normalizaba un tema a la vez (2 pasadas completas de ffmpeg
        por tema, una atrás de la otra) - con una carpeta grande eso tarda
        muchísimo. ffmpeg corre como proceso aparte, así que Python puede
        tener varios corriendo al mismo tiempo sin pisarse (cada uno usa su
        propio archivo temporal), y el tiempo total baja casi en proporción
        a cuántos entren en paralelo.
        """
        total = len(items)
        max_workers = min(self.HILOS_NORMALIZAR, len(items)) or 1
        progreso_lock = threading.Lock()
        completados = {"n": 0}
        cancelados = {"n": 0}
        def _procesar(iid, datos):
            nombre = os.path.basename(datos["ruta"])
            ultimo_pct = {"valor": -5}
            def _callback(pct, iid_fila=iid, u=ultimo_pct):
                # no satura la UI: solo actualiza si cambió al menos 1%
                if pct - u["valor"] >= 1 or pct >= 100:
                    u["valor"] = pct
                    self.parent.after(0, lambda p=pct: self._actualizar_progreso_fila(iid_fila, p))
            try:
                normalizar_volumen(datos["ruta"], callback_progreso=_callback,
                                    evento_cancelar=self._cancelar_normalizacion)
                nuevo_volumen = medir_volumen(datos["ruta"])
                self.parent.after(0, lambda i=iid, v=nuevo_volumen: self._actualizar_fila_volumen(i, v))
            except NormalizacionCancelada:
                with progreso_lock:
                    cancelados["n"] += 1
                self.parent.after(0, lambda i=iid: self._actualizar_progreso_fila(i, None))
            except Exception as e:
                self._set_estado(f"❌ Error normalizando {nombre}: {e}")
                self.parent.after(0, lambda i=iid: self._actualizar_progreso_fila(i, None))
            with progreso_lock:
                completados["n"] += 1
                n = completados["n"]
            if not self._cancelar_normalizacion.is_set():
                self._set_estado(f"🔊 Normalizando {n}/{total}: {nombre}")
            self._set_progreso((n / total) * 100 if total else 100)
        self._set_estado(f"🔊 Normalizando {total} temas ({max_workers} a la vez)...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futuros = [executor.submit(_procesar, iid, datos) for iid, datos in items]
            concurrent.futures.wait(futuros)
        fue_cancelado = self._cancelar_normalizacion.is_set()
        self._cancelar_normalizacion.clear()
        if fue_cancelado:
            hechos = completados["n"] - cancelados["n"]
            self._set_estado(f"⏹ Normalización cancelada: {hechos}/{total} temas alcanzaron a normalizarse antes de cortar.")
        else:
            self._set_estado(f"✅ Listo: {total} temas normalizados.")
        self.parent.after(0, lambda: self.btn_normalizar.config(state="normal", text="🔊 Normalizar volumen"))
        self.parent.after(0, lambda: self.btn_cancelar_normalizar.config(state="disabled", text="⏹ Cancelar normalización"))
    def _actualizar_progreso_fila(self, iid, pct):
        try:
            if iid not in self.filas:
                return
            valores = list(self.tabla.item(iid, "values"))
            while len(valores) < 6:
                valores.append("")
            # values = (formato, bpm, volumen_actual, volumen_objetivo, estado, progreso)
            valores[5] = "" if pct is None else self._formatear_barra_progreso(pct)
            self.tabla.item(iid, values=valores)
        except tk.TclError:
            pass
    @staticmethod
    def _formatear_barra_progreso(pct):
        pct = max(0.0, min(100.0, pct))
        llenos = int(round(pct / 10))
        return "▓" * llenos + "░" * (10 - llenos) + f" {pct:.0f}%"
    def _actualizar_fila_volumen(self, iid, volumen):
        try:
            if iid not in self.filas:
                return
            self.filas[iid]["volumen"] = volumen
            if volumen is None:
                texto_vol, tag, estado = "❓ error", "pendiente", "❓ No se pudo medir"
            else:
                desvio = volumen - VOLUMEN_TARGET_I
                texto_vol = f"{volumen:.1f}"
                if abs(desvio) > self.UMBRAL_DESVIO_VOLUMEN:
                    tag, estado = ("bajo", "🔉 Bajo") if desvio < 0 else ("alto", "🔊 Alto")
                else:
                    tag, estado = "ok", "✅ OK"
            valores_actuales = list(self.tabla.item(iid, "values"))
            # values = (formato, bpm, volumen_actual, volumen_objetivo, estado, progreso)
            texto_formato = valores_actuales[0] if valores_actuales else self.filas[iid].get("formato", "❓")
            texto_bpm = valores_actuales[1] if len(valores_actuales) > 1 else "❓"
            # progreso queda en blanco: la columna "Estado" ya refleja el resultado final
            self.tabla.item(iid, values=(texto_formato, texto_bpm, texto_vol, f"{VOLUMEN_TARGET_I:.1f}", estado, ""), tags=(tag,))
        except tk.TclError:
            pass
# =====================================================================
# LOGGER PROPIO PARA YT-DLP
#   Por defecto, yt-dlp escribe sus mensajes (sobre todo los "ERROR: ...")
#   directo a la consola del sistema operativo, aunque esté en modo
#   "quiet" - por eso esos errores solo se veían en la ventana negra de
#   Python y no en la consola propia del programa ("🖥"). Pasándole este
#   logger, todo lo que yt-dlp tendría para decir (búsquedas y descargas
#   por igual) queda registrado en un solo lugar: la consola del programa.
#   De paso evita un problema real si algún día se abre el .pyw con doble
#   click en vez de desde IDLE: ahí no hay consola del sistema operativo
#   (pythonw no tiene stdout), y sin este logger esos mensajes se
#   perderían del todo en lugar de solo no verse.
# =====================================================================
# =====================================================================
# TEMA VISUAL: colores y widgets propios (botones redondeados con efecto
# al pasar el mouse, interruptores, barra de progreso con degradé, barras
# de desplazamiento oscuras y campo de búsqueda con historial).
# Todo se dibuja con Canvas: no depende de ningún paquete extra.
# =====================================================================
import math
import tkinter.font as tkfont

TEMA = {
    "fondo": "#0d0f24", "panel": "#161a3b", "panel2": "#222759", "borde": "#2f3575",
    "texto": "#f1f2ff", "suave": "#9ba4d9", "acento": "#00c2ff", "acento2": "#8b5cf6",
    "rosa": "#ff4fa3", "ok": "#00d68f", "peligro": "#ff4d6d", "aviso": "#ffb347",
    "lista": "#11143a", "lista_sel": "#4a35c9", "deshab": "#3a3f6e", "campo": "#0a0c1e",
}

def _rgb(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))

def _hex(r, g, b):
    return "#%02x%02x%02x" % (max(0, min(255, int(r))), max(0, min(255, int(g))), max(0, min(255, int(b))))

def mezclar_color(c1, c2, t):
    a, b = _rgb(c1), _rgb(c2)
    return _hex(*(a[i] + (b[i] - a[i]) * t for i in range(3)))

def aclarar_color(c, f):
    return mezclar_color(c, "#ffffff", f)

def oscurecer_color(c, f):
    return mezclar_color(c, "#000000", f)

def degradado(paradas, t):
    """paradas: [(posicion 0..1, color), ...] ordenadas."""
    if t <= paradas[0][0]:
        return paradas[0][1]
    for (p0, c0), (p1, c1) in zip(paradas, paradas[1:]):
        if t <= p1:
            return mezclar_color(c0, c1, (t - p0) / max(p1 - p0, 1e-9))
    return paradas[-1][1]

def _inset(i, largo, r):
    """Cuánto se retrae la línea i de un rectángulo redondeado de ese largo."""
    if i < r:
        d = r - i - 0.5
    elif i >= largo - r:
        d = i - (largo - r) + 0.5
    else:
        return 0
    return r - math.sqrt(max(r * r - d * d, 0))

def rect_redondeado_degradado(cv, w, h, r, color_en, vertical=True, x_ini=0, tag="fx"):
    """Dibuja un rectángulo redondeado relleno con degradé usando líneas."""
    r = max(1, min(r, h // 2, w // 2))
    if vertical:
        for y in range(h):
            ins = _inset(y, h, r)
            cv.create_line(x_ini + ins, y, x_ini + w - ins, y, fill=color_en(y / max(h - 1, 1)), tags=tag)
    else:
        for x in range(w):
            ins = _inset(x, w, r)
            cv.create_line(x_ini + x, ins, x_ini + x, h - ins, fill=color_en(x / max(w - 1, 1)), tags=tag)

def _bg_de(w):
    try:
        return w.cget("bg")
    except Exception:
        return TEMA["fondo"]


class BotonRedondeado(tk.Canvas):
    """Botón redondeado con degradé. Al pasar el mouse se ilumina, al
    apretarlo se hunde y deshabilitado se apaga. Acepta config(text=,
    state=, bg=, fg=, command=) igual que un tk.Button."""
    def __init__(self, parent, text="", command=None, bg="#00c2ff", fg="#ffffff",
                 font=("Segoe UI", 10, "bold"), radio=16, padx=18, pady=8, bg_padre=None,
                 state="normal", alto=None):
        self._texto, self._comando, self._color, self._fg = text, command, bg, fg
        self._font = tkfont.Font(font=font)
        self._radio, self._estado = radio, state
        self._hover = self._presionado = False
        h = alto or (self._font.metrics("linespace") + 2 * pady)
        w = self._font.measure(text) + 2 * padx
        super().__init__(parent, width=w, height=h, bg=bg_padre or _bg_de(parent),
                         highlightthickness=0, bd=0,
                         cursor="hand2" if state == "normal" else "arrow")
        self._alto = h
        self.bind("<Configure>", lambda e: self._dibujar())
        self.bind("<Enter>", self._entrar)
        self.bind("<Leave>", self._salir)
        self.bind("<ButtonPress-1>", self._apretar)
        self.bind("<ButtonRelease-1>", self._soltar)
        self._dibujar()

    def _entrar(self, _e=None):
        self._hover = True
        self._dibujar()

    def _salir(self, _e=None):
        self._hover = self._presionado = False
        self._dibujar()

    def _apretar(self, _e=None):
        if self._estado == "normal":
            self._presionado = True
            self._dibujar()

    def _soltar(self, _e=None):
        activo = self._presionado and self._hover and self._estado == "normal"
        self._presionado = False
        self._dibujar()
        if activo and self._comando:
            self._comando()

    def _dibujar(self):
        try:
            w = max(self.winfo_width(), 4)
            h = self.winfo_height() if self.winfo_height() > 4 else self._alto
            self.delete("all")
            if self._estado != "normal":
                base, fg = TEMA["deshab"], TEMA["suave"]
            else:
                base, fg = self._color, self._fg
                if self._presionado:
                    base = oscurecer_color(base, 0.18)
                elif self._hover:
                    base = aclarar_color(base, 0.20)
            arriba, abajo = aclarar_color(base, 0.20), oscurecer_color(base, 0.22)
            rect_redondeado_degradado(self, w, h, self._radio, lambda t: mezclar_color(arriba, abajo, t))
            if self._estado == "normal" and self._hover and not self._presionado:
                # aro de luz alrededor: se nota enseguida que el botón "reacciona"
                self.create_line(self._radio, 0, w - self._radio, 0, fill=aclarar_color(base, 0.55))
            d = 1 if self._presionado else 0
            sombra = oscurecer_color(base, 0.45)
            self.create_text(w / 2 + d + 1, h / 2 + d + 1, text=self._texto, fill=sombra, font=self._font)
            self.create_text(w / 2 + d, h / 2 + d, text=self._texto, fill=fg, font=self._font)
        except Exception:
            pass

    def configure(self, cnf=None, **kw):
        if cnf:
            kw.update(cnf)
        redibujar = False
        for clave, attr in (("text", "_texto"), ("command", "_comando"), ("bg", "_color"),
                            ("background", "_color"), ("fg", "_fg"), ("foreground", "_fg")):
            if clave in kw:
                setattr(self, attr, kw.pop(clave))
                redibujar = True
        if "state" in kw:
            self._estado = kw.pop("state")
            self._hover = self._presionado = False
            tk.Canvas.configure(self, cursor="hand2" if self._estado == "normal" else "arrow")
            redibujar = True
        for ignorada in ("padx", "pady", "relief"):
            kw.pop(ignorada, None)
        if kw:
            tk.Canvas.configure(self, **kw)
        if redibujar:
            self._dibujar()
    config = configure


class Interruptor(tk.Frame):
    """Interruptor tipo "switch" ligado a una BooleanVar (reemplaza al
    Checkbutton cuadrado). Se ilumina al pasar el mouse."""
    def __init__(self, parent, text, variable, command=None, bg=None, fg=None,
                 font=("Segoe UI", 10), wraplength=0, color_on=None):
        bg = bg or _bg_de(parent)
        super().__init__(parent, bg=bg)
        self._var, self._cmd, self._bg = variable, command, bg
        self._color_on = color_on or TEMA["ok"]
        self._hover = False
        self._cv = tk.Canvas(self, width=46, height=24, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self._cv.pack(side="left", padx=(0, 10))
        self._lbl = tk.Label(self, text=text, bg=bg, fg=fg or TEMA["texto"], font=font,
                             wraplength=wraplength, justify="left", anchor="w", cursor="hand2")
        self._lbl.pack(side="left", fill="x", expand=True)
        for w in (self._cv, self._lbl):
            w.bind("<Button-1>", self._alternar)
            w.bind("<Enter>", self._entrar)
            w.bind("<Leave>", self._salir)
        self._traza = variable.trace_add("write", lambda *a: self._dibujar())
        self._dibujar()

    def _entrar(self, _e=None):
        self._hover = True
        self._dibujar()

    def _salir(self, _e=None):
        self._hover = False
        self._dibujar()

    def _alternar(self, _e=None):
        self._var.set(not self._var.get())
        if self._cmd:
            self._cmd()

    def _dibujar(self):
        try:
            cv = self._cv
            cv.delete("all")
            on = bool(self._var.get())
            pista = self._color_on if on else TEMA["deshab"]
            if self._hover:
                pista = aclarar_color(pista, 0.18)
            cv.create_oval(0, 0, 24, 24, fill=pista, outline=pista)
            cv.create_oval(22, 0, 46, 24, fill=pista, outline=pista)
            cv.create_rectangle(12, 0, 34, 25, fill=pista, outline=pista)
            x = 25 if on else 3
            cv.create_oval(x + 1, 4, x + 19, 22, fill=oscurecer_color(pista, 0.35), outline="")
            cv.create_oval(x, 3, x + 18, 21, fill="#ffffff", outline="")
        except Exception:
            pass  # el widget ya se destruyó


class BarraProgreso(tk.Canvas):
    """Barra de progreso con degradé (cian → violeta → rosa). Acepta
    config(value=0..100) como un ttk.Progressbar."""
    def __init__(self, parent, alto=14, bg=None):
        super().__init__(parent, height=alto, bg=bg or _bg_de(parent), highlightthickness=0, bd=0)
        self._valor, self._alto = 0.0, alto
        self.bind("<Configure>", lambda e: self._dibujar())

    def _dibujar(self):
        try:
            w, h = max(self.winfo_width(), 4), self._alto
            self.delete("all")
            rect_redondeado_degradado(self, w, h, h // 2, lambda t: TEMA["panel2"], vertical=False)
            wf = int(w * max(0.0, min(100.0, self._valor)) / 100.0)
            if wf >= 2:
                paradas = [(0, TEMA["acento"]), (0.55, TEMA["acento2"]), (1, TEMA["rosa"])]
                rect_redondeado_degradado(self, wf, h, h // 2,
                                          lambda t: degradado(paradas, t * wf / w), vertical=False)
        except Exception:
            pass

    def configure(self, cnf=None, **kw):
        if cnf:
            kw.update(cnf)
        if "value" in kw:
            try:
                self._valor = float(kw.pop("value"))
            except Exception:
                self._valor = 0.0
            self._dibujar()
        kw.pop("maximum", None)
        if kw:
            tk.Canvas.configure(self, **kw)
    config = configure


class BarraDesplazamiento(tk.Canvas):
    """Barra de desplazamiento oscura y redondeada (vertical u horizontal)."""
    def __init__(self, parent, orient="vertical", command=None, grosor=12, bg=None):
        self._vertical = orient == "vertical"
        kw = {"width": grosor} if self._vertical else {"height": grosor}
        super().__init__(parent, bg=bg or TEMA["lista"], highlightthickness=0, bd=0, **kw)
        self._cmd, self._lo, self._hi = command, 0.0, 1.0
        self._hover, self._arrastre, self._grosor = False, None, grosor
        self.bind("<Configure>", lambda e: self._dibujar())
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonPress-1>", self._apretar)
        self.bind("<B1-Motion>", self._mover)
        self.bind("<ButtonRelease-1>", lambda e: setattr(self, "_arrastre", None))

    def _set_hover(self, v):
        self._hover = v
        self._dibujar()

    def set(self, lo, hi):
        self._lo, self._hi = float(lo), float(hi)
        self._dibujar()

    def _largo(self):
        return max(self.winfo_height() if self._vertical else self.winfo_width(), 1)

    def _pulgar(self):
        L = self._largo()
        a, b = self._lo * L, self._hi * L
        if b - a < 30:
            c = (a + b) / 2
            a, b = max(0, c - 15), min(L, c + 15)
        return a, b

    def _dibujar(self):
        try:
            self.delete("all")
            if self._hi - self._lo >= 0.9999:
                return
            a, b = self._pulgar()
            g = self._grosor
            color = TEMA["acento2"] if self._hover or self._arrastre is not None else TEMA["borde"]
            if self._vertical:
                self.create_oval(2, a, g - 2, a + g - 4, fill=color, outline=color)
                self.create_oval(2, b - g + 4, g - 2, b, fill=color, outline=color)
                self.create_rectangle(2, a + (g - 4) / 2, g - 2, b - (g - 4) / 2, fill=color, outline=color)
            else:
                self.create_oval(a, 2, a + g - 4, g - 2, fill=color, outline=color)
                self.create_oval(b - g + 4, 2, b, g - 2, fill=color, outline=color)
                self.create_rectangle(a + (g - 4) / 2, 2, b - (g - 4) / 2, g - 2, fill=color, outline=color)
        except Exception:
            pass

    def _apretar(self, e):
        p = e.y if self._vertical else e.x
        a, b = self._pulgar()
        if a <= p <= b:
            self._arrastre = p - a
            self._dibujar()
        elif self._cmd:
            self._cmd("scroll", -1 if p < a else 1, "pages")

    def _mover(self, e):
        if self._arrastre is None or not self._cmd:
            return
        p = e.y if self._vertical else e.x
        self._cmd("moveto", max(0.0, (p - self._arrastre) / self._largo()))


class CampoBusqueda(tk.Frame):
    """Cuadro de texto oscuro con borde que se ilumina al escribir y una
    flecha ▾ que despliega las últimas búsquedas (reemplaza al Combobox)."""
    def __init__(self, parent, valores=(), font=("Segoe UI", 11)):
        super().__init__(parent, bg=TEMA["borde"], padx=2, pady=2)
        self._valores = list(valores)
        interior = tk.Frame(self, bg=TEMA["campo"])
        interior.pack(fill="both", expand=True)
        self._entry = tk.Entry(interior, font=font, bg=TEMA["campo"], fg=TEMA["texto"],
                               insertbackground=TEMA["acento"], relief="flat", bd=0,
                               highlightthickness=0, selectbackground=TEMA["lista_sel"],
                               selectforeground="white")
        self._entry.pack(side="left", fill="both", expand=True, padx=(12, 4), pady=8)
        self._flecha = tk.Label(interior, text="▾", bg=TEMA["campo"], fg=TEMA["suave"],
                                font=("Segoe UI", 12), cursor="hand2", padx=10)
        self._flecha.pack(side="right", fill="y")
        self._flecha.bind("<Button-1>", lambda e: self._abrir_lista())
        self._flecha.bind("<Enter>", lambda e: self._flecha.config(fg=TEMA["acento"]))
        self._flecha.bind("<Leave>", lambda e: self._flecha.config(fg=TEMA["suave"]))
        self._entry.bind("<FocusIn>", lambda e: self.config(bg=TEMA["acento"]))
        self._entry.bind("<FocusOut>", lambda e: self.config(bg=TEMA["borde"]))
        self._entry.bind("<Down>", lambda e: self._abrir_lista())

    def get(self):
        return self._entry.get()

    def bind(self, secuencia=None, func=None, add=None):
        return self._entry.bind(secuencia, func, add)

    def foco(self):
        self._entry.focus_set()

    def __setitem__(self, clave, valor):
        if clave == "values":
            self._valores = list(valor)
        else:
            tk.Frame.__setitem__(self, clave, valor)

    def _abrir_lista(self):
        menu = tk.Menu(self, tearoff=0, bg=TEMA["panel"], fg=TEMA["texto"], bd=0, relief="flat",
                       activebackground=TEMA["acento2"], activeforeground="white",
                       font=("Segoe UI", 10))
        if self._valores:
            for v in self._valores:
                menu.add_command(label="  " + v + "  ", command=lambda v=v: self._elegir(v))
        else:
            menu.add_command(label="  (todavía no hay búsquedas recientes)  ", state="disabled")
        try:
            menu.tk_popup(self.winfo_rootx(), self.winfo_rooty() + self.winfo_height())
        finally:
            menu.grab_release()

    def _elegir(self, v):
        self._entry.delete(0, "end")
        self._entry.insert(0, v)
        self._entry.focus_set()

class LoggerYtDlp:
    def __init__(self, app):
        self.app = app
    def debug(self, msg):
        if msg.startswith("[debug] "):
            return  # ruido interno de yt-dlp, no aporta nada para el usuario
        self.app.debug(msg)
    def warning(self, msg):
        self.app.warning(f"⚠️ {msg}")
    def error(self, msg):
        self.app.error(msg)
# =====================================================================
# APLICACIÓN PRINCIPAL
# =====================================================================
class DownloaderIA:
    def __init__(self, root, config_app):
        self.root = root
        self.config_app = config_app
        self.memoria = cargar_memoria()
        self.root.title("🧠 YouTube Mp3 - auto-aprendizaje")
        self.root.geometry("1000x790")
        self.root.minsize(940, 720)
        self.ventana_consola = None
        self.txt_consola = None
        self.historial_consola = []
        self._tags_consola_configurados = set()
        self.ventana_tabla_musica = None
        self.resultados = []
        self.ultima_busqueda = ""  # nombre del autor/búsqueda actual, para encarpetar al descargar toda la lista
        self.abortar = False
        self.descargando = False
        self.consola = self  # AntiBloqueoManager usa self.consola.warning/exito/etc
        self._logger_ytdlp = LoggerYtDlp(self)
        # Se apaga solo si impersonate falla en esta máquina (ver N07).
        self._impersonate_apagado = False
        self._cola_post = queue.Queue()
        self._lock_post = threading.Lock()
        self._hilo_post = None
        self._buscando = False
        self._cola = None
        self._lock_cola = threading.Lock()
        self._descartados = set()  # videos con fallo definitivo: la segunda vuelta no los reintenta
        # El predictor se crea ANTES que el manager, porque el manager le
        # avisa cada éxito y cada fallo para que siga aprendiendo.
        self.predictor = PredictorRiesgo(self.memoria, consola=self)
        self.anti_bloqueo = AntiBloqueoManager(self.memoria, consola=self, predictor=self.predictor)
        self.usar_firefox_exclusivo = tk.BooleanVar(value=self.config_app.get("usar_firefox_exclusivo", True))
        self._construir_ui()
        self.predictor.avisar_estado_inicial()
        self.avisar_estado_antisuplantacion()
        self._asegurar_sesion_firefox()
        self.root.after(1500, self._ofrecer_reanudar)
        self._guardar_memoria_periodica()
        self.root.protocol("WM_DELETE_WINDOW", self.cerrar)
    # --- logging usado también por AntiBloqueoManager ---
    def exito(self, msg):
        self._log(msg, "#27ae60")
    def error(self, msg):
        self._log(msg, "#e74c3c")
    def warning(self, msg):
        self._log(msg, "#e67e22")
    def debug(self, msg):
        self._log(msg, "#7f8c8d")
    def _log(self, msg, color="#d4d4d4"):
        """
        Guarda cada línea junto con su color (según sea éxito/error/warning/
        debug) y la agrega a la consola ya coloreada, usando ese color como
        nombre de "tag" de Tkinter - así no hace falta declarar los tags de
        antemano, cada color se configura la primera vez que aparece.
        """
        ts = datetime.now().strftime("%H:%M:%S")
        linea = f"[{ts}] {msg}\n"
        self.historial_consola.append((linea, color))
        def _agregar():
            if self.txt_consola is None:
                return
            self.txt_consola.config(state="normal")
            self._insertar_linea_coloreada(linea, color)
            self.txt_consola.see("end")
            self.txt_consola.config(state="disabled")
        self.root.after(0, _agregar)
    def _insertar_linea_coloreada(self, linea, color):
        if color not in self._tags_consola_configurados:
            self.txt_consola.tag_configure(color, foreground=color)
            self._tags_consola_configurados.add(color)
        self.txt_consola.insert("end", linea, color)
    def _abrir_consola(self):
        if self.ventana_consola is not None and self.ventana_consola.winfo_exists():
            self.ventana_consola.lift()
            self.ventana_consola.focus_force()
            return
        ventana = tk.Toplevel(self.root)
        ventana.title("🖥 Consola")
        ventana.geometry("640x360")
        ventana.configure(bg="#1e1e1e")
        self.txt_consola = tk.Text(ventana, bg="#1e1e1e", fg="#d4d4d4", state="disabled",
                                     font=("Consolas", 9))
        self.txt_consola.pack(fill="both", expand=True, padx=8, pady=8)
        # Los tags de color son propios de este widget de Text - como es uno
        # nuevo cada vez que se abre la ventana, hay que volver a registrar
        # todos los colores usados en el historial antes de reinsertarlo.
        self._tags_consola_configurados = set()
        self.txt_consola.config(state="normal")
        for linea, color in self.historial_consola:
            self._insertar_linea_coloreada(linea, color)
        self.txt_consola.see("end")
        self.txt_consola.config(state="disabled")
        def _cerrar():
            self.txt_consola = None
            self.ventana_consola = None
            ventana.destroy()
        ventana.protocol("WM_DELETE_WINDOW", _cerrar)
        self.ventana_consola = ventana
    # ------------------------------------------------------------
    # TABLA DE MÚSICA (⚙) - todo unificado: BPM + duplicados + volumen,
    # sobre la misma carpeta elegida. El engranaje la abre directamente.
    # ------------------------------------------------------------
    def _abrir_tabla_musica(self):
        if self.ventana_tabla_musica is not None and self.ventana_tabla_musica.ventana.winfo_exists():
            self.ventana_tabla_musica.ventana.lift()
            self.ventana_tabla_musica.ventana.focus_force()
            return
        self.ventana_tabla_musica = VentanaTablaMusica(self.root)
    def _construir_ui(self):
        T = TEMA
        self.root.configure(bg=T["fondo"])
        # ---------- ENCABEZADO con degradé ----------
        paradas_h = [(0, "#190f5a"), (0.45, "#5b2be0"), (0.72, "#d6328f"), (1, "#d6328f")]
        color_der = paradas_h[-1][1]
        header = tk.Canvas(self.root, height=78, highlightthickness=0, bd=0, bg=T["fondo"])
        header.pack(fill="x")
        def _pintar_header(_e=None):
            w = max(header.winfo_width(), 10)
            header.delete("all")
            for x in range(0, w, 3):
                header.create_line(x, 0, x, 78, width=3, fill=degradado(paradas_h, x / max(w - 1, 1)))
            header.create_line(0, 77, w, 77, fill=T["acento"], width=2)
            header.create_text(26, 31, text="🎧  YouTube MP3", anchor="w", fill="white",
                               font=("Segoe UI", 21, "bold"))
            header.create_text(30, 59, anchor="w", fill="#e6defc", font=("Segoe UI", 9),
                               text="Descargas inteligentes  ·  máxima calidad  ·  anti-bloqueo  ·  BPM y tonalidad")
        header.bind("<Configure>", _pintar_header)
        iconos = tk.Frame(header, bg=color_der)
        iconos.place(relx=1.0, x=-18, y=20, anchor="ne")
        BotonRedondeado(iconos, "⚙  Mi música", self._abrir_tabla_musica, bg="#2b2f75", radio=17,
                        font=("Segoe UI", 9, "bold"), padx=14, pady=6, bg_padre=color_der).pack(side="left")
        BotonRedondeado(iconos, "🖥  Consola", self._abrir_consola, bg="#2b2f75", radio=17,
                        font=("Segoe UI", 9, "bold"), padx=14, pady=6, bg_padre=color_der).pack(side="left", padx=(8, 0))
        # ---------- BÚSQUEDA ----------
        top = tk.Frame(self.root, bg=T["fondo"])
        top.pack(fill="x", padx=18, pady=(16, 6))
        historial = self.memoria.get("historial_busquedas", [])
        self.entry_busqueda = CampoBusqueda(top, historial)
        self.entry_busqueda.pack(side="left", fill="x", expand=True)
        self.entry_busqueda.bind("<Return>", lambda e: self.buscar())
        self.btn_buscar = BotonRedondeado(top, "🔎  Buscar", self.buscar, bg="#0aa5e8", radio=19,
                                           padx=22, pady=9, bg_padre=T["fondo"])
        self.btn_buscar.pack(side="left", padx=(12, 0))
        self.btn_similares = BotonRedondeado(top, "🎯  Similares", self.buscar_similares, bg="#8b5cf6",
                                              radio=19, padx=18, pady=9, bg_padre=T["fondo"])
        self.btn_similares.pack(side="left", padx=(8, 0))
        self.var_mejores_temas = tk.BooleanVar(value=self.config_app.get("buscar_mejores_temas", False))
        Interruptor(top, "🔥 Mejores temas", self.var_mejores_temas, command=self._guardar_config_descarga,
                    bg=T["fondo"], fg=T["aviso"], font=("Segoe UI", 10, "bold"),
                    color_on="#ff7a45").pack(side="left", padx=(16, 0))
        # ---------- OPCIONES DE LA BÚSQUEDA ----------
        opciones = tk.Frame(self.root, bg=T["fondo"])
        opciones.pack(fill="x", padx=18, pady=(4, 0))
        def _giro(var, desde, hasta, ancho):
            return tk.Spinbox(opciones, from_=desde, to=hasta, width=ancho, textvariable=var,
                              bg=T["campo"], fg=T["texto"], buttonbackground=T["panel2"],
                              insertbackground=T["acento"], relief="flat", bd=0,
                              highlightthickness=1, highlightbackground=T["borde"],
                              highlightcolor=T["acento"], font=("Segoe UI", 10),
                              disabledbackground=T["campo"], readonlybackground=T["campo"])
        def _etiqueta(texto):
            return tk.Label(opciones, text=texto, bg=T["fondo"], fg=T["suave"], font=("Segoe UI", 9, "bold"))
        _etiqueta("CANTIDAD").pack(side="left")
        self.var_cantidad = tk.IntVar(value=self.config_app.get("cantidad", 8))
        _giro(self.var_cantidad, 1, 9999, 5).pack(side="left", padx=(6, 18), ipady=3)
        _etiqueta("DURACIÓN MÁX (MIN)").pack(side="left")
        self.var_dur_max = tk.IntVar(value=self.config_app.get("duracion_max", 8))
        _giro(self.var_dur_max, 1, 60, 4).pack(side="left", padx=(6, 18), ipady=3)
        _etiqueta("SIMULTÁNEAS").pack(side="left")
        self.var_simultaneas = tk.IntVar(value=self.config_app.get("simultaneas", 1))
        _giro(self.var_simultaneas, 1, 4, 3).pack(side="left", padx=(6, 18), ipady=3)
        self.var_busqueda_exacta = tk.BooleanVar(value=self.config_app.get("busqueda_exacta", True))
        Interruptor(opciones, "🔎 Búsqueda exacta", self.var_busqueda_exacta, command=self._guardar_config_descarga,
                    bg=T["fondo"], font=("Segoe UI", 10, "bold"), color_on=T["acento"]).pack(side="left")
        tk.Label(opciones, text="(apagada: trae el Mix)", bg=T["fondo"], fg=T["suave"],
                 font=("Segoe UI", 8)).pack(side="left", padx=(8, 0))
        # ---------- LISTA + PANEL ----------
        medio = tk.Frame(self.root, bg=T["fondo"])
        medio.pack(fill="both", expand=True, padx=18, pady=(12, 8))
        marco_lista = tk.Frame(medio, bg=T["borde"], padx=1, pady=1)
        marco_lista.pack(side="left", fill="both", expand=True)
        interior = tk.Frame(marco_lista, bg=T["lista"])
        interior.pack(fill="both", expand=True)
        interior.grid_rowconfigure(0, weight=1)
        interior.grid_columnconfigure(0, weight=1)
        self.listbox = tk.Listbox(interior, selectmode="extended", font=("Segoe UI", 10), activestyle="none",
                                  bg=T["lista"], fg=T["texto"], selectbackground=T["lista_sel"],
                                  selectforeground="white", relief="flat", bd=0, highlightthickness=0)
        self.listbox.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=6)
        self.listbox.bind("<Double-Button-1>", self._previsualizar_item)
        sb_v = BarraDesplazamiento(interior, "vertical", self.listbox.yview)
        sb_v.grid(row=0, column=1, sticky="ns", padx=(2, 3), pady=6)
        sb_h = BarraDesplazamiento(interior, "horizontal", self.listbox.xview)
        sb_h.grid(row=1, column=0, sticky="ew", padx=(8, 0), pady=(0, 3))
        self.listbox.config(yscrollcommand=sb_v.set, xscrollcommand=sb_h.set)
        panel = tk.Frame(medio, bg=T["panel"], width=316, highlightthickness=1, highlightbackground=T["borde"])
        panel.pack(side="left", fill="y", padx=(14, 0))
        panel.pack_propagate(False)
        p = tk.Frame(panel, bg=T["panel"])
        p.pack(fill="both", expand=True, padx=16, pady=12)
        def _titulo(texto, arriba=0):
            tk.Label(p, text=texto, bg=T["panel"], fg=T["acento"],
                     font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(arriba, 4))
        def _nota(texto):
            tk.Label(p, text=texto, bg=T["panel"], fg=T["suave"], font=("Segoe UI", 8),
                     justify="left", wraplength=214).pack(anchor="w", padx=(56, 0), pady=(0, 8))
        def _interruptor(texto, var, comando=None):
            Interruptor(p, texto, var, command=comando, bg=T["panel"], font=("Segoe UI", 10),
                        wraplength=215).pack(anchor="w", fill="x", pady=(4, 2))
        _titulo("O P C I O N E S")
        self.var_lista_completa = tk.BooleanVar(value=self.config_app.get("descargar_toda_lista", True))
        _interruptor("Descargar toda la lista", self.var_lista_completa)
        _nota("Apagado: baja solo lo seleccionado.")
        self.var_analizar_bpm = tk.BooleanVar(value=self.config_app.get("analizar_bpm", True))
        _interruptor("Analizar BPM y renombrar", self.var_analizar_bpm)
        self.var_normalizar_volumen = tk.BooleanVar(value=self.config_app.get("normalizar_volumen", True))
        _interruptor("Normalizar volumen", self.var_normalizar_volumen)
        self.var_solo_estudio = tk.BooleanVar(value=self.config_app.get("solo_estudio", True))
        _interruptor("🎼 Solo versiones de estudio", self.var_solo_estudio, self._guardar_config_descarga)
        _nota("Descarta covers, sped up, slowed, karaoke, mixes y videos caseros.")
        _interruptor("Firefox exclusivo del programa", self.usar_firefox_exclusivo, self._guardar_firefox_exclusivo)
        _nota("Recomendado: sus cookies se leen solas.")
        self.btn_descargar = BotonRedondeado(p, "⬇  Descargar MP3  (máx. calidad)", self.iniciar_descarga,
                                              bg="#12b76a", radio=22, font=("Segoe UI", 11, "bold"),
                                              pady=10, bg_padre=T["panel"])
        self.btn_descargar.pack(fill="x", pady=(8, 6))
        self.btn_detener = BotonRedondeado(p, "⏹  Detener", self.detener, bg="#ef4444", radio=17,
                                            pady=7, bg_padre=T["panel"], state="disabled")
        self.btn_detener.pack(fill="x")
        BotonRedondeado(p, "📁  Abrir carpeta de descargas", self.abrir_carpeta_descargas, bg="#4a5199",
                        radio=17, font=("Segoe UI", 9, "bold"), pady=7,
                        bg_padre=T["panel"]).pack(fill="x", pady=(6, 0))
        _titulo("E S T A D O", arriba=14)
        self.lbl_encontrados = tk.Label(p, text="Encontrados en la lista: 0", bg=T["panel"],
                                        fg=T["texto"], font=("Segoe UI", 10, "bold"), justify="left")
        self.lbl_encontrados.pack(anchor="w", pady=(0, 6))
        self.lbl_anti_bloqueo = tk.Label(p, text=self._texto_anticipacion(), bg=T["panel"],
                                          font=("Segoe UI", 9, "bold"), fg=T["ok"], justify="left", wraplength=280)
        self.lbl_anti_bloqueo.pack(anchor="w")
        self.lbl_ritmo = tk.Label(p, text=f"Ritmo aprendido: {self.anti_bloqueo.delay_base}s",
                                  bg=T["panel"], fg=T["suave"], font=("Segoe UI", 8))
        self.lbl_ritmo.pack(anchor="w", pady=(2, 0))
        self.lbl_espera = tk.Label(p, text="", bg=T["panel"], fg=T["acento"], font=("Segoe UI", 8, "bold"))
        self.lbl_espera.pack(anchor="w", pady=(2, 0))
        stats = self.memoria.get("client_stats", {})
        total_registrado = sum(v.get("exitos", 0) + v.get("fallos", 0) for v in stats.values())
        tk.Label(p, text=f"Aprendizaje acumulado: {total_registrado} intentos",
                 bg=T["panel"], fg=T["suave"], font=("Segoe UI", 8), justify="left",
                 wraplength=280).pack(anchor="w", pady=(4, 0))
        self._ultimo_conteo_lista = 0
        self._refrescar_contador_lista()
        self.progress = BarraProgreso(self.root, alto=14, bg=T["fondo"])
        self.progress.pack(fill="x", padx=18, pady=(2, 16))
        self.entry_busqueda.foco()
    def _refrescar_contador_lista(self):
        """Mantiene al día "Encontrados en la lista: N" mirando el tamaño
        real de la lista (así cubre todas las búsquedas: normal, similares,
        mix y mejores temas, sin tocar cada una)."""
        try:
            n = self.listbox.size()
            if n != self._ultimo_conteo_lista:
                self._ultimo_conteo_lista = n
                self.lbl_encontrados.config(text=f"Encontrados en la lista: {n}")
            self.root.after(400, self._refrescar_contador_lista)
        except Exception:
            pass  # la ventana se cerró
    def _guardar_firefox_exclusivo(self):
        self.config_app["usar_firefox_exclusivo"] = self.usar_firefox_exclusivo.get()
        guardar_config_app(self.config_app)
    def _guardar_config_descarga(self):
        """Guarda las opciones de la pantalla principal (cantidad, duración,
        simultáneas, checkboxes) para que la próxima vez que se abra el
        programa arranque igual que como quedó la última vez."""
        try:
            self.config_app["cantidad"] = self.var_cantidad.get()
            self.config_app["duracion_max"] = self.var_dur_max.get()
            self.config_app["simultaneas"] = self.var_simultaneas.get()
            self.config_app["descargar_toda_lista"] = self.var_lista_completa.get()
            self.config_app["analizar_bpm"] = self.var_analizar_bpm.get()
            self.config_app["normalizar_volumen"] = self.var_normalizar_volumen.get()
            self.config_app["buscar_mejores_temas"] = self.var_mejores_temas.get()
            self.config_app["busqueda_exacta"] = self.var_busqueda_exacta.get()
            self.config_app["solo_estudio"] = self.var_solo_estudio.get()
            guardar_config_app(self.config_app)
        except Exception:
            pass
    def _asegurar_sesion_firefox(self):
        threading.Thread(target=self._hilo_sesion_firefox, daemon=True).start()
    def _hilo_sesion_firefox(self):
        """Deja las cookies de Firefox listas SIN que el usuario haga nada:
        - Si Firefox ya tiene la sesión de YouTube: la usa y, si hace más de
          12 h que no se renuevan, abre Firefox unos segundos en segundo
          plano para que YouTube las rote (solo si Firefox está cerrado).
        - Si no hay sesión: abre Firefox en el inicio de sesión (una vez
          cada 12 h) y espera hasta 15 minutos a que aparezca; apenas se
          inicia sesión las usa, sin reiniciar nada."""
        try:
            if not ruta_firefox():
                return
            if firefox_tiene_sesion_youtube():
                self.exito("🍪 Firefox ya tiene tu sesión de YouTube: las cookies se usan solas, "
                           "no hay que hacer nada")
                self._renovar_cookies_si_hace_falta()
                return
            ahora = datetime.now()
            abierto = self.memoria.get("login_firefox_abierto")
            reciente = False
            if abierto:
                try:
                    reciente = (ahora - datetime.fromisoformat(abierto)).total_seconds() < 12 * 3600
                except Exception:
                    reciente = False
            if not reciente:
                self.memoria["login_firefox_abierto"] = ahora.isoformat()
                guardar_memoria(self.memoria)
                self.warning("🍪 Firefox todavía no tiene sesión de YouTube: lo abro en el inicio de sesión. "
                             "Iniciá sesión UNA sola vez; el programa detecta la sesión y sigue solo.")
                try:
                    abrir_en_navegador("https://accounts.google.com/ServiceLogin?service=youtube"
                                       "&continue=https%3A%2F%2Fwww.youtube.com%2F")
                except Exception:
                    pass
            else:
                self.warning("🍪 Esperando que inicies sesión en YouTube desde Firefox...")
            for _ in range(180):
                time.sleep(5)
                if firefox_tiene_sesion_youtube():
                    self.exito("🍪 Sesión de YouTube detectada en Firefox: las descargas ya usan tus cookies")
                    return
            self.warning("🍪 No se detectó el inicio de sesión en Firefox; se descarga sin cookies "
                         "(más riesgo de bloqueo). Se vuelve a intentar al reabrir el programa.")
        except Exception as e:
            self.debug(f"🍪 No se pudo revisar la sesión de Firefox: {e}")
    def _renovar_cookies_si_hace_falta(self, horas_max=12):
        ultima = self.memoria.get("cookies_renovadas")
        if ultima:
            try:
                if (datetime.now() - datetime.fromisoformat(ultima)).total_seconds() < horas_max * 3600:
                    return
            except Exception:
                pass
        # Si Firefox está abierto, él mismo las renueva mientras se usa.
        if navegador_esta_corriendo("firefox"):
            return
        ff = ruta_firefox()
        if not ff:
            return
        self.debug("🍪 Renovando las cookies: abro Firefox unos segundos en segundo plano...")
        proc = None
        try:
            proc = subprocess.Popen([ff, "-headless", "https://www.youtube.com/"], creationflags=CREATIONFLAGS)
            time.sleep(20)
        except Exception as e:
            self.debug(f"🍪 No se pudo renovar las cookies: {e}")
        finally:
            if proc is not None:
                try:
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                   capture_output=True, timeout=15, creationflags=CREATIONFLAGS)
                except Exception:
                    pass
        self.memoria["cookies_renovadas"] = datetime.now().isoformat()
        guardar_memoria(self.memoria)
        invalidar_cache_cookies_firefox()
    def avisar_estado_antisuplantacion(self):
        """Cuenta con qué identidad le va a pedir las cosas a YouTube."""
        nav = (navegadores_disponibles_para_cookies(self.memoria) or [None])[0]
        objetivo = target_impersonate(nav)
        if objetivo is not None:
            self.exito(f"🛡️ Impersonate activo: con cookies de {nav or 'ningún navegador'} los pedidos "
                       f"se firman como {objetivo} (huella TLS coherente, vía curl_cffi)")
        else:
            self.debug(f"🛡️ Sin impersonate para {nav or 'pedidos sin cookies'} (falta curl_cffi, o esta "
                       "versión no trae un objetivo de ese navegador): se usa el user-agent real, "
                       "coherente con las cookies")
        hechas = cargar_descargas_hechas()
        if hechas:
            self.debug(f"📚 Memoria de descargas: {len(hechas)} video(s) ya bajados que no se "
                       f"vuelven a pedir")
    def _texto_anticipacion(self):
        """Texto del panel: estado reactivo + riesgo anticipado."""
        return f"{self.anti_bloqueo.estado_texto()} · {self.predictor.estado_texto()}"
    def abrir_carpeta_descargas(self):
        carpeta = os.path.join(ruta_base(), "musica")
        os.makedirs(carpeta, exist_ok=True)
        try:
            os.startfile(carpeta)
        except Exception:
            try:
                subprocess.Popen(["explorer", carpeta])
            except Exception as e:
                self.error(f"❌ No se pudo abrir la carpeta: {e}")
    # ------------------------------------------------------------
    # BÚSQUEDA
    # ------------------------------------------------------------
    def _iniciar_busqueda(self, funcion, *args):
        """Corre una búsqueda en segundo plano y deja a la vista que está
        trabajando: los botones se deshabilitan (no se puede disparar otra
        búsqueda encima), "Buscar" muestra una animación y el cursor pasa a
        reloj de arena. Al terminar -con resultados, sin nada o con error-
        todo vuelve a la normalidad (ver _terminar_busqueda)."""
        self._buscando = True
        self.btn_buscar.config(state="disabled")
        self.btn_similares.config(state="disabled")
        try:
            self.root.config(cursor="watch")
        except Exception:
            pass
        self._animar_busqueda(0)
        def _trabajo():
            try:
                funcion(*args)
            except Exception as e:
                self.error(f"❌ Error en la búsqueda: {e}")
            finally:
                # Se agenda DESPUÉS de lo que la búsqueda haya puesto en
                # root.after (la lista de resultados), así los botones se
                # habilitan recién cuando los resultados ya están a la vista.
                self.root.after(0, self._terminar_busqueda)
        threading.Thread(target=_trabajo, daemon=True).start()
    def _animar_busqueda(self, paso):
        if not self._buscando:
            return
        puntos = "." * (paso % 4)
        self.btn_buscar.config(text=f"⏳ Buscando{puntos}")
        self.root.after(350, lambda: self._animar_busqueda(paso + 1))
    def _terminar_busqueda(self):
        self._buscando = False
        self.btn_buscar.config(text="🔎  Buscar", state="normal")
        self.btn_similares.config(state="normal")
        # Red de seguridad: si no hay ninguna descarga corriendo de verdad, el
        # botón Descargar tiene que quedar habilitado cuando aparecen resultados.
        hilo = getattr(self, '_hilo_descarga', None)
        if self.resultados and not (self.descargando and hilo is not None and hilo.is_alive()):
            self.descargando = False
            self.btn_descargar.config(state="normal")
            self.btn_detener.config(state="disabled")
        try:
            self.root.config(cursor="")
        except Exception:
            pass
    def buscar(self):
        if self._buscando:
            return
        query = self.entry_busqueda.get().strip()
        if not query:
            return
        self._guardar_busqueda_en_historial(query)
        self.listbox.delete(0, "end")
        self.resultados = []
        self.ultima_busqueda = query
        if self.var_mejores_temas.get():
            self.debug(f"🔥 Buscando los mejores temas de '{query}' (ranking real vía YouTube Music)...")
            self._iniciar_busqueda(self._buscar_mejores_temas_thread, query)
        elif not self.var_busqueda_exacta.get():
            self.debug(f"🎧 Armando el Mix relacionado con '{query}' (como el de la derecha en YouTube)...")
            self._iniciar_busqueda(self._buscar_mix_relacionado_thread, query)
        else:
            self.debug(f"🔎 Buscando '{query}'...")
            self._iniciar_busqueda(self._buscar_thread, query)
    def _previsualizar_item(self, event):
        idx = self.listbox.nearest(event.y)
        if idx < 0 or idx >= len(self.resultados):
            return
        item = self.resultados[idx]
        url = item.get("url") or f"https://www.youtube.com/watch?v={item.get('id', '')}"
        try:
            navegador = abrir_en_navegador(url)
            self.debug(f"👀 Previsualizando con {navegador}: {item.get('title', '')}")
        except Exception as e:
            self.error(f"❌ No se pudo abrir la vista previa: {e}")
    def _guardar_busqueda_en_historial(self, query):
        historial = self.memoria.setdefault("historial_busquedas", [])
        if query in historial:
            historial.remove(query)
        historial.insert(0, query)
        del historial[10:]
        self.memoria["historial_busquedas"] = historial
        guardar_memoria(self.memoria)
        self.entry_busqueda["values"] = historial
    def _buscar_thread(self, query):
        """
        Busca en YouTube, pero filtrando por "autor/nombre exacto": no
        importa si se escribe en mayúsculas o minúsculas ni los acentos,
        pero el texto completo que se tipeó tiene que aparecer tal cual
        (ya normalizado) en el título o en el canal del resultado - así
        buscar "Tito Pachanga" no llena la lista con cualquier video que
        solo tenga la palabra "pachanga" suelta de otro autor (ver
        _normalizar_para_comparar_nombre).
        """
        import yt_dlp
        cantidad = self.var_cantidad.get()
        dur_max = self.var_dur_max.get() * 60
        query_norm = _normalizar_para_comparar_nombre(query)
        try:
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": True,
                "skip_download": True,
                "logger": self._logger_ytdlp,
            }
            # Se pide bastante más de lo que se quiere mostrar: al filtrar
            # después por coincidencia exacta del nombre, muchos de los
            # primeros resultados de YouTube (que solo matchean por palabra
            # suelta) van a quedar afuera.
            cantidad_pedir = min(max(cantidad * 5, cantidad + 10), 100)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch{cantidad_pedir}:{query}", download=False)
            entradas = info.get("entries", []) if info else []
            encontrados = []
            for e in entradas:
                if not e:
                    continue
                dur = e.get("duration") or 0
                vid = e.get("id") or extraer_video_id(e.get("url", ""))
                if es_irrecuperable_conocido(self.memoria, vid):
                    continue
                titulo = e.get("title", "sin título")
                canal = e.get("channel") or e.get("uploader") or ""
                if query_norm:
                    coincide = (query_norm in _normalizar_para_comparar_nombre(titulo)
                                or query_norm in _normalizar_para_comparar_nombre(canal))
                    if not coincide:
                        continue
                if self.var_solo_estudio.get() and es_version_dudosa(titulo, query_norm):
                    continue
                if 0 < dur <= dur_max:
                    encontrados.append({
                        "title": titulo,
                        "url": e.get("url") or f"https://www.youtube.com/watch?v={vid}",
                        "id": vid,
                        "duration": dur,
                        "channel": canal,
                    })
            encontrados = self._ordenar_estudio(encontrados)[:cantidad]
            self.resultados = encontrados
            def _mostrar():
                for r in self.resultados:
                    m, s = divmod(int(r["duration"]), 60)
                    self.listbox.insert("end", f"{r['title'][:70]}  ({m}:{s:02d})")
                if self.resultados:
                    self.exito(f"✅ Encontrados {len(self.resultados)} resultados")
                else:
                    self.warning(f"⚠️ No se encontró nada que coincida exactamente con '{query}'. "
                                  "Probá escribir el nombre tal cual aparece en YouTube.")
            self.root.after(0, _mostrar)
        except Exception as e:
            self.error(f"❌ Error buscando: {e}")
    def _ordenar_estudio(self, lista):
        """Con "Solo versiones de estudio": pasan primero los canales de
        audio oficial (Topic/VEVO/oficial) y se descartan los duplicados de
        un mismo tema (se queda el de mejor canal). Sin el tilde, no toca nada."""
        if not self.var_solo_estudio.get():
            return lista
        ordenada = sorted(lista, key=lambda r: -puntaje_canal_estudio(r.get("channel", "")))
        vistos, salida = set(), []
        for r in ordenada:
            clave = _normalizar_para_comparar_nombre(limpiar_titulo_video(r.get("title", "")))
            if clave in vistos:
                continue
            vistos.add(clave)
            salida.append(r)
        return salida
    def _crear_ytmusic(self):
        """
        Crea el cliente de YouTube Music. No hace falta ninguna cuenta ni
        API key - a diferencia de Last.fm, ytmusicapi habla directo con los
        endpoints públicos de music.youtube.com de forma anónima, igual que
        yt-dlp habla con youtube.com.
        """
        from ytmusicapi import YTMusic
        return YTMusic()
    def _buscar_mejores_temas_thread(self, consulta):
        """
        "🔥 Mejores temas". Antes tomaba lo escrito como NOMBRE DE ARTISTA,
        agarraba el primer artista que coincidiera y listaba su sección
        "Canciones" (que YouTube Music limita a unos 5 temas): por eso, con
        un género como "cumbias", traía casi nada y de un artista cualquiera.
        Ahora distingue dos casos y junta candidatos de varias fuentes:
          - Si lo escrito ES un artista (coincidencia casi exacta): se trae
            su lista COMPLETA de temas populares, no solo los primeros 5.
          - Si es un género o un tema ("cumbias", "cuarteto"): se buscan
            canciones y videos con varias formulaciones ("mejores...",
            "... éxitos", "... más escuchadas" y, en géneros de baile,
            "... bailables"/"... bailanteras") y las playlists armadas por
            gente que ya eligió lo mejor del género.
        Cada tema suma puntos por cada fuente en la que aparece (más arriba
        = más puntos; que aparezca en playlists y en varias búsquedas pesa
        más) y, cuando YouTube Music informa las reproducciones, un bonus
        por popularidad. Se quedan los de mayor puntaje.
        """
        import math
        import difflib
        cantidad = self.var_cantidad.get()
        dur_max = self.var_dur_max.get() * 60
        try:
            yt_music = self._crear_ytmusic()
        except Exception as e:
            self.error(f"❌ No se pudo consultar YouTube Music: {e}")
            return
        consulta_norm = _normalizar_para_comparar_nombre(consulta)
        limite = min(100, max(40, cantidad))
        candidatos = {}
        conteo_fuentes = {}
        def _sumar(items, peso, fuente):
            usados = 0
            for pos, it in enumerate(items or []):
                vid = it.get("videoId")
                if not vid:
                    continue
                c = candidatos.setdefault(vid, {
                    "id": vid, "title": it.get("title") or "", "score": 0.0, "fuentes": set(),
                    "vistas": 0, "dur": 0,
                    "artistas": ", ".join(a.get("name", "") for a in (it.get("artists") or [])
                                          if a.get("name"))})
                c["score"] += peso / (1 + pos / 10.0)
                c["fuentes"].add(fuente)
                c["tipo"] = c.get("tipo") or it.get("videoType")
                if not c.get("album"):
                    alb = it.get("album")
                    c["album"] = (alb.get("name") if isinstance(alb, dict) else alb) or ""
                if not c.get("portada") and it.get("thumbnails"):
                    c["portada"] = (it["thumbnails"][-1] or {}).get("url", "")
                c["vistas"] = max(c["vistas"], _parsear_vistas(it.get("views")))
                if not c["dur"]:
                    c["dur"] = it.get("duration_seconds") or _parsear_duracion_texto(it.get("duration")) or 0
                usados += 1
            conteo_fuentes[fuente] = conteo_fuentes.get(fuente, 0) + usados
        def _buscar(q, filtro, lim):
            try:
                return yt_music.search(q, filter=filtro, limit=lim) or []
            except Exception as e:
                self.debug(f"⚠️ YouTube Music: falló la búsqueda '{q}' ({filtro}): {str(e)[:80]}")
                return []
        # --- ¿lo escrito es un artista? (coincidencia casi exacta) ---
        # Con "Búsqueda exacta" tildada, si lo escrito NO es un género (cumbia,
        # salsa...) sino un nombre propio, solo valen los temas donde ese
        # nombre aparece en el título o en el artista.
        _generos = ("cumbia", "cuarteto", "reggaeton", "salsa", "bachata", "merengue",
                    "bailant", "tropical", "latin", "electr", "rock", "pop", "folklore",
                    "tango", "trap", "rap", "techno", "house")
        exigir_nombre = bool(self.var_busqueda_exacta.get()) and not any(g in consulta_norm for g in _generos)
        artista = None
        for a in _buscar(consulta, "artists", 3):
            nombre = a.get("artist") or ""
            if difflib.SequenceMatcher(None, consulta_norm, _normalizar_para_comparar_nombre(nombre)).ratio() >= 0.85:
                artista = a
                break
        if artista:
            nombre_art = artista.get("artist") or consulta
            self.debug(f"🔥 '{consulta}' es un artista: busco TODOS sus temas populares ({nombre_art})")
            try:
                datos = yt_music.get_artist(artista.get("browseId"))
                seccion = datos.get("songs") or {}
                lista = None
                if seccion.get("browseId"):
                    lista = (yt_music.get_playlist(seccion["browseId"], limit=limite) or {}).get("tracks")
                _sumar(lista or seccion.get("results"), 3.0, "artista")
            except Exception as e:
                self.debug(f"⚠️ No se pudo traer la lista completa del artista: {str(e)[:80]}")
            _sumar(_buscar(consulta, "songs", limite), 1.0, "búsqueda")
        else:
            variantes = [consulta, f"mejores {consulta}", f"{consulta} éxitos", f"{consulta} más escuchadas"]
            if any(g in consulta_norm for g in ("cumbia", "cuarteto", "reggaeton", "salsa", "bachata",
                                                "merengue", "bailant", "tropical", "latin", "electr")):
                variantes.append(f"{consulta} bailables")
                if "cumbia" in consulta_norm:
                    variantes.append(f"{consulta} bailanteras")
            self.debug(f"🔥 '{consulta}' es un género/tema: junto candidatos de {len(variantes)} "
                       f"búsquedas y de playlists")
            for q in variantes:
                _sumar(_buscar(q, "songs", limite), 1.0, "canciones")
            for q in variantes[:2]:
                _sumar(_buscar(q, "videos", limite), 0.8, "videos")
            for pl in _buscar(consulta, "playlists", 6)[:4]:
                try:
                    pista = (yt_music.get_playlist(pl.get("browseId"), limit=limite) or {}).get("tracks")
                    _sumar(pista, 1.5, "playlists")
                except Exception as e:
                    self.debug(f"⚠️ No se pudo leer una playlist: {str(e)[:80]}")
        # --- puntaje final, filtros y deduplicado ---
        # Fuera lo que no es "un tema": karaokes, tutoriales, reacciones y
        # los mixes/enganchados (un solo video de 20 minutos con muchos temas).
        patron_basura = re.compile(
            r"karaoke|tutorial|reacci|reaction|\bmix\b|megamix|enganchad|album completo|full album|playlist")
        # Tope de duración propio de "Mejores temas": un tema normal no pasa de
        # ~8 min. Se puede cambiar con "mejores_temas_dur_max" (minutos) en
        # config/config_app.json; nunca supera la duración máxima de la pantalla.
        try:
            tope_tema = int(float(self.config_app.get("mejores_temas_dur_max", 8)) * 60)
        except Exception:
            tope_tema = 8 * 60
        if dur_max:
            tope_tema = min(tope_tema, dur_max)
        mejores = {}
        for c in candidatos.values():
            titulo_norm = _normalizar_para_comparar_nombre(c["title"])
            if not titulo_norm or patron_basura.search(titulo_norm):
                continue
            if es_irrecuperable_conocido(self.memoria, c["id"]):
                continue
            if c["dur"] and tope_tema and c["dur"] > tope_tema:
                continue
            if exigir_nombre and consulta_norm not in titulo_norm \
                    and consulta_norm not in _normalizar_para_comparar_nombre(c["artistas"]):
                continue
            estudio = self.var_solo_estudio.get()
            tipo = c.get("tipo")
            # Si el nombre buscado es justamente el autor/canal del video, es del
            # propio autor: no se lo trata como "casero" aunque YouTube lo
            # marque como subido por usuario (UGC).
            del_autor = bool(exigir_nombre) and consulta_norm in _normalizar_para_comparar_nombre(c["artistas"])
            if estudio and not del_autor:
                # Videos subidos por cualquiera (UGC) o de origen desconocido
                # que solo salieron de la búsqueda de "videos": es de donde
                # vienen los temas modificados caseros. Se descartan.
                if tipo == "MUSIC_VIDEO_TYPE_UGC":
                    continue
                if tipo is None and c["fuentes"] <= {"videos"}:
                    continue
            if estudio and es_version_dudosa(c["title"], consulta_norm, tipo):
                continue
            puntaje = c["score"] + 0.5 * (len(c["fuentes"]) - 1)
            if estudio:
                puntaje += {"MUSIC_VIDEO_TYPE_ATV": 1.0, "MUSIC_VIDEO_TYPE_OMV": 0.3}.get(tipo, 0.0)
            if c["vistas"] > 0:
                puntaje += 0.6 * max(0.0, math.log10(c["vistas"]) - 5)
            c["puntaje"] = puntaje
            primer_artista = _normalizar_para_comparar_nombre(c["artistas"].split(",")[0]) if c["artistas"] else ""
            clave = f"{titulo_norm}|{primer_artista}"
            if clave not in mejores or puntaje > mejores[clave]["puntaje"]:
                mejores[clave] = c
        ordenados = sorted(mejores.values(), key=lambda x: x["puntaje"], reverse=True)[:cantidad]
        if exigir_nombre and not ordenados:
            # YouTube Music no tiene nada con ese nombre: se busca en YouTube
            # común (mismo filtro exacto por título/canal).
            self.debug(f"🔁 YouTube Music no tiene '{consulta}': busco en YouTube normal")
            self._buscar_thread(consulta)
            return
        detalle = ", ".join(f"{k}: {v}" for k, v in conteo_fuentes.items())
        self.debug(f"🔥 Candidatos reunidos: {len(candidatos)} ({detalle}) -> {len(ordenados)} elegidos")
        encontrados = []
        for posicion, c in enumerate(ordenados, start=1):
            encontrados.append({
                "title": c["title"],
                "url": f"https://www.youtube.com/watch?v={c['id']}",
                "id": c["id"],
                "duration": c["dur"],
                "channel": c["artistas"],
                "ranking": posicion,
                "artista": c["artistas"],
                "album": c.get("album", ""),
                "portada_url": c.get("portada", ""),
                "titulo_limpio": c["title"],
            })
        self.resultados = encontrados
        def _mostrar():
            for r in self.resultados:
                if r["duration"]:
                    m, s = divmod(int(r["duration"]), 60)
                    dur_txt = f"({m}:{s:02d})"
                else:
                    dur_txt = ""
                autor = f" — {r['channel'][:22]}" if r["channel"] else ""
                self.listbox.insert("end", f"🔥 #{r['ranking']} {r['title'][:52]}{autor}  {dur_txt}")
            if self.resultados:
                self.exito(f"✅ {len(self.resultados)} de los mejores temas de '{consulta}' (YouTube Music)")
            else:
                if exigir_nombre:
                    self.warning(f"⚠️ No hay temas cuyo título o artista contenga '{consulta}'. "
                                 f"Probá sin 'Búsqueda exacta' o revisá cómo está escrito.")
                else:
                    self.warning(f"⚠️ No se pudo armar la lista de mejores temas de '{consulta}'")
        self.root.after(0, _mostrar)
    def buscar_similares(self):
        """
        A partir del tema seleccionado en los resultados, busca más temas
        del mismo canal/artista (la forma confiable de encontrar "más como
        este" sin haber descargado nada todavía - el BPM/ritmo real recién
        se puede medir sobre el audio ya bajado, así que emparejar por
        ritmo ANTES de descargar no es posible; esto es lo más parecido
        que se puede hacer de forma confiable antes de la descarga).
        """
        if self._buscando:
            return
        seleccion = self.listbox.curselection()
        if not seleccion or not self.resultados:
            self.warning("⚠️ Elegí primero un tema de los resultados")
            return
        idx = seleccion[0]
        if idx >= len(self.resultados):
            return
        item = self.resultados[idx]
        canal = item.get("channel", "")
        if not canal:
            self.warning("⚠️ Este resultado no tiene canal identificado, no se puede buscar similares")
            return
        self.listbox.delete(0, "end")
        self.resultados = []
        self.ultima_busqueda = canal
        self.debug(f"🎯 Buscando más temas de '{canal}'...")
        self._iniciar_busqueda(self._buscar_similares_thread, canal, item.get("id", ""))
    def _buscar_similares_thread(self, canal, video_id_excluir):
        """
        Igual que _buscar_thread: filtra por coincidencia exacta del nombre
        del canal (sin importar mayúsculas/acentos) para no traer temas de
        otros canales que solo comparten alguna palabra suelta con este.
        """
        import yt_dlp
        cantidad = self.var_cantidad.get()
        dur_max = self.var_dur_max.get() * 60
        canal_norm = _normalizar_para_comparar_nombre(canal)
        try:
            ydl_opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True,
                        "logger": self._logger_ytdlp}
            cantidad_pedir = min(max(cantidad * 5, cantidad + 10), 100)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch{cantidad_pedir}:{canal}", download=False)
            entradas = info.get("entries", []) if info else []
            encontrados = []
            for e in entradas:
                if not e:
                    continue
                dur = e.get("duration") or 0
                vid = e.get("id") or extraer_video_id(e.get("url", ""))
                if vid == video_id_excluir or es_irrecuperable_conocido(self.memoria, vid):
                    continue
                canal_e = e.get("channel") or e.get("uploader") or ""
                if canal_norm and canal_norm not in _normalizar_para_comparar_nombre(canal_e):
                    continue
                if self.var_solo_estudio.get() and es_version_dudosa(e.get("title", ""), canal_norm):
                    continue
                if 0 < dur <= dur_max:
                    encontrados.append({
                        "title": e.get("title", "sin título"),
                        "url": e.get("url") or f"https://www.youtube.com/watch?v={vid}",
                        "id": vid,
                        "duration": dur,
                        "channel": canal_e,
                    })
                if len(encontrados) >= cantidad:
                    break
            self.resultados = encontrados
            def _mostrar():
                for r in self.resultados:
                    m, s = divmod(int(r["duration"]), 60)
                    self.listbox.insert("end", f"{r['title'][:70]}  ({m}:{s:02d})")
                if self.resultados:
                    self.exito(f"✅ {len(self.resultados)} temas más de '{canal}'")
                else:
                    self.warning(f"⚠️ No se encontraron más temas del mismo canal '{canal}'")
            self.root.after(0, _mostrar)
        except Exception as e:
            self.error(f"❌ Error buscando similares: {e}")
    def _buscar_mix_relacionado_thread(self, query):
        """
        "🔎 Búsqueda exacta" destildado: en vez de traer solo lo que
        coincide con el texto tipeado, arma el mismo tipo de playlist
        "Mix" que YouTube arma automáticamente y que se ve a la derecha de
        los resultados de una búsqueda (temas relacionados/similares).
        YouTube arma esos "Mix" a partir de un video "semilla" con una
        lista que empieza con "RD" + el id de ese video (es el mismo
        mecanismo que usa el botón "Mix" que aparece debajo de cualquier
        video) - así que primero se busca cuál es el video que mejor
        coincide con lo tipeado (mismo filtro por nombre exacto que usa la
        búsqueda normal) y después se pide esa lista relacionada.
        """
        import yt_dlp
        cantidad = self.var_cantidad.get()
        dur_max = self.var_dur_max.get() * 60
        query_norm = _normalizar_para_comparar_nombre(query)
        try:
            ydl_opts_base = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": True,
                "skip_download": True,
                "logger": self._logger_ytdlp,
            }
            # Paso 1: encontrar el video "semilla" a partir del cual armar
            # el Mix - el primer resultado que matchea de verdad lo
            # tipeado, para no terminar armando un Mix de cualquier cosa.
            with yt_dlp.YoutubeDL(ydl_opts_base) as ydl:
                info_busqueda = ydl.extract_info(f"ytsearch10:{query}", download=False)
            entradas_busqueda = info_busqueda.get("entries", []) if info_busqueda else []
            semilla = None
            for e in entradas_busqueda:
                if not e:
                    continue
                vid = e.get("id") or extraer_video_id(e.get("url", ""))
                if not vid or es_irrecuperable_conocido(self.memoria, vid):
                    continue
                titulo = e.get("title", "")
                canal = e.get("channel") or e.get("uploader") or ""
                if query_norm:
                    coincide = (query_norm in _normalizar_para_comparar_nombre(titulo)
                                or query_norm in _normalizar_para_comparar_nombre(canal))
                    if not coincide:
                        continue
                semilla = {"id": vid, "title": titulo}
                break
            if semilla is None and entradas_busqueda:
                # Nada matcheó exacto - mejor un Mix aproximado que no
                # traer nada: usamos el primer resultado tal cual.
                e = entradas_busqueda[0]
                vid = e.get("id") or extraer_video_id(e.get("url", ""))
                if vid:
                    semilla = {"id": vid, "title": e.get("title", "")}
            if semilla is None:
                self.root.after(0, lambda: self.warning(
                    f"⚠️ No se encontró ningún video para armar el Mix de '{query}'"))
                return
            self.debug(f"🎧 Armando el Mix de YouTube a partir de '{semilla['title'][:60]}'...")
            # Paso 2: pedir la playlist "Mix"/Radio de YouTube (lista "RD"
            # + id del video semilla) - la misma lista de temas
            # relacionados que se ve a la derecha en una búsqueda.
            url_mix = f"https://www.youtube.com/watch?v={semilla['id']}&list=RD{semilla['id']}"
            ydl_opts_mix = dict(ydl_opts_base)
            ydl_opts_mix["playlistend"] = min(max(cantidad * 3, cantidad + 10), 100)
            with yt_dlp.YoutubeDL(ydl_opts_mix) as ydl:
                info_mix = ydl.extract_info(url_mix, download=False)
            entradas_mix = info_mix.get("entries", []) if info_mix else []
            encontrados = []
            for e in entradas_mix:
                if not e:
                    continue
                dur = e.get("duration") or 0
                vid = e.get("id") or extraer_video_id(e.get("url", ""))
                if not vid or vid == semilla["id"] or es_irrecuperable_conocido(self.memoria, vid):
                    continue
                if not (0 < dur <= dur_max):
                    continue
                if self.var_solo_estudio.get() and es_version_dudosa(e.get("title", ""), query_norm):
                    continue
                encontrados.append({
                    "title": e.get("title", "sin título"),
                    "url": e.get("url") or f"https://www.youtube.com/watch?v={vid}",
                    "id": vid,
                    "duration": dur,
                    "channel": e.get("channel") or e.get("uploader") or "",
                })
                if len(encontrados) >= cantidad:
                    break
            self.resultados = encontrados
            def _mostrar():
                for r in self.resultados:
                    m, s = divmod(int(r["duration"]), 60)
                    self.listbox.insert("end", f"🎧 {r['title'][:65]}  ({m}:{s:02d})")
                if self.resultados:
                    self.exito(f"✅ {len(self.resultados)} temas del Mix relacionado con '{query}'")
                else:
                    self.warning(f"⚠️ El Mix de '{query}' no trajo temas dentro de la duración máxima "
                                  "(probá con 'Búsqueda exacta' tildado)")
            self.root.after(0, _mostrar)
        except Exception as e:
            self.error(f"❌ Error armando el Mix: {e}")
    # ------------------------------------------------------------
    # DESCARGA
    # ------------------------------------------------------------
    def detener(self):
        self.abortar = True
        self.warning("⏹ Deteniendo tras la descarga en curso...")
    def iniciar_descarga(self):
        if self.descargando:
            return
        if not self.resultados:
            self.warning("⚠️ Primero buscá algo")
            return
        self._guardar_config_descarga()
        seleccion = self.listbox.curselection()
        es_lista_completa = self.var_lista_completa.get() or not seleccion
        if es_lista_completa:
            items = list(enumerate(self.resultados))
        else:
            items = [(i, self.resultados[i]) for i in seleccion]
        # limpia marcas de una corrida anterior en los items que se van a tocar
        for idx_listbox, _ in items:
            self._marcar_item(idx_listbox, "pendiente")
        self.abortar = False
        self.descargando = True
        self.btn_descargar.config(state="disabled")
        self.btn_detener.config(state="normal")
        carpeta = self._carpeta_destino_descarga(es_lista_completa)
        self._hilo_descarga = threading.Thread(target=self._descargar_lista_protegida, args=(items, carpeta), daemon=True)
        self._hilo_descarga.start()
    def _descargar_lista_protegida(self, items, carpeta):
        """Corre la tanda y, pase lo que pase (un error inesperado, un hilo que
        falla), siempre deja la ventana lista para otra descarga: antes, si algo
        reventaba a mitad de camino, el botón Descargar quedaba deshabilitado
        para siempre."""
        try:
            self._descargar_lista(items, carpeta)
        except Exception as e:
            try:
                self.error(f"❌ Error inesperado en la descarga: {e}")
            except Exception:
                pass
        finally:
            try:
                self.root.after(0, lambda: self.lbl_espera.config(text=""))
                self.root.after(0, self._descarga_finalizada)
            except Exception:
                pass
    def _carpeta_destino_descarga(self, es_lista_completa):
        """
        "Descargar toda la lista" encarpeta por autor: cada búsqueda arma
        (o reutiliza, si ya existía) una carpeta con el mismo nombre de lo
        que se buscó, dentro de "musica". Si esa carpeta ya existe, la
        búsqueda nueva usa "nombre_1", "nombre_2", etc. Si se descargan solo temas sueltos seleccionados
        a mano (no toda la lista), quedan en la carpeta raíz "musica" como
        siempre, porque ahí no hay un único autor claro al cual asociarlos.
        """
        base = os.path.join(ruta_base(), "musica")
        if es_lista_completa and self.ultima_busqueda:
            subcarpeta = nombre_carpeta_valido(self.ultima_busqueda)
            if subcarpeta:
                # Si esta MISMA búsqueda ya armó su carpeta (por ejemplo se
                # apretó Detener y después Descargar para seguir), se sigue
                # usando esa: si no, cada reanudación abriría una carpeta nueva.
                previa = getattr(self, "_carpeta_busqueda_actual", None)
                if previa and previa[0] is self.resultados and os.path.isdir(previa[1]):
                    return previa[1]
                # Búsqueda nueva: si ya existe una carpeta con ese nombre se
                # crea "nombre_1", "nombre_2", etc. (nunca se mezclan).
                candidata = os.path.join(base, subcarpeta)
                n = 0
                while os.path.exists(candidata):
                    n += 1
                    candidata = os.path.join(base, f"{subcarpeta}_{n}")
                self._carpeta_busqueda_actual = (self.resultados, candidata)
                return candidata
        return base
    def _debe_abortar(self):
        return self.abortar
    def _sonda_salud(self, items):
        """Pregunta barata para medir cómo está YouTube AHORA MISMO.
        No descarga audio: pide solo la metadata del primer video de la
        lista, con el mejor cliente y las cookies disponibles, que es
        exactamente lo que después hace cada descarga real. Si ya viene
        frenado, la tanda entera arranca en modo cauteloso en vez de
        descubrirlo perdiendo las primeras descargas de la cola.
        Solo se hace cuando hay cola: con un solo video no vale la pena."""
        import yt_dlp
        if not items or self.abortar or len(items) < 2:
            return
        item = items[0][1]
        navegadores = navegadores_disponibles_para_cookies(self.memoria)
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "socket_timeout": 15,
            "extractor_args": {"youtube": {"player_client": ranking_clientes(self.memoria)[:1]}},
            "logger": self._logger_ytdlp,
        }
        archivo_cookies = None
        _aplicar_antisuplantacion(ydl_opts, navegadores[0] if navegadores else None)
        if navegadores:
            archivo_cookies = aplicar_cookies(ydl_opts, navegadores[0])
        cache = _cachedir_ytdlp()
        if cache:
            ydl_opts["cachedir"] = cache
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(item["url"], download=False)
            self.predictor.calibrar_con_sonda(True)
        except Exception as e:
            tipo = self.anti_bloqueo.clasificar_error(str(e))
            if tipo == "bloqueo":
                # Esto sube el nivel y dispara la pausa preventiva global
                # ANTES de que la cola empiece a golpear.
                self.anti_bloqueo.registrar_error(str(e))
                self.predictor.calibrar_con_sonda(False, "señal de bloqueo en la sonda", es_bloqueo=True)
            else:
                # Un error local (permisos, red) NO significa que YouTube
                # esté frenando: se informa distinto para no asustar ni
                # subir la cautela sin motivo.
                self.predictor.calibrar_con_sonda(False, tipo, es_bloqueo=False)
        finally:
            borrar_temporal(archivo_cookies)
    def _descargar_lista(self, items, carpeta):
        os.makedirs(carpeta, exist_ok=True)
        simultaneas = max(1, min(4, self.var_simultaneas.get()))
        # Los temas que ya están bajados se sacan de la cola ANTES de
        # cualquier cosa: no se les pide ni la metadata (ni la sonda). Volver
        # a buscar algo que ya tenés pasa a costar cero pedidos a YouTube,
        # que es la mejor defensa contra el bloqueo que hay.
        pendientes = []
        ya_estaban = 0
        copiados = 0
        indice_musica = None
        raiz_musica = os.path.join(ruta_base(), "musica")
        for idx_lista, item in items:
            if ya_descargado(item.get("id"), carpeta):
                ya_estaban += 1
                self._marcar_item(idx_lista, "exito")
                continue
            # Carpeta nueva (nombre_1, nombre_2...): si el tema ya lo bajaste
            # antes y está en OTRA carpeta de "musica", se COPIA acá en vez
            # de volver a pedírselo a YouTube.
            if os.path.normpath(carpeta) != os.path.normpath(raiz_musica):
                if indice_musica is None:
                    indice_musica = self._indice_musica(carpeta)
                if self._copiar_si_ya_estaba(item.get("id"), carpeta, indice_musica):
                    copiados += 1
                    self._marcar_item(idx_lista, "exito")
                    continue
            pendientes.append((idx_lista, item))
        if copiados:
            self.exito(f"♻️ {copiados} tema(s) ya los tenías en otra carpeta: se copiaron sin pedirle nada a YouTube")
        if ya_estaban:
            self.exito(f"⏭️ {ya_estaban} tema(s) ya estaban descargados: no se les pide nada a YouTube")
        items = pendientes
        if not items:
            self.memoria["ultima_sesion"] = datetime.now().isoformat()
            guardar_memoria(self.memoria)
            self.exito("\n===== RESUMEN: no había nada nuevo para bajar =====")
            self.root.after(0, lambda: self.lbl_espera.config(text=""))
            self.root.after(0, self._descarga_finalizada)
            return
        # SONDA DE SALUD: un pedido barato antes de arrancar la tanda (ver
        # _sonda_salud).
        self._cola_guardar(items, carpeta)
        self._sonda_salud(items)
        if simultaneas <= 1:
            self._descargar_lista_secuencial(items, carpeta)
        else:
            self._descargar_lista_paralela(items, carpeta, simultaneas)
    def _descargar_lista_paralela(self, items, carpeta, simultaneas):
        """
        Cola de descargas con varios workers a la vez. Cada descarga individual
        sigue pasando por el mismo AntiBloqueoManager (con lock interno, ver
        AntiBloqueoManager._lock) así que si YouTube empieza a poner problemas,
        TODOS los workers frenan juntos - no es concurrencia libre, es la misma
        cautela de siempre repartida en varios hilos. El arranque de cada
        worker se escalona un poco para no golpear todo de una.
        """
        total = len(items)
        contador_lock = threading.Lock()
        estado = {"completados": 0, "exitos": 0, "fallos": 0, "fallidos": []}
        control_concurrencia = ControlConcurrenciaAdaptativo(self.anti_bloqueo, simultaneas, consola=self,
                                                            predictor=self.predictor)
        def _procesar(idx_listbox, item):
            if self.abortar:
                return
            control_concurrencia.adquirir()
            try:
                if self.abortar:
                    return
                self.anti_bloqueo.esperar_turno(control_concurrencia._limite_actual(), self._debe_abortar)
                if self.abortar:
                    return
                self._marcar_item(idx_listbox, "descargando")
                if es_irrecuperable_conocido(self.memoria, item["id"]):
                    self.warning(f"⏭️ Ya sabíamos que este video no está disponible, se salta: {item['title']}")
                    ok = False
                else:
                    ok = self._descargar_uno(item, carpeta)
            finally:
                control_concurrencia.liberar()
            with contador_lock:
                estado["completados"] += 1
                if ok:
                    estado["exitos"] += 1
                    self.memoria["total_descargas_exitosas"] = self.memoria.get("total_descargas_exitosas", 0) + 1
                else:
                    estado["fallos"] += 1
                    estado["fallidos"].append((idx_listbox, item))
                    self.memoria["total_descargas_fallidas"] = self.memoria.get("total_descargas_fallidas", 0) + 1
                self._marcar_item(idx_listbox, "exito" if ok else "fallo")
                self._actualizar_progreso(estado["completados"] - 1, total)
                if ok or item.get("id") in self._descartados:
                    self._cola_quitar(item.get("id"))
                guardar_memoria(self.memoria)
            self.root.after(0, lambda: self.lbl_anti_bloqueo.config(text=self._texto_anticipacion()))
            self.root.after(0, lambda: self.lbl_ritmo.config(text=f"Ritmo aprendido: {self.anti_bloqueo.delay_base}s"))
        with concurrent.futures.ThreadPoolExecutor(max_workers=simultaneas) as executor:
            futuros = []
            for idx_listbox, item in items:
                if self.abortar:
                    self.warning("⏹ Descarga detenida por el usuario")
                    break
                futuros.append(executor.submit(_procesar, idx_listbox, item))
            concurrent.futures.wait(futuros)
        if estado["fallidos"] and not self.abortar:
            recuperados = self._segunda_vuelta(estado["fallidos"], carpeta)
            estado["exitos"] += recuperados
            estado["fallos"] -= recuperados
        self.anti_bloqueo.guardar_en_memoria()
        self.memoria["ultima_sesion"] = datetime.now().isoformat()
        guardar_memoria(self.memoria)
        self.exito(f"\n===== RESUMEN: {estado['exitos']} ok / {estado['fallos']} fallidos de {total} =====")
        if not self.abortar:
            self._cola_borrar()
        self.root.after(0, lambda: self.lbl_espera.config(text=""))
        self.root.after(0, self._descarga_finalizada)
    def _descargar_lista_secuencial(self, items, carpeta):
        total = len(items)
        exitos = 0
        fallos = 0
        fallidos = []
        for pos, (idx_listbox, item) in enumerate(items):
            if self.abortar:
                self.warning("⏹ Descarga detenida por el usuario")
                break
            self.debug(f"\n--- [{pos + 1}/{total}] {item['title']} ---")
            self._marcar_item(idx_listbox, "descargando")
            if es_irrecuperable_conocido(self.memoria, item["id"]):
                self.warning(f"⏭️ Ya sabíamos que este video no está disponible, se salta: {item['title']}")
                fallos += 1
                self._marcar_item(idx_listbox, "fallo")
                self._actualizar_progreso(pos, total)
                continue
            ok = self._descargar_uno(item, carpeta)
            if ok:
                exitos += 1
                self.memoria["total_descargas_exitosas"] = self.memoria.get("total_descargas_exitosas", 0) + 1
                self._marcar_item(idx_listbox, "exito")
            else:
                fallos += 1
                fallidos.append((idx_listbox, item))
                self.memoria["total_descargas_fallidas"] = self.memoria.get("total_descargas_fallidas", 0) + 1
                self._marcar_item(idx_listbox, "fallo")
            self._actualizar_progreso(pos, total)
            self.root.after(0, lambda: self.lbl_anti_bloqueo.config(text=self._texto_anticipacion()))
            self.root.after(0, lambda: self.lbl_ritmo.config(text=f"Ritmo aprendido: {self.anti_bloqueo.delay_base}s"))
            guardar_memoria(self.memoria)
            if ok or item.get("id") in self._descartados:
                self._cola_quitar(item.get("id"))
            if pos < total - 1 and not self.abortar:
                espera = self.anti_bloqueo.delay_actual() * random.uniform(0.85, 1.15)
                self.debug(f"⏸ Pausa de {espera:.1f}s antes del siguiente...")
                self._esperar(espera)
        if fallidos and not self.abortar:
            recuperados = self._segunda_vuelta(fallidos, carpeta)
            exitos += recuperados
            fallos -= recuperados
        self.anti_bloqueo.guardar_en_memoria()
        self.memoria["ultima_sesion"] = datetime.now().isoformat()
        guardar_memoria(self.memoria)
        self.exito(f"\n===== RESUMEN: {exitos} ok / {fallos} fallidos de {total} =====")
        if not self.abortar:
            self._cola_borrar()
        self.root.after(0, lambda: self.lbl_espera.config(text=""))
        self.root.after(0, self._descarga_finalizada)
    # --- COLA REANUDABLE: si el programa se cierra a la mitad de una tanda,
    #     lo que faltaba queda guardado y al abrirlo se ofrece seguir. ---
    def _archivo_cola(self):
        return ruta_config("cola_pendiente.json")
    def _cola_guardar(self, items, carpeta):
        with self._lock_cola:
            self._cola = {"busqueda": self.ultima_busqueda, "carpeta": carpeta,
                          "fecha": datetime.now().isoformat(), "items": [it for _, it in items]}
            try:
                escribir_json_atomico(self._archivo_cola(), self._cola, indent=1)
            except Exception:
                pass
    def _cola_quitar(self, video_id):
        with self._lock_cola:
            if not self._cola or not video_id:
                return
            antes = len(self._cola["items"])
            self._cola["items"] = [i for i in self._cola["items"] if i.get("id") != video_id]
            if len(self._cola["items"]) != antes:
                try:
                    escribir_json_atomico(self._archivo_cola(), self._cola, indent=1)
                except Exception:
                    pass
    def _cola_borrar(self):
        with self._lock_cola:
            self._cola = None
            try:
                os.remove(self._archivo_cola())
            except OSError:
                pass
    def _ofrecer_reanudar(self):
        ruta = self._archivo_cola()
        if self.descargando or not os.path.exists(ruta):
            return
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                datos = json.load(f)
            carpeta = datos.get("carpeta") or ""
            pendientes = [it for it in datos.get("items", [])
                          if it.get("id") and not ya_descargado(it["id"], carpeta)]
        except Exception:
            self._cola_borrar()
            return
        if not pendientes:
            self._cola_borrar()
            return
        busqueda = datos.get("busqueda") or "la última búsqueda"
        if not messagebox.askyesno(
                "Descarga interrumpida",
                f"La descarga de '{busqueda}' quedó a medias: faltan {len(pendientes)} tema(s).\n\n"
                "¿Querés continuarla ahora?"):
            self._cola_borrar()
            return
        os.makedirs(carpeta, exist_ok=True)
        self.listbox.delete(0, "end")
        self.resultados = pendientes
        self.ultima_busqueda = datos.get("busqueda", "")
        for r in pendientes:
            m, s = divmod(int(r.get("duration") or 0), 60)
            self.listbox.insert("end", f"{(r.get('title') or '')[:70]}  ({m}:{s:02d})")
        self._carpeta_busqueda_actual = (self.resultados, carpeta)
        self.var_lista_completa.set(True)
        self.exito(f"▶️ Retomando la descarga de '{busqueda}': {len(pendientes)} tema(s) pendientes")
        self.iniciar_descarga()
    def _indice_musica(self, carpeta_excluir):
        """Lista (carpeta, archivo) de todo el audio que ya hay en "musica",
        salvo la carpeta de destino. Se arma una sola vez por tanda."""
        indice = []
        raiz = os.path.join(ruta_base(), "musica")
        excluir = os.path.normpath(carpeta_excluir)
        try:
            for dirpath, _dirs, archivos in os.walk(raiz):
                if os.path.normpath(dirpath) == excluir:
                    continue
                for a in archivos:
                    if a.lower().endswith((".mp3", ".m4a", ".flac")) and not a.startswith(PREFIJO_TEMPORAL):
                        indice.append((dirpath, a))
        except Exception:
            pass
        return indice
    def _copiar_si_ya_estaba(self, video_id, carpeta, indice):
        import shutil
        ficha = cargar_descargas_hechas().get(video_id) or {}
        nombre = ficha.get("archivo") or ""
        base = os.path.splitext(nombre)[0]
        if not base:
            return False
        origen = next((os.path.join(d, a) for d, a in indice if a == nombre), None)
        if origen is None:
            # El post-proceso pudo agregarle el BPM adelante al nombre.
            origen = next((os.path.join(d, a) for d, a in indice if base in a), None)
        if origen is None:
            return False
        try:
            os.makedirs(carpeta, exist_ok=True)
            destino = os.path.join(carpeta, os.path.basename(origen))
            if not os.path.exists(destino):
                shutil.copy2(origen, destino)
            return True
        except Exception:
            return False
    def _segunda_vuelta(self, fallidos, carpeta):
        """Segunda oportunidad para lo que falló por causas pasajeras (un
        cliente que no andó, un corte, una pausa de bloqueo): se reintenta
        UNA vez, de a uno y con calma, cuando ya pasó el apuro. Los videos
        con fallo definitivo (no disponible, privado, etc.) se saltean."""
        candidatos = [(i, it) for i, it in fallidos
                      if it.get("id") not in self._descartados
                      and not es_irrecuperable_conocido(self.memoria, it.get("id"))]
        if not candidatos or self.abortar:
            return 0
        self.warning(f"🔁 Segunda vuelta: reintento {len(candidatos)} tema(s) que fallaron, "
                     f"de a uno y con calma")
        self._esperar(random.uniform(30, 60))
        recuperados = 0
        for pos, (idx_listbox, item) in enumerate(candidatos):
            if self.abortar:
                break
            self.anti_bloqueo.esperar_si_pausado(self._debe_abortar)
            self._marcar_item(idx_listbox, "descargando")
            ok = self._descargar_uno(item, carpeta)
            if ok:
                recuperados += 1
                self._cola_quitar(item.get("id"))
                self.memoria["total_descargas_exitosas"] = self.memoria.get("total_descargas_exitosas", 0) + 1
                self.memoria["total_descargas_fallidas"] = max(0, self.memoria.get("total_descargas_fallidas", 0) - 1)
            self._marcar_item(idx_listbox, "exito" if ok else "fallo")
            guardar_memoria(self.memoria)
            if pos < len(candidatos) - 1 and not self.abortar:
                self._esperar(self.anti_bloqueo.delay_actual() * random.uniform(0.85, 1.15))
        if recuperados:
            self.exito(f"🔁 Segunda vuelta: {recuperados} tema(s) recuperado(s)")
        return recuperados
    def _descarga_finalizada(self):
        self.descargando = False
        self.btn_descargar.config(state="normal")
        self.btn_detener.config(state="disabled")
    def _actualizar_progreso(self, pos, total):
        pct = ((pos + 1) / total) * 100
        self.root.after(0, lambda: self.progress.config(value=pct))
    def _marcar_item(self, idx, estado):
        colores = {
            "pendiente": (TEMA["lista"], TEMA["texto"]),
            "descargando": ("#b86e00", "white"),
            "exito": ("#0b7a4b", "white"),
            "fallo": ("#a3263f", "white"),
        }
        prefijos = {"pendiente": "", "descargando": "⏳ ", "exito": "✅ ", "fallo": "❌ "}
        def _aplicar():
            try:
                texto_actual = self.listbox.get(idx)
                for pre in ("⏳ ", "✅ ", "❌ "):
                    if texto_actual.startswith(pre):
                        texto_actual = texto_actual[len(pre):]
                        break
                nuevo_texto = prefijos[estado] + texto_actual
                self.listbox.delete(idx)
                self.listbox.insert(idx, nuevo_texto)
                bg, fg = colores[estado]
                self.listbox.itemconfig(idx, bg=bg, fg=fg)
            except Exception:
                pass
        self.root.after(0, _aplicar)
    def _esperar(self, segundos):
        restante = segundos
        while restante > 0 and not self.abortar:
            self.root.after(0, lambda r=restante: self.lbl_espera.config(text=f"⏸ Esperando {r:.0f}s..."))
            time.sleep(min(1, restante))
            restante -= 1
        self.root.after(0, lambda: self.lbl_espera.config(text=""))
    def _construir_intentos(self):
        """
        Arma la lista de intentos a probar, en orden. Antes probaba primero
        SIN cookies y recién al final caía a las cookies de Firefox - pero
        en la práctica, hoy YouTube casi siempre exige alguna sesión
        iniciada ("sign in to confirm you're not a bot"), así que probar
        sin cookies primero solo hacía perder tiempo y disparaba subidas
        de nivel de precaución (con sus pausas de cooldown) en cada
        descarga, aunque las cookies después funcionaran a la primera.
        Ahora, si hay cookies de Firefox disponibles, se prueban PRIMERO -
        y solo si fallan (video realmente restringido, sesión de Firefox
        vencida, etc.) se cae al resto de los intentos como antes.
        """
        ranking = ranking_clientes(self.memoria)
        navegadores_libres = navegadores_disponibles_para_cookies(self.memoria)
        intentos = []
        if navegadores_libres:
            nav = navegadores_libres[0]
            # El primer intento va con el MEJOR cliente del ranking (hoy
            # android) + cookies, que es el que de verdad descarga.
            # Antes iba primero "web", porque es el cliente contra el que
            # YouTube contrasta las cookies en el chequeo de "no sos un
            # bot"; pero medido en la práctica, "web" no devuelve ningún
            # formato utilizable y falla siempre con "Requested format is
            # not available" (con skip de hls/dash y también sin él), así
            # que CADA descarga gastaba su primer intento al vacío: en una
            # sesión real de 59 descargas quedaron 44 fallos "otro" y cero
            # bloqueos, o sea que la mitad de los pedidos a YouTube eran al
            # aire. Ahora "web" queda como respaldo (sirve para videos que
            # exigen sesión iniciada), no como primer intento.
            mejor_cliente = ranking[0] if ranking else "android"
            if mejor_cliente != "web":
                intentos.append({"clientes": [mejor_cliente], "cookies": nav, "permisivo": False})
                intentos.append({"clientes": ["web"], "cookies": nav, "permisivo": False})
            else:
                intentos.append({"clientes": ["web"], "cookies": nav, "permisivo": False})
            intentos.append({"clientes": ranking, "cookies": nav, "permisivo": False})
        if self.anti_bloqueo.nivel >= 3 and navegadores_libres:
            clientes_a_probar = ranking[:1]
        else:
            clientes_a_probar = ranking[:3]
        for cliente in clientes_a_probar:
            intentos.append({"clientes": [cliente], "cookies": None, "permisivo": False})
        # Antes de rendirse del todo: último intento permisivo pero
        # CON cookies (antes se perdían al llegar al modo permisivo,
        # y las cookies son lo que más veces salva una descarga).
        if navegadores_libres:
            intentos.append({"clientes": ranking, "cookies": navegadores_libres[0], "permisivo": True})
        intentos.append({"clientes": ranking, "cookies": None, "permisivo": True})
        return intentos
    def _descargar_uno(self, item, carpeta):
        if not hasattr(self, '_carpetas_trabajo'):
            self._carpetas_trabajo = set()
        self._carpetas_trabajo.add(carpeta)
        import yt_dlp
        url = item["url"]
        # Ya bajado antes: no se le pide NADA a YouTube (ni la metadata).
        # Repetir una búsqueda que ya se descargó pasa a costar cero pedidos.
        ya_esta = ya_descargado(item.get("id"), carpeta)
        if ya_esta:
            self.debug(f"⏭️ Ya lo tenías descargado, no se vuelve a pedir: {ya_esta}")
            return True
        # ===== ANTICIPACIÓN: se consulta ANTES de intentar nada =====
        # El predictor mira lo aprendido (el ritmo de los últimos minutos,
        # la hora del día, cómo viene el cliente, cuánto hace que no se
        # revalidan las cookies) y decide si conviene arrancar normal, más
        # lento o con una pausa preventiva. Es lo que hace que el programa
        # se anticipe al bloqueo en lugar de reaccionar cuando ya perdió
        # el intento.
        self.predictor.plan_antes_de_descargar(
            self.anti_bloqueo,
            clientes_candidatos=ranking_clientes(self.memoria)[:2],
            debe_abortar=self._debe_abortar,
            dormir=self._esperar,
        )
        intentos = self._construir_intentos()
        # Nombre del archivo: "Artista - Título" sin el ruido del video
        # ("(Video Oficial HD)", "| Canal"...). Se escapa "%" porque yt-dlp
        # interpreta la plantilla.
        base_nombre = nombre_archivo_limpio(item)
        plantilla_nombre = base_nombre.replace("%", "%%") if base_nombre else "%(title)s"
        for intento in intentos:
            if self.abortar:
                return False
            # Barrera global: si CUALQUIER hilo ya vio una señal de
            # bloqueo, acá frenan todos (antes solo se dormía el hilo que
            # había fallado y el resto seguía golpeando a YouTube).
            self.anti_bloqueo.esperar_si_pausado(self._debe_abortar)
            if self.abortar:
                return False
            etiqueta = "+".join(intento["clientes"])
            if intento["cookies"]:
                etiqueta += f" (cookies {intento['cookies']})"
            if intento["permisivo"]:
                etiqueta += " [permisivo]"
            formato = "bestaudio/best" + ("/best" if intento["permisivo"] else "")
            ydl_opts = {
                "format": formato,
                # Máxima calidad: entre los formatos de audio disponibles se
                # prioriza el de MAYOR bitrate (antes mandaba el códec, y
                # un opus de ~130 kbps le ganaba a un AAC de 256 kbps).
                "format_sort": ["abr", "asr", "acodec"],
                "outtmpl": os.path.join(carpeta.replace("%", "%%"), plantilla_nombre + ".%(ext)s"),
                "ffmpeg_location": obtener_ruta_ffmpeg(),
                "postprocessors": [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"},
                    {"key": "FFmpegMetadata"},
                ],
                "extractor_args": {
                    "youtube": {
                        "player_client": intento["clientes"],
                        # Antes se salteaban "webpage" y "configs", que son
                        # justo de donde yt-dlp saca el visitorData/ytcfg con
                        # el que YouTube valida la sesión: saltearlos dejaba
                        # el pedido "pelado" y es una causa directa del
                        # "sign in to confirm you're not a bot". Solo se
                        # sigue salteando dash/hls en los intentos no
                        # permisivos, para pedir un formato progresivo y
                        # hacer menos pedidos.
                        "skip": [] if intento["permisivo"] else ["hls", "dash"],
                    }
                },
                "referer": "https://www.youtube.com/",
                "http_headers": {"Accept-Language": "es-AR,es;q=0.9,en;q=0.8"},
                "limit_rate": "2M",
                "sleep_interval_requests": self.anti_bloqueo.pausa_entre_pedidos(),
                "quiet": True,
                "no_warnings": True,
                "retries": 8,
                "fragment_retries": 8,
                # Sin esto, un socket colgado traba la cola entera y
                # retrasa la rotación de cliente, que es justo lo que saca
                # del bloqueo.
                "socket_timeout": 15,
                "extractor_retries": 2,
                "skip_unavailable_fragments": True,
                "logger": self._logger_ytdlp,
            }
            # Identidad del pedido: impersonate si se puede, o el
            # user-agent coherente con las cookies (ver
            # _aplicar_antisuplantacion).
            _aplicar_antisuplantacion(ydl_opts, intento["cookies"])
            cache = _cachedir_ytdlp()
            if cache:
                # Reusar la caché de yt-dlp evita volver a bajarse el
                # reproductor JS en cada intento: menos pedidos es menos
                # motivo para que YouTube frene por ritmo.
                ydl_opts["cachedir"] = cache
            archivo_cookies = None
            if intento["cookies"]:
                archivo_cookies = aplicar_cookies(ydl_opts, intento["cookies"])
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    ruta_final = ydl.prepare_filename(info)
                    base, _ = os.path.splitext(ruta_final)
                    ruta_mp3 = base + ".mp3"
                # Un mp3 de unos pocos KB no es música: es una descarga
                # vacía o cortada. No se da por buena (antes bastaba con que
                # yt-dlp no tirara error): se borra y sigue el próximo intento.
                if os.path.exists(ruta_mp3) and os.path.getsize(ruta_mp3) < 20000:
                    tam = os.path.getsize(ruta_mp3)
                    try:
                        os.remove(ruta_mp3)
                    except Exception:
                        pass
                    raise RuntimeError(f"archivo descargado vacío o truncado ({tam} bytes)")
                # Se aprende de TODOS los intentos de un solo cliente, con o
                # sin cookies: desde que las cookies van primero, casi todos
                # los pedidos las llevan y el ranking quedaba congelado (en
                # memoria.json ningún cliente tenía fecha de actualización).
                if not intento["permisivo"] and len(intento["clientes"]) == 1:
                    registrar_resultado_cliente(self.memoria, intento["clientes"], exito=True)
                if intento["cookies"]:
                    registrar_navegador_efectivo(self.memoria, intento["cookies"])
                self.anti_bloqueo.registrar_exito()
                registrar_descarga_hecha(item.get("id"), os.path.basename(ruta_mp3))
                abr = 0.0
                try:
                    abr = float(info.get("abr") or 0)
                    fuente = f" · fuente {info.get('acodec', '?')} {abr:.0f} kbps -> mp3 máx. calidad"
                except Exception:
                    fuente = ""
                self.exito(f"✅ Descargado con {etiqueta}{fuente}")
                if 0 < abr < 96:
                    self.warning(f"⚠️ Fuente de baja calidad ({abr:.0f} kbps): YouTube no tiene mejor audio de este video")
                if os.path.exists(ruta_mp3):
                    self._post_procesar_descarga(ruta_mp3, item)
                return True
            except Exception as e:
                error_msg = str(e)
                if "impersonate" in error_msg.lower() and not self._impersonate_apagado:
                    # No es culpa de YouTube: en esta máquina falló la
                    # suplantación. Se apaga y se sigue con el próximo
                    # intento, sin ensuciar el aprendizaje con un error que
                    # no habla del bloqueo.
                    self._impersonate_apagado = True
                    desactivar_impersonate()
                    self.warning(f"⚠️ Impersonate falló ({error_msg[:90]}) - lo apago y sigo sin él")
                    continue
                tipo_error = self.anti_bloqueo.registrar_error(error_msg)
                if tipo_error == "irrecuperable":
                    self._descartados.add(item["id"])
                    registrar_irrecuperable(self.memoria, item["id"], error_msg)
                    self.warning(f"⏭️ Video irrecuperable (no vuelve a intentarse por unos días): {error_msg[:120]}")
                    return False
                if tipo_error == "dudoso":
                    # No se sabe si el video está muerto o si el intento no
                    # sirvió: NO se lo veta al primer fallo.
                    if registrar_dudoso(self.memoria, item["id"], error_msg):
                        self._descartados.add(item["id"])
                        self.warning(f"⏭️ Dos intentos fallaron por lo mismo ({error_msg[:80]}): "
                                     f"lo doy por no disponible por unos días")
                        return False
                if not intento["permisivo"] and len(intento["clientes"]) == 1:
                    registrar_resultado_cliente(self.memoria, intento["clientes"], exito=False)
                self.error(f"❌ Falló con {etiqueta}: {error_msg[:150]}")
                if tipo_error == "bloqueo" and self.anti_bloqueo.necesita_cooldown():
                    espera = self.anti_bloqueo.duracion_cooldown()
                    self.warning(f"🧊 Cooldown anti-bloqueo: pausando {espera}s a TODOS los hilos")
                    # La pausa ahora es global: antes solo se dormía este
                    # hilo y los demás seguían descargando durante el
                    # bloqueo, que es justo lo que lo empeora.
                    self.anti_bloqueo.pausar_todos(espera, "señales de bloqueo repetidas")
                    self.anti_bloqueo.esperar_si_pausado(self._debe_abortar)
                    self.anti_bloqueo.reset_cooldown()
                    if self.anti_bloqueo.registrar_cooldown() >= 3 and not self.abortar:
                        self.abortar = True
                        self.error("🛑 YouTube bloqueó 3 veces seguidas sin dejar pasar ni una descarga: "
                                   "corto la tanda para no empeorarlo. Probá de nuevo en un rato "
                                   "(lo que ya se bajó queda registrado).")
            finally:
                borrar_temporal(archivo_cookies)
        self.error("❌ Se agotaron todos los intentos para este video")
        return False
    def _post_procesar_descarga(self, ruta_mp3, item=None):
        """Corre después de cada descarga, en un solo hilo secuencial para que
        no se pisen entre sí: primero pareja el volumen (si está activado) y
        después analiza BPM y tonalidad, escribe las etiquetas con la
        carátula y renombra (así el volumen ya no toca el archivo una vez que
        quedó con el nombre final)."""
        def _trabajo():
            ruta_actual = ruta_mp3
            if self.var_normalizar_volumen.get():
                try:
                    normalizar_volumen(ruta_actual)
                    self.debug("🔊 Volumen normalizado")
                except Exception as e:
                    self.debug(f"🔊 Error normalizando volumen: {e}")
            if os.path.exists(ruta_actual):
                self._analizar_etiquetar_renombrar(ruta_actual, item or {})
        # UN solo hilo para toda la cola de post-proceso. Antes se lanzaba
        # un hilo nuevo por descarga (aunque el texto decía "secuencial"):
        # con 4 descargas en paralelo corrían 4 ffmpeg/librosa a la vez y
        # le robaban CPU a las descargas.
        with self._lock_post:
            self._cola_post.put(_trabajo)
            if self._hilo_post is None or not self._hilo_post.is_alive():
                self._hilo_post = threading.Thread(target=self._consumir_post, daemon=True)
                self._hilo_post.start()
    def _consumir_post(self):
        while True:
            try:
                trabajo = self._cola_post.get(timeout=5)
            except queue.Empty:
                with self._lock_post:
                    if self._cola_post.empty():
                        self._hilo_post = None
                        return
                continue
            try:
                trabajo()
            except Exception as e:
                self.debug(f"⚙️ Error en el post-proceso: {e}")
    def _analizar_etiquetar_renombrar(self, ruta_mp3, item):
        """BPM + tonalidad (Camelot) -> etiquetas ID3 (título, artista, álbum,
        TBPM, TKEY) con carátula, sin recodificar -> nombre con [BPM] [clave].
        Cada paso es opcional: si uno falla, los demás siguen."""
        bpm = clave = None
        if self.var_analizar_bpm.get():
            try:
                bpm, clave = analizar_audio(ruta_mp3)
            except Exception as e:
                self.debug(f"🎵 Error analizando BPM/tonalidad: {e}")
        if self.config_app.get("etiquetas_y_portada", True):
            portada = None
            try:
                titulo, artista = datos_etiquetas(item)
                etiquetas = {}
                if titulo:
                    etiquetas["title"] = titulo
                if artista:
                    etiquetas["artist"] = artista
                if item.get("album"):
                    etiquetas["album"] = item["album"]
                if bpm and bpm > 0:
                    etiquetas["TBPM"] = str(bpm)
                if clave:
                    etiquetas["TKEY"] = clave
                portada = descargar_portada(item) if item.get("id") else None
                if escribir_etiquetas_mp3(ruta_mp3, etiquetas, portada):
                    self.debug("🏷️ Etiquetas" + (" y carátula" if portada else "") + " guardadas en el mp3")
            except Exception as e:
                self.debug(f"🏷️ No se pudieron escribir las etiquetas: {e}")
            finally:
                if portada:
                    try:
                        os.remove(portada)
                    except Exception:
                        pass
        if bpm and bpm > 0:
            try:
                nueva_ruta = renombrar_con_bpm(ruta_mp3, bpm, clave if TONALIDAD_EN_NOMBRE else None)
                self.debug(f"🎵 BPM {bpm}" + (f" · tonalidad {clave}" if clave else "") +
                           f" -> {os.path.basename(nueva_ruta)}")
            except Exception as e:
                self.debug(f"🎵 Error renombrando: {e}")
        elif self.var_analizar_bpm.get():
            self.debug("🎵 No se pudo determinar el BPM")
    def _guardar_memoria_periodica(self):
        guardar_memoria(self.memoria)
        self.root.after(60000, self._guardar_memoria_periodica)
    def cerrar(self):
        self.abortar = True
        self._guardar_config_descarga()
        self.anti_bloqueo.guardar_en_memoria()
        guardar_memoria(self.memoria)
        # Se da un instante a que los hilos suelten los archivos y se borra
        # lo que haya quedado a medias (.part, temporales, mp3 vacíos).
        try:
            time.sleep(1.0)
            limpiar_parciales(getattr(self, '_carpetas_trabajo', ()))
        except Exception:
            pass
        self.root.destroy()
# =====================================================================
# BLOQUE PRINCIPAL
# =====================================================================
if __name__ == "__main__":
    verif = VentanaVerificacion()
    config_app = cargar_config_app()
    root = tk.Tk()
    app = DownloaderIA(root, config_app)
    root.mainloop()
