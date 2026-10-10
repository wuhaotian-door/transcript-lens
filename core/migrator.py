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


#: 版本链：``(起点, 终点, 变换函数)``。
#:
#: ★ 这是"未来版本在此添加分支"的那张表（任务书 2.3）。加一跳只需要
#:   往这里插一行 + 写一个 ``_lift_x_to_y`` 函数。
_STEPS: tuple[tuple[str, str, Callable[[dict[str, Any]], dict[str, Any]]], ...] = (
    ("1.0", "1.6", _lift_1_0_to_1_6),
    ("1.6", "1.7", _lift_1_6_to_1_7),
    ("1.7", "1.8", _lift_1_7_to_1_8),
    ("1.8", "1.8.1", _lift_1_8_to_1_8_1),
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

    def _skip(_dir: str, names: list[str]) -> set[str]:
        # 跳过已有的备份（否则套娃）和临时文件
        return {
            name
            for name in names
            if name.startswith(BACKUP_PREFIX) or name.endswith(".tmp")
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
