"""侧写正文和谈话共用的阅读样式。内容仍按原格式复制和保存。"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextOption
from PySide6.QtWidgets import QFrame, QScrollArea, QTextEdit, QWidget


def profile_scroll(content: QWidget) -> QScrollArea:
    scroll = QScrollArea()
    scroll.setObjectName("ProfileScroll")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setWidget(content)
    return scroll


class ProfileDocument(QTextEdit):
    def __init__(self, parent: QWidget | None = None, *, conversation: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName("ProfileConversation" if conversation else "ProfileDocument")
        self.setAcceptRichText(False)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.document().setDocumentMargin(14)
        self._space_paragraphs()

    @staticmethod
    def _paragraph(tone: str = "") -> QTextBlockFormat:
        fmt = QTextBlockFormat()
        fmt.setLineHeight(145, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        fmt.setBottomMargin(6)
        if tone == "user":
            fmt.setBackground(QColor("#E6F0FC"))
            fmt.setLeftMargin(16)
        elif tone == "assistant":
            fmt.setBackground(QColor("#F0F7F5"))
            fmt.setRightMargin(16)
        if tone in {"user", "assistant"}:
            fmt.setTextIndent(8)
            fmt.setTopMargin(7)
            fmt.setBottomMargin(12)
        return fmt

    def _space_paragraphs(self) -> None:
        cursor = QTextCursor(self.document())
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.mergeBlockFormat(self._paragraph())
        emphasis = QTextCharFormat()
        emphasis.setFontWeight(QFont.Weight.DemiBold)
        emphasis.setForeground(QColor("#264F88"))
        block = self.document().begin()
        while block.isValid():
            text = block.text()
            marker_end = text.find("】", 1, 24) if text.startswith("【") else -1
            if marker_end > 0:
                cursor.setPosition(block.position())
                cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor,
                                    marker_end + 1)
                cursor.mergeCharFormat(emphasis)
            block = block.next()

    def setPlainText(self, text: str) -> None:  # noqa: N802 - Qt API
        super().setPlainText(text)
        self._space_paragraphs()

    def clear(self) -> None:
        super().clear()
        self._space_paragraphs()

    def append_entry(self, text: str, tone: str = "") -> None:
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = self._paragraph(tone)
        if not self.document().isEmpty():
            cursor.insertBlock(fmt)
        else:
            cursor.setBlockFormat(fmt)
        cursor.insertText(text)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()
