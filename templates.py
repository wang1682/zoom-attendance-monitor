"""
templates.py — Telegram 推送模板引擎
从 brand.json 加载 telegram_templates，提供 format() 方法
"""
import json
from pathlib import Path

BASE = Path(__file__).parent

with open(BASE / "brand.json") as f:
    _BRAND = json.load(f)

_TEMPLATES = _BRAND.get("telegram_templates", {})

_HEADER_EMOJI = _BRAND.get("header_emoji", "📊")


def render(template_name: str, **kwargs) -> str:
    """渲染模板，返回纯文本。不存在的模板返回空字符串。"""
    tmpl = _TEMPLATES.get(template_name)
    if tmpl is None:
        return ""
    # 注入通用变量
    kwargs.setdefault("header_emoji", _HEADER_EMOJI)
    kwargs.setdefault("app_name", _BRAND.get("app_name_zh", "Zoom Monitor"))
    # 安全渲染：只传模板需要的参数，缺少的参数填空字符串
    import re
    needed = set(re.findall(r'\{(\w+)\}', tmpl))
    safe_kw = {k: (kwargs.get(k) if k in kwargs else '') for k in needed}
    try:
        return tmpl.format(**safe_kw)
    except KeyError as e:
        return f"[模板渲染错误: 缺少参数 {e}]"


def list_templates() -> dict:
    """列出所有可用模板名"""
    return {k: v for k, v in _TEMPLATES.items()}
