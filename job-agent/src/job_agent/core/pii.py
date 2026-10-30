"""简历脱敏：发给模型前把个人信息换成占位符，拿到结果后再换回来。

只做确定性的正则替换，不靠模型判断（模型可能漏掉，而且已经发出去就晚了）。
"""
import re

PATTERNS = {
    "EMAIL": r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+",
    "ID": r"(?<!\d)\d{17}[\dXx](?!\d)",       # 身份证号（先于手机号匹配）
    "PHONE": r"(?<!\d)1[3-9]\d{9}(?!\d)",     # 大陆手机号
}
NAME_PATTERN = r"姓\s*名\s*[:：]?\s*([一-龥]{2,4})"


class Masker:
    def __init__(self, names: list[str] | None = None):
        self.names = list(names or [])
        self.mapping: dict[str, str] = {}  # 占位符 → 原文

    def _placeholder(self, kind: str, value: str) -> str:
        for ph, v in self.mapping.items():
            if v == value:
                return ph
        ph = f"[{kind}_{sum(k.startswith(f'[{kind}_') for k in self.mapping) + 1}]"
        self.mapping[ph] = value
        return ph

    def mask(self, text: str) -> str:
        # 之前从简历里识别出的名字也要替换：网页上人工修改的改写文本里可能写了真名
        known = [v for ph, v in self.mapping.items() if ph.startswith("[NAME_")]
        names = self.names + known + re.findall(NAME_PATTERN, text)
        for kind, pattern in PATTERNS.items():
            text = re.sub(pattern, lambda m, k=kind: self._placeholder(k, m.group()), text)
        for name in sorted(set(names), key=len, reverse=True):  # 长名字先替换，避免被短名字截断
            text = text.replace(name, self._placeholder("NAME", name))
        return text

    def unmask(self, text: str) -> str:
        for ph, value in self.mapping.items():
            text = text.replace(ph, value)
        return text
