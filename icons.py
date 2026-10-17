"""Ícones vetoriais desenhados em código (nítidos em qualquer escala e na cor que quisermos)."""
import math

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap

_cache = {}


def icon(name: str, color: str = "#ffffff", size: int = 64) -> QIcon:
    key = (name, color, size)
    if key not in _cache:
        _cache[key] = QIcon(_draw(name, QColor(color), size))
    return _cache[key]


def _draw(name: str, c: QColor, s: int) -> QPixmap:
    pm = QPixmap(s, s)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)

    def fill():
        p.setPen(Qt.NoPen)
        p.setBrush(c)

    def stroke(w):
        pen = QPen(c, w * s)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)

    def pt(x, y):
        return QPointF(x * s, y * s)

    def poly(*pts, close=True):
        path = QPainterPath(pt(*pts[0]))
        for xy in pts[1:]:
            path.lineTo(pt(*xy))
        if close:
            path.closeSubpath()
        p.drawPath(path)

    def rrect(x, y, w, h, r):
        p.drawRoundedRect(QRectF(x * s, y * s, w * s, h * s), r * s, r * s)

    def speaker():
        fill()
        poly((.12, .38), (.28, .38), (.48, .2), (.48, .8), (.28, .62), (.12, .62))

    if name == "play":
        fill()
        poly((.34, .2), (.34, .8), (.82, .5))
    elif name == "pause":
        fill()
        rrect(.26, .2, .16, .6, .04)
        rrect(.58, .2, .16, .6, .04)
    elif name == "stop":
        fill()
        rrect(.26, .26, .48, .48, .07)
    elif name == "prev":
        fill()
        rrect(.2, .24, .1, .52, .03)
        poly((.8, .24), (.8, .76), (.36, .5))
    elif name == "next":
        fill()
        rrect(.7, .24, .1, .52, .03)
        poly((.2, .24), (.2, .76), (.64, .5))
    elif name in ("fwd10", "back10"):
        if name == "back10":                     # espelha horizontalmente só a seta
            p.translate(s, 0)
            p.scale(-1, 1)
        stroke(.075)
        r = .31
        p.drawArc(QRectF((.5 - r) * s, (.5 - r) * s, 2 * r * s, 2 * r * s), 60 * 16, 300 * 16)
        th = math.radians(60)
        px, py = .5 + r * math.cos(th), .5 - r * math.sin(th)
        dx, dy = math.sin(th), math.cos(th)      # tangente no sentido horário (tela)
        nx, ny = -dy, dx
        a = .16
        fill()
        poly((px + dx * a * .9, py + dy * a * .9),
             (px - dx * a * .3 + nx * a * .7, py - dy * a * .3 + ny * a * .7),
             (px - dx * a * .3 - nx * a * .7, py - dy * a * .3 - ny * a * .7))
        p.resetTransform()
        f = QFont()
        f.setPixelSize(int(s * .27))
        f.setBold(True)
        p.setFont(f)
        p.setPen(c)
        p.drawText(QRectF(0, s * .03, s, s), Qt.AlignCenter, "10")
    elif name == "volume":
        speaker()
        stroke(.065)
        p.drawArc(QRectF(.42 * s, .36 * s, .24 * s, .28 * s), -50 * 16, 100 * 16)
        p.drawArc(QRectF(.36 * s, .22 * s, .48 * s, .56 * s), -50 * 16, 100 * 16)
    elif name == "mute":
        speaker()
        stroke(.075)
        p.drawLine(pt(.62, .38), pt(.86, .62))
        p.drawLine(pt(.86, .38), pt(.62, .62))
    elif name == "fullscreen":
        stroke(.08)
        for a, b, d in (((.2, .4), (.2, .2), (.4, .2)), ((.6, .2), (.8, .2), (.8, .4)),
                        ((.8, .6), (.8, .8), (.6, .8)), ((.4, .8), (.2, .8), (.2, .6))):
            poly(a, b, d, close=False)
    elif name == "exit_fullscreen":
        stroke(.08)
        for a, b, d in (((.2, .38), (.38, .38), (.38, .2)), ((.62, .2), (.62, .38), (.8, .38)),
                        ((.8, .62), (.62, .62), (.62, .8)), ((.38, .8), (.38, .62), (.2, .62))):
            poly(a, b, d, close=False)
    elif name == "gear":
        fill()
        p.translate(s / 2, s / 2)
        for _ in range(8):
            p.drawRoundedRect(QRectF(-.075 * s, -.42 * s, .15 * s, .2 * s), .03 * s, .03 * s)
            p.rotate(45)
        p.drawEllipse(QPointF(0, 0), .28 * s, .28 * s)
        p.setCompositionMode(QPainter.CompositionMode_Clear)
        p.drawEllipse(QPointF(0, 0), .11 * s, .11 * s)
    elif name in ("star", "star_filled"):
        pts = []
        for i in range(10):
            ang = math.radians(-90 + i * 36)
            rad = .42 if i % 2 == 0 else .18
            pts.append((.5 + rad * math.cos(ang), .53 + rad * math.sin(ang)))
        if name == "star_filled":
            fill()
        else:
            stroke(.06)
        poly(*pts)
    elif name == "refresh":
        stroke(.08)
        r = .28
        p.drawArc(QRectF((.5 - r) * s, (.5 - r) * s, 2 * r * s, 2 * r * s), 30 * 16, 290 * 16)
        fill()
        poly((.78, .16), (.8, .44), (.54, .36))
    elif name == "list":
        fill()
        for y in (.22, .45, .68):
            rrect(.18, y, .64, .11, .04)
    elif name == "download":
        stroke(.085)
        p.drawLine(pt(.5, .16), pt(.5, .6))
        poly((.3, .42), (.5, .62), (.7, .42), close=False)
        poly((.18, .64), (.18, .82), (.82, .82), (.82, .64), close=False)
    elif name == "check":
        stroke(.11)
        poly((.22, .52), (.42, .72), (.8, .3), close=False)
    elif name == "folder":
        fill()
        path = QPainterPath(pt(.12, .26))
        path.lineTo(pt(.42, .26))
        path.lineTo(pt(.5, .34))
        path.lineTo(pt(.88, .34))
        path.lineTo(pt(.88, .8))
        path.lineTo(pt(.12, .8))
        path.closeSubpath()
        p.drawPath(path)
    p.end()
    return pm
