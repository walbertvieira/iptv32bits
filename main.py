"""IPTV Player 3.2 — edição 32 bits (Windows 10/11 32 e 64 bits) com VLC embutido.

Mesmos recursos da v3.2: biblioteca de playlists, continuar assistindo, downloads offline e
tela sempre ligada durante a reprodução. Interface em PyQt5 (Qt 5.15, que tem versão 32 bits);
o VLC vai dentro da pasta do programa (subpasta "vlc"), sem precisar instalar.

Requisitos para rodar pelo código: Python 3.12 32 bits, PyQt5, python-vlc e a pasta vlc\
(criada pelo preparar_vlc.ps1). O pacote pronto (.exe) não precisa de nada instalado.
"""
import hashlib
import os
import re
import subprocess
import sys
import time

APP_VERSION = "3.2-32bits"
SELFTEST = "--selftest" in sys.argv
if SELFTEST:                                     # autoteste isolado: não toca nos dados do usuário
    import tempfile
    os.environ["APPDATA"] = tempfile.mkdtemp(prefix="iptv-selftest-")
import urllib.request
from collections import OrderedDict

from PyQt5.QtCore import (QAbstractListModel, QByteArray, QModelIndex, QObject, QRect,
                            QRunnable, QSize, QThreadPool, QTimer, QUrl, Qt)
from PyQt5.QtCore import pyqtSignal as Signal, pyqtSlot as Slot
from PyQt5.QtGui import (QColor, QDesktopServices, QFont, QGuiApplication, QIcon, QKeySequence,
                           QPainter, QPalette, QPixmap)
from PyQt5.QtNetwork import (QNetworkAccessManager, QNetworkDiskCache, QNetworkReply,
                             QNetworkRequest, QSslSocket)
from PyQt5.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QDialog,
                               QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QListView, QListWidget,
                               QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton,
                               QSizePolicy, QSlider, QSpinBox, QSplitter, QStyle,
                               QStyledItemDelegate, QStyleOptionViewItem, QToolBar, QToolButton,
                               QVBoxLayout, QWidget, QShortcut)

import icons
from keepawake import KeepAwake
from downloads import DownloadManager, fmt_bytes
from library import APP_DIR, Store, is_url, short_source
from m3u_parser import USER_AGENT, Entry, group_series, norm, series_key

import vlcloader

vlc, VLC_ERROR = vlcloader.import_vlc()     # VLC embutido na pasta do programa (sem instalação)

APP_NAME = "IPTV Player"
KINDS = [("continue", "Continuar assistindo"), ("live", "Ao vivo"), ("movie", "Filmes"),
         ("series", "Séries"), ("downloads", "Baixados"), ("fav", "Favoritos")]
CONT_GROUPS = [("movie", "Filmes"), ("series", "Séries"), ("live", "Canais recentes")]
DL_GROUPS = [("active", "Baixando / na fila"), ("movie", "Filmes"), ("series", "Séries")]
ROLE_ITEM = Qt.UserRole
ROLE_PROGRESS = Qt.UserRole + 1
ROLE_DL = Qt.UserRole + 2
ACCENT = "#4c7dff"
ICON_COLOR = "#f4f5f7"


def fmt_ms(ms) -> str:
    if ms is None or ms < 0:
        return "--:--"
    s = int(ms) // 1000
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_left(ms) -> str:
    mins = max(1, int(ms // 60_000))
    h, m = divmod(mins, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def ago(ts) -> str:
    if not ts:
        return "nunca"
    d = time.time() - ts
    if d < 60:
        return "agora"
    if d < 3600:
        return f"há {int(d // 60)} min"
    if d < 86400:
        return f"há {int(d // 3600)} h"
    return f"há {int(d // 86400)} dia(s)"


def ep_key(e: Entry):
    return (e.season, e.episode, e.name)


# --------------------------------------------------------------------------- tarefas em 2º plano
class TaskSignals(QObject):
    done = Signal(object)
    error = Signal(str)


class FuncTask(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = TaskSignals()
        self.setAutoDelete(False)

    def run(self):
        try:
            result = self.fn()
        except Exception as exc:
            self.signals.error.emit(str(exc) or exc.__class__.__name__)
            return
        self.signals.done.emit(result)


class TaskHandle(QObject):
    """Recebe o resultado na thread da interface e chama os callbacks."""

    def __init__(self, task, on_done, on_error, parent):
        super().__init__(parent)
        self.task, self.on_done, self.on_error = task, on_done, on_error
        task.signals.done.connect(self._done, Qt.QueuedConnection)
        task.signals.error.connect(self._error, Qt.QueuedConnection)

    @Slot(object)
    def _done(self, result):
        try:
            self.on_done(result)
        finally:
            self.task = None
            self.deleteLater()

    @Slot(str)
    def _error(self, msg):
        try:
            self.on_error(msg)
        finally:
            self.task = None
            self.deleteLater()


# --------------------------------------------------------------------------- logos / capas
class LogoCache(QObject):
    """Baixa logos sob demanda (só dos itens visíveis), com cache em disco e limite de memória.

    - http:// e https:// pelo Qt (QNetworkAccessManager) quando o Qt tem suporte a TLS;
    - https:// pelo Python (urllib, que traz seu próprio OpenSSL) quando o Qt 5 de 32 bits
      não encontra as DLLs do OpenSSL — assim as capas aparecem do mesmo jeito.
    - Memória limitada (LRU): importante num processo de 32 bits, que tem ~2 GB no total.
    """
    ready = Signal(str)
    _fetched = Signal(str, object)          # (url, bytes | None) vindo das threads do Python
    SIZES = {"l": QSize(192, 120), "p": QSize(140, 210)}
    MAX_RAW, MAX_ICONS, MAX_PH = 400, 700, 600

    def __init__(self, parent=None):
        super().__init__(parent)
        self.nam = QNetworkAccessManager(self)
        cache = QNetworkDiskCache(self)
        cache.setCacheDirectory(os.path.join(APP_DIR, "cache_logos"))
        cache.setMaximumCacheSize(300 * 1024 * 1024)
        self.nam.setCache(cache)
        self.qt_tls = False
        try:
            self.qt_tls = bool(QSslSocket.supportsSsl())
        except Exception:
            pass
        self.py_dir = os.path.join(APP_DIR, "cache_logos_https")
        self.py_pool = None
        self._fetched.connect(self._py_done)
        self.raw, self.icons, self.ph = OrderedDict(), OrderedDict(), OrderedDict()
        self.failed, self.wanted = set(), set()
        self.queue, self.inflight, self.max_inflight = [], 0, 12

    @staticmethod
    def _lru_put(d: OrderedDict, key, value, limit: int):
        d[key] = value
        d.move_to_end(key)
        while len(d) > limit:
            d.popitem(last=False)

    def icon(self, url: str, title: str, kind: str) -> QIcon:
        orient = "p" if kind in ("movie", "series") else "l"
        if url:
            key = (url, orient)
            ic = self.icons.get(key)
            if ic is not None:
                self.icons.move_to_end(key)
                return ic
            pm = self.raw.get(url)
            if pm is not None:
                self.raw.move_to_end(url)
                ic = QIcon(self._fit(pm, orient))
                self._lru_put(self.icons, key, ic, self.MAX_ICONS)
                return ic
            if url not in self.failed and url not in self.wanted:
                self._want(url)
        return self._placeholder(title, orient)

    def _want(self, url: str):
        if is_url(url):
            self.wanted.add(url)
            self.queue.append(url)
            if len(self.queue) > 300:              # descarta pedidos antigos (rolagem rápida)
                self.wanted.discard(self.queue.pop(0))
            self._pump()
            return
        path = QUrl(url).toLocalFile() if url.startswith("file:") else url
        pm = QPixmap(path)
        if pm.isNull():
            self.failed.add(url)
        else:
            self._lru_put(self.raw, url, self._shrink(pm), self.MAX_RAW)
            QTimer.singleShot(0, lambda: self.ready.emit(url))

    def _pump(self):
        while self.queue and self.inflight < self.max_inflight:
            url = self.queue.pop()                  # LIFO: prioriza o que acabou de aparecer
            self.inflight += 1
            if url.lower().startswith("https://") and not self.qt_tls:
                self._py_fetch(url)
                continue
            req = QNetworkRequest(QUrl(url))
            req.setHeader(QNetworkRequest.UserAgentHeader, USER_AGENT)
            req.setAttribute(QNetworkRequest.CacheLoadControlAttribute, QNetworkRequest.PreferCache)
            req.setAttribute(QNetworkRequest.FollowRedirectsAttribute, True)
            req.setTransferTimeout(15000)
            reply = self.nam.get(req)
            reply.finished.connect(lambda r=reply, u=url: self._done(r, u))

    def _done(self, reply: QNetworkReply, url: str):
        ok = reply.error() == QNetworkReply.NoError
        data = reply.readAll().data() if ok else None
        reply.deleteLater()
        self._finish(url, data)

    # --- caminho Python (https sem OpenSSL no Qt)
    def _py_fetch(self, url: str):
        if self.py_pool is None:
            from concurrent.futures import ThreadPoolExecutor
            os.makedirs(self.py_dir, exist_ok=True)
            self.py_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="logo")
            self.py_pool.submit(self._py_prune)
        self.py_pool.submit(self._py_worker, url)

    def _py_worker(self, url: str):
        path = os.path.join(self.py_dir, hashlib.sha1(url.encode("utf-8", "ignore")).hexdigest())
        data = None
        try:
            if os.path.isfile(path):
                with open(path, "rb") as fh:
                    data = fh.read()
            else:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read(8 * 1024 * 1024)
                tmp = path + ".tmp"
                with open(tmp, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, path)
        except Exception:
            data = None
        self._fetched.emit(url, data)              # entregue na thread da interface

    def _py_prune(self, limit: int = 300 * 1024 * 1024):
        try:
            files = [os.path.join(self.py_dir, n) for n in os.listdir(self.py_dir)]
            files = [(os.path.getmtime(f), os.path.getsize(f), f) for f in files if os.path.isfile(f)]
            total = sum(sz for _, sz, _ in files)
            for _, sz, f in sorted(files):
                if total <= limit:
                    break
                try:
                    os.remove(f)
                    total -= sz
                except OSError:
                    pass
        except OSError:
            pass

    @Slot(str, object)
    def _py_done(self, url: str, data):
        self._finish(url, data)

    def _finish(self, url: str, data):
        self.inflight -= 1
        self.wanted.discard(url)
        pm = QPixmap()
        if data and pm.loadFromData(data):
            self._lru_put(self.raw, url, self._shrink(pm), self.MAX_RAW)
            self.ready.emit(url)
        else:
            self.failed.add(url)
        self._pump()

    @staticmethod
    def _shrink(pm: QPixmap) -> QPixmap:
        if pm.width() <= 280 and pm.height() <= 280:
            return pm
        return pm.scaled(280, 280, Qt.KeepAspectRatio, Qt.SmoothTransformation)

    def _fit(self, pm: QPixmap, orient: str) -> QPixmap:
        size = self.SIZES[orient]
        canvas = QPixmap(size)
        canvas.fill(Qt.transparent)
        scaled = pm.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        p = QPainter(canvas)
        p.drawPixmap((size.width() - scaled.width()) // 2,
                     (size.height() - scaled.height()) // 2, scaled)
        p.end()
        return canvas

    def _placeholder(self, title: str, orient: str) -> QIcon:
        words = re.findall(r"\w+", title)
        initials = "".join(w[0] for w in words[:2]).upper() or "?"
        hue = int(hashlib.md5(title.encode("utf-8", "ignore")).hexdigest()[:4], 16) % 360
        key = (initials, hue, orient)
        ic = self.ph.get(key)
        if ic is None:
            size = self.SIZES[orient]
            pm = QPixmap(size)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor.fromHsv(hue, 120, 115))
            p.drawRoundedRect(pm.rect().adjusted(3, 3, -3, -3), 12, 12)
            f = QFont()
            f.setPixelSize(38)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor(255, 255, 255, 230))
            p.drawText(pm.rect(), Qt.AlignCenter, initials)
            p.end()
            ic = QIcon(pm)
            self._lru_put(self.ph, key, ic, self.MAX_PH)
        return ic


# --------------------------------------------------------------------------- catálogo
class CatalogItem:
    __slots__ = ("title", "logo", "kind", "entry", "episodes", "group", "sub", "resume", "dl", "key")

    def __init__(self, title, logo, kind, entry=None, episodes=None, group="", sub="", resume=0, dl=False,
                 key=None):
        self.title, self.logo, self.kind = title, logo, kind
        self.entry, self.episodes, self.group = entry, episodes, group
        self.sub, self.resume, self.dl, self.key = sub, resume, dl, key


class CatalogModel(QAbstractListModel):
    def __init__(self, logos: LogoCache, store: Store, dl: DownloadManager, parent=None):
        super().__init__(parent)
        self.logos, self.store, self.dl = logos, store, dl
        self.items, self.rows_by_logo = [], {}

    def set_items(self, items):
        self.beginResetModel()
        self.items = items
        self.rows_by_logo = {}
        for i, it in enumerate(items):
            if it.logo:
                self.rows_by_logo.setdefault(it.logo, []).append(i)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        it = self.items[index.row()]
        if role == Qt.DisplayRole:
            sub = self.dl.describe(it.entry.url) if it.dl else it.sub
            text = it.title + (f"\n{sub}" if sub else "")
            if it.entry is not None and self.store.is_fav(it.entry.url):
                text = "★ " + text
            return text
        if role == Qt.DecorationRole:
            return self.logos.icon(it.logo, it.title, it.kind)
        if role == Qt.ToolTipRole:
            return f"{it.title}\n{it.group}" + (f"\n{it.sub}" if it.sub else "")
        if role == ROLE_ITEM:
            return it
        if role == ROLE_PROGRESS:
            if it.entry is None or it.entry.kind == "live":
                return None
            if it.dl and self.dl.status(it.entry.url) in ("downloading", "queued", "paused", "error"):
                return (self.dl.fraction(it.entry.url) or 0.0, False, ACCENT)
            return self.store.progress(it.entry.url)
        if role == ROLE_DL:
            return self.dl.status(it.entry.url) if it.entry is not None else None
        return None

    def logo_ready(self, url: str):
        for row in self.rows_by_logo.get(url, ()):
            ix = self.index(row)
            self.dataChanged.emit(ix, ix, [Qt.DecorationRole])

    def refresh(self):
        if self.items:
            self.dataChanged.emit(self.index(0), self.index(len(self.items) - 1))


class CatalogDelegate(QStyledItemDelegate):
    """Desenha a barra de progresso (e o selo de assistido) sobre a capa."""

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        prog = index.data(ROLE_PROGRESS)
        dl = index.data(ROLE_DL)
        if not prog and not dl:
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        style = opt.widget.style() if opt.widget else QApplication.style()
        r = style.subElementRect(QStyle.SE_ItemViewItemDecoration, opt, opt.widget)
        if r.width() < 12 or r.height() < 12:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        if dl:                                   # selo de download no canto superior esquerdo
            d = max(14, min(22, r.width() // 5))
            badge = QRect(r.left() + 2, r.top() + 2, d, d)
            color = {"done": ACCENT, "downloading": "#ff9f1c", "queued": "#8a8f98",
                     "paused": "#8a8f98", "error": "#e5484d"}.get(dl, "#8a8f98")
            painter.setBrush(QColor(color))
            painter.drawEllipse(badge)
            painter.drawPixmap(badge.adjusted(3, 3, -3, -3),
                               icons.icon("download", "#ffffff").pixmap(64, 64))
            painter.setPen(Qt.NoPen)
        if not prog:
            painter.restore()
            return
        frac, done = prog[0], prog[1]
        bar_color = prog[2] if len(prog) > 2 else "#ff4d57"
        if done:
            d = max(14, min(22, r.width() // 5))
            badge = QRect(r.right() - d - 2, r.top() + 2, d, d)
            painter.setBrush(QColor("#2fbf71"))
            painter.drawEllipse(badge)
            painter.setPen(QColor("white"))
            f = QFont()
            f.setPixelSize(int(d * .7))
            f.setBold(True)
            painter.setFont(f)
            painter.drawText(badge, Qt.AlignCenter, "✓")
        else:
            h = 5 if r.height() > 50 else 3
            bar = QRect(r.left() + 4, r.bottom() - h - 3, r.width() - 8, h)
            painter.setBrush(QColor(0, 0, 0, 170))
            painter.drawRoundedRect(bar, 2, 2)
            done_w = max(3, int(bar.width() * frac))
            painter.setBrush(QColor(bar_color))
            painter.drawRoundedRect(QRect(bar.left(), bar.top(), done_w, h), 2, 2)
        painter.restore()


# --------------------------------------------------------------------------- player
class ClickSlider(QSlider):
    """Slider que pula direto para o ponto clicado."""

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton and self.maximum() > self.minimum():
            val = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(),
                                                 int(ev.pos().x()), self.width())
            self.setValue(val)
        super().mousePressEvent(ev)


class VideoFrame(QFrame):
    doubleClicked = Signal()

    def __init__(self):
        super().__init__()
        self.setAttribute(Qt.WA_NativeWindow)
        self.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.setMinimumSize(320, 180)
        self.message = "Selecione um canal, filme ou episódio"

    def set_message(self, text: str):
        self.message = text
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.black)
        if self.message:
            p.setPen(QColor("#9a9da4"))
            p.drawText(self.rect().adjusted(20, 20, -20, -20),
                       Qt.AlignCenter | Qt.TextWordWrap, self.message)

    def mouseDoubleClickEvent(self, ev):
        self.doubleClicked.emit()


class PlayerPanel(QWidget):
    fullscreenRequested = Signal()
    nextRequested = Signal()
    prevRequested = Signal()
    favRequested = Signal()
    ended = Signal()
    stopped = Signal()
    downloadRequested = Signal()
    progress = Signal(str, object, object)

    def __init__(self, store: Store, parent=None):
        super().__init__(parent)
        self.store = store
        self.current = None
        self._embedded = False
        self._dragging = False
        self._last_state = None

        self.title = QLabel("—")
        self.title.setObjectName("playerTitle")
        self.title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.offline_badge = QLabel("⬇ OFFLINE")
        self.offline_badge.setObjectName("offlineBadge")
        self.offline_badge.setToolTip("Reproduzindo o arquivo baixado (não usa a internet)")
        self.offline_badge.hide()
        self.video = VideoFrame()
        self.video.doubleClicked.connect(self.fullscreenRequested)

        # --- linha do tempo
        self.time_lbl = QLabel("--:--")
        self.time_lbl.setObjectName("timeLabel")
        self.len_lbl = QLabel("--:--")
        self.len_lbl.setObjectName("timeLabel")
        self.len_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.live_badge = QLabel("● AO VIVO")
        self.live_badge.setObjectName("liveBadge")
        self.live_badge.hide()
        self.seek = ClickSlider(Qt.Horizontal)
        self.seek.setObjectName("seek")
        self.seek.setRange(0, 1000)
        self.seek.setEnabled(False)
        self.seek.setCursor(Qt.PointingHandCursor)
        self.seek.sliderPressed.connect(lambda: setattr(self, "_dragging", True))
        self.seek.sliderReleased.connect(self._seek_released)
        self.seek.sliderMoved.connect(self._seek_preview)

        # --- botões
        def mbtn(icon_name, tip, slot, primary=False):
            b = QToolButton()
            b.setObjectName("mediaPrimary" if primary else "media")
            b.setIcon(icons.icon(icon_name, ICON_COLOR))
            b.setIconSize(QSize(26, 26) if primary else QSize(19, 19))
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
            if slot:
                b.clicked.connect(slot)
            return b

        self.prev_btn = mbtn("prev", "Anterior (Ctrl+PgUp)", self.prevRequested.emit)
        self.back_btn = mbtn("back10", "Voltar 10 s (Ctrl+←)", lambda: self.seek_by(-10_000))
        self.play_btn = mbtn("play", "Reproduzir / pausar (Espaço)", self.toggle_pause, primary=True)
        self.fwd_btn = mbtn("fwd10", "Avançar 10 s (Ctrl+→)", lambda: self.seek_by(10_000))
        self.next_btn = mbtn("next", "Próximo (Ctrl+PgDown)", self.nextRequested.emit)
        self.stop_btn = mbtn("stop", "Parar", self.stop)
        self.fav_btn = mbtn("star", "Adicionar aos favoritos (Ctrl+D)", self.favRequested.emit)
        self.dl_btn = mbtn("download", "Baixar para assistir offline", self.downloadRequested.emit)
        self.dl_btn.setEnabled(False)
        self.opts_btn = mbtn("gear", "Áudio, legendas e proporção", None)
        self.opts_btn.setPopupMode(QToolButton.InstantPopup)
        self.opts_menu = QMenu(self)
        self.opts_menu.aboutToShow.connect(self._fill_opts)
        self.opts_btn.setMenu(self.opts_menu)
        self.mute_btn = mbtn("volume", "Mudo (M)", self.toggle_mute)
        self.volume = ClickSlider(Qt.Horizontal)
        self.volume.setObjectName("volume")
        self.volume.setRange(0, 150)
        self.volume.setFixedWidth(100)
        self.volume.setCursor(Qt.PointingHandCursor)
        self.volume.setToolTip("Volume (Ctrl+↑ / Ctrl+↓)")
        self.volume.valueChanged.connect(self._volume_changed)
        self.vol_lbl = QLabel("80%")
        self.vol_lbl.setObjectName("volLabel")
        self.fs_btn = mbtn("fullscreen", "Tela cheia (F11 / duplo clique)", self.fullscreenRequested.emit)
        self.state_lbl = QLabel("")
        self.state_lbl.setObjectName("stateLabel")

        bar = QFrame()
        bar.setObjectName("controlBar")
        bl = QVBoxLayout(bar)
        bl.setContentsMargins(12, 8, 12, 10)
        bl.setSpacing(6)
        seek_row = QHBoxLayout()
        seek_row.setSpacing(10)
        seek_row.addWidget(self.time_lbl)
        seek_row.addWidget(self.seek, 1)
        seek_row.addWidget(self.len_lbl)
        seek_row.addWidget(self.live_badge)
        ctrl = QHBoxLayout()
        ctrl.setSpacing(8)
        for w in (self.prev_btn, self.back_btn, self.play_btn, self.fwd_btn, self.next_btn,
                  self.stop_btn):
            ctrl.addWidget(w, 0, Qt.AlignVCenter)
        ctrl.addSpacing(6)
        ctrl.addWidget(self.state_lbl)
        ctrl.addStretch(1)
        for w in (self.dl_btn, self.fav_btn, self.opts_btn, self.mute_btn, self.volume, self.vol_lbl, self.fs_btn):
            ctrl.addWidget(w, 0, Qt.AlignVCenter)
        bl.addLayout(seek_row)
        bl.addLayout(ctrl)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 6)
        lay.setSpacing(6)
        title_row = QHBoxLayout()
        title_row.addWidget(self.title, 1)
        title_row.addWidget(self.offline_badge)
        lay.addLayout(title_row)
        lay.addWidget(self.video, 1)
        lay.addWidget(bar)

        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._tick)
        self.awake = KeepAwake()              # impede a tela de apagar enquanto o vídeo toca
        self._awake_ticks = 0

        self.instance = self.mp = None
        if vlc is not None:
            try:
                self.instance = vlc.Instance("--network-caching=1500", "--no-video-title-show", "--quiet")
                self.mp = self.instance.media_player_new() if self.instance else None
            except Exception:
                self.mp = None
        if self.mp is None:
            self.video.set_message("O VLC não foi encontrado.\n\nEsta edição usa o VLC embutido na pasta "
                                   "\"vlc\" ao lado do programa. Extraia o pacote completo de novo "
                                   "(sem separar os arquivos) e abra o programa.")
            for w in (self.play_btn, self.stop_btn, self.back_btn, self.fwd_btn, self.seek,
                      self.volume, self.opts_btn, self.mute_btn):
                w.setEnabled(False)
        self.volume.setValue(int(store.data.get("volume", 80)))
        self._volume_changed(self.volume.value())

    # --- controle
    def _embed(self):
        if self._embedded or self.mp is None:
            return
        wid = int(self.video.winId())
        if sys.platform.startswith("win"):
            self.mp.set_hwnd(wid)
        elif sys.platform == "darwin":
            self.mp.set_nsobject(wid)
        else:
            self.mp.set_xwindow(wid)
        self.mp.video_set_mouse_input(False)
        self.mp.video_set_key_input(False)
        self._embedded = True

    def play(self, entry: Entry, start_ms: int = 0, local_path: str = None):
        if self.mp is None:
            QMessageBox.warning(self, APP_NAME, "Os arquivos do VLC (pasta \"vlc\" ao lado do programa) não foram encontrados.\n\n"
                                + (VLC_ERROR or ""))
            return False
        self._embed()
        if local_path:                              # arquivo baixado: nada de rede
            media = self.instance.media_new_path(local_path)
        else:
            media = self.instance.media_new(entry.url)
            for opt in entry.options:
                media.add_option(opt)
        if start_ms > 0:
            media.add_option(f":start-time={start_ms / 1000:.1f}")
        self.mp.set_media(media)
        self.mp.play()
        self.mp.audio_set_volume(self.volume.value())
        self.current = entry
        self.title.setText(entry.name)
        self.title.setToolTip(f"{entry.name}\n{entry.group}" + (f"\n{local_path}" if local_path else ""))
        self.offline_badge.setProperty("on", bool(local_path))
        self.offline_badge.setVisible(bool(local_path))
        self.video.set_message("")
        self._last_state = None
        self.seek.setValue(0)
        self.timer.start()
        return True

    def stop(self):
        if self.mp is None:
            return
        self.mp.stop()
        self.timer.stop()
        self.awake.set(False)
        self._update_play_icon(False)
        self.state_lbl.setText("")
        self.time_lbl.setText("--:--")
        self.len_lbl.setText("--:--")
        self.len_lbl.show()
        self.live_badge.hide()
        self.seek.setValue(0)
        self.seek.setEnabled(False)
        self.offline_badge.setProperty("on", False)
        self.offline_badge.hide()
        self.video.set_message("Parado")
        self.stopped.emit()

    def toggle_pause(self):
        if self.mp is None:
            return
        state = self.mp.get_state()
        if state == vlc.State.Playing:
            self.mp.pause()
        elif state in (vlc.State.Ended, vlc.State.Stopped, vlc.State.Error, vlc.State.NothingSpecial):
            if self.current:
                self.play(self.current)
        else:
            self.mp.play()

    def seek_by(self, ms: int):
        if self.mp is None:
            return
        length = self.mp.get_length()
        if length > 0:
            t = self.mp.get_time()
            self.mp.set_time(max(0, min(length - 1000, t + ms)))

    def change_volume(self, delta: int):
        self.volume.setValue(self.volume.value() + delta)

    def toggle_mute(self):
        if self.mp is None:
            return
        self.mp.audio_set_mute(not self.mp.audio_get_mute())
        muted = bool(self.mp.audio_get_mute())
        self.mute_btn.setIcon(icons.icon("mute", "#ff6b6b") if muted else icons.icon("volume", ICON_COLOR))

    def set_fav(self, state: bool):
        self.fav_btn.setIcon(icons.icon("star_filled", "#ffc93c") if state else icons.icon("star", ICON_COLOR))
        self.fav_btn.setToolTip("Remover dos favoritos (Ctrl+D)" if state else "Adicionar aos favoritos (Ctrl+D)")

    def set_download_state(self, state):
        """state: None (não baixável), "none", "done" ou um status em andamento."""
        self.dl_btn.setEnabled(state is not None and state != "done")
        if state == "done":
            self.dl_btn.setIcon(icons.icon("check", "#5be38f"))
            self.dl_btn.setToolTip("Já baixado — reproduzindo sem internet")
        elif state in ("downloading", "queued"):
            self.dl_btn.setIcon(icons.icon("download", "#ffb454"))
            self.dl_btn.setToolTip("Baixando… (veja a aba Baixados)")
        else:
            self.dl_btn.setIcon(icons.icon("download", ICON_COLOR))
            self.dl_btn.setToolTip("Baixar para assistir offline" if state is not None
                                   else "Canais ao vivo não podem ser baixados")

    def set_fullscreen(self, fs: bool):
        self.title.setVisible(not fs)
        self.offline_badge.setVisible(not fs and self.offline_badge.property("on") is True)
        self.fs_btn.setIcon(icons.icon("exit_fullscreen" if fs else "fullscreen", ICON_COLOR))
        self.fs_btn.setToolTip("Sair da tela cheia (Esc)" if fs else "Tela cheia (F11 / duplo clique)")

    # --- internos
    def _volume_changed(self, v: int):
        self.store.data["volume"] = v
        self.vol_lbl.setText(f"{v}%")
        if self.mp is not None:
            self.mp.audio_set_volume(v)

    def _seek_preview(self, v: int):
        if self.mp is not None:
            length = self.mp.get_length()
            if length > 0:
                self.time_lbl.setText(fmt_ms(length * v / 1000))

    def _seek_released(self):
        self._dragging = False
        if self.mp is not None and self.mp.get_length() > 0:
            self.mp.set_position(self.seek.value() / 1000)

    def _update_play_icon(self, playing: bool):
        self.play_btn.setIcon(icons.icon("pause" if playing else "play", "#ffffff"))

    def _tick(self):
        state = self.mp.get_state()
        if state != self._last_state:
            self._last_state = state
            self._on_state(state)
        self._awake_ticks += 1
        if self._awake_ticks >= 120:          # 120 × 500 ms = 60 s
            self._awake_ticks = 0
            self.awake.refresh()
        length, t = self.mp.get_length(), self.mp.get_time()
        seekable = length > 0
        self.seek.setEnabled(seekable)
        self.back_btn.setEnabled(seekable)
        self.fwd_btn.setEnabled(seekable)
        if seekable and not self._dragging:
            self.seek.blockSignals(True)
            self.seek.setValue(int(max(t, 0) * 1000 / length))
            self.seek.blockSignals(False)
        if not self._dragging:
            self.time_lbl.setText(fmt_ms(t))
        is_live = not seekable and state == vlc.State.Playing
        self.live_badge.setVisible(is_live)
        self.len_lbl.setVisible(not is_live)
        self.len_lbl.setText(fmt_ms(length) if seekable else "--:--")
        if (state == vlc.State.Playing and seekable and self.current
                and self.current.kind != "live" and t > 0):
            self.progress.emit(self.current.url, int(t), int(length))

    def _on_state(self, state):
        S = vlc.State
        self._update_play_icon(state == S.Playing)
        # tela sempre ligada só enquanto toca (ou abrindo/carregando o vídeo); pausado/parado/fim/erro libera
        self.awake.set(state in (S.Playing, S.Opening, S.Buffering))
        labels = {S.Opening: "Abrindo…", S.Buffering: "Carregando…", S.Paused: "Pausado",
                  S.Ended: "Fim"}
        if state == S.Error:
            self.timer.stop()
            self.state_lbl.setText("Erro")
            self.video.set_message("Não foi possível reproduzir este conteúdo.\n"
                                   "O link pode estar fora do ar ou exigir outro formato.")
            return
        self.state_lbl.setText(labels.get(state, ""))
        if state == S.Ended:
            self.timer.stop()
            self.ended.emit()

    def _fill_opts(self):
        m = self.opts_menu
        m.clear()
        if self.mp is None:
            return

        def dec(x):
            return x.decode("utf-8", "replace") if isinstance(x, bytes) else str(x)

        def add(menu, text, fn, checked=None):
            a = menu.addAction(text)
            if checked is not None:
                a.setCheckable(True)
                a.setChecked(checked)
            a.triggered.connect(fn)

        audio = m.addMenu("Faixa de áudio")
        cur = self.mp.audio_get_track()
        for tid, name in (self.mp.audio_get_track_description() or []):
            add(audio, dec(name), lambda _=False, i=tid: self.mp.audio_set_track(i), tid == cur)
        if audio.isEmpty():
            audio.addAction("(indisponível)").setEnabled(False)

        subs = m.addMenu("Legendas")
        cur = self.mp.video_get_spu()
        for tid, name in (self.mp.video_get_spu_description() or []):
            add(subs, dec(name), lambda _=False, i=tid: self.mp.video_set_spu(i), tid == cur)
        subs.addSeparator()
        add(subs, "Carregar arquivo de legenda…", self._load_sub)

        ar = m.addMenu("Proporção da imagem")
        for label, val in (("Automática", None), ("16:9", "16:9"), ("4:3", "4:3"),
                           ("16:10", "16:10"), ("21:9", "21:9")):
            add(ar, label, lambda _=False, v=val: self.mp.video_set_aspect_ratio(v))

    def _load_sub(self):
        path, _ = QFileDialog.getOpenFileName(self, "Legenda", "",
                                              "Legendas (*.srt *.ass *.ssa *.sub *.vtt);;Todos (*.*)")
        if path:
            self.mp.add_slave(vlc.MediaSlaveType.subtitle, QUrl.fromLocalFile(path).toString(), True)


# --------------------------------------------------------------------------- diálogos
class AddPlaylistDialog(QDialog):
    def __init__(self, parent, source=""):
        super().__init__(parent)
        self.setWindowTitle("Adicionar playlist")
        self.setMinimumWidth(560)
        self._name_edited = False
        self.name = QLineEdit()
        self.name.setPlaceholderText("Ex.: Minha lista")
        self.name.textEdited.connect(lambda _: setattr(self, "_name_edited", True))
        self.source = QLineEdit()
        self.source.setPlaceholderText("https://servidor/lista.m3u   ou   C:\\pasta\\lista.m3u")
        self.source.textChanged.connect(self._source_changed)
        browse = QPushButton("Procurar arquivo…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.source, 1)
        row.addWidget(browse)
        form = QFormLayout()
        form.addRow("Endereço (URL) ou arquivo:", row)
        form.addRow("Nome:", self.name)
        hint = QLabel("A playlist fica salva dentro do aplicativo e abre instantaneamente nas próximas vezes. "
                      "Listas por URL são atualizadas automaticamente em segundo plano.")
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok |
                                        QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Adicionar")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(hint)
        lay.addWidget(self.buttons)
        self.source.setText(source)
        self._source_changed(source)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Escolher playlist", "",
                                              "Playlists (*.m3u *.m3u8);;Todos os arquivos (*.*)")
        if path:
            self.source.setText(path)

    def _source_changed(self, text):
        text = text.strip()
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(bool(text))
        if not self._name_edited:
            base = short_source(text) if text else ""
            if not is_url(text):
                base = os.path.splitext(base)[0]
            self.name.setText(base)

    def values(self):
        return self.name.text().strip(), self.source.text().strip()


class ManageDialog(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win, self.store = win, win.store
        self.setWindowTitle("Minhas playlists")
        self.resize(640, 420)
        self.list = QListWidget()
        self.list.setObjectName("manageList")
        self.list.itemDoubleClicked.connect(lambda _: self._open())

        def b(text, slot):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            return btn

        side = QVBoxLayout()
        for w in (b("Abrir", self._open), b("Atualizar agora", self._refresh),
                  b("Renomear…", self._rename), b("Remover", self._remove)):
            side.addWidget(w)
        side.addSpacing(12)
        side.addWidget(b("Adicionar…", self._add))
        side.addStretch(1)

        self.hours = QSpinBox()
        self.hours.setRange(0, 168)
        self.hours.setSuffix(" h")
        self.hours.setValue(int(self.store.data.get("auto_update_hours", 12)))
        self.hours.valueChanged.connect(self._hours_changed)
        auto = QHBoxLayout()
        auto.addWidget(QLabel("Atualizar playlists por URL automaticamente a cada"))
        auto.addWidget(self.hours)
        auto.addWidget(QLabel("(0 = nunca)"))
        auto.addStretch(1)

        body = QHBoxLayout()
        body.addWidget(self.list, 1)
        body.addLayout(side)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(body, 1)
        lay.addLayout(auto)
        lay.addWidget(close)
        self.reload()

    def reload(self):
        cur = self._pid()
        self.list.clear()
        active = self.store.data.get("active")
        for p in self.store.playlists():
            mark = "●  " if p["id"] == active else "     "
            it = QListWidgetItem(f"{mark}{p['name']}\n      {short_source(p['source'])}  ·  "
                                 f"{p.get('count', 0)} itens  ·  atualizada {ago(p.get('updated'))}")
            it.setData(ROLE_ITEM, p["id"])
            self.list.addItem(it)
            if p["id"] == (cur or active):
                self.list.setCurrentItem(it)

    def _pid(self):
        it = self.list.currentItem()
        return it.data(ROLE_ITEM) if it else None

    def _open(self):
        pid = self._pid()
        if pid:
            self.win.open_playlist(pid)
            self.accept()

    def _refresh(self):
        pid = self._pid()
        if pid:
            self.win.refresh_playlist(pid, silent=False, on_finish=self.reload)

    def _rename(self):
        pid = self._pid()
        p = self.store.playlist(pid)
        if not p:
            return
        name, ok = QInputDialog.getText(self, "Renomear", "Novo nome:", text=p["name"])
        if ok and name.strip():
            self.store.rename_playlist(pid, name)
            self.win.update_playlist_widgets()
            self.reload()

    def _remove(self):
        pid = self._pid()
        p = self.store.playlist(pid)
        if not p:
            return
        r = QMessageBox.question(self, "Remover playlist",
                                 f"Remover “{p['name']}” do aplicativo?\n"
                                 "Favoritos e histórico continuam salvos.")
        if r == QMessageBox.Yes:
            self.win.remove_playlist(pid)
            self.reload()

    def _add(self):
        self.win.add_playlist(on_finish=self.reload)

    def _hours_changed(self, v):
        self.store.data["auto_update_hours"] = v
        self.store.save()


# --------------------------------------------------------------------------- janela principal
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1480, 860)
        self.setAcceptDrops(True)

        self.store = Store()
        self.entries = []
        self.by_kind = {"live": [], "movie": [], "series": []}
        ui = self.store.data["ui"]
        self.kind = ui.get("kind") if ui.get("kind") in dict(KINDS) else "live"
        self.group, self.open_series = None, None
        self.active_pid = self.store.data.get("active")
        self.shown_pid = None
        self._pending_group = ui.get("group")
        self._refreshing = set()
        self._busy_n = 0
        self.play_queue, self.play_index = [], -1
        self._last_save = time.monotonic()
        self._was_max = False

        self.logos = LogoCache(self)
        self.dl = DownloadManager(self.store, self)
        self.model = CatalogModel(self.logos, self.store, self.dl, self)
        self.logos.ready.connect(self.model.logo_ready)
        self.dl.changed.connect(self._on_dl_changed)
        self.dl.progressed.connect(self._on_dl_progress)
        self.dl_timer = QTimer(self)                 # atualiza a aba Baixados ~1x por segundo
        self.dl_timer.setSingleShot(True)
        self.dl_timer.setInterval(800)
        self.dl_timer.timeout.connect(self.model.refresh)

        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(250)
        self.search_timer.timeout.connect(self.rebuild_catalog)

        self._build_toolbar()
        self._build_body()
        self._build_shortcuts()
        self._restore_window()
        self.update_playlist_widgets()
        self._update_kind_labels()
        self.set_kind(self.kind, group=self._pending_group if self.kind == "continue" else None)
        QTimer.singleShot(0, self._startup)

    # ------------------------------------------------------------------ interface
    def _build_toolbar(self):
        tb = QToolBar("Principal")
        tb.setMovable(False)
        tb.setIconSize(QSize(18, 18))
        self.addToolBar(tb)
        self.toolbar = tb

        self.pl_btn = QToolButton()
        self.pl_btn.setObjectName("playlistButton")
        self.pl_btn.setIcon(icons.icon("list", ICON_COLOR))
        self.pl_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.pl_btn.setPopupMode(QToolButton.InstantPopup)
        self.pl_menu = QMenu(self)
        self.pl_menu.aboutToShow.connect(self._fill_playlist_menu)
        self.pl_btn.setMenu(self.pl_menu)
        tb.addWidget(self.pl_btn)

        self.refresh_btn = QToolButton()
        self.refresh_btn.setIcon(icons.icon("refresh", ICON_COLOR))
        self.refresh_btn.setToolTip("Atualizar a playlist agora (F5)")
        self.refresh_btn.clicked.connect(lambda: self.active_pid and self.refresh_playlist(self.active_pid, silent=False))
        tb.addWidget(self.refresh_btn)
        tb.addSeparator()

        self.kind_buttons = {}
        self.kind_group = QButtonGroup(self)
        self.kind_group.setExclusive(True)
        for key, label in KINDS:
            b = QToolButton()
            b.setText(label)
            b.setCheckable(True)
            b.setObjectName("continueButton" if key == "continue" else "kindButton")
            b.setProperty("kind", key)
            self.kind_group.addButton(b)
            tb.addWidget(b)
            self.kind_buttons[key] = b
        for _k, _b in self.kind_buttons.items():
            _b.clicked.connect(lambda _=False, k=_k: self.set_kind(k))

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Buscar…  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(260)
        self.search.textChanged.connect(lambda _: self.search_timer.start())
        self.search.returnPressed.connect(lambda: self.view.setFocus())
        tb.addWidget(self.search)

        self.view_btn = QToolButton()
        self.view_btn.setCheckable(True)
        self.view_btn.setToolTip("Alternar entre grade e lista")
        self.view_btn.setChecked(bool(self.store.data.get("list_mode")))
        self.view_btn.setText("Grade" if self.view_btn.isChecked() else "Lista")
        self.view_btn.toggled.connect(self._view_toggled)
        tb.addWidget(self.view_btn)

    def _build_body(self):
        self.groups = QListWidget()
        self.groups.setObjectName("groups")
        self.groups.currentRowChanged.connect(self._group_changed)

        self.catalog_widget = QWidget()
        cv = QVBoxLayout(self.catalog_widget)
        cv.setContentsMargins(4, 4, 4, 0)
        cv.setSpacing(4)
        bar = QHBoxLayout()
        self.back_btn = QToolButton()
        self.back_btn.setText("◀ Voltar")
        self.back_btn.setToolTip("Voltar para a lista de séries (Alt+← ou Esc)")
        self.back_btn.clicked.connect(self.close_series)
        self.back_btn.hide()
        self.crumb = QLabel()
        self.crumb.setObjectName("crumb")
        self.clear_hist_btn = QToolButton()
        self.clear_hist_btn.setText("Limpar histórico")
        self.clear_hist_btn.clicked.connect(self._clear_history)
        self.clear_hist_btn.hide()
        self.dl_opts_btn = QToolButton()
        self.dl_opts_btn.setText("Opções de download  ▾")
        self.dl_opts_btn.setIcon(icons.icon("folder", ICON_COLOR))
        self.dl_opts_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.dl_opts_btn.setPopupMode(QToolButton.InstantPopup)
        self.dl_opts_btn.setObjectName("plainMenuButton")
        self.dl_menu = QMenu(self)
        self.dl_menu.aboutToShow.connect(self._fill_dl_menu)
        self.dl_opts_btn.setMenu(self.dl_menu)
        self.dl_opts_btn.hide()
        bar.addWidget(self.back_btn)
        bar.addWidget(self.crumb, 1)
        bar.addWidget(self.clear_hist_btn)
        bar.addWidget(self.dl_opts_btn)
        cv.addLayout(bar)

        self.view = QListView()
        self.view.setModel(self.model)
        self.view.setItemDelegate(CatalogDelegate(self.view))
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.view.setLayoutMode(QListView.Batched)
        self.view.setBatchSize(300)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.activated.connect(self._activate)
        cv.addWidget(self.view, 1)

        self.player = PlayerPanel(self.store)
        self.player.fullscreenRequested.connect(self.toggle_fullscreen)
        self.player.nextRequested.connect(self.play_next)
        self.player.prevRequested.connect(self.play_prev)
        self.player.favRequested.connect(self._fav_current)
        self.player.ended.connect(self._on_ended)
        self.player.stopped.connect(self._after_playback)
        self.player.downloadRequested.connect(self._download_current)
        self.player.progress.connect(self._on_progress)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(self.groups)
        self.splitter.addWidget(self.catalog_widget)
        self.splitter.addWidget(self.player)
        self.splitter.setSizes([230, 610, 640])
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 1)
        self.setCentralWidget(self.splitter)

        self.info_lbl = QLabel()
        self.info_lbl.setObjectName("infoLabel")
        self.statusBar().addPermanentWidget(self.info_lbl)

    def _build_shortcuts(self):
        def sc(keys, fn):
            for k in keys:
                s = QShortcut(QKeySequence(k), self)
                s.setContext(Qt.ApplicationShortcut)
                s.activated.connect(fn)

        sc(["Space"], self.player.toggle_pause)
        sc(["F11", "F"], self.toggle_fullscreen)
        sc(["Escape"], self._escape)
        sc(["Ctrl+F"], lambda: (self.search.setFocus(), self.search.selectAll()))
        sc(["Ctrl+Right"], lambda: self.player.seek_by(10_000))
        sc(["Ctrl+Left"], lambda: self.player.seek_by(-10_000))
        sc(["Ctrl+Shift+Right"], lambda: self.player.seek_by(60_000))
        sc(["Ctrl+Shift+Left"], lambda: self.player.seek_by(-60_000))
        sc(["Ctrl+Up"], lambda: self.player.change_volume(5))
        sc(["Ctrl+Down"], lambda: self.player.change_volume(-5))
        sc(["M"], self.player.toggle_mute)
        sc(["Ctrl+PgDown"], self.play_next)
        sc(["Ctrl+PgUp"], self.play_prev)
        sc(["Alt+Left"], self.close_series)
        sc(["Ctrl+D"], self._fav_current)
        sc(["Ctrl+N", "Ctrl+O"], self.add_playlist)
        sc(["F5"], lambda: self.active_pid and self.refresh_playlist(self.active_pid, silent=False))

    def _restore_window(self):
        ui = self.store.data["ui"]
        try:
            if ui.get("geometry"):
                self.restoreGeometry(QByteArray.fromHex(ui["geometry"].encode()))
            if ui.get("splitter"):
                self.splitter.restoreState(QByteArray.fromHex(ui["splitter"].encode()))
        except Exception:
            pass

    # ------------------------------------------------------------------ tarefas
    def run_task(self, fn, on_done, on_error):
        task = FuncTask(fn)
        TaskHandle(task, on_done, on_error, self)      # o handle (filho da janela) mantém a tarefa viva
        QThreadPool.globalInstance().start(task)

    def _busy(self, on: bool, msg: str = None):
        if on:
            self._busy_n += 1
            if self._busy_n == 1:
                QApplication.setOverrideCursor(Qt.BusyCursor)
        elif self._busy_n > 0:
            self._busy_n -= 1
            if self._busy_n == 0:
                QApplication.restoreOverrideCursor()
        if msg:
            self.statusBar().showMessage(msg, 8000)

    # ------------------------------------------------------------------ biblioteca de playlists
    def _startup(self):
        if self.active_pid and self.store.playlist(self.active_pid):
            self.open_playlist(self.active_pid)
        elif self.store.playlists():
            self.open_playlist(self.store.playlists()[0]["id"])
        else:
            self._update_crumb(0)
            self.statusBar().showMessage("Adicione sua primeira playlist: Ctrl+N ou arraste um arquivo .m3u para a janela.")

    def update_playlist_widgets(self):
        p = self.store.playlist(self.active_pid)
        name = p["name"] if p else "Adicionar playlist"
        self.pl_btn.setText(f"  {name[:30]}{'…' if len(name) > 30 else ''}  ▾")
        self.refresh_btn.setEnabled(bool(p))
        self.setWindowTitle(f"{APP_NAME} — {p['name']}" if p else APP_NAME)
        if p:
            state = "  ⟳ atualizando…" if self.active_pid in self._refreshing else ""
            self.info_lbl.setText(f"{p['name']}  ·  {p.get('count', 0)} itens  ·  "
                                  f"atualizada {ago(p.get('updated'))}{state}")
        else:
            self.info_lbl.setText("")

    def _fill_playlist_menu(self):
        m = self.pl_menu
        m.clear()
        pls = self.store.playlists()
        for p in pls:
            a = m.addAction(p["name"])
            a.setCheckable(True)
            a.setChecked(p["id"] == self.active_pid)
            a.triggered.connect(lambda _=False, pid=p["id"]: self.open_playlist(pid))
        if not pls:
            m.addAction("(nenhuma playlist salva)").setEnabled(False)
        m.addSeparator()
        m.addAction("Adicionar playlist…   Ctrl+N").triggered.connect(lambda: self.add_playlist())
        m.addAction("Gerenciar playlists…").triggered.connect(lambda: ManageDialog(self).exec_())

    def add_playlist(self, source: str = "", on_finish=None):
        dlg = AddPlaylistDialog(self, source)
        if not dlg.exec_():
            return
        name, src = dlg.values()
        existing = self.store.find_by_source(src)
        if existing:
            self.open_playlist(existing["id"])
            if on_finish:
                on_finish()
            return
        p = self.store.add_playlist(name, src)
        pid = p["id"]
        self._busy(True, f"Baixando “{p['name']}”…")

        def work():
            text, entries = self.store.download(src)
            self.store.write_cache(pid, text, entries)
            return entries

        def done(entries):
            self._busy(False)
            self.store.mark_updated(pid, len(entries))
            self.active_pid = pid
            self.store.data["active"] = pid
            self.store.save()
            self.apply_entries(pid, entries, preserve=False)
            self.statusBar().showMessage(f"“{p['name']}” adicionada — {len(entries)} itens", 8000)
            if on_finish:
                on_finish()

        def fail(msg):
            self._busy(False)
            self.store.remove_playlist(pid)
            self.update_playlist_widgets()
            QMessageBox.warning(self, APP_NAME, f"Não foi possível carregar a playlist.\n\n{msg}")
            if on_finish:
                on_finish()

        self.run_task(work, done, fail)

    def open_playlist(self, pid, force_refresh: bool = False):
        p = self.store.playlist(pid)
        if not p:
            return
        self.active_pid = pid
        self.store.data["active"] = pid
        self.store.save()
        self.update_playlist_widgets()
        if self.shown_pid == pid and self.entries and not force_refresh:
            return
        self._busy(True, f"Abrindo “{p['name']}”…")

        def done(entries):
            self._busy(False)
            if pid != self.active_pid:
                return
            if entries:
                self.apply_entries(pid, entries, preserve=(self.shown_pid == pid))
                self.statusBar().showMessage(f"“{p['name']}” — {len(entries)} itens", 5000)
            if not entries or force_refresh or self.store.needs_refresh(p):
                self.refresh_playlist(pid, silent=bool(entries))

        self.run_task(lambda: self.store.read_cache(pid), done, lambda msg: done(None))

    def refresh_playlist(self, pid, silent: bool = True, on_finish=None):
        p = self.store.playlist(pid)
        if not p or pid in self._refreshing:
            return
        self._refreshing.add(pid)
        self.update_playlist_widgets()
        if not silent:
            self._busy(True, f"Atualizando “{p['name']}”…")

        def work():
            text, entries = self.store.download(p["source"])
            self.store.write_cache(pid, text, entries)
            return entries

        def done(entries):
            self._refreshing.discard(pid)
            if not silent:
                self._busy(False)
            self.store.mark_updated(pid, len(entries))
            if pid == self.active_pid:
                self.apply_entries(pid, entries, preserve=(self.shown_pid == pid))
            self.update_playlist_widgets()
            self.statusBar().showMessage(f"“{p['name']}” atualizada — {len(entries)} itens", 8000)
            if on_finish:
                on_finish()

        def fail(msg):
            self._refreshing.discard(pid)
            self.update_playlist_widgets()
            if silent:
                self.statusBar().showMessage(
                    f"Não foi possível atualizar “{p['name']}” (usando a cópia salva): {msg}", 12000)
            else:
                self._busy(False)
                QMessageBox.warning(self, APP_NAME, f"Não foi possível atualizar “{p['name']}”.\n\n{msg}")
            if on_finish:
                on_finish()

        self.run_task(work, done, fail)

    def remove_playlist(self, pid):
        was_active = pid == self.active_pid
        self.store.remove_playlist(pid)
        if was_active:
            self.active_pid = self.store.data.get("active")
            if self.active_pid:
                self.open_playlist(self.active_pid)
            else:
                self.apply_entries(None, [], preserve=False)
        self.update_playlist_widgets()

    def apply_entries(self, pid, entries, preserve: bool):
        state = None
        if preserve:
            state = {"group": self.group, "scroll": self.view.verticalScrollBar().value(),
                     "series": self.open_series.key if self.open_series is not None else None}
        self.shown_pid = pid
        self.entries = entries
        self.by_kind = {k: [e for e in entries if e.kind == k] for k in ("live", "movie", "series")}
        self._update_kind_labels()
        self.update_playlist_widgets()
        if state:
            self.set_kind(self.kind, group=state["group"])
            if state["series"]:
                it = next((i for i in self.model.items
                           if i.episodes is not None and i.key == state["series"]), None)
                if it is not None:
                    self.open_series = it
                    self.rebuild_catalog()
            QTimer.singleShot(0, lambda v=state["scroll"]: self.view.verticalScrollBar().setValue(v))
        else:
            kind = self.kind
            if kind in self.by_kind and not self.by_kind[kind]:
                kind = next((k for k in ("live", "movie", "series") if self.by_kind[k]), kind)
            group, self._pending_group = self._pending_group, None
            self.set_kind(kind, group=group)

    # ------------------------------------------------------------------ catálogo
    def _update_kind_labels(self):
        counts = {"live": len(self.by_kind["live"]), "movie": len(self.by_kind["movie"]),
                  "series": len({series_key(e) for e in self.by_kind["series"]}),
                  "fav": len(self.store.data["favorites"]),
                  "downloads": len(self.dl.recs),
                  "continue": sum(1 for r in self.store.continue_list() if r["entry"].get("kind") != "live")}
        for key, label in KINDS:
            txt = f"▶ {label}" if key == "continue" else label
            self.kind_buttons[key].setText(f"{txt} ({counts[key]})")

    def _current_entries(self):
        if self.kind == "fav":
            return [Entry.from_dict(d) for d in self.store.data["favorites"].values()]
        return self.by_kind.get(self.kind, [])

    def set_kind(self, kind: str, group=None):
        self.kind = kind
        self.kind_buttons[kind].setChecked(True)
        self.open_series = None
        self.clear_hist_btn.setVisible(kind == "continue")
        self.dl_opts_btn.setVisible(kind == "downloads")
        self._fill_groups(group)
        self.rebuild_catalog()
        self.store.data["ui"]["kind"] = kind

    def _fill_groups(self, select=None):
        rows = []
        if self.kind == "downloads":
            recs = list(self.dl.recs.values())
            rows.append((None, f"Tudo  ({len(recs)})"))
            rows.append(("active", f"Baixando / na fila  ({sum(1 for r in recs if r['status'] != 'done')})"))
            for k, label in DL_GROUPS[1:]:
                rows.append((k, f"{label}  ({sum(1 for r in recs if r['entry'].get('kind') == k)})"))
        elif self.kind == "continue":
            recs = self.store.continue_list()
            rows.append((None, f"Tudo  ({len(recs)})"))
            for k, label in CONT_GROUPS:
                rows.append((k, f"{label}  ({sum(1 for r in recs if r['entry'].get('kind') == k)})"))
        else:
            counts = {}
            if self.kind == "series":
                seen = {}
                for e in self._current_entries():
                    seen.setdefault(e.group, set()).add(series_key(e))
                counts = {g: len(s) for g, s in seen.items()}
            else:
                for e in self._current_entries():
                    counts[e.group] = counts.get(e.group, 0) + 1
            rows.append((None, f"Todos  ({sum(counts.values())})"))
            rows.extend((g, f"{g}  ({c})") for g, c in counts.items())
        self.groups.blockSignals(True)
        self.groups.clear()
        sel_row = 0
        for n, (key, text) in enumerate(rows):
            it = QListWidgetItem(text)
            it.setData(ROLE_ITEM, key)
            self.groups.addItem(it)
            if select is not None and key == select:
                sel_row = n
        self.groups.setCurrentRow(sel_row)
        self.group = rows[sel_row][0]
        self.groups.blockSignals(False)

    def _group_changed(self, row: int):
        item = self.groups.item(row)
        self.group = item.data(ROLE_ITEM) if item else None
        self.store.data["ui"]["group"] = self.group
        self.open_series = None
        self.rebuild_catalog()

    def rebuild_catalog(self):
        q = norm(self.search.text().strip())
        if self.kind == "continue":
            items = self._continue_items(q)
        elif self.kind == "downloads":
            items = self._download_items(q)
        else:
            entries = self._current_entries()
            if self.group is not None:
                entries = [e for e in entries if e.group == self.group]
            if self.open_series is not None:
                items = [CatalogItem(e.name, e.logo or self.open_series.logo, "live", entry=e, group=e.group)
                         for e in self.open_series.episodes if not q or q in e.search]
            elif self.kind == "series":
                items = self._series_items(entries, q)
            else:
                items = [CatalogItem(e.name, e.logo, e.kind, entry=e, group=e.group)
                         for e in entries if not q or q in e.search]
        self._apply_view()
        self.model.set_items(items)
        self._update_crumb(len(items))

    def _series_items(self, entries, q):
        groups = group_series(entries)
        # nomes que aparecem em mais de um grupo: mostra o grupo no card para diferenciar
        names = {}
        for key, name, group, logo, eps in groups:
            names[key[0]] = names.get(key[0], 0) + 1
        hist = self.store.data["history"]
        items = []
        for key, name, group, logo, eps in groups:
            if q and q not in key[0]:
                continue
            seen = sum(1 for e in eps if (hist.get(e.url) or {}).get("done"))
            got = sum(1 for e in eps if self.dl.status(e.url) == "done")
            sub = (f"{len(eps)} episódio(s)" + (f" · {seen} visto(s)" if seen else "")
                   + (f" · {got} baixado(s)" if got else ""))
            if names[key[0]] > 1 and self.group is None:
                sub = f"{group} · {sub}"
            items.append(CatalogItem(name, logo, "series", episodes=eps, group=group, sub=sub, key=key))
        return items

    def _download_items(self, q):
        order = {"downloading": 0, "queued": 1, "paused": 2, "error": 3, "done": 4}
        recs = sorted(self.dl.recs.values(),
                      key=lambda r: (order.get(r["status"], 5), -r.get("done_ts", r.get("added", 0))))
        items = []
        for r in recs:
            e = Entry.from_dict(r["entry"])
            if self.group == "active" and r["status"] == "done":
                continue
            if self.group in ("movie", "series") and e.kind != self.group:
                continue
            if q and q not in e.search and q not in norm(e.series_name):
                continue
            items.append(CatalogItem(e.name, e.logo, e.kind, entry=e, group=e.group, dl=True))
        return items

    def _continue_items(self, q):
        items = []
        for r in self.store.continue_list():
            e = Entry.from_dict(r["entry"])
            if self.group and e.kind != self.group:
                continue
            if q and q not in e.search and q not in norm(e.series_name):
                continue
            if e.kind == "live":
                sub, resume = f"Canal · assistido {ago(r.get('ts'))}", 0
            elif r.get("next"):
                sub, resume = "▶ Próximo episódio", 0
            else:
                length, t = r.get("len", 0), r.get("t", 0)
                pct = int(t * 100 / length) if length else 0
                sub = f"{pct}% · faltam {fmt_left(length - t)}" if length else f"parou em {fmt_ms(t)}"
                resume = t
            items.append(CatalogItem(e.name, e.logo, e.kind, entry=e, group=e.group, sub=sub, resume=resume))
        return items

    def _update_crumb(self, n: int):
        label = dict(KINDS)[self.kind]
        if self.kind == "continue":
            group_label = dict(CONT_GROUPS).get(self.group, "Tudo")
        elif self.kind == "downloads":
            group_label = dict(DL_GROUPS).get(self.group, "Tudo")
        else:
            group_label = self.group or "Todos"
        parts = [label, group_label]
        if self.open_series is not None:
            parts.append(self.open_series.title)
        self.back_btn.setVisible(self.open_series is not None)
        if not self.entries and self.kind not in ("fav", "continue", "downloads"):
            if self.store.playlists():
                self.crumb.setText("Carregando catálogo…")
            else:
                self.crumb.setText("Adicione uma playlist (Ctrl+N) ou arraste um arquivo .m3u para cá")
        elif self.kind == "continue" and n == 0:
            self.crumb.setText("Nada por aqui ainda — o que você assistir aparece nesta aba para continuar depois.")
        elif self.kind == "downloads" and n == 0 and self.group is None:
            self.crumb.setText("Nenhum download — clique com o botão direito em um filme ou episódio › Baixar.")
        elif self.kind == "downloads":
            done, active = self.dl.counts()
            used = sum(r.get("size", 0) for r in self.dl.recs.values() if r["status"] == "done")
            self.crumb.setText(f"{' › '.join(parts)}   —   {done} baixado(s) · {fmt_bytes(used)}"
                               + (f" · {active} em andamento/fila" if active else ""))
        else:
            self.crumb.setText("  ›  ".join(parts) + f"   —   {n} itens")

    def _view_toggled(self, checked: bool):
        self.store.data["list_mode"] = checked
        self.view_btn.setText("Grade" if checked else "Lista")
        self._apply_view()

    def _apply_view(self):
        v = self.view
        list_mode = self.view_btn.isChecked() or self.open_series is not None
        if self.open_series is not None:
            icon, grid = QSize(96, 60), QSize()
        elif self.kind in ("fav", "continue", "downloads"):
            icon, grid = QSize(128, 110), QSize(160, 178)
        elif self.kind in ("movie", "series"):
            icon, grid = QSize(110, 165), QSize(138, 232)
        else:
            icon, grid = QSize(128, 80), QSize(158, 132)
        if list_mode:
            v.setViewMode(QListView.ListMode)
            v.setGridSize(QSize())
            h = 60 if self.open_series is not None else 48
            v.setIconSize(QSize(max(1, icon.width() * h // icon.height()), h))
            v.setSpacing(1)
        else:
            v.setViewMode(QListView.IconMode)
            v.setIconSize(icon)
            v.setGridSize(grid)
            v.setSpacing(4)
        v.setMovement(QListView.Static)
        v.setResizeMode(QListView.Adjust)
        v.setWordWrap(True)
        v.setUniformItemSizes(True)
        v.setTextElideMode(Qt.ElideRight)

    def close_series(self):
        if self.open_series is not None:
            self.open_series = None
            self.rebuild_catalog()

    def _activate(self, index):
        it = index.data(ROLE_ITEM)
        if it is None:
            return
        if it.episodes is not None:
            self.open_series = it
            self.search.blockSignals(True)
            self.search.clear()
            self.search.blockSignals(False)
            self.rebuild_catalog()
            return
        e = it.entry
        if self.kind == "continue":
            queue, idx = [e], 0
            if e.kind == "series":        # fila com os episódios da série, para seguir ao próximo
                k = series_key(e)
                eps = next((g[4] for g in group_series(x for x in self.by_kind["series"] if series_key(x) == k)), [])
                pos = next((n for n, x in enumerate(eps) if x.url == e.url), None)
                if pos is not None:
                    queue, idx = eps, pos
            self.play_queue, self.play_index = queue, idx
            self._play_entry(queue[idx], start=it.resume)
            return
        self.play_queue = [i.entry for i in self.model.items if i.entry is not None]
        self.play_index = next((n for n, x in enumerate(self.play_queue) if x is e), 0)
        self._play_entry(e)

    def _context_menu(self, pos):
        index = self.view.indexAt(pos)
        if not index.isValid():
            return
        it = index.data(ROLE_ITEM)
        m = QMenu(self)
        if it.entry is None:
            m.addAction("Abrir série").triggered.connect(lambda: self._activate(index))
            missing = [e for e in it.episodes if self.dl.status(e.url) != "done"]
            if missing:
                m.addAction(f"Baixar série inteira ({len(missing)} episódio(s))").triggered.connect(
                    lambda: self._download_many(it.episodes))
                seasons = sorted({e.season for e in it.episodes if e.season})
                if len(seasons) > 1:
                    sm = m.addMenu("Baixar temporada")
                    for sn in seasons:
                        eps = [e for e in it.episodes if e.season == sn]
                        sm.addAction(f"Temporada {sn} ({len(eps)} ep.)").triggered.connect(
                            lambda _=False, eps=eps: self._download_many(eps))
            m.exec_(self.view.viewport().mapToGlobal(pos))
            return
        e = it.entry
        m.addAction("Reproduzir").triggered.connect(lambda: self._activate(index))
        if e.kind != "live" and self.store.resume_point(e.url):
            m.addAction("Assistir do início").triggered.connect(lambda: self._play_from_start(e))
        fav = self.store.is_fav(e.url)
        m.addAction("Remover dos favoritos" if fav else "Adicionar aos favoritos") \
            .triggered.connect(lambda: self._toggle_fav(e))
        m.addSeparator()
        if e.kind != "live":
            prog = self.store.progress(e.url)
            if prog and prog[1]:
                m.addAction("Marcar como não assistido").triggered.connect(lambda: self._forget(e))
            else:
                m.addAction("Marcar como assistido").triggered.connect(lambda: self._mark_watched(e))
        if self.kind == "continue":
            m.addAction("Remover de Continuar assistindo").triggered.connect(lambda: self._forget(e))
        if self.dl.downloadable(e):
            m.addSeparator()
            st = self.dl.status(e.url)
            local = self.dl.local_path(e.url)
            if st is None or (st == "done" and not local):
                m.addAction("⬇  Baixar para assistir offline").triggered.connect(lambda: self._download(e))
            elif st == "downloading" or st == "queued":
                m.addAction("Pausar download").triggered.connect(lambda: self.dl.pause(e.url))
            elif st in ("paused", "error"):
                m.addAction("Retomar download" if st == "paused" else "Tentar novamente") \
                    .triggered.connect(lambda: self.dl.resume(e.url))
            if local:
                m.addAction("Mostrar arquivo na pasta").triggered.connect(lambda: self._show_in_folder(local))
            if st is not None:
                label = "Excluir arquivo baixado" if st == "done" else "Cancelar download"
                m.addAction(label).triggered.connect(lambda: self._delete_download(e))
            m.addSeparator()
        m.addAction("Copiar URL do stream").triggered.connect(
            lambda: QGuiApplication.clipboard().setText(e.url))
        m.exec_(self.view.viewport().mapToGlobal(pos))

    def _history_changed(self):
        self.store.save()
        self._update_kind_labels()
        if self.kind == "continue":
            self._fill_groups(self.group)
            self.rebuild_catalog()
        else:
            self.model.refresh()

    def _forget(self, e: Entry):
        self.store.remove_history(e.url)
        self._history_changed()

    def _mark_watched(self, e: Entry):
        self.store.mark_done(e)
        self._history_changed()

    def _clear_history(self):
        r = QMessageBox.question(self, "Limpar histórico",
                                 "Apagar todo o histórico de reprodução e as posições salvas?")
        if r == QMessageBox.Yes:
            self.store.clear_history()
            self._history_changed()

    # ------------------------------------------------------------------ reprodução
    def _ask_resume(self, t: int):
        box = QMessageBox(self)
        box.setWindowTitle("Continuar assistindo")
        box.setText(f"Você parou em {fmt_ms(t)}.")
        cont = box.addButton(f"Continuar de {fmt_ms(t)}", QMessageBox.AcceptRole)
        start = box.addButton("Começar do início", QMessageBox.ActionRole)
        cancel = box.addButton("Cancelar", QMessageBox.RejectRole)
        box.setDefaultButton(cont)
        box.setEscapeButton(cancel)
        box.exec_()
        clicked = box.clickedButton()
        if clicked is cont:
            return t
        if clicked is start:
            return 0
        return None

    def _play_from_start(self, e: Entry):
        self.play_queue, self.play_index = [e], 0
        self._play_entry(e, start=0)

    def _play_entry(self, e: Entry, start=None, ask: bool = True):
        if start is None:
            start = self.store.resume_point(e.url) if e.kind != "live" else 0
            if start and ask:
                start = self._ask_resume(start)
                if start is None:
                    return
        local = self.dl.local_path(e.url)          # baixado? usa o arquivo, mesmo com internet
        if not self.player.play(e, start, local_path=local):
            return
        self.player.set_download_state(self.dl.status(e.url) or "none" if self.dl.downloadable(e) else None)
        rec = self.store.get_history(e.url) or {}
        self.store.record(e, start, rec.get("len", 0), next_up=bool(rec.get("next")) and start == 0)
        self.store.save()
        self._update_kind_labels()
        self.player.set_fav(self.store.is_fav(e.url))
        self.statusBar().showMessage(f"Reproduzindo: {e.name}  ·  {e.group}"
                                     + ("  ·  arquivo baixado (offline)" if local else "")
                                     + (f"  ·  retomando de {fmt_ms(start)}" if start else ""), 8000)
        for n, it in enumerate(self.model.items):
            if it.entry is e or (it.entry is not None and it.entry.url == e.url):
                self.view.setCurrentIndex(self.model.index(n))
                break

    def play_next(self):
        if 0 <= self.play_index + 1 < len(self.play_queue):
            self.play_index += 1
            self._play_entry(self.play_queue[self.play_index])

    def play_prev(self):
        if self.play_index - 1 >= 0 and self.play_queue:
            self.play_index -= 1
            self._play_entry(self.play_queue[self.play_index])

    def _on_ended(self):
        cur = self.player.current
        if cur is None:
            return
        nxt = None
        if cur.kind != "live":
            self.store.mark_done(cur)
            if cur.kind == "series" and self.play_index + 1 < len(self.play_queue):
                nxt = self.play_queue[self.play_index + 1]
                self.store.record(nxt, 0, 0, next_up=True)
        self.store.save()
        if nxt is not None:
            self.play_index += 1
            self._play_entry(nxt, ask=False)       # próximo episódio automaticamente
        self._after_playback()

    def _after_playback(self):
        self.store.save()
        self._update_kind_labels()
        if self.kind == "continue":
            scroll = self.view.verticalScrollBar().value()
            self._fill_groups(self.group)
            self.rebuild_catalog()
            QTimer.singleShot(0, lambda: self.view.verticalScrollBar().setValue(scroll))
        else:
            self.model.refresh()

    def _on_progress(self, url: str, t: int, length: int):
        cur = self.player.current
        if cur is None or cur.url != url:
            return
        self.store.record(cur, t, length)
        now = time.monotonic()
        if now - self._last_save > 10:
            self.store.save()
            self._last_save = now

    # ------------------------------------------------------------------ downloads
    def _download(self, e: Entry):
        if self.dl.enqueue(e):
            self.statusBar().showMessage(f"Download adicionado: {e.name}  (acompanhe na aba Baixados)", 6000)

    def _download_many(self, eps):
        n = sum(1 for e in sorted(eps, key=ep_key) if self.dl.enqueue(e))
        self.statusBar().showMessage(f"{n} episódio(s) adicionados aos downloads", 6000)

    def _download_current(self):
        e = self.player.current
        if e is None:
            return
        if not self.dl.downloadable(e):
            self.statusBar().showMessage("Canais ao vivo não podem ser baixados.", 5000)
            return
        self._download(e)

    def _delete_download(self, e: Entry):
        st = self.dl.status(e.url)
        if st == "done":
            r = QMessageBox.question(self, "Excluir download",
                                     f"Excluir o arquivo baixado de “{e.name}”?\n"
                                     "O conteúdo continua disponível para assistir online.")
            if r != QMessageBox.Yes:
                return
            cur = self.player.current
            if cur is not None and cur.url == e.url and self.player.offline_badge.isVisible():
                self.player.stop()                   # libera o arquivo antes de apagar
        self.dl.delete(e.url)

    def _show_in_folder(self, path: str):
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))

    def _fill_dl_menu(self):
        m = self.dl_menu
        m.clear()
        folder = self.store.data["download_dir"]
        m.addAction("Abrir pasta de downloads").triggered.connect(
            lambda: (os.makedirs(folder, exist_ok=True), QDesktopServices.openUrl(QUrl.fromLocalFile(folder))))
        m.addAction("Alterar pasta de downloads…").triggered.connect(self._change_dl_dir)
        info = m.addAction(f"Pasta atual: {folder}")
        info.setEnabled(False)
        m.addSeparator()
        sim = m.addMenu("Downloads simultâneos")
        cur = int(self.store.data.get("max_downloads", 2))
        for n in (1, 2, 3, 4):
            a = sim.addAction(str(n))
            a.setCheckable(True)
            a.setChecked(n == cur)
            a.triggered.connect(lambda _=False, n=n: self._set_max_downloads(n))
        m.addSeparator()
        m.addAction("Pausar todos").triggered.connect(self.dl.pause_all)
        m.addAction("Retomar todos").triggered.connect(self.dl.resume_all)

    def _set_max_downloads(self, n: int):
        self.store.data["max_downloads"] = n
        self.store.save()
        self.dl._pump()

    def _change_dl_dir(self):
        folder = QFileDialog.getExistingDirectory(self, "Pasta de downloads", self.store.data["download_dir"])
        if folder:
            self.store.data["download_dir"] = folder
            self.store.save()
            self.statusBar().showMessage(f"Novos downloads serão salvos em {folder}", 8000)

    def _on_dl_changed(self, url: str):
        self._update_kind_labels()
        cur = self.player.current
        if cur is not None and (not url or cur.url == url):
            self.player.set_download_state(self.dl.status(cur.url) or "none" if self.dl.downloadable(cur) else None)
        if self.kind == "downloads":
            scroll = self.view.verticalScrollBar().value()
            self._fill_groups(self.group)
            self.rebuild_catalog()
            QTimer.singleShot(0, lambda: self.view.verticalScrollBar().setValue(scroll))
        else:
            self.model.refresh()
            if self.kind == "series" and self.open_series is None:
                self.dl_timer.start()

    def _on_dl_progress(self, url: str):
        if self.kind == "downloads" and not self.dl_timer.isActive():
            self.dl_timer.start()

    # ------------------------------------------------------------------ favoritos
    def _toggle_fav(self, e: Entry):
        state = self.store.toggle_fav(e)
        if self.player.current is not None and self.player.current.url == e.url:
            self.player.set_fav(state)
        self._update_kind_labels()
        if self.kind == "fav":
            self._fill_groups(self.group)
            self.rebuild_catalog()
        else:
            self.model.refresh()

    def _fav_current(self):
        if self.player.current is not None:
            self._toggle_fav(self.player.current)

    # ------------------------------------------------------------------ janela
    def toggle_fullscreen(self):
        chrome = (self.groups, self.catalog_widget, self.toolbar, self.statusBar())
        if self.isFullScreen():
            for w in chrome:
                w.show()
            self.player.set_fullscreen(False)
            self.showMaximized() if self._was_max else self.showNormal()
        else:
            self._was_max = self.isMaximized()
            for w in chrome:
                w.hide()
            self.player.set_fullscreen(True)
            self.showFullScreen()

    def _escape(self):
        if self.isFullScreen():
            self.toggle_fullscreen()
        elif self.open_series is not None:
            self.close_series()

    def dragEnterEvent(self, ev):
        md = ev.mimeData()
        if md.hasUrls() or md.hasText():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        md = ev.mimeData()
        if md.hasUrls() and md.urls():
            u = md.urls()[0]
            self.add_playlist(u.toLocalFile() if u.isLocalFile() else u.toString())
        elif md.hasText():
            self.add_playlist(md.text().strip())

    def closeEvent(self, ev):
        if self.dl.jobs:
            r = QMessageBox.question(self, APP_NAME,
                                     f"Há {len(self.dl.jobs)} download(s) em andamento.\n"
                                     "Eles continuam de onde pararam na próxima vez que você abrir o programa.\n\n"
                                     "Fechar agora?")
            if r != QMessageBox.Yes:
                ev.ignore()
                return
        self.dl.shutdown()
        cur = self.player.current
        if cur is not None and cur.kind != "live" and self.player.mp is not None:
            t, length = self.player.mp.get_time(), self.player.mp.get_length()
            if t > 0 and length > 0:
                self.store.record(cur, t, length)
        self.player.stop()
        self.player.awake.release()          # devolve a economia de energia ao Windows
        if self.isFullScreen():
            self.toggle_fullscreen()
        ui = self.store.data["ui"]
        ui["geometry"] = self.saveGeometry().toHex().data().decode()
        ui["splitter"] = self.splitter.saveState().toHex().data().decode()
        ui["kind"], ui["group"] = self.kind, self.group
        self.store.save()
        super().closeEvent(ev)


# --------------------------------------------------------------------------- tema
def apply_dark_theme(app: QApplication):
    app.setStyle("Fusion")
    p = QPalette()
    R = QPalette
    p.setColor(R.Window, QColor(30, 31, 34))
    p.setColor(R.WindowText, QColor(228, 230, 234))
    p.setColor(R.Base, QColor(24, 25, 28))
    p.setColor(R.AlternateBase, QColor(36, 37, 41))
    p.setColor(R.Text, QColor(228, 230, 234))
    p.setColor(R.Button, QColor(43, 45, 49))
    p.setColor(R.ButtonText, QColor(228, 230, 234))
    p.setColor(R.Highlight, QColor(ACCENT))
    p.setColor(R.HighlightedText, QColor(255, 255, 255))
    p.setColor(R.ToolTipBase, QColor(43, 45, 49))
    p.setColor(R.ToolTipText, QColor(228, 230, 234))
    p.setColor(R.PlaceholderText, QColor(130, 133, 140))
    p.setColor(QPalette.Disabled, R.Text, QColor(110, 112, 118))
    p.setColor(QPalette.Disabled, R.ButtonText, QColor(110, 112, 118))
    app.setPalette(p)
    app.setStyleSheet(f"""
        QToolBar {{ border: 0; padding: 5px 8px; spacing: 6px; background: #17181b; }}
        QToolButton {{ padding: 4px 8px; border-radius: 6px; }}
        QToolButton:hover {{ background: #2f3136; }}
        QToolButton#playlistButton {{ font-weight: 600; background: #26282d; padding: 5px 10px; }}
        QToolButton#playlistButton::menu-indicator {{ image: none; width: 0; }}
        QToolButton#kindButton, QToolButton#continueButton {{ padding: 6px 14px; font-weight: 600; }}
        QToolButton#kindButton:checked {{ background: {ACCENT}; color: white; }}
        QToolButton#continueButton {{ color: #ff8a8f; }}
        QToolButton#continueButton:checked {{ background: #e5484d; color: white; }}

        QLineEdit {{ padding: 5px 8px; border-radius: 6px; border: 1px solid #3a3c42; background: #25262a; }}
        QLineEdit:focus {{ border-color: {ACCENT}; }}
        QListWidget#groups {{ border: 0; background: #1a1b1e; padding: 4px; }}
        QListWidget#groups::item {{ padding: 7px 8px; border-radius: 5px; }}
        QListWidget#groups::item:selected {{ background: #2c3d69; color: white; }}
        QListWidget#manageList::item {{ padding: 6px; }}
        QListView {{ border: 0; background: #1e1f22; }}
        QListView::item {{ border-radius: 6px; padding: 2px; }}
        QListView::item:selected {{ background: #2c3d69; }}
        QListView::item:hover {{ background: #2a2c31; }}
        QLabel#crumb {{ color: #a3a6ad; padding: 2px 4px; }}
        QLabel#hint {{ color: #a3a6ad; }}
        QLabel#infoLabel {{ color: #a3a6ad; padding-right: 6px; }}
        QStatusBar {{ color: #a3a6ad; }}
        QSplitter::handle {{ background: #26272b; width: 3px; }}
        QPushButton {{ padding: 6px 14px; border-radius: 6px; background: #2c2e33; border: 1px solid #3a3d44; }}
        QPushButton:hover {{ background: #363940; }}

        /* ---- player ---- */
        QLabel#playerTitle {{ font-size: 16px; font-weight: 700; padding: 2px 4px; color: #ffffff; }}
        QFrame#controlBar {{ background: #121316; border: 1px solid #25272c; border-radius: 12px; }}
        QToolButton#media {{
            background: #2b2e35; border: 1px solid #454a55; border-radius: 19px;
            min-width: 38px; max-width: 38px; min-height: 38px; max-height: 38px; padding: 0;
        }}
        QToolButton#media:hover {{ background: {ACCENT}; border-color: #7da0ff; }}
        QToolButton#media:pressed {{ background: #2f5fd6; }}
        QToolButton#media:disabled {{ background: #1c1d21; border-color: #2a2c31; }}
        QToolButton#media::menu-indicator {{ image: none; width: 0; }}
        QToolButton#mediaPrimary {{
            background: {ACCENT}; border: 2px solid #8fb0ff; border-radius: 26px;
            min-width: 52px; max-width: 52px; min-height: 52px; max-height: 52px; padding: 0;
        }}
        QToolButton#mediaPrimary:hover {{ background: #6b94ff; }}
        QToolButton#mediaPrimary:pressed {{ background: #2f5fd6; }}
        QToolButton#mediaPrimary:disabled {{ background: #2a2c31; border-color: #3a3d44; }}
        QLabel#timeLabel {{ color: #ffffff; font-weight: 600; font-size: 13px; min-width: 56px; }}
        QLabel#offlineBadge {{ background: #1f8f55; color: white; font-weight: 800; border-radius: 5px; padding: 3px 9px; }}
        QToolButton#plainMenuButton::menu-indicator {{ image: none; width: 0; }}
        QLabel#liveBadge {{ background: #e5484d; color: white; font-weight: 800; border-radius: 5px; padding: 3px 9px; }}
        QLabel#stateLabel {{ color: #ffc15e; font-weight: 700; }}
        QLabel#volLabel {{ color: #e4e6ea; font-weight: 600; min-width: 40px; }}
        QSlider::groove:horizontal {{ height: 6px; background: #3b3f47; border-radius: 3px; }}
        QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 3px; }}
        QSlider::handle:horizontal {{ width: 16px; margin: -5px 0; border-radius: 8px; background: #ffffff; }}
        QSlider::handle:horizontal:hover {{ background: #dfe7ff; }}
        QSlider#seek::sub-page:horizontal {{ background: #ff4d57; }}
        QSlider::sub-page:horizontal:disabled {{ background: #3b3f47; }}
        QSlider::handle:horizontal:disabled {{ background: #5a5e66; }}
    """)


# --------------------------------------------------------------------------- erros e autoteste
def install_error_handler():
    """No PyQt5, um erro não tratado dentro de um evento ENCERRA o programa (qFatal).
    Este tratador grava o erro em %APPDATA%\\IPTVPlayer\\erro.log e mantém o app aberto."""
    import traceback

    def hook(etype, value, tb):
        text = "".join(traceback.format_exception(etype, value, tb))
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            with open(os.path.join(APP_DIR, "erro.log"), "a", encoding="utf-8") as fh:
                fh.write(time.strftime("\n=== %Y-%m-%d %H:%M:%S ===\n") + text)
        except OSError:
            pass
        if sys.stderr:
            try:
                sys.stderr.write(text)
            except Exception:
                pass
        _SELFTEST_ERRORS.append(text)

    sys.excepthook = hook


_SELFTEST_ERRORS = []
SELFTEST_M3U = """#EXTM3U
#EXTINF:-1 tvg-logo="http://127.0.0.1:9/logo.png" group-title="Canais | Teste",Canal Teste
http://127.0.0.1:9/live/u/p/1.ts
#EXTINF:-1 group-title="Filmes | Teste",Filme A (2020)
http://127.0.0.1:9/movie/u/p/1.mp4
#EXTINF:-1 group-title="Filmes | Teste",Filme B (2021)
http://127.0.0.1:9/movie/u/p/2.mkv
#EXTINF:-1 group-title="Séries | Dublado",Dark S01E01
http://127.0.0.1:9/series/u/p/11.mkv
#EXTINF:-1 group-title="Séries | Dublado",Dark S01E02
http://127.0.0.1:9/series/u/p/12.mkv
#EXTINF:-1 group-title="Séries | Legendado",Dark S01E01
http://127.0.0.1:9/series/u/p/21.mkv
"""


def run_selftest(app, log_path):
    """Autoteste usado pelo build: abre a janela, carrega uma playlist de teste, percorre todas as
    abas, a tela de série, favoritos, histórico, downloads e confere o VLC embutido.
    Grava o resultado em log_path e encerra com código 0 (sucesso) ou 1 (falha)."""
    import struct
    import traceback
    from PyQt5.QtCore import PYQT_VERSION_STR, QT_VERSION_STR

    lines, fails = [], []
    state = {"done": False}

    def log(msg):
        lines.append(msg)

    def check(cond, msg):
        log(("OK     " if cond else "FALHA  ") + msg)
        if not cond:
            fails.append(msg)

    def finish():
        if state["done"]:
            return
        state["done"] = True
        for err in _SELFTEST_ERRORS:
            check(False, "erro durante a execução:\n" + err)
        log("RESULTADO: " + ("SUCESSO" if not fails else f"{len(fails)} FALHA(S)"))
        text = "\n".join(lines) + "\n"
        try:
            with open(log_path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            pass
        if sys.stdout:
            try:
                print(text, flush=True)
            except Exception:
                pass
        os._exit(0 if not fails else 1)

    def guarded(fn):
        def run():
            try:
                fn()
            except Exception:
                check(False, "exceção:\n" + traceback.format_exc())
                finish()
        return run

    def wait(cond, then, label, timeout_ms=20000, t0=None):
        t0 = t0 or time.monotonic()
        try:
            ok = cond()
        except Exception:
            ok = False
        if ok:
            QTimer.singleShot(50, guarded(then))
        elif (time.monotonic() - t0) * 1000 > timeout_ms:
            check(False, f"tempo esgotado: {label}")
            finish()
        else:
            QTimer.singleShot(100, lambda: wait(cond, then, label, timeout_ms, t0))

    QTimer.singleShot(120_000, lambda: (check(False, "autoteste passou de 2 minutos"), finish()))

    log(f"IPTV Player {APP_VERSION} · Python {sys.version.split()[0]} ({struct.calcsize('P') * 8} bits) · "
        f"Qt {QT_VERSION_STR} · PyQt {PYQT_VERSION_STR} · {sys.platform}")
    log(vlcloader.describe())
    try:
        log("TLS no Qt: " + ("sim" if QSslSocket.supportsSsl() else "não (logos https via Python)"))
    except Exception:
        pass
    check(vlc is not None, "python-vlc carregado" + (f" — {VLC_ERROR}" if VLC_ERROR else ""))
    if vlc is not None:
        try:
            log("libVLC " + vlc.libvlc_get_version().decode("utf-8", "replace"))
        except Exception:
            pass

    win = MainWindow()
    win.show()
    ctx = {}

    def stage1():
        p = win.player
        check(p.instance is not None and p.mp is not None, "player VLC criado")
        if p.instance is not None:
            outs = p.instance.audio_output_list_get()
            check(bool(outs), "plugins do VLC encontrados (saídas de áudio)")
            try:
                if outs:
                    vlc.libvlc_audio_output_list_release(outs)
            except Exception:
                pass
            check(p.instance.media_new_path(log_path) is not None, "mídia VLC criada")
        if sys.platform.startswith("win"):
            check(p.awake.supported, "bloqueio de tela disponível (SetThreadExecutionState)")
            p.awake.set(True)
            check(p.awake.active, "bloqueio de tela ligado")
            p.awake.set(False)
            check(not p.awake.active, "bloqueio de tela desligado")
        check(not icons.icon("play").isNull() and not icons.icon("fwd10").isNull(), "ícones desenhados")
        check(not win.logos.icon("", "Teste", "movie").isNull(), "capa provisória (iniciais)")

        m3u = os.path.join(APP_DIR, "teste.m3u")
        os.makedirs(APP_DIR, exist_ok=True)
        with open(m3u, "w", encoding="utf-8") as fh:
            fh.write(SELFTEST_M3U)
        pl = win.store.add_playlist("Teste", m3u)
        text, entries = win.store.download(m3u)
        win.store.write_cache(pl["id"], text, entries)
        win.store.mark_updated(pl["id"], len(entries))
        check(win.store.read_cache(pl["id"]) is not None, "cache da playlist gravado e lido")
        ctx["pid"] = pl["id"]
        win.open_playlist(pl["id"])
        wait(lambda: len(win.entries) == 6, stage2, "carregar o catálogo")

    def stage2():
        check(len(win.by_kind["live"]) == 1 and len(win.by_kind["movie"]) == 2 and len(win.by_kind["series"]) == 3,
              "classificação canal/filme/série")
        rows = {}
        for k in ("live", "movie", "series", "downloads", "fav", "continue"):
            win.set_kind(k)
            QApplication.processEvents()
            rows[k] = win.model.rowCount()
        log(f"       itens por aba: {rows}")
        check(rows["live"] == 1 and rows["movie"] == 2, "abas Ao vivo e Filmes")
        check(rows["series"] == 2, "séries homônimas (dublado/legendado) separadas")

        win.set_kind("series")
        win._activate(win.model.index(0))
        QApplication.processEvents()
        check(win.open_series is not None and win.model.rowCount() == 2, "tela da série com episódios")
        win.close_series()
        check(win.open_series is None, "voltar da série")

        win.view_btn.setChecked(True)
        QApplication.processEvents()
        win.view_btn.setChecked(False)
        win.toggle_fullscreen()
        QApplication.processEvents()
        win.toggle_fullscreen()
        QApplication.processEvents()
        check(not win.isFullScreen(), "lista/grade e tela cheia")

        movie = win.by_kind["movie"][0]
        win._toggle_fav(movie)
        win.store.record(movie, 20 * 60_000, 90 * 60_000)
        win.set_kind("fav")
        QApplication.processEvents()
        check(win.model.rowCount() == 1, "favoritos")
        win.set_kind("continue")
        QApplication.processEvents()
        check(win.model.rowCount() >= 1, "continuar assistindo")
        win.set_kind("movie")
        win.view.viewport().repaint()          # desenha capas, barra de progresso e selos
        QApplication.processEvents()

        d = ManageDialog(win)
        d.reload()
        d.deleteLater()
        a = AddPlaylistDialog(win, "http://exemplo/lista.m3u")
        check(a.values()[1] == "http://exemplo/lista.m3u", "diálogos de playlist")
        a.deleteLater()

        ep = win.by_kind["series"][0]
        ctx["dl_url"] = ep.url
        check(win.dl.enqueue(ep), "download enfileirado")
        wait(lambda: win.dl.status(ep.url) in ("downloading", "error"), stage3, "download iniciar", 15000)

    def stage3():
        url = ctx["dl_url"]
        win.set_kind("downloads")
        QApplication.processEvents()
        check(win.model.rowCount() == 1, "aba Baixados")
        win.dl.delete(url)                      # cancela a thread de download
        wait(lambda: not win.dl.jobs and url not in win.dl.recs, stage4, "cancelar download", 15000)

    def stage4():
        check(True, "download cancelado e removido")
        win.store.save()
        check(os.path.isfile(os.path.join(APP_DIR, "config.json")), "configuração salva")
        QTimer.singleShot(500, finish)          # dá tempo de pintar e de erros aparecerem

    QTimer.singleShot(300, guarded(stage1))


def main():
    install_error_handler()
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("IPTVPlayer.App")
        except Exception:
            pass
    # Qt 5: ajuste para telas com escala 125%/150% (no Qt 6 isso é automático)
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    try:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception:
        pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    apply_dark_theme(app)
    if SELFTEST:
        idx = sys.argv.index("--selftest")
        log_path = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else os.path.join(os.getcwd(), "selftest.log")
        run_selftest(app, os.path.abspath(log_path))
        sys.exit(app.exec_())
    win = MainWindow()
    win.show()
    if len(sys.argv) > 1:                       # abrir com: "IPTV Player.exe" lista.m3u
        QTimer.singleShot(300, lambda: win.add_playlist(sys.argv[1]))
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
