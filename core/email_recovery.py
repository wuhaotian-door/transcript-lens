"""邮箱找回密码 —— **接口预留，本阶段不发送任何邮件**。

本模块现在是**壳**：定义了接口、配置读写和自检，但没有任何真实发信实现。
下一阶段拿到用户选定的邮件 API 之后，只需实现一个类（见下）即可接通。


先说清楚：邮箱**做不到**什么
============================

必须先把一个常见误解按死。库密钥是**随机生成、只存在于本机**的：

* ``users.json`` 里只有 ``wrapped_key`` = ``Fernet(PBKDF2(密码)).encrypt(库密钥)``。
* 没有任何服务器持有库密钥，本程序也**不上传**任何东西。

所以**"邮箱验证码 → 服务器帮你重置密码"这条路在本架构下不存在**：验证码里
没有库密钥，换完新密码之后 ``wrapped_key`` 还是旧密码包的，成绩依旧解不开。
任何外部服务都拿不到库密钥，这是设计使然，不是实现偷懒。

邮箱**能**做到什么
==================

邮箱能做的只有一件事：**替用户保存一份他自己不用记的恢复材料**。

于是可行的方案只有一种形状 —— **注册时就埋好**：

1. 注册成功时（此刻库密钥还在手里），生成一个高熵随机 ``recovery_secret``；
2. 本地存一份 ``Fernet(recovery_secret).encrypt(库密钥)`` 的包裹；
3. 把这个 secret 发到用户邮箱（邮件正文或链接）；
4. 将来忘密码时，用户从邮箱里翻出这封信，把 secret 粘回来 → 解出库密钥 →
   他就能设一个新密码。

★ **时序是这个方案的命门**：恢复材料必须在**注册时**送出。事到临头再想发，
那时库密钥已经解不开了，没有东西可发。

★ 上线这个功能会**打破 Spec 的"不联网"约束**（红线：不联网、无自动更新）。
   出网的只有那一个随机 token，**不含姓名、不含成绩、不含学号**。这是需要
   用户明确点头的规格变更。


下一阶段要填的地方
==================

只有一个类需要写：:class:`EmailSender` 的实现。

    class MyProviderSender:
        def send(self, message: MailMessage) -> None:
            # 在这里调你的邮件 API
            ...

然后把它接进 :func:`get_sender` 的分支即可。**其余代码不需要改动** ——
调用方只认 :class:`MailMessage` 和 ``send()``，与本模块选择的厂商无关。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from core.crypto_store import CryptoError, default_root

__all__ = [
    "CONFIG_FILENAME",
    "EmailNotConfiguredError",
    "EmailSendError",
    "EmailSender",
    "MailConfig",
    "MailMessage",
    "NotImplementedSender",
    "config_example_path",
    "config_path",
    "get_sender",
    "load_config",
    "save_config",
    "send_recovery_mail",
]


#: 配置文件名。**放在 AppData，不在仓库里** —— API key 是密钥，
#: 绝不能跟着 git 走。仓库里只有 ``config/email.example.json`` 这个模板。
CONFIG_FILENAME = "email.json"

#: 仓库里那份模板的相对路径（只用于界面提示"去哪儿填"）
_EXAMPLE_RELATIVE = Path("config") / "email.example.json"


class EmailSendError(CryptoError):
    """发信失败（网络、鉴权、配额、收件人无效……）。"""


class EmailNotConfiguredError(EmailSendError):
    """还没配邮件 API —— 本阶段的正常状态，不是 bug。"""


@dataclass(frozen=True)
class MailMessage:
    """一封要发的信。**这是本模块与厂商 API 之间唯一的契约。**

    刻意只保留三样东西：所有邮件服务商（SMTP / SendGrid / Resend / 阿里云
    邮件推送 / 腾讯云 SES ……）都能用 ``收件人 + 主题 + 正文`` 表达。
    多出来的字段（模板 ID、发信人别名、回执地址……）都塞进 :class:`MailConfig`，
    不要污染这个结构。
    """

    to: str
    subject: str
    body: str

    def redacted(self) -> dict[str, str]:
        """给日志/自检用的脱敏版本：正文只留长度，不打印内容。

        恢复邮件正文里就是那把能解开库密钥的 secret —— **绝不能进日志**。
        """
        return {
            "to": _mask_address(self.to),
            "subject": self.subject,
            "body": f"<{len(self.body)} 字符，已隐去>",
        }


@dataclass
class MailConfig:
    """邮件 API 的配置。字段是**按厂商无关**的方式命名的。

    下一阶段用户给我具体的 API 之后，只需在这里补厂商特有的字段
    （或者干脆加一个 ``extra: dict``），不必改动 :class:`MailMessage`。
    """

    #: 厂商标识，例如 "smtp" / "resend" / "aliyun" / "tencent"
    provider: str = ""
    #: API 基地址（有些厂商需要，SMTP 则是 host:port）
    endpoint: str = ""
    #: ★ 密钥。只存在于 AppData 的 email.json，**绝不入库、绝不进日志**
    api_key: str = field(default="", repr=False)
    #: 发信人地址（大多厂商要求与已验证域名一致）
    sender: str = ""
    #: 发信人显示名
    sender_name: str = "成绩单透镜"
    #: 厂商特有字段的逃生舱，避免每接一家就要改这个 dataclass
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        """够不够发信。**只看有没有密钥和发信人**，具体厂商要求各自校验。"""
        return bool(self.api_key and self.sender)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "api_key": self.api_key,
            "sender": self.sender,
            "sender_name": self.sender_name,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "MailConfig":
        if not isinstance(data, dict):
            return cls()
        extra = data.get("extra")
        return cls(
            provider=str(data.get("provider", "")),
            endpoint=str(data.get("endpoint", "")),
            api_key=str(data.get("api_key", "")),
            sender=str(data.get("sender", "")),
            sender_name=str(data.get("sender_name", "成绩单透镜")),
            extra=dict(extra) if isinstance(extra, dict) else {},
        )

    def __repr__(self) -> str:  # pragma: no cover - 防御性
        return (
            f"MailConfig(provider={self.provider!r}, endpoint={self.endpoint!r}, "
            f"api_key=<hidden>, sender={self.sender!r}, "
            f"configured={self.configured})"
        )


@runtime_checkable
class EmailSender(Protocol):
    """**下一阶段唯一要实现的东西。**

    契约（实现时必须遵守）：

    1. 成功就正常返回，失败就抛 :class:`EmailSendError`（或它的子类）。
    2. **绝不静默失败** —— 调用方要靠异常来判断"信到底发出去没有"。
    3. **绝不把 ``message.body`` 写进日志** —— 里面是能解开库密钥的 secret。
    4. 不重试到天荒地老：超时/配额这类错误直接往上抛，由界面提示用户重试。
    """

    def send(self, message: MailMessage) -> None:  # pragma: no cover - 协议
        ...


class NotImplementedSender:
    """本阶段的默认实现：**什么都不发，明确报错**。

    刻意不返回"假装成功"。如果这里静默放过，用户会以为恢复邮件已经寄出，
    等到真忘密码那天才发现邮箱里什么都没有 —— 那才是灾难。
    """

    def send(self, message: MailMessage) -> None:
        raise EmailNotConfiguredError(
            "邮件 API 尚未接入（本阶段只预留了接口，不会真的发信）。\n\n"
            f"配置位置：{config_path()}\n"
            f"模板参考：{config_example_path()}"
        )


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------


def config_path(root: str | Path | None = None) -> Path:
    """真实配置文件的位置：``<数据根>\\email.json``。

    默认数据根与成绩库一致（``AppData\\Local\\SGMS``），**不在仓库里**。
    只返回路径，不创建、不读取。
    """
    base = Path(root) if root is not None else default_root()
    return base / CONFIG_FILENAME


def config_example_path() -> Path:
    """仓库里那份模板的路径（写文档、界面提示用）。"""
    return _EXAMPLE_RELATIVE


def load_config(root: str | Path | None = None) -> MailConfig:
    """读配置。**文件不存在 → 返回未配置的空配置，不抛异常。**

    和成绩库的做法一致：配置文件缺失是"还没配"，属于正常状态；
    但**文件存在却解析不了**要抛 :class:`EmailSendError` ——
    那种情况说明配置写坏了，静默当成"没配"会让用户对着一个错误的 key 反复试。
    """
    path = config_path(root)
    if not path.exists():
        return MailConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EmailSendError(f"邮件配置读不了：{path} —— {exc}") from exc
    return MailConfig.from_dict(data)


def save_config(config: MailConfig, root: str | Path | None = None) -> Path:
    """写配置到 AppData。目录按需创建。

    ★ 这里**故意不做**"写进仓库"的分支 —— 带 API key 的文件永远不落在仓库里。
    """
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(config.to_dict(), ensure_ascii=False, indent=2)
    path.write_text(blob + "\n", encoding="utf-8")
    return path


def get_sender(root: str | Path | None = None) -> EmailSender:
    """拿到当前的发信实现。

    本阶段：**永远**返回 :class:`NotImplementedSender`，不论配置里填了什么。
    下一阶段在这里按 ``config.provider`` 分派到具体实现，例如::

        config = load_config(root)
        if not config.configured:
            return NotImplementedSender()
        if config.provider == "resend":
            return ResendSender(config)
        ...
    """
    return NotImplementedSender()


# ---------------------------------------------------------------------------
# 业务入口（下一阶段实现）
# ---------------------------------------------------------------------------


def send_recovery_mail(
    to: str,
    recovery_secret: str,
    *,
    username: str = "",
    root: str | Path | None = None,
) -> None:
    """把恢复材料寄到用户邮箱。**本阶段必定抛异常。**

    ⚠️ 调用时机是**注册时**（或用户主动'绑定邮箱'时），不是忘密码时 ——
       原因见模块开头。忘密码那天已经解不开库密钥了，没东西可寄。

    下一阶段接入时，这个函数体只需要做三件事：

    1. 把 ``recovery_secret`` 拼成一段人话（正文/链接）；
    2. ``get_sender(root).send(MailMessage(to=..., ...))``；
    3. **不要**把 ``recovery_secret`` 写进任何日志、异常消息或返回值。

    注意 ``username`` 只用于称呼，**不要**把姓名、学号、成绩写进邮件 ——
    出网的只有收件地址和那一个随机 token（Spec 红线 11）。
    """
    raise EmailNotConfiguredError(
        "邮箱找回密码尚未实现（本阶段只有接口）。\n\n"
        "需要先决定并接入邮件 API；接入前，忘记密码没有任何找回途径。"
    )


def _mask_address(address: str) -> str:
    """``zhangsan@example.com`` → ``z***@example.com``，给日志用。"""
    text = (address or "").strip()
    if "@" not in text:
        return "***"
    local, _, domain = text.partition("@")
    head = local[:1] if local else ""
    return f"{head}***@{domain}"
