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

**2026-09 更新：已接通网易 163 SMTP。** 见 :class:`SmtpSender`。
凭据存在 ``%LOCALAPPDATA%\\SGMS\\email.json``（**仓库外**），
本模块任何函数都**不会**把授权码写进日志、异常消息或返回值。
"""

from __future__ import annotations

import json
import smtplib
import ssl
from dataclasses import dataclass, field
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from core.crypto_store import CryptoError, default_root

__all__ = [
    "CONFIG_FILENAME",
    "DEFAULT_SSL_PORT",
    "DEFAULT_STARTTLS_PORT",
    "EmailNotConfiguredError",
    "EmailSendError",
    "EmailSender",
    "MailConfig",
    "MailMessage",
    "NotImplementedSender",
    "SmtpSender",
    "config_example_path",
    "config_path",
    "get_sender",
    "load_config",
    "save_config",
    "send_recovery_mail",
    "send_verification_mail",
]

#: 隐式 SSL 的默认端口（163 / QQ 都用 465）
DEFAULT_SSL_PORT = 465
#: STARTTLS 的默认端口
DEFAULT_STARTTLS_PORT = 587

#: 连服务器 / 发一封信的超时。**必须有** —— 没有超时的话，
#: 网络不通时界面会直接冻死在这里（Qt 主线程被阻塞）。
SMTP_TIMEOUT = 15.0


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


class SmtpSender:
    """用标准库 ``smtplib`` 直接发信 —— **不引入任何第三方邮件 SDK**。

    已实测可用的是网易 163：

    ==================  ==========================================
    ``endpoint``        ``smtp.163.com:465``
    ``api_key``         **授权码**（不是登录密码）
    ``sender``          必须与账号一致，163 会拒收伪造发件人
    ==================  ==========================================

    端口决定加密方式：**465 走隐式 SSL，其它端口走 STARTTLS 升级**。
    两种都失败就报错，**绝不降级成明文发信** —— 那等于把授权码和
    恢复码一起裸奔在网络上。

    ★ 安全约定：本类任何异常消息里**都不含授权码**。``smtplib`` 自己抛的
      异常可能带上账号，所以这里统一换成自己的消息再往上抛。
    """

    def __init__(self, config: MailConfig) -> None:
        self._config = config

    # ------------------------------------------------------------------ 内部
    def _split_endpoint(self) -> tuple[str, int]:
        """``"smtp.163.com:465"`` → ``("smtp.163.com", 465)``。"""
        raw = (self._config.endpoint or "").strip()
        if not raw:
            raise EmailNotConfiguredError("还没填 SMTP 服务器地址（endpoint）")
        host, _, port_text = raw.partition(":")
        host = host.strip()
        if not host:
            raise EmailNotConfiguredError(f"SMTP 地址不对：{raw!r}")
        try:
            port = int(port_text) if port_text.strip() else DEFAULT_SSL_PORT
        except ValueError as exc:
            raise EmailNotConfiguredError(f"SMTP 端口不是数字：{port_text!r}") from exc
        return host, port

    def _build_message(self, message: MailMessage) -> MIMEText:
        """拼一封 UTF-8 的纯文本邮件。"""
        body = MIMEText(message.body, "plain", "utf-8")
        body["Subject"] = Header(message.subject, "utf-8")
        # 发件人显示名也要用 Header 编码，否则中文名在客户端里是乱码
        body["From"] = formataddr(
            (str(Header(self._config.sender_name, "utf-8")), self._config.sender)
        )
        body["To"] = message.to
        body["Date"] = formatdate(localtime=True)
        return body

    # ------------------------------------------------------------------ 发送
    def send(self, message: MailMessage) -> None:
        """发一封信。失败抛 :class:`EmailSendError`（消息里不含授权码）。"""
        config = self._config
        if not config.sender:
            raise EmailNotConfiguredError("还没填发件人地址（sender）")
        if not config.api_key:
            raise EmailNotConfiguredError("还没填 SMTP 授权码（api_key）")

        host, port = self._split_endpoint()
        payload = self._build_message(message)
        context = ssl.create_default_context()

        try:
            if port == DEFAULT_SSL_PORT:
                server = smtplib.SMTP_SSL(
                    host, port, timeout=SMTP_TIMEOUT, context=context
                )
            else:
                server = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)
        except (OSError, ssl.SSLError) as exc:
            raise EmailSendError(
                f"连不上邮件服务器 {host}:{port} —— {type(exc).__name__}: {exc}\n\n"
                "请检查网络，以及服务器地址和端口是否填对（163 用 smtp.163.com:465）。"
            ) from exc

        try:
            with server:
                if port != DEFAULT_SSL_PORT:
                    try:
                        server.starttls(context=context)
                    except (OSError, smtplib.SMTPException) as exc:
                        raise EmailSendError(
                            f"STARTTLS 握手失败：{exc}。已中止，不会用明文发信。"
                        ) from exc
                try:
                    server.login(config.sender, config.api_key)
                except smtplib.SMTPAuthenticationError as exc:
                    raise EmailSendError(
                        "邮箱认证失败。163 要求用**授权码**而不是登录密码；"
                        "并在邮箱设置里确认已开启 SMTP 服务。\n"
                        f"（服务器返回码 {exc.smtp_code}）"
                    ) from exc
                server.sendmail(config.sender, [message.to], payload.as_string())
        except smtplib.SMTPRecipientsRefused as exc:
            raise EmailSendError(f"收件人被拒：{message.to}（地址可能不存在）") from exc
        except smtplib.SMTPSenderRefused as exc:
            raise EmailSendError(
                f"发件人被拒：{config.sender}。163 要求发件人与登录账号一致。"
            ) from exc
        except smtplib.SMTPException as exc:
            raise EmailSendError(f"发信失败：{type(exc).__name__}: {exc}") from exc
        except OSError as exc:
            raise EmailSendError(
                f"发信过程中网络中断：{type(exc).__name__}: {exc}"
            ) from exc

    def verify(self) -> str:
        """连一次服务器并登录，确认配置可用。**不发信。**

        给"设置 → 测试邮件配置"用：不想为了试配置就往用户邮箱里塞垃圾邮件。
        """
        host, port = self._split_endpoint()
        config = self._config
        context = ssl.create_default_context()
        try:
            if port == DEFAULT_SSL_PORT:
                server = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT, context=context)
            else:
                server = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)
        except (OSError, ssl.SSLError) as exc:
            raise EmailSendError(f"连不上 {host}:{port} —— {exc}") from exc
        try:
            with server:
                if port != DEFAULT_SSL_PORT:
                    server.starttls(context=context)
                server.login(config.sender, config.api_key)
        except smtplib.SMTPAuthenticationError as exc:
            raise EmailSendError(
                f"认证失败（{exc.smtp_code}）。163 要用授权码，不是登录密码。"
            ) from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise EmailSendError(f"验证失败：{type(exc).__name__}: {exc}") from exc
        return f"{host}:{port} 连接与登录都正常（{config.sender}）"


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

    按 ``config.provider`` 分派。目前支持：

    * ``"smtp"`` —— :class:`SmtpSender`（标准库，已实测网易 163 可用）
    * 空 / 其它 —— :class:`NotImplementedSender`（明确报错，不假装成功）

    想接别家（Resend / SendGrid / 阿里云……）就在下面加一个分支，
    **其余代码一处都不用改** —— 调用方只认 ``MailMessage`` 和 ``send()``。
    """
    config = load_config(root)
    if config.provider == "smtp" and config.configured:
        return SmtpSender(config)
    return NotImplementedSender()


# ---------------------------------------------------------------------------
# 业务入口
# ---------------------------------------------------------------------------


def send_recovery_mail(
    to: str,
    recovery_secret: str,
    *,
    username: str = "",
    root: str | Path | None = None,
) -> None:
    """把恢复码寄到用户邮箱。

    ⚠️ 调用时机是**注册成功时**（或用户主动"重新生成恢复码"时），
       **不是**忘密码时 —— 忘密码那天已经解不开库密钥了，没东西可寄。

    ★ 出网的内容只有：收件地址 + 称呼 + 那一串恢复码。
      **不含姓名、不含学号、不含任何成绩**（Spec 红线 11）。

    ★ ``recovery_secret`` 不写日志、不进异常消息、不作为返回值。
    """
    who = f"{username}　" if username else ""
    message = MailMessage(
        to=to,
        subject="【成绩单透镜】密码找回码 —— 请长期保留这封邮件",
        body=(
            f"{who}你好：\n\n"
            "这是「成绩单透镜」的密码找回码。将来忘记密码时，它是唯一能\n"
            "打开你成绩库的东西。\n\n"
            "────────────  找回码  ────────────\n"
            f"{recovery_secret}\n"
            "──────────────────────────────────\n\n"
            "怎么用：\n"
            "  1. 打开「成绩单透镜」，在登录页点「忘记密码？」\n"
            "  2. 填账号，把这串找回码粘进去\n"
            "  3. 设一个新密码，原来的成绩一条都不会丢\n\n"
            "请务必：\n"
            "  · 不要删除这封邮件（建议星标或加标签）\n"
            "  · 不要把找回码发给任何人 —— 拿到它的人就能打开你的成绩库\n\n"
            "说明：本程序不保存你的密码，也没有服务器。找回码在注册时就已生成，\n"
            "只有这封邮件里有。丢了它，忘记密码时数据就再也打不开了。\n\n"
            "—— 成绩单透镜（本邮件由程序自动发出，请勿回复）"
        ),
    )
    get_sender(root).send(message)


def send_verification_mail(
    to: str,
    code: str,
    *,
    purpose: str = "注册",
    minutes: int = 10,
    root: str | Path | None = None,
) -> None:
    """把邮箱验证码寄给用户，用来确认这个邮箱真的存在。

    ``purpose`` 只影响文案（"注册" / "重新生成找回码"）。

    ★ 验证码是**短时效**的一次性凭据，但它同样不该出现在日志里 ——
      这里和 :func:`send_recovery_mail` 一个规矩。
    """
    message = MailMessage(
        to=to,
        subject=f"【成绩单透镜】{purpose}验证码",
        body=(
            f"你的{purpose}验证码是：\n\n"
            f"        {code}\n\n"
            f"（{minutes} 分钟内有效，请勿转发给任何人）\n\n"
            "如果不是你本人操作，忽略本邮件即可，不会产生任何影响。\n\n"
            "—— 成绩单透镜（本邮件由程序自动发出，请勿回复）"
        ),
    )
    get_sender(root).send(message)


def _mask_address(address: str) -> str:
    """``zhangsan@example.com`` → ``z***@example.com``，给日志用。"""
    text = (address or "").strip()
    if "@" not in text:
        return "***"
    local, _, domain = text.partition("@")
    head = local[:1] if local else ""
    return f"{head}***@{domain}"
