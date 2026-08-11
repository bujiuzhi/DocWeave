"""定义任务编排与外部实现之间的稳定接口和结果对象。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from app.domain.pdf import (
    AutomatedQualityReport,
    LayoutRenderReport,
    PdfImageRegion,
    PdfLayoutDocument,
    PdfTextBlock,
)
from app.models import LocalizationJob


@dataclass(frozen=True)
class TokenUsage:
    """单次或多次大模型调用累计的 Token 用量。"""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    available: bool = False

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        """合并两次大模型调用的 Token 用量。"""

        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            available=self.available or other.available,
        )


@dataclass(frozen=True)
class LlmTextResult:
    """大模型文本结果及其 Token 用量。"""

    text: str
    usage: TokenUsage = TokenUsage()


@dataclass(frozen=True)
class DocumentParseResult:
    """解析器返回的语义文本和带坐标内容列表。"""

    markdown: str
    content_list: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class VisualResidual:
    """最终 PDF 页面中仍可见、可定位且可自动修复的源语言文字。"""

    region: PdfImageRegion
    source_text: str
    translated_text: str
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True)
class LayoutTranslationResult:
    """按文本块 ID 保存的译文和累计 Token 用量。"""

    translations: dict[str, str]
    usage: TokenUsage = TokenUsage()
    additional_blocks: tuple[PdfTextBlock, ...] = ()
    memory_hit_count: int = 0


ParseDocument = Callable[[Path, str], DocumentParseResult | str]
TranslateDocument = Callable[
    [PdfLayoutDocument, LocalizationJob],
    LayoutTranslationResult | dict[str, str],
]
TranslateFileName = Callable[[LocalizationJob], LlmTextResult | str]


class RenderDocument(Protocol):
    """原版式 PDF 渲染实现必须满足的调用接口。"""

    def __call__(
        self,
        source_path: Path,
        result_path: Path,
        layout: PdfLayoutDocument,
        translations: dict[str, str],
        vision_mask_padding: float = 12.0,
    ) -> LayoutRenderReport:
        """按页面中间模型生成原版式译文 PDF。"""


class ValidateDocument(Protocol):
    """PDF 全页质量校验实现必须满足的调用接口。"""

    def __call__(
        self,
        source_path: Path,
        result_path: Path,
        layout: PdfLayoutDocument,
        render_report: LayoutRenderReport,
        *,
        resolution: int = 96,
    ) -> AutomatedQualityReport:
        """校验最终 PDF 的结构、可读性和非文本内容保持情况。"""
