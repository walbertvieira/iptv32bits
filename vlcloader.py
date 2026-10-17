"""Carrega o VLC EMBUTIDO na pasta do programa (subpasta "vlc"), sem precisar do VLC instalado.

Deve ser importado ANTES de "import vlc". Procura, nesta ordem:
  1. <pasta do .exe>\\vlc\\libvlc.dll   (programa empacotado)
  2. <pasta do main.py>\\vlc\\libvlc.dll (rodando pelo código-fonte)
Se não achar (ou se a DLL for de outra arquitetura, ex.: VLC 64 bits com Python 32 bits),
o python-vlc tenta o VLC instalado no Windows, como antes.

Também evita que o python-vlc encerre o programa com sys.exit() quando a DLL não carrega.
"""
import ctypes
import os
import struct
import sys

BITS = struct.calcsize("P") * 8          # 32 ou 64 (arquitetura deste Python/programa)
LOADED_FROM = None                       # pasta do VLC embutido em uso (None = VLC do sistema)
LOAD_ERROR = None                        # motivo, se o VLC embutido existir mas não carregar


def _candidates():
    dirs = []
    if getattr(sys, "frozen", False):
        dirs.append(os.path.dirname(os.path.abspath(sys.executable)))
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            dirs.append(meipass)
    dirs.append(os.path.dirname(os.path.abspath(__file__)))
    seen, out = set(), []
    for d in dirs:
        v = os.path.join(d, "vlc")
        if v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out


def setup():
    global LOADED_FROM, LOAD_ERROR
    if LOADED_FROM is not None or not sys.platform.startswith("win"):
        return
    for d in _candidates():
        lib = os.path.join(d, "libvlc.dll")
        if not os.path.isfile(lib):
            continue
        # dependências (libvlccore.dll) ficam na mesma pasta
        try:
            os.add_dll_directory(d)
        except (AttributeError, OSError):
            pass
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        try:
            ctypes.CDLL(lib)                  # teste antes do python-vlc (que faria sys.exit)
        except OSError as exc:
            LOAD_ERROR = (f"O VLC embutido em {d} não carregou ({exc}). "
                          f"Este programa é {BITS} bits: a pasta vlc precisa ser do VLC {BITS} bits.")
            continue
        os.environ["PYTHON_VLC_LIB_PATH"] = lib
        plugins = os.path.join(d, "plugins")
        if os.path.isdir(plugins):
            os.environ["PYTHON_VLC_MODULE_PATH"] = plugins
            os.environ["VLC_PLUGIN_PATH"] = plugins
        LOADED_FROM = d
        return


def import_vlc():
    """Importa o python-vlc com segurança. Retorna (módulo ou None, mensagem de erro ou None)."""
    setup()
    try:
        import vlc
        return vlc, None
    except (Exception, SystemExit) as exc:   # python-vlc chama sys.exit() em algumas falhas
        msg = LOAD_ERROR or str(exc) or exc.__class__.__name__
        return None, msg


def describe() -> str:
    if LOADED_FROM:
        return f"VLC embutido ({BITS} bits): {LOADED_FROM}"
    return f"VLC do sistema ({BITS} bits)"


setup()
