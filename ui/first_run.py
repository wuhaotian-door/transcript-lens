"""新账户的首次使用引导。"""

from PySide6.QtWidgets import QMessageBox

from core import applog
from core.crypto_store import CryptoError
from core.user_store import load_user_json, save_user_json

_FILE = "first_run.json.enc"


def mark_first_run(store, username: str) -> None:
    _save_state(store, username, pending=True)


def _save_state(store, username: str, *, pending: bool) -> None:
    try:
        save_user_json(store, username, _FILE, {"pending": pending})
    except (CryptoError, OSError, ValueError) as exc:
        applog.write("first_run", type(exc).__name__, root=store.root)


def show_first_run_guide(window, store, username: str) -> bool:
    """仅在新账户的首次登录显示引导，返回是否要继续导入。"""
    state = load_user_json(store, username, _FILE, {})
    if not isinstance(state, dict) or state.get("pending") is not True:
        return False
    dialog = QMessageBox(window)
    dialog.setWindowTitle("开始使用成绩单透镜")
    dialog.setText("三步查看学业分析")
    dialog.setInformativeText(
        "1. 在「智育分析」导入教务系统导出的 Excel 成绩表。\n"
        "2. 选择班级与学期，核对课程、分数和学分后确认导入。\n"
        "3. 在「成长档案」选一名学生，打开个人侧写查看各学期表现。\n\n"
        "已经保存的找回码可用来重设密码。需要 AI 辅导时，可在「模型中心」下载推理核心和模型。"
    )
    start = dialog.addButton("导入成绩表", QMessageBox.ButtonRole.AcceptRole)
    later = dialog.addButton("稍后", QMessageBox.ButtonRole.RejectRole)
    dialog.setDefaultButton(start)
    dialog.setEscapeButton(later)
    dialog.exec()
    _save_state(store, username, pending=False)
    return dialog.clickedButton() == start
