"""定义与具体 PDF 解析、渲染实现无关的页面中间模型。"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PdfTextBlock:
    """一段可独立翻译并回写到原坐标的文本。"""

    id: str
    page_index: int
    source_text: str
    bbox: tuple[float, float, float, float]
    render_bbox: tuple[float, float, float, float]
    font_size: float
    alignment: str
    background_rgb: tuple[float, float, float]
    text_rgb: tuple[float, float, float]
    source_type: str = "native"
    mask_bbox: tuple[float, float, float, float] | None = None
    mask_image_png: bytes | None = None
    font_name: str = ""
    font_weight: int = 400
    rotation: int = 0
    region_type: str = "body"
    table_cell: tuple[float, float, float, float] | None = None
    reading_order: int = 0
    fragment_confidence: float = 1.0
    preserve_reason: str | None = None
    geometry_fill_ratio: float = 1.0

    @property
    def is_translatable(self) -> bool:
        """返回文本块是否适合自动翻译和覆盖。"""

        return self.preserve_reason is None and self.fragment_confidence >= 0.55


@dataclass(frozen=True)
class PdfImageRegion:
    """需要视觉模型识别文字的嵌入图片区域。"""

    id: str
    page_index: int
    bbox: tuple[float, float, float, float]
    image_png: bytes


@dataclass(frozen=True)
class PdfLayoutDocument:
    """包含页面尺寸和可翻译文本块的 PDF 中间模型。"""

    page_sizes: tuple[tuple[float, float], ...]
    blocks: tuple[PdfTextBlock, ...]
    source_type: str
    image_regions: tuple[PdfImageRegion, ...] = ()
    native_character_count: int = 0
    covered_native_character_count: int = 0

    @property
    def character_count(self) -> int:
        """返回全部文本块的非空白字符数。"""

        return sum(
            len(re.sub(r"\s+", "", block.source_text))
            for block in self.blocks
        )

    @property
    def has_usable_native_text(self) -> bool:
        """返回原生文字数量和坐标覆盖率是否均达到要求。"""

        return (
            self.source_type == "native"
            and self.character_count >= 8
            and self.native_text_coverage >= 0.98
        )

    @property
    def native_text_coverage(self) -> float:
        """返回可见原生字符被翻译文本块覆盖的比例。"""

        if self.native_character_count <= 0:
            return 0.0
        return min(
            1.0,
            self.covered_native_character_count / self.native_character_count,
        )

    @property
    def unsafe_block_count(self) -> int:
        """返回因碎片或旋转结构而不宜自动覆盖的文本块数。"""

        return sum(not block.is_translatable for block in self.blocks)

    @property
    def table_block_count(self) -> int:
        """返回位于表格单元格中的文本块数。"""

        return sum(block.region_type == "table" for block in self.blocks)

    @property
    def image_table_block_count(self) -> int:
        """返回从嵌入图片表格恢复出的单元格文本块数。"""

        return sum(block.source_type == "image-table" for block in self.blocks)

    @property
    def requires_structural_analysis(self) -> bool:
        """返回智能路由是否应补充 MinerU 结构解析。"""

        return (
            not self.has_usable_native_text
            or self.unsafe_block_count > 0
            or self.table_block_count >= 4
            or bool(self.image_regions)
        )


@dataclass(frozen=True)
class LayoutRenderReport:
    """原版式回写后的结构统计。"""

    page_count: int
    block_count: int
    replaced_block_count: int
    overflow_block_count: int
    source_type: str
    replaced_block_ids: tuple[str, ...] = ()
    overflow_block_ids: tuple[str, ...] = ()
    minimum_font_block_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class LayoutQualityIssue:
    """自动版式质检发现的单个结构问题。"""

    code: str
    severity: str
    message: str
    page_index: int | None = None
    block_id: str | None = None
    stage: str = "validation"

    def as_dict(self) -> dict[str, object]:
        """转换为持久化质检问题所需字典。"""

        return {
            "stage": self.stage,
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "page_index": self.page_index,
            "block_id": self.block_id,
        }


@dataclass(frozen=True)
class AutomatedQualityReport:
    """全页结构和视觉差异质检结果。"""

    page_count: int
    rendered_page_count: int
    issues: tuple[LayoutQualityIssue, ...]
    maximum_non_text_change_ratio: float = 0.0

    @property
    def passed(self) -> bool:
        """返回是否不存在阻止正式交付的问题。"""

        return not any(issue.severity == "error" for issue in self.issues)
