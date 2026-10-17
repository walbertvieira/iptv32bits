"""Parser de playlists M3U/M3U8 no formato estendido (#EXTINF) usado em IPTV."""
import re
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) IPTVPlayer/1.0"

ATTR_RE = re.compile(r'([A-Za-z0-9_\-]+)\s*=\s*"([^"]*)"')
# "Nome S01E02", "Nome - S1 E2", "Nome T01E02", "Nome S01EP02"
SE_RE = re.compile(
    r'^(?P<name>.*?)[\s\-_.:|\[(]*(?<![A-Za-z])[SsTt](?P<s>\d{1,2})\s*[\-_.]?\s*[Ee][Pp]?(?P<e>\d{1,4})(?!\d)'
)
# "Nome 1x02"
X_RE = re.compile(r'^(?P<name>.*?)[\s\-_.:|]*(?<!\d)(?P<s>\d{1,2})x(?P<e>\d{1,3})(?!\d)')

VOD_EXT = (".mp4", ".mkv", ".avi", ".mov", ".m4v", ".wmv", ".flv", ".webm", ".mpg", ".mpeg")
LIVE_KW = ("canal", "canais", "24h", "24 h", "ao vivo", "live", "radio", "tv ")
SERIES_KW = ("serie", "temporada", "season", "novela", "tvshow", "tv show")
MOVIE_KW = ("filme", "movie", "vod", "cinema", "lancamento")


def norm(text: str) -> str:
    """Minúsculas e sem acentos — usado em buscas e classificação."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


@dataclass
class Entry:
    name: str
    url: str
    group: str = "Sem grupo"
    logo: str = ""
    tvg_id: str = ""
    kind: str = "live"          # live | movie | series
    options: list = field(default_factory=list)   # opções de mídia do VLC (":http-user-agent=...")
    series_name: str = ""
    season: int = 0
    episode: int = 0
    search: str = ""

    _KEYS = ("name", "url", "group", "logo", "tvg_id", "kind", "options",
             "series_name", "season", "episode")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self._KEYS}

    @classmethod
    def from_dict(cls, d: dict) -> "Entry":
        e = cls(**{k: v for k, v in d.items() if k in cls._KEYS})
        e.search = norm(e.name)
        return e


def load_text(source: str, timeout: int = 60) -> str:
    """Lê a playlist de um arquivo local ou de uma URL http(s)."""
    if source.lower().startswith(("http://", "https://")):
        req = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    else:
        with open(source, "rb") as fh:
            data = fh.read()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _split_extinf(line: str):
    """Separa '#EXTINF:-1 attr="a,b",Título' em (cabeçalho, título), respeitando aspas."""
    body = line.split(":", 1)[1] if ":" in line else ""
    in_quotes = False
    for i, ch in enumerate(body):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "," and not in_quotes:
            return body[:i], body[i + 1:].strip()
    return body, ""


def _split_url_headers(line: str):
    """Suporta o formato 'url|User-Agent=xxx&Referer=yyy'."""
    if "|" not in line:
        return line.strip(), []
    url, _, hdr = line.partition("|")
    opts = []
    for part in hdr.split("&"):
        key, _, val = part.partition("=")
        key, val = key.strip().lower(), urllib.parse.unquote(val.strip())
        if not val:
            continue
        if key == "user-agent":
            opts.append(f":http-user-agent={val}")
        elif key in ("referer", "referrer"):
            opts.append(f":http-referrer={val}")
    return url.strip(), opts


def _name_from_url(url: str) -> str:
    path = urllib.parse.urlsplit(url).path
    base = urllib.parse.unquote(path.rstrip("/").rsplit("/", 1)[-1])
    return base or url


def parse_episode(title: str):
    """Retorna (nome_da_série, temporada, episódio) ou None."""
    m = SE_RE.match(title) or X_RE.match(title)
    if not m:
        return None
    name = m.group("name").strip(" -_.:|[(")
    return (name or title.strip()), int(m.group("s")), int(m.group("e"))


def classify(name: str, url: str, group: str, attrs: dict) -> str:
    t = (attrs.get("tvg-type") or attrs.get("type") or "").lower()
    if t in ("live", "tv", "channel"):
        return "live"
    if t in ("movie", "movies", "vod"):
        return "movie"
    if t in ("series", "serie", "tvshow"):
        return "series"

    path = urllib.parse.urlsplit(url).path.lower()
    if "/series/" in path:
        return "series"
    if "/movie/" in path or "/movies/" in path:
        return "movie"
    if "/live/" in path:
        return "live"

    is_vod = path.endswith(VOD_EXT)
    g = norm(group) + " "
    if not is_vod and any(k in g for k in LIVE_KW):
        return "live"
    if any(k in g for k in SERIES_KW) or (is_vod and parse_episode(name)):
        return "series"
    if is_vod or any(k in g for k in MOVIE_KW):
        return "movie"
    return "live"


def series_key(e: Entry):
    """Identidade de uma série: nome + grupo.

    Séries homônimas em grupos diferentes (ex.: "Dark" em "Séries | Dublado" e em "Séries | Legendado",
    ou duas produções distintas com o mesmo título) não podem ser misturadas: cada uma tem seus
    próprios episódios.
    """
    return (norm(e.series_name).strip(), e.group)


def group_series(entries) -> list:
    """Agrupa episódios em séries -> [(key, nome, grupo, logo, episódios ordenados)], na ordem da playlist.

    Dentro de uma série, entradas duplicadas (mesma URL) são descartadas.
    """
    out = {}
    for e in entries:
        k = series_key(e)
        s = out.get(k)
        if s is None:
            s = out[k] = [k, e.series_name, e.group, e.logo, [], set()]
        elif not s[3] and e.logo:
            s[3] = e.logo
        if e.url not in s[5]:
            s[5].add(e.url)
            s[4].append(e)
    result = []
    for k, name, group, logo, eps, _ in out.values():
        eps.sort(key=lambda x: (x.season, x.episode, x.name))
        result.append((k, name, group, logo, eps))
    return result


def parse(text: str) -> list:
    entries = []
    info = None          # (attrs, título) do último #EXTINF
    ext_group = None     # #EXTGRP
    vlc_opts = []        # #EXTVLCOPT

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            head = line[:12].upper()
            if head.startswith("#EXTINF"):
                header, title = _split_extinf(line)
                attrs = {k.lower(): v for k, v in ATTR_RE.findall(header)}
                info, ext_group, vlc_opts = (attrs, title), None, []
            elif head.startswith("#EXTGRP:"):
                ext_group = line[8:].strip()
            elif head.startswith("#EXTVLCOPT:"):
                vlc_opts.append(":" + line[11:].strip())
            continue

        url, extra_opts = _split_url_headers(line)
        attrs, title = info if info else ({}, "")
        name = (title or attrs.get("tvg-name") or _name_from_url(url)).strip()
        group = (attrs.get("group-title") or ext_group or "Sem grupo").strip() or "Sem grupo"

        opts = vlc_opts + extra_opts
        ua = attrs.get("user-agent") or attrs.get("http-user-agent")
        if ua:
            opts.append(f":http-user-agent={ua}")
        ref = attrs.get("referrer") or attrs.get("http-referrer") or attrs.get("referer")
        if ref:
            opts.append(f":http-referrer={ref}")

        e = Entry(name=name, url=url, group=group,
                  logo=attrs.get("tvg-logo", "").strip(),
                  tvg_id=attrs.get("tvg-id", ""), options=opts)
        e.kind = classify(name, url, group, attrs)
        if e.kind == "series":
            ep = parse_episode(name)
            if ep:
                e.series_name, e.season, e.episode = ep
            else:
                e.series_name = name
        e.search = norm(name)
        entries.append(e)
        info, ext_group, vlc_opts = None, None, []

    return entries
