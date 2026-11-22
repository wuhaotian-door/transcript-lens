"""固定 0–100 分刻度的学期成绩趋势图。"""

from __future__ import annotations

from html import escape
from typing import Iterable

from PySide6.QtCore import QPointF, QRectF, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QStyle, QToolTip, QWidget


class GradeTrendChart(QWidget):
    """展示加权均分，缺失学期保留位置并断开折线。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(184)
        self.setMouseTracking(True)
        self.setAccessibleName("学期加权均分趋势")
        self._series: list[tuple[str, float | None]] = []
        self._hover = -1
        self._opacity = 1.0
        self._reveal = QVariantAnimation(self)
        self._reveal.setStartValue(0.25)
        self._reveal.setEndValue(1.0)
        self._reveal.valueChanged.connect(self._fade)
        self.hide()

    def set_series(self, series: Iterable[tuple[str, float | None]]) -> None:
        points = [(term, float(score) if score is not None and 0 <= score <= 100 else None)
                  for term, score in series]
        self._reveal.stop()
        changed = points != self._series
        self._series = points
        self._hover = -1
        values = [f"{term} {score:.2f} 分" for term, score in points if score is not None]
        self.setAccessibleDescription("纵轴 0 到 100 分。" + "，".join(values))
        self.setVisible(bool(values))
        self._opacity = 1.0
        duration = min(260, self.style().styleHint(QStyle.StyleHint.SH_Widget_Animation_Duration))
        if changed and len(values) > 1 and duration > 0:
            self._reveal.setDuration(duration)
            self._reveal.start()
        self.update()

    def _fade(self, value: float) -> None:
        self._opacity = value
        self.update()

    def _geometry(self) -> tuple[QRectF, list[QPointF | None]]:
        plot = QRectF(40, 15, max(1, self.width() - 60), max(1, self.height() - 44))
        spacing = plot.width() / max(1, len(self._series) - 1)
        x = plot.center().x() if len(self._series) == 1 else plot.left()
        points = [
            None if score is None else QPointF(x + index * spacing, plot.bottom() - score * plot.height() / 100)
            for index, (_, score) in enumerate(self._series)
        ]
        return plot, points

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot, points = self._geometry()
        grid, text, ink = QColor("#DCE6F0"), QColor("#496078"), QColor("#236FA1")
        for score in (0, 25, 50, 75, 100):
            y = plot.bottom() - score * plot.height() / 100
            painter.setPen(grid)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(text)
            painter.drawText(QRectF(0, y - 9, plot.left() - 8, 18),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(score))

        spacing = plot.width() / max(1, len(points) - 1)
        metrics = painter.fontMetrics()
        label_width = max((metrics.horizontalAdvance(term) for term, _ in self._series), default=0)
        skip = max(1, int((label_width + 14) / max(1, spacing)) + 1)
        for index, (term, _score) in enumerate(self._series):
            if index % skip:
                continue
            x = plot.center().x() if len(points) == 1 else plot.left() + index * spacing
            width = min(self.width() - 8, max(20, label_width + 4))
            left = max(4.0, min(x - width / 2, self.width() - 4 - width))
            label = metrics.elidedText(term, Qt.TextElideMode.ElideRight, int(width))
            painter.drawText(QRectF(left, plot.bottom() + 6, width, 19),
                             Qt.AlignmentFlag.AlignCenter, label)

        path = QPainterPath()
        previous = None
        for point in points:
            if point is not None:
                if previous is None:
                    path.moveTo(point)
                else:
                    path.lineTo(point)
            previous = point
        painter.setOpacity(self._opacity)
        painter.setPen(QPen(ink, 2.1))
        painter.drawPath(path)
        for index, point in enumerate(points):
            if point is None:
                continue
            if index == self._hover:
                painter.setPen(QPen(grid, 1))
                painter.drawLine(QPointF(point.x(), plot.top()), QPointF(point.x(), plot.bottom()))
                painter.setPen(QPen(ink, 2.1))
            painter.setBrush(QColor("#F8FCFF") if index != self._hover else ink)
            radius = 6 if index == self._hover else 3.5
            painter.drawEllipse(point, radius, radius)

    def mouseMoveEvent(self, event) -> None:
        plot, points = self._geometry()
        pos = event.position()
        candidates = [(abs(point.x() - pos.x()), index) for index, point in enumerate(points)
                      if point is not None and plot.adjusted(-6, -6, 6, 6).contains(pos)]
        distance, index = min(candidates, default=(float("inf"), -1))
        index = index if distance <= 16 else -1
        if index == self._hover:
            return
        self._hover = index
        self.update()
        if index < 0:
            QToolTip.hideText()
            return
        term, score = self._series[index]
        message = f"{escape(term)}　{score:.2f} 分"
        if index > 0 and (previous := self._series[index - 1][1]) is not None:
            message += f"<br>比前一学期 {score - previous:+.2f} 分"
        QToolTip.showText(event.globalPosition().toPoint(), f"<qt>{message}</qt>", self)

    def leaveEvent(self, event) -> None:
        self._hover = -1
        QToolTip.hideText()
        self.update()
        super().leaveEvent(event)
