"""远程通知负载定义。

触发客户端设备屏幕通知的详细选项。
"""

from pydantic import BaseModel, Field


class NotificationPayload(BaseModel):
    """广播消息的结构化数据。"""

    MessageMask: str = Field(default="", max_length=4096)
    MessageContent: str = Field(default="", max_length=4096)
    OverlayIconLeft: int = Field(default=0, ge=0)
    OverlayIconRight: int = Field(default=0, ge=0)
    IsEmergency: bool = False
    IsSpeechEnabled: bool = False
    IsEffectEnabled: bool = False
    IsSoundEnabled: bool = False
    IsTopmost: bool = False
    DurationSeconds: float = Field(default=5.0, ge=0, le=3600)
    RepeatCounts: int = Field(default=1, ge=0, le=100)

    # ─── 通知呈现类型（v2.1 增强）───────────────
    # kind 决定设备端/插件端怎么呈现这条通知：
    #   · island     —— 岛内普通通知（默认，非打断横幅）
    #   · popup      —— 弹窗通知（需确认，可回复）
    #   · fullscreen —— 全屏紧急通知（覆盖整屏 + 置顶 + 手动确认）
    # 空串 = 未指定，设备端按普通播报处理（向后兼容旧客户端）。
    # 这些字段同时被「下发」与「消息中心只读视图」使用，服务端在此统一约束。

    Kind: str = Field(default="", max_length=20)
    """呈现类型：island / popup / fullscreen；空 = 普通播报。"""

    RequireAck: bool = False
    """popup/fullscreen 是否需要设备端手动确认（确认回执上报操控端）。"""

    ReplyPresets: list[str] = Field(default_factory=list, max_length=6)
    """popup 的快捷回复预设文案（设备端一键发送，回执上报操控端）。"""

    AutoDismissSeconds: int = Field(default=0, ge=0, le=300)
    """fullscreen 自动关闭秒数（0 = 必须手动确认）。"""

    NoticeId: int = Field(default=0, ge=0)
    """服务端通知编号（回执归组依据；0 = 未指定）。"""

    Tts: bool = False
    """是否需要设备端语音朗读（TTS）。"""
