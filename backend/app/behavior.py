"""Deterministic interaction boundaries; text rules are not voice emotion detection."""
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import re

from .budgeting import SHANGHAI
from .storage import CompanionStore


def clean(text: str) -> str:
    return text.strip().strip("。！!？?，,；; ")


@dataclass(frozen=True)
class Context:
    label: str = "normal"
    confidence: float = 0.2
    source: str = "text_rules"


class ContextAnalyzer:
    def analyze(self, text: str, enabled: bool = True) -> Context:
        if not enabled:
            return Context()
        text = clean(text)
        rules = [
            (r"^(?:我(?:现在|今天|真的|特别|很|好|有点)*)(?:难过|伤心|低落|不开心)", "sad", 0.9),
            (r"^(?:我(?:现在|今天|真的|特别|很|好|有点)*)(?:开心|高兴|兴奋)", "excited", 0.9),
            (r"^(?:我(?:现在|真的|很|有点)*)(?:生气|不耐烦)|^(?:你怎么这么烦|别啰嗦)", "impatient", 0.9),
            (r"^(?:我赶时间|我很着急|快点|直接说结论)", "urgent", 0.9),
            (r"^(?:我)?(?:今天|现在|真的|很|有点|好|太)*(?:累(?:了)?|困(?:了)?|疲惫)", "tired", 0.85),
            (r"^(?:详细解释|展开讲|给我详细讲)", "detailed", 1.0),
            (r"^(?:教我|帮我理解|讲解一下|解释一下|这道题)", "studying", 0.8),
            (r"等于(?:几|多少)|怎么算|计算一下|求解", "studying", 0.85),
            (r"^(?:开个玩笑|讲个笑话|逗我笑)", "joking", 1.0),
        ]
        for pattern, label, confidence in rules:
            if re.search(pattern, text):
                return Context(label, confidence)
        return Context()


class QuietMode:
    def __init__(self, store: CompanionStore):
        self.store = store

    def enter(self):
        self.store.set_quiet(True)

    def leave(self, reason: str = "explicit_wake"):
        self.store.set_quiet(False, reason)

    def active(self) -> bool:
        return self.store.controls()["quiet"]

    def allows_proactive(self, kind: str, *, quiet_exception: bool = False, at: datetime | None = None) -> bool:
        controls = self.store.controls()
        if kind == "system_safety":
            return True
        if quiet_exception and kind == "reminder":
            return True
        if controls["quiet"]:
            return False
        at = at or datetime.now(timezone.utc)
        local = at.astimezone(SHANGHAI if controls["timezone"] == "Asia/Shanghai" else timezone.utc)
        minute = local.hour * 60 + local.minute
        def minutes(value):
            hour, part = value.split(":")
            return int(hour) * 60 + int(part)
        start, end = minutes(controls["quiet_hours_start"]), minutes(controls["quiet_hours_end"])
        quiet_hours = (start <= minute < end) if start < end else (minute >= start or minute < end) if start != end else False
        return not quiet_hours and (kind != "greeting" or controls["greeting_enabled"])


@dataclass(frozen=True)
class ResponseStyle:
    length: str
    max_characters: int
    humor: float
    cuteness: float
    followup_enabled: bool
    offer_advice: bool
    encourage: bool


@dataclass(frozen=True)
class Presentation:
    response_style: ResponseStyle
    emotion_display: str
    speech_style: dict
    interaction_state: str

    def to_dict(self):
        return asdict(self)


def requested_story_seconds(text: str) -> int:
    if '故事' not in text:return 0
    match=re.search(r'(\d{1,3}|[一二三四五六七八九十两]{1,3})\s*秒',text)
    if not match:return 0
    value=match.group(1);digits={'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
    if value.isdigit():seconds=int(value)
    elif '十' in value:
        parts=value.split('十')
        if len(parts)!=2:return 0
        left,right=parts
        if (left and left not in digits) or (right and right not in digits):return 0
        seconds=(digits.get(left,1)*10)+digits.get(right,0)
    else:seconds=digits.get(value,0)
    return seconds if 5<=seconds<=120 else 0


class ResponseStyleManager:
    def choose(self, context: Context, controls: dict, text: str = "") -> Presentation:
        quiet = controls["quiet"]
        length = controls["reply_length"]
        if length == "normal" and context.label in {"urgent", "tired", "sad", "impatient"}:
            length = "brief"
        if length == "normal" and context.label == "detailed":
            length = "detailed"
        serious = controls["serious"] or context.label in {"studying", "urgent", "impatient"}
        playful = context.label in {"excited", "joking"} and context.confidence >= 0.8
        humor = 0.0 if quiet or serious or context.label in {"sad", "tired"} else (0.4 if playful else 0.1)
        cute = 0.0 if quiet or serious or context.label in {"sad", "tired"} else (0.2 if playful else 0.05)
        display = "quiet" if quiet else "neutral"
        if not quiet and context.confidence >= 0.7:
            display = {"excited": "happy", "joking": "happy", "studying": "thinking", "detailed": "thinking",
                       "sad": "gentle", "tired": "sleepy"}.get(context.label, "neutral")
        style = ResponseStyle(length, {"brief": 160, "normal": 500, "detailed": 1800}[length], humor, cute,
                              controls["followup_enabled"] and not quiet,
                              context.label != "sad" or bool(re.search("怎么办|建议|帮我解决", text)),
                              context.label == "sad" and not quiet)
        duration=requested_story_seconds(text)
        if duration:
            # An explicit duration applies to this answer, without changing the
            # user's saved preference for brief everyday replies.
            style=replace(style,length="normal" if duration<=40 else "detailed",
                          max_characters=max(style.max_characters,min(duration*5+32,1000)))
        speech = {"enabled": bool(controls["speech_enabled"] and not quiet),
                  "speed": min(controls["speech_speed"], 0.95) if context.label in {"sad", "tired"} else controls["speech_speed"],
                  "volume": 0.0 if quiet else controls["volume"], "emotion_control": False}
        return Presentation(style, display, speech, "QUIET" if quiet else "IDLE")

    @staticmethod
    def prompt(presentation: Presentation, request_text: str = "") -> str:
        style = presentation.response_style
        duration=requested_story_seconds(request_text)
        story=(f"用户本轮明确要求约{duration}秒的故事，优先于平时的简短偏好。请直接讲一个有开端、经过和结尾的完整故事，正文约{int(duration*4.2)}到{int(duration*5)}个汉字；不要只给梗概或答应后省略故事，在完整结局后结束，保留用户指定的结尾。" if duration else "")
        length_requirement="按本轮故事时长展开" if duration else {
            "brief":"简短说清楚，闲聊通常一到三句，不省略必要内容",
            "normal":"按问题自然回答，闲聊别讲长篇道理",
            "detailed":"把用户要了解的内容讲清楚，说明要具体"}[style.length]
        tone_requirement="语气平实直接。" if style.humor==0 else "轻松自然，有合适的笑点再说，不刻意抖机灵。"
        return (story+f"本轮回复要求：{length_requirement}，最多 {style.max_characters} 字符。"
                +tone_requirement+"说得顺口，语气词可有可无，不要硬加。" + ("不要在结尾添加不必要的追问。" if not style.followup_enabled else "只有确有必要时才追问。")
                + ("回答结束就停下，不要求用户继续提问或说明问题。" if not style.followup_enabled else "")
                + ("直接回答当前问题，不额外自我介绍，不写动作旁白。" if style.humor<=0.1 else "")
                + ("认真直接回答，不自夸，不评论题目简单，不比较自己和答案，不加卖萌句子。" if style.cuteness==0 else "")
                + ("优先自然回应，不主动给长篇建议。" if not style.offer_advice else "按实际问题提供帮助。"))

    @staticmethod
    def enforce(text: str, presentation: Presentation) -> str:
        style = presentation.response_style
        if not style.followup_enabled:
            # Drop only the tag question, keeping a preceding factual answer.
            text=re.sub(r'[，,]?\s*(?:对吧|是吧|是不是)[？?]\s*$', '。', text)
            # Remove recognizable engagement tails, not questions embedded in quoted answers.
            tail = re.compile(r"(?:^|(?<=[。！!，,\n]))\s*(?:你想|你要不要|要不要|需要我|想不想|你愿意|你觉得|要我|你是否|你是不是|是不是|你喜欢|你平时|你还想|你有没有|方便说说|有什么想分享|有什么想聊|还有什么想聊|愿意分享|想说说|想聊聊)[^。！!\n]{0,180}[？?]\s*$")
            invitation=re.compile(r"(?:^|(?<=[。！!\n]))\s*(?:不过|那么|现在)?(?:现在)?(?:请)?(?:你)?(?:直接)?告诉我(?:你|您)的(?:问题|想法|需求)[^。！!\n]{0,100}[？?]\s*$")
            for _ in range(3):
                replacement = invitation.sub("",tail.sub("",text)).strip()
                if replacement == text:
                    break
                text = re.sub(r"[，,]\s*$", "。", replacement)
        if style.cuteness == 0:
            text = re.sub(r"(?:^|(?<=[。！!？?\n]))\s*(?:小糯|我)(?:眨(?:眨)?眼(?:睛)?|摇(?:摇)?尾巴|摆(?:摆)?尾巴|歪(?:歪)?头)[^。！!？?\n]*(?:[。！!？?]|$)", "", text)
            text = re.sub(r"喵[~～!！。]?(?=$|[，,。.!！])", "", text)
            text = re.sub("[🐱🐈😺😸😹😻😼😽🙀😿😾]", "", text)
        text = text.strip()
        if not text:
            return "这次没有生成合适的回答。"
        if len(text) > style.max_characters:
            clipped = text[:style.max_characters - 1]
            boundary = max(clipped.rfind(c) for c in "。！？!?\n")
            text = clipped[:boundary + 1] if boundary > len(clipped) // 2 else clipped + "…"
        return text


DIRECTIVES = {
    "别问了": {"followup_enabled": False}, "不要追问": {"followup_enabled": False},
    "停止追问": {"followup_enabled": False}, "可以继续问": {"followup_enabled": True},
    "认真一点": {"serious": True}, "认真点": {"serious": True},
    "简单说": {"reply_length": "brief"}, "简短一点": {"reply_length": "brief"},
    "详细解释": {"reply_length": "detailed"}, "详细一点": {"reply_length": "detailed"},
    "恢复正常聊天": {"reply_length": "normal", "serious": False, "followup_enabled": False},
    "不要主动问候": {"greeting_enabled": False}, "关闭主动问候": {"greeting_enabled": False}}


def directives(text: str) -> tuple[dict, bool]:
    clauses = [clean(c).removeprefix("请") for c in re.split(r"[，,。；;！!？?]", text) if clean(c)]
    updates = {}
    all_commands = bool(clauses)
    for clause in clauses:
        if clause in DIRECTIVES:
            updates.update(DIRECTIVES[clause])
        else:
            all_commands = False
    return updates, all_commands


def quiet_command(text: str) -> bool:
    first = clean(re.split(r"[，,。；;！!]", text, maxsplit=1)[0]).removeprefix("请")
    return first in {"我想安静一会儿", "我想安静一下", "我需要安静一会儿", "我想静一静", "安静一下", "安静一会儿", "别打扰我", "不要打扰我", "进入安静模式"}


def wake_command(text: str) -> tuple[bool, str]:
    stripped = clean(text)
    wake_words = {"小糯", "解除安静", "解除安静模式", "退出安静模式", "恢复互动", "继续聊天", "你可以说话了"}
    if stripped in wake_words:
        return True, ""
    # ASR commonly drops the comma/space after the name. Strip the name even
    # without a separator so "小糯我想安静一会儿" still reaches quiet_command.
    if stripped.startswith("小糯"):
        remaining = stripped[2:].lstrip("，,：: \t")
        if not remaining or remaining in wake_words:
            return True, ""
        return True, remaining
    return False, text
