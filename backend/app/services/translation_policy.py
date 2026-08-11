"""将任务级语言规则转换为 LLM 可执行的翻译约束。"""

from collections.abc import Iterable, Mapping
from typing import Any


LANGUAGE_NAMES: dict[str, str] = {
    "auto": "自动识别的所有源语言",
    "zh-CN": "简体中文",
    "en": "英语",
    "ja": "日语",
    "ko": "韩语",
    "de": "德语",
    "fr": "法语",
}


def build_translation_instruction(
    policy: Any,
    glossary_terms: Iterable[Mapping[str, str]] = (),
) -> str:
    """生成面向大模型的文档翻译指令。

    Args:
        policy: 当前任务的语言方向及内容保留策略。
        glossary_terms: 任务创建时冻结的术语原文与标准译文。

    Returns:
        明确要求翻译所有可翻译自然语言的中文指令。
    """

    source_name = LANGUAGE_NAMES[policy.source_language.value]
    target_name = LANGUAGE_NAMES[policy.target_language.value]
    lines = [
        "你是专业 PDF 本地化译者。",
        f"输入语言范围：{source_name}。输出语言：{target_name}。",
    ]

    if policy.translate_all_translatable_text:
        lines.extend([
            "必须翻译文档中所有可翻译的自然语言内容，不因原文是英文、日文或其他语言而跳过。",
            "翻译范围包括：正文、标题、表头、表格单元格、图表标题、图例、图表标签、图片注释、脚注、页眉和页脚。",
            "完成后不得遗留可翻译的源语言自然语言；若原图文字无法自动覆盖，必须将其列入视觉质检待处理项。",
        ])

    preserved_categories: list[str] = []
    if policy.preserve_proper_nouns:
        preserved_categories.append("确认的公司、品牌、机构、人名和地名")
    if policy.preserve_glossary_terms:
        preserved_categories.append("术语库命中项")
    if policy.preserve_models_and_standards:
        preserved_categories.append("产品型号、化学式、标准号、数值和单位")
    if preserved_categories:
        lines.append(f"仅保留以下内容的原写法：{'；'.join(preserved_categories)}。其余内容应翻译。")
    if policy.protected_terms:
        lines.append(f"本任务额外保留词：{'、'.join(policy.protected_terms)}。")
    glossary_pairs = [
        (
            str(term.get("source_text", "")).strip(),
            str(term.get("target_text", "")).strip(),
        )
        for term in glossary_terms
        if str(term.get("source_text", "")).strip()
        and str(term.get("target_text", "")).strip()
    ]
    if glossary_pairs:
        lines.append(
            "本任务术语快照（必须逐项使用标准译文）："
            + "；".join(f"{source} → {target}" for source, target in glossary_pairs)
            + "。"
        )

    lines.extend([
        "若“保留专有名词”已开启，表格中的完整公司、品牌或机构名称应整体保留官方写法，包括 Company、Corporation、Inc.、Ltd.、Co., Ltd. 等名称后缀；只有脱离专名、独立承担栏目语义的组织类型词才翻译。地址中的国家、城市和普通方位词应翻译。",
        "不要把未经确认的普通英文、日文或其他外语词误判为术语；应优先翻译为目标语言。",
        "保持数字、百分比、单位、表格行列关系和版面定位信息不变。",
    ])
    return "\n".join(lines)
