"""版本链与迁移跳（AGENTS.md §23b 的**仓库内**判据）。

★ 为什么要有这个文件：AGENTS.md §23b 说"升版本号 = 同时补迁移链的一跳"，
  不补的后果是老用户**启动就报错**：

      MigrationError: 没有从 v1.7 升级的路径（已知：1.0, 1.6, 1.7, 1.8）

  这条规则以前只有 `%TEMP%` 里的开发套件守着 —— 而 `%TEMP%` 会被系统清理
  （2026-10-10 实测：`tl_migrate_test.py` 等一批套件已经不见了）。
  规则落在**仓库**里才守得住，所以这里用 pytest 把它锁死。

覆盖三件事：

1. 版本链**无断档**：`known_data_versions()` 里每一个旧版本都能一路迁到
   `CURRENT_VERSION`（断档会抛 `MigrationError`）；
2. `CURRENT_VERSION` 确实在链的末端（升了版本号却忘了补跳 → 老库迁不过来）；
3. 每次迁移都**两个键一起写**、`_migration` 记账、数据版本比程序新时不迁移。
"""

from __future__ import annotations

import pytest

from core import migrator as M
from core import version as V

#: 除了当前版本以外的所有已知数据版本（都该能迁过来）
OLD_VERSIONS = tuple(v for v in M.known_data_versions() if v != V.CURRENT_VERSION)


def _vault(version: str) -> dict:
    """一份最小可迁移的库数据：只带版本标记（迁移只看标记）。"""
    return {
        V.VAULT_VERSION_KEY: version,
        V.LEGACY_VAULT_VERSION_KEY: version,
        "users": [],
    }


# ---------------------------------------------------------------------------
# 1. 链无断档
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("old", OLD_VERSIONS)
def test_每个已知版本都能迁到当前版本(old: str) -> None:
    out = M.migrate(old, _vault(old))
    assert out[V.VAULT_VERSION_KEY] == V.CURRENT_VERSION
    assert out[V.LEGACY_VAULT_VERSION_KEY] == V.CURRENT_VERSION


@pytest.mark.parametrize("old", OLD_VERSIONS)
def test_每个旧版本都被判定为需要迁移(old: str) -> None:
    assert M.needs_migration(_vault(old)) is True


def test_当前版本不需要迁移() -> None:
    assert M.needs_migration(_vault(V.CURRENT_VERSION)) is False


def test_没有标记的老库按默认版本算且需要迁移() -> None:
    data: dict = {"users": []}
    assert M.vault_version(data) == V.DEFAULT_VAULT_VERSION
    assert M.needs_migration(data) is True


def test_已知版本列表覆盖从默认版本到当前版本() -> None:
    known = M.known_data_versions()
    assert known[0] == V.DEFAULT_VAULT_VERSION
    assert known[-1] == V.CURRENT_VERSION
    assert len(set(known)) == len(known), "版本链里出现了重复版本"


def test_升版本号必须同时补一跳() -> None:
    """★ §23b 的核心断言：**紧挨着** CURRENT_VERSION 的那个版本必须有路可走。

    这条就是"改完 `CURRENT_VERSION` 忘了往 `_STEPS` 加一行"的探针 ——
    没有它，老用户升级后第一次启动会撞 `MigrationError`。
    """
    previous = [v for v in M.known_data_versions() if v != V.CURRENT_VERSION]
    assert previous, "版本链不该只有当前版本"
    nearest = previous[-1]
    out = M.migrate(nearest, _vault(nearest))
    assert out[V.VAULT_VERSION_KEY] == V.CURRENT_VERSION
    assert out["_migration"]["hops"], "迁移必须记下走过的那一跳"


# ---------------------------------------------------------------------------
# 2. 记账与键名
# ---------------------------------------------------------------------------


def test_迁移两个键一起写且值相同() -> None:
    out = M.migrate("1.0", _vault("1.0"))
    assert out[V.VAULT_VERSION_KEY] == out[V.LEGACY_VAULT_VERSION_KEY]
    assert out["_migration"]["from"] == "1.0"
    assert out["_migration"]["to"] == V.CURRENT_VERSION


def test_迁移不改动其它字段() -> None:
    data = _vault("1.0")
    data["note"] = "辅导员自己的东西"
    out = M.migrate("1.0", data)
    assert out["note"] == "辅导员自己的东西"
    assert out["users"] == []


def test_从1_8到当前版本记的那一跳() -> None:
    """v1.8 → v1.8.1 是一次**发版需要**的空迁移（数据结构没变）。"""
    out = M.migrate("1.8", _vault("1.8"))
    hops = out["_migration"]["hops"]
    assert hops and hops[-1].endswith(f"→v{V.CURRENT_VERSION}"), hops


# ---------------------------------------------------------------------------
# 3. 边界
# ---------------------------------------------------------------------------


def test_数据版本比程序新时拒绝迁移() -> None:
    with pytest.raises(M.MigrationError):
        M.migrate("9.9", _vault("9.9"))


def test_链上不存在的版本拒绝迁移() -> None:
    with pytest.raises(M.MigrationError):
        M.migrate("1.5", _vault("1.5"))


def test_兼容性检查返回元组_判真假要用needs_migration() -> None:
    """★ `check_compatibility` 返回元组，元组**永远是真值** —— 别拿它当布尔用。"""
    result = M.check_compatibility(_vault(V.CURRENT_VERSION))
    assert isinstance(result, tuple)
    assert result[0] is False  # 不需要迁移


def test_版本比较按数值不按字符串() -> None:
    assert V.compare_versions("1.10", "1.9") > 0
    assert V.compare_versions(V.CURRENT_VERSION, "1.8") > 0
