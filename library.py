"""Armazenamento local do aplicativo.

- Biblioteca de playlists: cada playlist é copiada para dentro do app (texto original + catálogo
  já interpretado em cache), então abre instantaneamente mesmo offline ou se o arquivo sumir.
- Histórico de reprodução (continuar assistindo), favoritos e preferências da interface.

Tudo fica em %APPDATA%\\IPTVPlayer.
"""
import gzip
import json
import os
import pickle
import time
import urllib.parse
import uuid

from m3u_parser import Entry, load_text, parse

APP_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "IPTVPlayer")
PL_DIR = os.path.join(APP_DIR, "playlists")
CACHE_VERSION = 2
HISTORY_MAX = 1500


def is_url(src: str) -> bool:
    return src.lower().startswith(("http://", "https://"))


def short_source(src: str) -> str:
    """Nome curto para exibir, sem expor usuário/senha de URLs."""
    if is_url(src):
        return urllib.parse.urlsplit(src).netloc or src
    return os.path.basename(src) or src


class Store:
    def __init__(self):
        os.makedirs(PL_DIR, exist_ok=True)
        self.path = os.path.join(APP_DIR, "config.json")
        self.data = {
            "playlists": [], "active": None, "favorites": {}, "history": {},
            "volume": 80, "list_mode": False, "auto_update_hours": 12, "ui": {},
        }
        try:
            with open(self.path, encoding="utf-8") as fh:
                self.data.update(json.load(fh))
        except (OSError, ValueError):
            pass
        self._migrate()

    def _migrate(self):
        d = self.data
        if not d["playlists"] and d.get("last"):          # versão 1 guardava só a última fonte
            self.add_playlist(short_source(d["last"]), d["last"], save=False)
            d["active"] = d["playlists"][0]["id"]
        for k in ("recent", "last", "positions"):
            d.pop(k, None)
        if not isinstance(d.get("ui"), dict):
            d["ui"] = {}

    def save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # ------------------------------------------------------------------ playlists
    def playlists(self) -> list:
        return self.data["playlists"]

    def playlist(self, pid):
        return next((p for p in self.data["playlists"] if p["id"] == pid), None)

    @staticmethod
    def _same_source(a: str, b: str) -> bool:
        a, b = a.strip(), b.strip()
        if is_url(a) or is_url(b):
            return a == b
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))

    def find_by_source(self, source: str):
        return next((p for p in self.data["playlists"] if self._same_source(p["source"], source)), None)

    def unique_name(self, name: str, exclude_pid=None) -> str:
        """Evita duas playlists com o mesmo nome no menu: "Lista", "Lista (2)"…"""
        taken = {p["name"].casefold() for p in self.data["playlists"] if p["id"] != exclude_pid}
        cand, n = name, 2
        while cand.casefold() in taken:
            cand = f"{name} ({n})"
            n += 1
        return cand

    def add_playlist(self, name: str, source: str, save: bool = True) -> dict:
        source = source.strip()
        existing = self.find_by_source(source)
        if existing:
            return existing
        p = {"id": uuid.uuid4().hex[:12],
             "name": self.unique_name((name or "").strip() or short_source(source)),
             "source": source, "added": time.time(), "updated": 0, "count": 0}
        self.data["playlists"].append(p)
        if save:
            self.save()
        return p

    def rename_playlist(self, pid, name: str):
        p = self.playlist(pid)
        if p and name.strip():
            p["name"] = self.unique_name(name.strip(), exclude_pid=pid)
            self.save()

    def remove_playlist(self, pid):
        for path in self._paths(pid):
            try:
                os.remove(path)
            except OSError:
                pass
        self.data["playlists"] = [p for p in self.data["playlists"] if p["id"] != pid]
        if self.data.get("active") == pid:
            self.data["active"] = self.data["playlists"][0]["id"] if self.data["playlists"] else None
        self.save()

    def mark_updated(self, pid, count: int):
        p = self.playlist(pid)
        if p:
            p["updated"] = time.time()
            p["count"] = count
            self.save()

    def needs_refresh(self, p: dict) -> bool:
        if is_url(p["source"]):
            hours = float(self.data.get("auto_update_hours", 12) or 0)
            return hours > 0 and time.time() - p.get("updated", 0) > hours * 3600
        try:                                    # arquivo local: só relê se foi modificado
            return os.path.getmtime(p["source"]) > p.get("updated", 0)
        except OSError:
            return False                        # arquivo sumiu: segue usando a cópia salva

    # --- cache (métodos seguros para rodar fora da thread da interface: só mexem em arquivos)
    @staticmethod
    def _paths(pid):
        return (os.path.join(PL_DIR, f"{pid}.cache"), os.path.join(PL_DIR, f"{pid}.m3u.gz"))

    @staticmethod
    def download(source: str):
        text = load_text(source)
        entries = parse(text)
        if not entries:
            raise ValueError("Nenhum item encontrado. O conteúdo é uma playlist M3U válida?")
        return text, entries

    def write_cache(self, pid, text: str, entries: list):
        cache, raw = self._paths(pid)
        with gzip.open(raw + ".tmp", "wt", encoding="utf-8", compresslevel=5) as fh:
            fh.write(text)
        os.replace(raw + ".tmp", raw)
        self._write_pickle(cache, entries)

    @staticmethod
    def _write_pickle(cache, entries):
        with open(cache + ".tmp", "wb") as fh:
            pickle.dump({"v": CACHE_VERSION, "entries": entries}, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(cache + ".tmp", cache)

    def read_cache(self, pid):
        """Catálogo salvo: tenta o cache binário (rápido); se inválido, reinterpreta a cópia do M3U."""
        cache, raw = self._paths(pid)
        try:
            with open(cache, "rb") as fh:
                obj = pickle.load(fh)
            if isinstance(obj, dict) and obj.get("v") == CACHE_VERSION:
                return obj["entries"]
        except Exception:
            pass
        try:
            with gzip.open(raw, "rt", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            return None
        entries = parse(text)
        if entries:
            try:
                self._write_pickle(cache, entries)
            except OSError:
                pass
        return entries or None

    # ------------------------------------------------------------------ favoritos
    def is_fav(self, url: str) -> bool:
        return url in self.data["favorites"]

    def toggle_fav(self, entry: Entry) -> bool:
        favs = self.data["favorites"]
        if entry.url in favs:
            del favs[entry.url]
            state = False
        else:
            favs[entry.url] = entry.to_dict()
            state = True
        self.save()
        return state

    # ------------------------------------------------------------------ histórico
    @staticmethod
    def _is_done(t, length) -> bool:
        return length > 0 and (length - t) < max(45_000, length * 0.04)

    def get_history(self, url):
        return self.data["history"].get(url)

    def record(self, entry: Entry, t: int = 0, length: int = 0, next_up: bool = False):
        h = self.data["history"]
        h.pop(entry.url, None)                  # reinsere no fim = mais recente
        h[entry.url] = {"entry": entry.to_dict(), "t": int(t), "len": int(length),
                        "ts": time.time(), "done": self._is_done(t, length), "next": next_up}
        while len(h) > HISTORY_MAX:
            h.pop(next(iter(h)))

    def mark_done(self, entry: Entry):
        h = self.data["history"]
        rec = h.pop(entry.url, None) or {"entry": entry.to_dict(), "t": 0, "len": 0}
        rec.update(done=True, next=False, ts=time.time())
        if rec.get("len"):
            rec["t"] = rec["len"]
        h[entry.url] = rec

    def remove_history(self, url: str):
        self.data["history"].pop(url, None)

    def clear_history(self):
        self.data["history"] = {}
        self.save()

    def resume_point(self, url: str) -> int:
        rec = self.data["history"].get(url)
        if rec and not rec.get("done") and rec.get("t", 0) > 30_000:
            return int(rec["t"])
        return 0

    def progress(self, url: str):
        """(fração 0–1, assistido) para desenhar a barra de progresso no catálogo."""
        rec = self.data["history"].get(url)
        if not rec or rec["entry"].get("kind") == "live":
            return None
        if rec.get("done"):
            return (1.0, True)
        if rec.get("len", 0) > 0 and rec.get("t", 0) > 0:
            return (min(1.0, rec["t"] / rec["len"]), False)
        return None

    def continue_list(self, live_limit: int = 30) -> list:
        """Itens para a aba Continuar assistindo, do mais recente para o mais antigo."""
        out, lives = [], 0
        for rec in sorted(self.data["history"].values(), key=lambda r: r.get("ts", 0), reverse=True):
            kind = rec["entry"].get("kind")
            if kind == "live":
                if lives < live_limit:
                    out.append(rec)
                    lives += 1
            elif not rec.get("done") and (rec.get("t", 0) >= 15_000 or rec.get("next")):
                out.append(rec)
        return out
