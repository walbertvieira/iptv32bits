"""Gerenciador de downloads para assistir offline.

- Arquivos diretos (MP4/MKV/AVI… — o caso dos filmes e séries de listas Xtream) são baixados via HTTP
  com retomada (Range): pausar, fechar o app ou cair a internet não perde o que já foi baixado.
- Streams HLS (.m3u8) são gravados pelo próprio VLC em um arquivo .ts.
- Cada download roda numa thread própria e conversa com a interface por sinais Qt.
"""
import http.client
import json
import os
import re
import shutil
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from PyQt5.QtCore import QObject, Qt, QTimer
from PyQt5.QtCore import pyqtSignal as Signal, pyqtSlot as Slot

from m3u_parser import USER_AGENT, VOD_EXT, Entry, series_key

import vlcloader

vlc, _VLC_ERR = vlcloader.import_vlc()     # VLC embutido na pasta do programa

CHUNK = 256 * 1024
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "Videos", "IPTV Player")
RESERVE_BYTES = 200 * 1024 * 1024          # sempre deixa ao menos 200 MB livres no disco
ACTIVE_STATES = ("queued", "downloading", "paused", "error")


WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(name: str, limit: int = 90) -> str:
    """Nome válido no Windows: sem caracteres proibidos, sem ponto/espaço no fim,
    sem nomes reservados (CON, NUL, COM1…) e curto o bastante para não estourar 260 caracteres."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "")
    s = re.sub(r"\s+", " ", s).strip(" .")
    s = s[:limit].rstrip(" .")
    if not s:
        s = "video"
    if s.split(".")[0].upper() in WIN_RESERVED:
        s = "_" + s
    return s


def _taken(path: str) -> bool:
    """Caminho ocupado por um arquivo pronto OU por um download parcial de outro item."""
    return any(os.path.exists(path + suf) for suf in ("", ".part", ".part.ts", ".part.meta"))


def unique_path(stem: str, ext: str, used=()) -> str:
    """stem+ext, ou stem (2)+ext, (3)… — nunca sobrescreve nem retoma o parcial de outro item.
    `used` são caminhos já reservados por outros downloads (em casefold)."""
    path, n = stem + ext, 2
    while path.casefold() in used or _taken(path):
        path = f"{stem} ({n}){ext}"
        n += 1
    return path


def fmt_bytes(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            txt = f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
            return txt.replace(".", ",")
        n /= 1024


def fmt_eta(sec) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec} s"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min"


class _Stopped(Exception):
    pass


class _NeedVLC(Exception):
    pass


class _Incomplete(ConnectionError):
    pass


class _Restart(ConnectionError):
    """O parcial não corresponde ao arquivo atual do servidor: apaga e recomeça do zero."""


NET_ERRORS = (urllib.error.URLError, ConnectionError, TimeoutError, socket.timeout,
              http.client.HTTPException)


class DownloadSignals(QObject):
    progress = Signal(str, object, object, float, float)   # url, baixado, total (0 = ?), fração (-1 = ?), bytes/s
    finished = Signal(str, str, object)                    # url, caminho final, tamanho
    failed = Signal(str, str)                              # url, mensagem
    stopped = Signal(str, str)                             # url, motivo (pause | cancel | quit)


class DownloadJob(threading.Thread):
    def __init__(self, url: str, entry: Entry, path: str, method: str, signals: DownloadSignals):
        super().__init__(daemon=True)
        self.url, self.entry, self.path, self.method = url, entry, path, method
        self.signals = signals
        self.reason = None

    def stop(self, reason: str):
        self.reason = reason

    # ------------------------------------------------------------------
    def run(self):
        try:
            if self.method == "http":
                try:
                    self._http()
                    return
                except _NeedVLC:
                    self.method = "vlc"
                    self.path = unique_path(os.path.splitext(self.path)[0], ".ts")
            self._vlc()
        except _Stopped:
            if self.reason == "cancel":
                self._remove_parts()
            self.signals.stopped.emit(self.url, self.reason or "pause")
        except Exception as exc:
            self.signals.failed.emit(self.url, str(exc) or exc.__class__.__name__)

    def _check(self):
        if self.reason:
            raise _Stopped()

    def _sleep(self, sec):
        end = time.monotonic() + sec
        while time.monotonic() < end:
            self._check()
            time.sleep(0.2)

    def _remove_parts(self):
        for p in (self.path + ".part", self.path + ".part.ts", self.path + ".part.meta"):
            try:
                os.remove(p)
            except OSError:
                pass

    def _headers(self) -> dict:
        h = {"User-Agent": USER_AGENT}
        for opt in self.entry.options:
            key, _, val = opt.lstrip(":").partition("=")
            if key == "http-user-agent" and val:
                h["User-Agent"] = val
            elif key == "http-referrer" and val:
                h["Referer"] = val
        return h

    # --- download HTTP com retomada ------------------------------------
    def _http(self):
        attempts = 0
        while True:
            try:
                self._http_once()
                return
            except urllib.error.HTTPError as exc:
                if exc.code < 500:
                    raise RuntimeError(f"O servidor recusou o download (HTTP {exc.code}).")
                err = exc
            except _Restart:
                for suf in (".part", ".part.meta"):
                    try:
                        os.remove(self.path + suf)
                    except OSError:
                        pass
                continue
            except NET_ERRORS as exc:
                err = exc
            attempts += 1
            if attempts > 6:
                raise RuntimeError(f"Falha de conexão: {err}")
            self._sleep(min(30, 3 * attempts))       # espera e continua de onde parou

    def _http_once(self):
        folder = os.path.dirname(self.path)
        os.makedirs(folder, exist_ok=True)
        part, meta_path = self.path + ".part", self.path + ".part.meta"
        start = os.path.getsize(part) if os.path.exists(part) else 0
        meta = {}
        if start:
            try:
                with open(meta_path, encoding="utf-8") as fh:
                    meta = json.load(fh)
            except (OSError, ValueError):
                meta = {}
            if meta.get("url") != self.url:          # parcial de outro conteúdo: descarta
                start, meta = 0, {}
        headers = self._headers()
        if start:
            headers["Range"] = f"bytes={start}-"
            validator = meta.get("etag") or meta.get("modified")
            if validator:
                headers["If-Range"] = validator      # se o arquivo mudou no servidor, vem inteiro (200)
        req = urllib.request.Request(self.url, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and start:            # já estava completo
                if meta.get("total") and start != meta["total"]:
                    raise _Restart()
                os.replace(part, self.path)
                self.signals.finished.emit(self.url, self.path, start)
                return
            raise
        with resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if not start and any(k in ctype for k in ("mpegurl", "text/html", "text/plain")):
                raise _NeedVLC()
            if start and resp.status != 206:           # servidor ignorou o Range ou o arquivo mudou
                start = 0
            length = int(resp.headers.get("Content-Length") or 0)
            total = start + length if length else 0
            if start and meta.get("total") and total and total != meta["total"]:
                raise _Restart()                       # tamanho diferente: é outro arquivo, recomeça
            if not start:
                meta = {"url": self.url, "total": total,
                        "etag": resp.headers.get("ETag") or "",
                        "modified": resp.headers.get("Last-Modified") or ""}
                with open(meta_path, "w", encoding="utf-8") as fh:
                    json.dump(meta, fh)
            if length:
                free = shutil.disk_usage(folder).free
                if length > free - RESERVE_BYTES:
                    raise RuntimeError(f"Espaço em disco insuficiente: faltam "
                                       f"{fmt_bytes(length - free + RESERVE_BYTES)}.")
            done = start
            last_t, last_done, speed = time.monotonic(), done, 0.0
            with open(part, "ab" if start else "wb") as fh:
                while True:
                    self._check()
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    now = time.monotonic()
                    if now - last_t >= 0.7:
                        speed = (done - last_done) / (now - last_t)
                        last_t, last_done = now, done
                        self.signals.progress.emit(self.url, done, total,
                                                   done / total if total else -1.0, speed)
        if total and done < total:
            raise _Incomplete(f"conexão interrompida em {fmt_bytes(done)} de {fmt_bytes(total)}")
        os.replace(part, self.path)
        try:
            os.remove(meta_path)
        except OSError:
            pass
        self.signals.finished.emit(self.url, self.path, done)

    # --- gravação via VLC (HLS e afins) ---------------------------------
    def _vlc(self):
        if vlc is None:
            raise RuntimeError("O VLC embutido (pasta vlc) é necessário para baixar este formato.")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".part.ts"
        try:
            os.remove(tmp)                           # gravação via VLC não tem retomada
        except OSError:
            pass
        dst = tmp.replace("\\", "\\\\").replace('"', '\\"')
        inst = vlc.Instance("--quiet", "--no-video-title-show")
        media = inst.media_new(self.url)
        for opt in self.entry.options:
            media.add_option(opt)
        media.add_option(f':sout=#std{{access=file,mux=ts,dst="{dst}"}}')
        media.add_option(":sout-all")
        media.add_option(":sout-keep")
        mp = inst.media_player_new()
        mp.set_media(media)
        mp.play()
        last_t, last_size, speed = time.monotonic(), 0, 0.0
        try:
            while True:
                time.sleep(0.7)
                if self.reason:
                    raise _Stopped()
                st = mp.get_state()
                size = os.path.getsize(tmp) if os.path.exists(tmp) else 0
                now = time.monotonic()
                speed = (size - last_size) / max(0.001, now - last_t)
                last_t, last_size = now, size
                pos = mp.get_position()
                self.signals.progress.emit(self.url, size, 0, pos if pos and pos > 0 else -1.0, speed)
                if st == vlc.State.Ended:
                    break
                if st == vlc.State.Error:
                    raise RuntimeError("O VLC não conseguiu gravar este stream.")
        finally:
            mp.stop()
            mp.release()
            inst.release()
        size = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        if size == 0:
            raise RuntimeError("Nenhum dado foi gravado.")
        os.replace(tmp, self.path)
        self.signals.finished.emit(self.url, self.path, size)


class DownloadManager(QObject):
    changed = Signal(str)       # status de um download mudou (url; "" = vários)
    progressed = Signal(str)    # novo progresso

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        d = store.data
        d.setdefault("downloads", {})
        d.setdefault("download_dir", DEFAULT_DIR)
        d.setdefault("max_downloads", 2)
        self.jobs, self.live = {}, {}
        self._last_save = time.monotonic()
        self.sig = DownloadSignals()
        Q = Qt.QueuedConnection
        self.sig.progress.connect(self._on_progress, Q)
        self.sig.finished.connect(self._on_finished, Q)
        self.sig.failed.connect(self._on_failed, Q)
        self.sig.stopped.connect(self._on_stopped, Q)
        for r in self.recs.values():                 # downloads interrompidos ao fechar: retoma
            if r["status"] == "downloading":
                r["status"] = "queued"
        QTimer.singleShot(1500, self._pump)

    @property
    def recs(self) -> dict:
        return self.store.data["downloads"]

    # --- consultas
    @staticmethod
    def downloadable(e: Entry) -> bool:
        return e is not None and e.kind in ("movie", "series")

    def status(self, url):
        r = self.recs.get(url)
        return r["status"] if r else None

    def local_path(self, url):
        r = self.recs.get(url)
        if r and r["status"] == "done" and os.path.isfile(r["path"]):
            return r["path"]
        return None

    def fraction(self, url):
        r = self.recs.get(url)
        if not r:
            return None
        done, total, frac, _ = self.live.get(url, (r.get("size", 0), r.get("total", 0), -1.0, 0.0))
        if total:
            return min(1.0, done / total)
        return frac if frac and frac > 0 else 0.0

    def describe(self, url) -> str:
        r = self.recs.get(url)
        if not r:
            return ""
        st = r["status"]
        if st == "done":
            if not os.path.isfile(r["path"]):
                return "⚠ Arquivo não encontrado"
            return f"✓ Baixado · {fmt_bytes(r.get('size'))}"
        done, total, frac, speed = self.live.get(url, (r.get("size", 0), r.get("total", 0), -1.0, 0.0))
        if st == "downloading":
            if total:
                eta = f" · {fmt_eta((total - done) / speed)}" if speed > 1024 else ""
                return f"⬇ {int(done * 100 / total)}% · {fmt_bytes(done)} de {fmt_bytes(total)} · {fmt_bytes(speed)}/s{eta}"
            if frac > 0:
                return f"⬇ {int(frac * 100)}% · {fmt_bytes(done)} · {fmt_bytes(speed)}/s"
            return f"⬇ {fmt_bytes(done)} · {fmt_bytes(speed)}/s" if done else "⬇ Conectando…"
        if st == "queued":
            return "Na fila…"
        if st == "paused":
            return f"⏸ Pausado · {int(done * 100 / total)}%" if total else "⏸ Pausado"
        return f"⚠ Erro: {r.get('error', '')[:70]}"

    def counts(self):
        done = sum(1 for r in self.recs.values() if r["status"] == "done")
        return done, sum(1 for r in self.recs.values() if r["status"] in ACTIVE_STATES)

    # --- ações
    def enqueue(self, entry: Entry) -> bool:
        if not self.downloadable(entry):
            return False
        r = self.recs.get(entry.url)
        if r:
            if r["status"] == "done" and os.path.isfile(r["path"]):
                return False
            if r["status"] in ("queued", "downloading"):
                return False
            if r["status"] == "done":               # arquivo foi apagado: baixa de novo
                r["size"] = r["total"] = 0
            r.update(status="queued", error="")
        else:
            method = self._method(entry)
            folder, path = self._target_path(entry, method)
            self.recs[entry.url] = {"entry": entry.to_dict(), "path": path, "folder": folder,
                                    "method": method, "status": "queued", "size": 0, "total": 0,
                                    "added": time.time(), "error": ""}
        self.store.save()
        self.changed.emit(entry.url)
        self._pump()
        return True

    def pause(self, url):
        job = self.jobs.get(url)
        r = self.recs.get(url)
        if job:
            job.stop("pause")
        elif r and r["status"] == "queued":
            r["status"] = "paused"
            self.store.save()
            self.changed.emit(url)

    def resume(self, url):
        r = self.recs.get(url)
        if r and r["status"] in ("paused", "error"):
            r.update(status="queued", error="")
            self.store.save()
            self.changed.emit(url)
            self._pump()

    def delete(self, url):
        """Cancela (se em andamento) e apaga o arquivo baixado."""
        r = self.recs.pop(url, None)
        job = self.jobs.get(url)
        if job:
            job.stop("cancel")
        elif r:
            for p in (r["path"], r["path"] + ".part", r["path"] + ".part.ts", r["path"] + ".part.meta"):
                try:
                    os.remove(p)
                except OSError:
                    pass
            self._remove_empty_dir(os.path.dirname(r["path"]))
        self.live.pop(url, None)
        self.store.save()
        self.changed.emit(url)

    def pause_all(self):
        for url, r in self.recs.items():
            if r["status"] in ("queued", "downloading"):
                self.pause(url)

    def resume_all(self):
        for r in self.recs.values():
            if r["status"] in ("paused", "error"):
                r.update(status="queued", error="")
        self.store.save()
        self.changed.emit("")
        self._pump()

    def shutdown(self):
        for job in self.jobs.values():
            job.stop("quit")
        for r in self.recs.values():
            if r["status"] == "downloading":
                r["status"] = "queued"               # continua na próxima vez que abrir o app

    # --- internos
    def _remove_empty_dir(self, folder):
        base = os.path.normcase(os.path.abspath(self.store.data["download_dir"]))
        if os.path.normcase(os.path.abspath(folder)) != base:
            try:
                os.rmdir(folder)                     # só remove se estiver vazia
            except OSError:
                pass

    @staticmethod
    def _method(e: Entry) -> str:
        path = urllib.parse.urlsplit(e.url).path.lower()
        return "vlc" if path.endswith((".m3u8", ".m3u")) else "http"

    def _series_folder(self, e: Entry) -> str:
        """Pasta da série.

        - Episódios da mesma série (mesmo nome + grupo) vão sempre para a mesma pasta,
          mesmo que o nome venha escrito diferente ("Dark" / "DARK").
        - Se a pasta com esse nome já pertence a OUTRA série homônima (outro grupo),
          esta vai para "Nome (Grupo)" para os episódios não se misturarem.
        Comparações ignoram maiúsculas/minúsculas, como o Windows.
        """
        key = series_key(e)
        owners = {}                                   # pasta (casefold) -> identidade da série dona
        for r in self.recs.values():
            other = r["entry"]
            if other.get("kind") != "series":
                continue
            folder = r.get("folder") or os.path.basename(os.path.dirname(r["path"]))
            okey = series_key(Entry.from_dict(other))
            if okey == key:
                return folder                         # já tem episódios baixados: mesma pasta
            owners.setdefault(folder.casefold(), okey)
        for cand in (safe_name(e.series_name or e.name, 70),
                     safe_name(f"{e.series_name} ({e.group})", 70)):
            if cand.casefold() not in owners:
                return cand
        n = 2
        while True:
            cand = safe_name(f"{e.series_name} ({e.group}) {n}", 70)
            if cand.casefold() not in owners:
                return cand
            n += 1

    def _target_path(self, e: Entry, method: str):
        base = self.store.data["download_dir"]
        folder = self._series_folder(e) if e.kind == "series" else "Filmes"
        ext = os.path.splitext(urllib.parse.urlsplit(e.url).path)[1].lower()
        if method == "vlc":
            ext = ".ts"
        elif ext not in VOD_EXT:
            ext = ".mp4"
        used = {r["path"].casefold() for r in self.recs.values()}
        return folder, unique_path(os.path.join(base, folder, safe_name(e.name)), ext, used)

    def _pump(self):
        limit = max(1, int(self.store.data.get("max_downloads", 2)))
        active = len(self.jobs)
        for url, r in list(self.recs.items()):
            if active >= limit:
                break
            if r["status"] == "queued" and url not in self.jobs:
                r["status"] = "downloading"
                job = DownloadJob(url, Entry.from_dict(r["entry"]), r["path"], r.get("method", "http"), self.sig)
                self.jobs[url] = job
                job.start()
                active += 1
                self.changed.emit(url)

    def _finish_job(self, url):
        self.jobs.pop(url, None)
        self.live.pop(url, None)

    @Slot(str, object, object, float, float)
    def _on_progress(self, url, done, total, frac, speed):
        self.live[url] = (done, total, frac, speed)
        r = self.recs.get(url)
        if r:
            r["size"], r["total"] = int(done), int(total or 0)
        now = time.monotonic()
        if now - self._last_save > 15:
            self.store.save()
            self._last_save = now
        self.progressed.emit(url)

    @Slot(str, str, object)
    def _on_finished(self, url, path, size):
        self._finish_job(url)
        r = self.recs.get(url)
        if r:
            r.update(status="done", path=path, size=int(size), total=int(size), done_ts=time.time(), error="")
        self.store.save()
        self.changed.emit(url)
        self._pump()

    @Slot(str, str)
    def _on_failed(self, url, msg):
        self._finish_job(url)
        r = self.recs.get(url)
        if r:
            r.update(status="error", error=msg)
        self.store.save()
        self.changed.emit(url)
        self._pump()

    @Slot(str, str)
    def _on_stopped(self, url, reason):
        job = self.jobs.get(url)
        self._finish_job(url)
        r = self.recs.get(url)
        if r:
            r["status"] = "queued" if reason == "quit" else "paused"
        if reason == "cancel" and job is not None:
            self._remove_empty_dir(os.path.dirname(job.path))
        self.store.save()
        self.changed.emit(url)
        if reason != "quit":
            self._pump()
