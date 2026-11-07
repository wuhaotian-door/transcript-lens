"""数据迁移与版本保护（charter v1.7 第八节）。

给谁用
------

辅导员换新版 exe 的时候，**旧数据必须能平滑升上来**。这条链上三层保护：

1. **版本标记** —— 加密库里写着数据版本（`core.version.VAULT_VERSION_KEY`）。
   没有标记的老库按 ``1.0`` 算（任务书 2.2）。
2. **迁移前强制备份** —— :func:`backup_vault` 把整个数据目录复制到
   ``%LOCALAPPDATA%/SGMS/backup_v{旧版本}_{YYYYMMDD_HHMMSS}/``。
   **备份失败就拒绝迁移**（charter 8.3），绝不"先改了再说"。
3. **失败回滚** —— 迁移中途出错就把 ``users.json`` 从备份恢复回去。

★ "加密库"到底指什么
------------------

这个项目里没有单个 ``vault.enc``。加密库 = **整个数据目录**：

    %LOCALAPPDATA%/SGMS/
        users.json          ← 用户索引（**明文**，Spec 第 5 章就是这样设计的）
        app.salt            ← 应用级盐，**丢了的话所有应用级密文都解不开**
        u_xxxxxxxx/         ← 每个用户的密文载荷
            grades.enc / tags.json.enc / archive.json.enc / ...

所以 :func:`backup_vault` 备份的是**整个目录**，不是只拷 ``users.json`` ——
只备份索引等于没备份：成绩全在 ``u_*/`` 里，而 ``app.salt`` 一丢，
意愿、班级、档案、AI 设置这些应用级数据就永远打不开了。

★ 备份目录本身要被排除
-------------------

备份放在数据目录**里面**（charter 8.2 指定的路径）。那么"备份整个目录"
就必须跳过已有的 ``backup_*`` —— 否则备份会把自己套娃进去，
每迁移一次目录就翻一倍。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from core.crypto_store import CryptoStore, UserIndex
from core.version import (
    CURRENT_VERSION,
    DEFAULT_VAULT_VERSION,
    LEGACY_VAULT_VERSION_KEY,
    VAULT_VERSION_KEY,
    compare_versions,
    normalize_version,
    stamp_vault_version,
    vault_version_of,
)

__all__ = [
    "BACKUP_PREFIX",
    "MigrationError",
    "MigrationOutcome",
    "backup_vault",
    "check_compatibility",
    "known_data_versions",
    "migrate",
    "migrate_store",
    "needs_migration",
    "vault_version",
]

#: 备份目录名前缀（charter 8.2：``backup_v{old}_{timestamp}``）
BACKUP_PREFIX = "backup_v"

#: 备份时**不抄**的顶层目录：体积大、且丢了能重建（重新下载 / 重装 / 本来就是日志）。
#: 真正不可再生的数据（``users.json`` / ``app.salt`` / ``u_*``）一个字都不省。
#:
#: ★ 只按**顶层**判（``backup_vault._skip`` 用 :func:`_dir_key` 比层级）—— 同名的
#:   ``u_<uid>/cache/`` 里是删了就回不来的聊天历史，不能跟着被跳过。
SKIP_DIR_NAMES = frozenset({"models", "runtime", "logs", "cache"})

#: 时间戳格式：``YYYYMMDD_HHMMSS``
STAMP_FORMAT = "%Y%m%d_%H%M%S"


class MigrationError(RuntimeError):
    """迁移链上的任何一种失败：没有升级路径、备份失败、回滚也失败。"""


# ---------------------------------------------------------------------------
# 版本链
# ---------------------------------------------------------------------------


def _lift_1_0_to_1_6(data: dict[str, Any]) -> dict[str, Any]:
    """1.0 → 1.6：**没有数据结构变化**。

    1.6 加的是学期下拉和班级快照，那些都是**新文件**
    （``class_*.json.enc`` / ``archive.json.enc``），老库原样就兼容。
    """
    return data


def _lift_1_6_to_1_7(data: dict[str, Any]) -> dict[str, Any]:
    """1.6 → 1.7：**没有数据结构变化**，只是从这一版起写版本标记。

    ★ 将来真要改数据结构，就在这里改，并且**同时**把
      :data:`_STEPS` 里补上对应的下一跳 —— 别的文件不用动。
    """
    return data


def _lift_1_7_to_1_8(data: dict[str, Any]) -> dict[str, Any]:
    """1.7 → 1.8：**没有数据结构变化**。

    v1.8 加的是自动更新器和 AI 模型下载器骨架 —— 两者都只在
    ``%LOCALAPPDATA%/SGMS/`` 下**新建自己的目录**（``backup_exe/`` /
    ``models/``），加密库里一个字段都没增删。

    ★ 顺带一提：v1.8 确实动了版本标记的**键名策略**（canonical +
      兼容别名两个键都写），但那是 :func:`core.version.stamp_vault_version`
      在**每一次写盘**时做的，不属于"把老数据抬到新格式"，
      所以这里依然什么都不用改 —— 迁移只需要把标记从 1.7 抬到 1.8。
    """
    return data


def _lift_1_8_to_1_8_1(data: dict[str, Any]) -> dict[str, Any]:
    """1.8 → 1.8.1：**没有数据结构变化**。

    v1.8.1 是把"v1.8.2 的文档合并 + v1.9 的四项功能"打成的**发布版本**。
    v1.9 只**新增**文件（``moral_batches.json.enc``、``models/installed.json``）
    与配置（``update/manifest.json``、``update/ai_models.json``），加密库里
    一个字段都没增删；``core/parser.py`` 的表格干扰修复只影响**新导入**的结果，
    已经存进库里的班级数据原样可读。

    ★ 这一跳是**发版需要**（更新检查靠版本号比大小），不是"数据结构变了" ——
      所以照旧什么都不用改，只把标记从 1.8 抬到 1.8.1。
    """
    return data


def _lift_1_8_1_to_1_9(data: dict[str, Any]) -> dict[str, Any]:
    """1.8.1 → 1.9：**没有数据结构变化**。

    v1.9 补完的是三件事：模型中心增强（负载指示 / 卸载 / 复制链接）、
    AI 推理引擎（``core/ai/inference.py``，可选依赖、懒加载）、
    以及档案合并与成绩覆盖机制。加密库里新增的两个载荷
    （``grade_history.json.enc``、``student_profiles.json.enc``）都是**新建文件** ——
    老库打开时读到"没有这个文件"就当成空，已有数据一个字节都不动；
    ``archive.json.enc`` 的格式也没改，覆盖策略只决定**新导入**怎么写。

    ★ 这一跳同样是**发版需要**（更新检查靠版本号比大小），照旧什么都不用改，
      只把标记从 1.8.1 抬到 1.9。
    """
    return data


def _lift_1_9_to_1_9_1(data: dict[str, Any]) -> dict[str, Any]:
    """1.9 → 1.9.1：**没有数据结构变化**。

    v1.9.1 是 v1.9 的**紧急修复版**（七个问题）：全局 AI 助手入口删掉、谈话能力
    收进档案页的「AI 辅导」标签页、模型中心改卡片流、成绩单条删除与「删除成绩库」
    判据修复（``core.database.library_inventory``）、审计日志
    （``audit/grade_changes.json.enc``）、GPA 方案 1（新建 ``core/gpa.py``，
    ``gpa_settings.json.enc``）与核对界面 QSplitter。

    这些全部是**新增文件**或**界面行为**：加密库里没有字段增删，老库原样可读。
    ``audit/grade_changes.json.enc`` 与 ``gpa_settings.json.enc`` 都是新建载荷 ——
    老库打开时读到"没有这个文件"就当空，已有数据一个字节都不动。

    ★ 这一跳是**发版需要**（更新检查靠版本号比大小，见 AGENTS.md §23b），
      照旧什么都不用改，只把标记从 1.9 抬到 1.9.1。
    """
    return data


def _lift_1_9_1_to_1_9_2(data: dict[str, Any]) -> dict[str, Any]:
    """1.9.1 → 1.9.2：**没有数据结构变化**。

    v1.9.2 新增的都是**新载荷文件**，老库读到"没有这个文件"就当空，
    已有数据一个字节都不动：

    * ``students.json.enc`` —— 学生主档（学号 / 姓名 / 班级 / 备注 / 归档 / 来源）；
    * ``semester_side/<学号>/<学期>.json.enc`` —— 学期侧写正文；
    * ``runtime/site/`` —— 推理后端（python 包）的可选安装目录，**不在加密库里**。

    ``grade_history.json.enc`` / ``student_profiles.json.enc`` / 审计日志的格式
    保持向后兼容（审计行只多了一个可选的 ``detail`` 字段，旧行读出来是空串）。

    ★ 这一跳同样是**发版需要**（更新检查靠版本号比大小，见 AGENTS.md §23b），
      照旧什么都不用改，只把标记从 1.9.1 抬到 1.9.2。
    """
    return data


def _lift_1_9_2_to_1_9_3(data: dict[str, Any]) -> dict[str, Any]:
    """1.9.2 → 1.9.3：**没有数据结构变化**，只多了"可以不备份哪些目录"。

    v1.9.3 全部是启动可靠性修复：

    * ``logs/app.log`` —— 新增运行期日志（以前 windowed exe 出问题完全没痕迹）；
    * 备份改为**跳过** ``models`` / ``runtime`` / ``logs`` / ``cache``
      （见 :data:`SKIP_DIR_NAMES`）—— 备份里少几个可重建的目录，不影响老库读取；
    * 单文件包改成解到 exe 所在目录（打包参数，与数据无关）。

    老库原样读得动，所以这一跳只抬标记。
    """
    return data


#: 版本链：``(起点, 终点, 变换函数)``。
#:
#: ★ 这是"未来版本在此添加分支"的那张表（任务书 2.3）。加一跳只需要
#:   往这里插一行 + 写一个 ``_lift_x_to_y`` 函数。
_STEPS: tuple[tuple[str, str, Callable[[dict[str, Any]], dict[str, Any]]], ...] = (
    ("1.0", "1.6", _lift_1_0_to_1_6),
    ("1.6", "1.7", _lift_1_6_to_1_7),
    ("1.7", "1.8", _lift_1_7_to_1_8),
    ("1.8", "1.8.1", _lift_1_8_to_1_8_1),
    ("1.8.1", "1.9", _lift_1_8_1_to_1_9),
    ("1.9", "1.9.1", _lift_1_9_to_1_9_1),
    ("1.9.1", "1.9.2", _lift_1_9_1_to_1_9_2),
    ("1.9.2", "1.9.3", _lift_1_9_2_to_1_9_3),
)


def known_data_versions() -> tuple[str, ...]:
    """版本链上出现过的所有数据版本，从早到晚（自检和界面用）。"""
    seen: list[str] = []
    for source, target, _fn in _STEPS:
        for version in (source, target):
            if version not in seen:
                seen.append(version)
    if CURRENT_VERSION not in seen:
        seen.append(CURRENT_VERSION)
    if DEFAULT_VAULT_VERSION not in seen:
        seen.insert(0, DEFAULT_VAULT_VERSION)
    return tuple(sorted(seen, key=lambda v: tuple(int(p) for p in v.split("."))))


# ---------------------------------------------------------------------------
# 读版本 / 判断
# ---------------------------------------------------------------------------


def vault_version(vault_data: Mapping[str, Any] | None) -> str:
    """这份加密库数据的数据版本。没有标记 → ``"1.0"``。"""
    return vault_version_of(vault_data)


def needs_migration(
    vault_data: Mapping[str, Any] | None, *, current: str = CURRENT_VERSION
) -> bool:
    """要不要迁移（库版本**低于**程序版本）。

    ★ 单独给出这个函数，是因为 :func:`check_compatibility` 返回的是元组
      （任务书 2.3 要 ``(bool, str)``），而**元组永远是真值** ——
      写成 ``if check_compatibility(data):`` 会恒为真。想判真假用这个。
    """
    return compare_versions(vault_version(vault_data), current) < 0


def check_compatibility(
    vault_data: Mapping[str, Any] | None, *, current: str = CURRENT_VERSION
) -> tuple[bool, str]:
    """``(是否需要迁移, 当前数据版本)`` —— 任务书 2.3 指定的签名。

    ★ 返回的是**元组**，直接拿去当条件用会踩坑（元组恒为真）。
      判断真假请用 :func:`needs_migration`。
    """
    version = vault_version(vault_data)
    return compare_versions(version, current) < 0, version


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------


def _is_backup_dir(path: Path) -> bool:
    return path.is_dir() and path.name.startswith(BACKUP_PREFIX)


def _unique_dir(parent: Path, name: str) -> Path:
    """同名目录已存在就加 ``-2`` / ``-3``。

    同一秒里连迁两次（自动化测试就会）直接 copytree 会**把上一次备份覆盖掉**，
    那是"备份"这两个字最不该有的行为。
    """
    candidate = parent / name
    suffix = 2
    while candidate.exists():
        candidate = parent / f"{name}-{suffix}"
        suffix += 1
    return candidate


def _dir_key(path: str | Path) -> str:
    """目录的**归一化比较键**（大小写、结尾分隔符、``.`` 段都不敏感）。

    ``copytree`` 的 ``ignore`` 回调把目录当字符串给出来（可能是 ``"C:\\\\x\\\\y"``
    也可能是 ``"C:/x/y"``），直接和 ``Path`` 比会假不等 —— 那样"只在顶层跳过"
    就会变成"每一层都跳过"，而 ``u_<uid>/cache/`` 里存的是不可重建的聊天历史。
    """
    try:
        return os.path.normcase(os.path.realpath(str(path)))
    except OSError:  # pragma: no cover - 真出不来就退回字符串比较
        return os.path.normcase(str(path))


def backup_vault(vault_path: str | Path, old_version: str = "") -> Path:
    """把整个加密库目录复制一份，返回备份目录。

    Args:
        vault_path: 数据根目录（``CryptoStore.root``，即 ``%LOCALAPPDATA%/SGMS``）。
        old_version: 旧数据版本；留空则从库里的标记读，读不到按 ``1.0``。

    Returns:
        备份目录的路径。

    Raises:
        MigrationError: 目录不存在、复制失败、或者复制完**核对不上**
            （备份必须可信，宁可拒绝迁移也不能留一份半拉子的备份）。
    """
    source = Path(vault_path)
    if not source.is_dir():
        raise MigrationError(f"数据目录不存在，没法备份：{source}")

    version = normalize_version(old_version) or _version_from_disk(source)

    root_key = _dir_key(source)

    def _skip(directory: str, names: list[str]) -> set[str]:
        # 跳过已有的备份（否则套娃）、临时文件，以及**可重建的大目录**。
        #
        # ★ 为什么必须跳过 models / runtime / logs：
        #   本地模型动辄 1~5 GB，抄一份要几十秒到几分钟。v1.9.2 的现场就是
        #   「点迁移 → 拷贝 1.1 GB 期间被占用 → 抛异常 → 迁移没做成」，
        #   而每次都还多留一份 GB 级备份。这三类目录丢了都能重建
        #   （models 重新下载、runtime 重装引擎、logs 本来就是日志），
        #   真正不能丢的是 users.json / app.salt / u_*/ —— 那些照旧全备。
        #
        # ★★ 只在**数据根目录这一层**跳（v1.9.3 修）：``u_<uid>/cache/`` 里放的是
        #   按学生分开的 AI 辅导聊天历史（``chat_history_<学号>.json.enc``），
        #   那是**删了就再也长不回来**的用户数据，不能因为目录名叫 cache 就被
        #   备份漏掉。copytree 的 ignore 会在每一层目录被调用，所以必须自己判层级。
        if _dir_key(directory) != root_key:
            return {name for name in names if name.startswith(BACKUP_PREFIX) or name.endswith(".tmp")}
        return {
            name
            for name in names
            if name.startswith(BACKUP_PREFIX)
            or name.endswith(".tmp")
            or name in SKIP_DIR_NAMES
        }

    target = _unique_dir(source, f"{BACKUP_PREFIX}{version}_{_stamp()}")
    try:
        shutil.copytree(source, target, ignore=_skip)
    except (OSError, shutil.Error) as exc:
        raise MigrationError(f"备份失败（{type(exc).__name__}: {exc}），已拒绝迁移。") from exc

    # --- 核对：原本有什么，备份里就得有什么 -------------------------------
    users = source / "users.json"
    if users.exists() and not (target / "users.json").exists():
        raise MigrationError(f"备份核对不通过：{target} 里没有 users.json，已拒绝迁移。")
    if (source / "app.salt").exists() and not (target / "app.salt").exists():
        raise MigrationError(f"备份核对不通过：{target} 里没有 app.salt，已拒绝迁移。")
    return target


def _stamp() -> str:
    return datetime.now().strftime(STAMP_FORMAT)


def _version_from_disk(root: Path) -> str:
    """直接从 ``users.json`` 里读版本（读不出/坏了 → ``1.0``）。"""
    import json

    path = root / "users.json"
    if not path.exists():
        return DEFAULT_VAULT_VERSION
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_VAULT_VERSION
    return vault_version_of(data if isinstance(data, dict) else None)


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------


def _verify_password(vault_data: Mapping[str, Any], password: str) -> None:
    """迁移前确认"这个密码真能开这个库"。

    只在调用方**给了**密码时才查（启动时的检测发生在登录之前，
    那时候手上没有密码）。密码本身绝不进日志、绝不进异常消息（铁律 2）。
    """
    try:
        index = UserIndex.from_dict(dict(vault_data))
    except Exception:  # noqa: BLE001 - 结构不对时不做密码校验，交给调用方
        return
    if len(index) == 0:
        return
    record = next(iter(index))
    if not CryptoStore.check_password(record, password):
        raise MigrationError("密码不对，没法确认这个加密库属于你。已拒绝迁移。")


def migrate(
    old_version: str,
    vault_data: Mapping[str, Any],
    password: str = "",
) -> dict[str, Any]:
    """按版本链把库数据升到 :data:`core.version.CURRENT_VERSION`。

    Args:
        old_version: 库当前的数据版本（``"1.0"`` / ``"1.6"`` …）。
        vault_data: 库数据（``UserIndex.to_dict()`` 的形状）。**不会被就地修改。**
        password: 可选。给了就先验证它确实能开这个库（防止对别人的库动手）。
            当前 1.0→1.7 这条链**不需要密码**（没有数据结构变化、不重包裹密钥），
            参数留着是为了将来需要重新包裹库密钥的那一跳不用改签名。

    Returns:
        升级后的**新** dict（调用方负责写回）。

    Raises:
        MigrationError: 版本比程序还新、链上缺一跳、或者密码验证不过。
    """
    start = normalize_version(old_version) or DEFAULT_VAULT_VERSION
    if compare_versions(start, CURRENT_VERSION) > 0:
        raise MigrationError(
            f"数据版本 v{start} 比程序版本 v{CURRENT_VERSION} 还新，"
            "不敢动它 —— 请先更新程序。"
        )

    if password:
        _verify_password(vault_data, password)

    data: dict[str, Any] = dict(vault_data)
    current = start
    hops: list[str] = []
    guard = 0
    while compare_versions(current, CURRENT_VERSION) < 0:
        guard += 1
        if guard > 32:  # pragma: no cover - 防呆
            raise MigrationError("迁移链出现了环，已中止。")
        step = next((s for s in _STEPS if s[0] == current), None)
        if step is None:
            raise MigrationError(
                f"没有从 v{current} 升级的路径（已知：{', '.join(known_data_versions())}）"
            )
        _source, target, lift = step
        data = lift(data)
        if not isinstance(data, dict):  # pragma: no cover - 防呆
            raise MigrationError(f"v{current} → v{target} 这一步返回了非 dict。")
        hops.append(f"v{current}→v{target}")
        current = target

    # 盖上新版本标记。
    #
    # ★ v1.8 改：不再"写 canonical + 删旧键"，而是**两个键都写**。
    #   旧写法会让启动检测读到哪个键取决于上次是谁写的 —— 而 charter 8.1
    #   和任务书 1.1 用的键名不一样。两个都写、值相同，谁读都对
    #   （见 core.version.VAULT_VERSION_KEYS）。
    stamp_vault_version(data, CURRENT_VERSION)
    data["_migration"] = {"from": start, "to": CURRENT_VERSION, "hops": hops,
                          "at": datetime.now().isoformat(timespec="seconds")}
    return data


# ---------------------------------------------------------------------------
# 高层：对一个 CryptoStore 走完整个流程
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MigrationOutcome:
    """一次迁移尝试的结果（界面只读这个，不自己判断）。"""

    performed: bool
    old_version: str
    new_version: str
    backup_dir: Path | None = None
    detail: str = ""

    @property
    def rolled_back(self) -> bool:
        return "已回滚" in self.detail


def migrate_store(
    store: CryptoStore,
    *,
    password: str = "",
    current: str = CURRENT_VERSION,
) -> MigrationOutcome:
    """把一个真实的加密库迁到当前版本。**备份 → 迁移 → 写回 → 失败回滚。**

    这也是启动检测调的入口。逻辑放在这里而不是 main.py，是为了
    **不带界面也能整条测**（``%TEMP%\\tl_migrate_test.py`` 就是这么测的）。

    Raises:
        MigrationError: 备份失败 / 迁移失败且回滚也失败。**这两种情况都必须
            让用户看见**，不能吞掉。
    """
    root = Path(store.root)
    if not store.users_path.exists():
        return MigrationOutcome(
            performed=False,
            old_version=current,
            new_version=current,
            detail="这台机器上还没有加密库，不需要迁移。",
        )

    index = store.load_users()
    data = index.to_dict()
    needs, old = check_compatibility(data, current=current)
    if not needs:
        return MigrationOutcome(
            performed=False,
            old_version=old,
            new_version=old,
            detail=f"数据版本 v{old} 已经是当前版本，不需要迁移。",
        )

    # ---- 1. 备份（失败就到此为止，charter 8.3）---------------------------
    backup = backup_vault(root, old)

    # ---- 2. 迁移 ---------------------------------------------------------
    try:
        new_data = migrate(old, data, password)
        # ---- 3. 写回（只有这一个文件会动）-------------------------------
        store.save_users(UserIndex.from_dict(new_data))
    except Exception as exc:  # noqa: BLE001 - 任何一步失败都要回滚
        note = _rollback(root, backup)
        raise MigrationError(
            f"迁移失败（{type(exc).__name__}: {exc}）。{note}"
            f"备份在：{backup}"
        ) from exc

    return MigrationOutcome(
        performed=True,
        old_version=old,
        new_version=current,
        backup_dir=backup,
        detail=(
            f"数据已从 v{old} 迁移到 v{current}。"
            f"迁移前的完整备份在：{backup}（建议重启程序）。"
        ),
    )


def _rollback(root: Path, backup: Path) -> str:
    """把 ``users.json`` 从备份恢复回去。

    ★ 只恢复 ``users.json``：迁移全程**只动过这一个文件**
      （成绩、意愿、档案都在 ``u_*/`` 里，一个字节都没碰）。
      整目录回滚反而会把备份之后新写的数据盖掉 —— 那才是真的丢数据。
    """
    source = backup / "users.json"
    if not source.exists():
        return "（备份里没有 users.json，没能回滚 —— 请手工处理）"
    try:
        shutil.copy2(source, root / "users.json")
    except OSError as exc:
        return f"（回滚也失败了：{type(exc).__name__}: {exc} —— 请手工处理）"
    return "已回滚到迁移前的 users.json。"
