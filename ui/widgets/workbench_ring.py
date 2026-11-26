"""工作台分布圆环与图例。"""

from math import atan2, degrees, hypot

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFontMetrics, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QWidget

from ui.workbench_widgets import label

PALETTE = (
    ("#006CCE", "#38BDF8"),
    ("#6444EB", "#A281F4"),
    ("#C74925", "#FFAA55"),
    ("#00856F", "#2BCB9E"),
    ("#BA367C", "#F68BC7"),
    ("#576777", "#93ADC0"),
)


class _Ring(QWidget):
    def __init__(self, rows, unit):
        super().__init__()
        self.rows = rows
        self.unit = unit
        self.total = sum(count for _, count in rows)
        self.active = -1
        self.segments = []
        start = 0.0
        for _name, count in self.rows:
            span = 360 * count / self.total
            self.segments.append((start, span))
            start += span
        self.setFixedSize(144, 144)
        self.setMouseTracking(True)
        self.setAccessibleName(f"合计 {self.total} {unit}")
        self.setAccessibleDescription("，".join(f"{name} {count} {unit}" for name, count in rows))

    def highlight(self, index):
        if index != self.active:
            self.active = index
            self.update()

    def mouseMoveEvent(self, event):
        point = event.position()
        x, y = point.x() - self.width() / 2, point.y() - self.height() / 2
        radius = hypot(x, y)
        if 47 <= radius <= 72:
            angle = degrees(atan2(x, -y)) % 360
            self.highlight(next((i for i, (start, span) in enumerate(self.segments)
                                 if start <= angle < start + span), -1))
        else:
            self.highlight(-1)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.highlight(-1)
        super().leaveEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = QRectF(self.rect()).adjusted(13, 13, -13, -13)
        painter.setPen(QPen(QColor("#E4ECF5"), 16))
        painter.drawEllipse(bounds)
        for index, (start, span) in enumerate(self.segments):
            selected = index == self.active
            a, b = (QColor(color) for color in PALETTE[index % len(PALETTE)])
            gradient = QLinearGradient(bounds.topLeft(), bounds.bottomRight())
            if self.active >= 0 and not selected:
                a.setAlpha(130)
                b.setAlpha(130)
            gradient.setColorAt(0, a)
            gradient.setColorAt(1, b)
            pen = QPen(QBrush(gradient), 23 if selected else 17)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            painter.setPen(pen)
            if len(self.segments) == 1:
                painter.drawEllipse(bounds)
            else:
                gap = min(2.1, span * .15)
                painter.drawArc(bounds, round((90 - start - gap / 2) * 16),
                                -max(1, round((span - gap) * 16)))
        name, number = self.rows[self.active] if self.active >= 0 else ("合计", self.total)
        value = f"{number:,}"
        secondary = f"{name} · {number / self.total:.0%}" if self.active >= 0 else f"{name}（{self.unit}）"
        font = self.font()
        font.setPixelSize(23 if len(value) < 6 else 19)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#172C45"))
        painter.drawText(QRectF(15, 45, 114, 31), Qt.AlignmentFlag.AlignCenter, value)
        font.setPixelSize(11)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor("#48617C"))
        secondary = QFontMetrics(font).elidedText(secondary, Qt.TextElideMode.ElideRight, 101)
        painter.drawText(QRectF(21, 77, 102, 19), Qt.AlignmentFlag.AlignCenter, secondary)


class _LegendItem(QFrame):
    def __init__(self, ring, index, name, count, unit):
        super().__init__()
        self._ring = ring
        self._index = index
        self.setObjectName("WorkbenchLegend")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setToolTip(f"{name}　{count:,} {unit}（{count / ring.total:.1%}）")
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 1, 6, 1)
        row.setSpacing(6)
        dot = QFrame()
        dot.setFixedSize(8, 8)
        dot.setStyleSheet(f"background: {PALETTE[index % len(PALETTE)][0]}; border-radius: 4px;")
        row.addWidget(dot)
        title = label(name, "WorkbenchLegendLabel")
        title.setWordWrap(True)
        row.addWidget(title, 1)
        value = label(f"{count:,} {unit}", "WorkbenchLegendLabel")
        value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(value)

    def enterEvent(self, event):
        self._ring.highlight(self._index)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._ring.highlight(-1)
        super().leaveEvent(event)


class WorkbenchRingChart(QWidget):
    def __init__(self, rows, unit):
        super().__init__()
        self.ring = _Ring(rows, unit)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 1, 0, 1)
        outer.setSpacing(10)
        outer.addWidget(self.ring)
        legend = QVBoxLayout()
        legend.setSpacing(2)
        legend.addStretch(1)
        for index, (name, count) in enumerate(rows):
            legend.addWidget(_LegendItem(self.ring, index, name, count, unit))
        legend.addStretch(1)
        outer.addLayout(legend, 1)
