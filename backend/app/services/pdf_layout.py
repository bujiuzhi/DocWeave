"""提取 PDF 文本块并在原页面坐标中写回译文。"""

from __future__ import annotations

import html
import io
import logging
import math
import re
import statistics
import tempfile
from collections import Counter
from dataclasses import dataclass, replace as dataclass_replace
from pathlib import Path
from threading import Lock
from typing import Iterable

import pdfplumber
import pymupdf
from PIL import Image, ImageChops, ImageDraw
from pypdf import PdfReader, PdfWriter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

from app.domain.pdf import (
    AutomatedQualityReport,
    LayoutQualityIssue,
    LayoutRenderReport,
    PdfImageRegion,
    PdfLayoutDocument,
    PdfTextBlock,
)
from app.services.image_table import recover_image_table_blocks

LOGGER = logging.getLogger(__name__)
DEFAULT_FONT_NAME = "STSong-Light"
DEFAULT_BOLD_FONT_NAME = "STSong-Light"
MINIMUM_FONT_SIZE = 6.0
MINIMUM_BODY_FONT_SIZE = 7.0
MINIMUM_TABLE_FONT_SIZE = 6.5
MINIMUM_IMAGE_TABLE_FONT_SIZE = 4.5
MINIMUM_CHART_FONT_SIZE = 6.0
MINIMUM_IMAGE_AREA_RATIO = 0.01
MAXIMUM_NON_TEXT_CHANGE_RATIO = 0.002
MAXIMUM_COLORED_GRAPHIC_LOSS_RATIO = 0.03
MAXIMUM_DARK_GRAPHIC_LOSS_RATIO = 0.05
COARSE_IMAGE_TABLE_PRESERVE_REASON = (
    "MinerU 仅提供嵌入图片整表坐标，缺少可安全回写的单元格坐标"
)
MINIMUM_COARSE_IMAGE_TABLE_AREA_RATIO = 0.18
MINIMUM_COARSE_IMAGE_TABLE_CHARACTERS = 80
CJK_REGULAR_FONT_PATHS = (
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 2),
    ("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc", 2),
)
CJK_BOLD_FONT_PATHS = (
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 2),
    ("/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc", 2),
)
_PDF_EXTRACTION_LOCK = Lock()


@dataclass(frozen=True)
class _NativeTextBox:
    """尚未按段落或表格单元格合并的原生文字框。"""

    text: str
    bbox: tuple[float, float, float, float]
    characters: tuple[dict[str, object], ...]
    table_cell: tuple[float, float, float, float] | None
    source_type: str = "native"
    rotation: int = 0


def extract_pdf_layout(
    source_path: Path,
    mineru_content_list: Iterable[dict[str, object]] = (),
) -> PdfLayoutDocument:
    """优先提取原生文本坐标，扫描件则使用 MinerU 区块坐标。

    Args:
        source_path: 原始 PDF 路径。
        mineru_content_list: MinerU 返回的结构化内容列表。

    Returns:
        可供逐块翻译和原坐标回写的文档中间模型。
    """

    native_layout = extract_native_pdf_layout(source_path)
    if native_layout.has_usable_native_text:
        return native_layout

    mineru_layout = _extract_mineru_layout(
        native_layout.page_sizes,
        mineru_content_list,
        native_layout.image_regions,
    )
    if mineru_layout.blocks:
        return mineru_layout
    raise RuntimeError("PDF 中未提取到可翻译的文本区块")


def extract_mineru_pdf_layout(
    source_path: Path,
    mineru_content_list: Iterable[dict[str, object]],
    native_layout: PdfLayoutDocument | None = None,
) -> PdfLayoutDocument:
    """将 MinerU 结果与原生坐标融合，优先保留原 PDF 精确字形位置。

    Args:
        source_path: 原始 PDF。
        mineru_content_list: MinerU 带页码、坐标和结构类型的内容列表。
        native_layout: 已完成的原生坐标分析；提供后可避免重复解析。

    Returns:
        原生坐标与 MinerU 结构标签融合后的页面中间表示。
    """

    resolved_native_layout = native_layout or extract_native_pdf_layout(source_path)
    mineru_layout = _extract_mineru_layout(
        resolved_native_layout.page_sizes,
        mineru_content_list,
        resolved_native_layout.image_regions,
    )
    if not mineru_layout.blocks:
        if resolved_native_layout.blocks:
            return resolved_native_layout
        raise RuntimeError("MinerU 未返回可定位的文本区块")
    return fuse_pdf_layouts(resolved_native_layout, mineru_layout)


def should_use_mineru(layout: PdfLayoutDocument, strategy: str) -> bool:
    """根据任务策略和页面复杂度决定是否调用 MinerU。"""

    if strategy == "MinerU 结构解析":
        return True
    if strategy == "原生文字优先":
        return not layout.has_usable_native_text
    return layout.requires_structural_analysis


def fuse_pdf_layouts(
    native_layout: PdfLayoutDocument,
    mineru_layout: PdfLayoutDocument,
) -> PdfLayoutDocument:
    """融合原生精确坐标与 MinerU 语义结构，避免整页重新排版。

    原生文字页只使用 MinerU 补充区域类型，禁止把 MinerU 的粗粒度文本框
    追加到渲染层。MinerU 的框经常覆盖整张表或整幅图，即使文本只有一个
    ``JMS`` 标识，实体背景遮罩也会擦除框内网格线。只有两类坐标可以新增：

    1. 页面没有足够原生文字坐标时的 MinerU 兜底块；
    2. 已从嵌入图片真实网格恢复出的 ``image-table`` 单元格块。
    """

    safe_mineru_blocks = _preserve_coarse_image_table_blocks(
        mineru_layout.blocks,
        native_layout.image_regions,
    )
    if not native_layout.blocks:
        return PdfLayoutDocument(
            page_sizes=mineru_layout.page_sizes,
            blocks=safe_mineru_blocks,
            source_type="mineru",
            image_regions=native_layout.image_regions,
            native_character_count=native_layout.native_character_count,
            covered_native_character_count=native_layout.covered_native_character_count,
        )

    mineru_by_page: dict[int, list[PdfTextBlock]] = {}
    for mineru_block in safe_mineru_blocks:
        mineru_by_page.setdefault(mineru_block.page_index, []).append(mineru_block)

    fused_blocks: list[PdfTextBlock] = []
    for native_block in native_layout.blocks:
        overlapping_blocks = [
            mineru_block
            for mineru_block in mineru_by_page.get(native_block.page_index, [])
            if _bbox_overlap_ratio(native_block.bbox, mineru_block.bbox) >= 0.25
        ]
        region_type = native_block.region_type
        if overlapping_blocks:
            region_type = max(
                overlapping_blocks,
                key=lambda block: _bbox_overlap_ratio(
                    native_block.bbox,
                    block.bbox,
                ),
            ).region_type
        fused_blocks.append(
            dataclass_replace(
                native_block,
                region_type=region_type,
            )
        )

    existing_by_page: dict[int, list[tuple[float, float, float, float]]] = {}
    native_blocks_by_page: dict[int, list[PdfTextBlock]] = {}
    for block in fused_blocks:
        existing_by_page.setdefault(block.page_index, []).append(block.bbox)
        native_blocks_by_page.setdefault(block.page_index, []).append(block)
    for mineru_block in safe_mineru_blocks:
        if any(
            _bbox_overlap_ratio(mineru_block.bbox, bbox) >= 0.4
            for bbox in existing_by_page.get(mineru_block.page_index, [])
        ):
            continue
        page_native_blocks = native_blocks_by_page.get(
            mineru_block.page_index,
            [],
        )
        if (
            mineru_block.source_type != "image-table"
            and _has_authoritative_native_page(page_native_blocks)
        ):
            LOGGER.debug(
                "忽略原生文字页上的 MinerU 粗框：block=%s page=%s",
                mineru_block.id,
                mineru_block.page_index + 1,
            )
            continue
        fused_blocks.append(mineru_block)

    return PdfLayoutDocument(
        page_sizes=native_layout.page_sizes,
        blocks=tuple(
            sorted(
                fused_blocks,
                key=lambda block: (
                    block.page_index,
                    block.reading_order,
                    block.bbox[1],
                    block.bbox[0],
                ),
            )
        ),
        source_type="hybrid",
        image_regions=native_layout.image_regions,
        native_character_count=native_layout.native_character_count,
        covered_native_character_count=native_layout.covered_native_character_count,
    )


def _has_authoritative_native_page(blocks: list[PdfTextBlock]) -> bool:
    """判断页面是否已有足够细粒度的原生坐标可作为唯一渲染依据。

    少量页眉、页脚不应阻止扫描页使用 MinerU 兜底；达到三个独立原生块且
    含有可见文字时，说明页面已有逐块坐标，继续追加 MinerU 粗框的风险大于
    收益。

    Args:
        blocks: 同一页已提取的原生文字块。

    Returns:
        页面是否应禁止 MinerU 粗框进入覆盖渲染。
    """

    visible_character_count = sum(
        len(re.sub(r"\s+", "", block.source_text))
        for block in blocks
    )
    return len(blocks) >= 3 and visible_character_count >= 12


def _preserve_coarse_image_table_blocks(
    mineru_blocks: tuple[PdfTextBlock, ...],
    image_regions: tuple[PdfImageRegion, ...],
) -> tuple[PdfTextBlock, ...]:
    """禁止把仅有整表坐标的 MinerU 图片表格作为普通段落覆盖。

    Args:
        mineru_blocks: MinerU 提取的结构文本块。
        image_regions: 原 PDF 的嵌入图片区域。

    Returns:
        已标记不宜自动覆盖的 MinerU 文本块。
    """

    regions_by_page: dict[int, list[PdfImageRegion]] = {}
    for region in image_regions:
        regions_by_page.setdefault(region.page_index, []).append(region)

    safe_blocks: list[PdfTextBlock] = []
    for block in mineru_blocks:
        matching_region = next(
            (
                region
                for region in regions_by_page.get(block.page_index, [])
                if _is_coarse_image_table_block(block, region)
            ),
            None,
        )
        if matching_region is None:
            safe_blocks.append(block)
            continue
        LOGGER.warning(
            "保留密集图片表格原图：block=%s region=%s page=%s",
            block.id,
            matching_region.id,
            block.page_index + 1,
        )
        safe_blocks.append(
            dataclass_replace(
                block,
                preserve_reason=COARSE_IMAGE_TABLE_PRESERVE_REASON,
            )
        )
    return tuple(safe_blocks)


def _is_coarse_image_table_block(
    block: PdfTextBlock,
    region: PdfImageRegion,
) -> bool:
    """判断 MinerU 表格块是否只有整张嵌入图片级别的粗粒度坐标。"""

    if block.source_type != "mineru" or block.region_type != "table":
        return False
    if block.page_index != region.page_index:
        return False
    compact_text = re.sub(r"\s+", "", block.source_text)
    if (
        len(compact_text) < MINIMUM_COARSE_IMAGE_TABLE_CHARACTERS
        and block.source_text.count("\n") < 3
    ):
        return False
    block_area = max(
        0.1,
        (block.bbox[2] - block.bbox[0]) * (block.bbox[3] - block.bbox[1]),
    )
    region_area = max(
        0.1,
        (region.bbox[2] - region.bbox[0]) * (region.bbox[3] - region.bbox[1]),
    )
    return (
        _bbox_overlap_ratio(block.bbox, region.bbox) >= 0.8
        and block_area / region_area >= MINIMUM_COARSE_IMAGE_TABLE_AREA_RATIO
    )


def extract_native_pdf_layout(source_path: Path) -> PdfLayoutDocument:
    """串行提取原 PDF 文字坐标，避免底层渲染库并发导致进程退出。"""

    with _PDF_EXTRACTION_LOCK:
        return _extract_native_layout(source_path)


def render_pdf_image_regions(
    pdf_path: Path,
    regions: tuple[PdfImageRegion, ...],
    resolution: int = 144,
) -> tuple[PdfImageRegion, ...]:
    """渲染最终 PDF 页面并按原图片区域裁剪，确保质检看到覆盖层。

    Args:
        pdf_path: 已完成文字覆盖的结果 PDF。
        regions: 原始文档中需要复检的图片页面坐标。
        resolution: 页面渲染分辨率。

    Returns:
        ID、页码和页面坐标不变，图片内容来自最终合成页面的区域。
    """

    with _PDF_EXTRACTION_LOCK:
        return _render_pdf_image_regions(pdf_path, regions, resolution)


def _render_pdf_image_regions(
    pdf_path: Path,
    regions: tuple[PdfImageRegion, ...],
    resolution: int,
) -> tuple[PdfImageRegion, ...]:
    """在串行锁内执行最终页面渲染与区域裁剪。"""

    if not regions:
        return ()
    regions_by_page: dict[int, list[PdfImageRegion]] = {}
    for region in regions:
        regions_by_page.setdefault(region.page_index, []).append(region)

    rendered_regions: list[PdfImageRegion] = []
    with pdfplumber.open(pdf_path) as document:
        for page_index, page_regions in regions_by_page.items():
            if page_index < 0 or page_index >= len(document.pages):
                raise RuntimeError(
                    f"图片质检区域页码越界：{page_index + 1}"
                )
            page = document.pages[page_index]
            page_image = page.to_image(resolution=resolution).original.convert(
                "RGB"
            )
            scale_x = page_image.width / float(page.width)
            scale_y = page_image.height / float(page.height)
            for region in page_regions:
                x0, top, x1, bottom = region.bbox
                crop_box = (
                    max(0, round(x0 * scale_x)),
                    max(0, round(top * scale_y)),
                    min(page_image.width, round(x1 * scale_x)),
                    min(page_image.height, round(bottom * scale_y)),
                )
                if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
                    raise RuntimeError(
                        f"图片质检区域坐标无效：{region.id}"
                    )
                crop = page_image.crop(crop_box)
                output = io.BytesIO()
                crop.save(output, format="PNG", optimize=True)
                rendered_regions.append(
                    PdfImageRegion(
                        id=region.id,
                        page_index=region.page_index,
                        bbox=region.bbox,
                        image_png=output.getvalue(),
                    )
                )
    return tuple(rendered_regions)


def _register_translation_fonts() -> None:
    """优先注册可子集嵌入的中文字体，缺失时回退内置宋体。"""

    global DEFAULT_FONT_NAME, DEFAULT_BOLD_FONT_NAME
    if DEFAULT_FONT_NAME != "STSong-Light":
        return
    regular_font = next(
        (
            (Path(path), subfont_index)
            for path, subfont_index in CJK_REGULAR_FONT_PATHS
            if Path(path).is_file()
        ),
        None,
    )
    bold_font = next(
        (
            (Path(path), subfont_index)
            for path, subfont_index in CJK_BOLD_FONT_PATHS
            if Path(path).is_file()
        ),
        None,
    )
    if regular_font is not None:
        regular_path, regular_subfont_index = regular_font
        try:
            pdfmetrics.registerFont(
                TTFont(
                    "DocWeaveCJK-Regular",
                    str(regular_path),
                    subfontIndex=regular_subfont_index,
                )
            )
            DEFAULT_FONT_NAME = "DocWeaveCJK-Regular"
        except Exception as error:
            LOGGER.warning("中文常规字体注册失败，回退内置字体：%s", error)
    if bold_font is not None:
        bold_path, bold_subfont_index = bold_font
        try:
            pdfmetrics.registerFont(
                TTFont(
                    "DocWeaveCJK-Bold",
                    str(bold_path),
                    subfontIndex=bold_subfont_index,
                )
            )
            DEFAULT_BOLD_FONT_NAME = "DocWeaveCJK-Bold"
        except Exception as error:
            LOGGER.warning("中文粗体字体注册失败：%s", error)
    if (
        DEFAULT_FONT_NAME == "STSong-Light"
        or DEFAULT_BOLD_FONT_NAME == "STSong-Light"
    ):
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    if DEFAULT_BOLD_FONT_NAME == "STSong-Light":
        DEFAULT_BOLD_FONT_NAME = DEFAULT_FONT_NAME


def render_translated_pdf(
    source_path: Path,
    result_path: Path,
    layout: PdfLayoutDocument,
    translations: dict[str, str],
    vision_mask_padding: float = 12.0,
) -> LayoutRenderReport:
    """以原 PDF 为底稿遮盖原文字，并在原坐标写入译文。

    Args:
        source_path: 原始 PDF。
        result_path: 输出 PDF。
        layout: 页面尺寸和文本区块。
        translations: 文本块 ID 到译文的映射。
        vision_mask_padding: 图片文字遮盖框外扩点数，用于视觉质检后的定向修复。

    Returns:
        页面数、替换数量和溢出数量。
    """

    result_path.parent.mkdir(parents=True, exist_ok=True)
    _register_translation_fonts()
    overlay_stream = io.BytesIO()
    overlay_canvas = canvas.Canvas(overlay_stream, pagesize=(1, 1))
    replaced_block_count = 0
    overflow_block_count = 0
    overflow_block_ids: list[str] = []
    minimum_font_block_ids: list[str] = []
    replaced_block_ids: list[str] = []
    native_redactions_by_page: dict[
        int,
        list[tuple[float, float, float, float]],
    ] = {}

    blocks_by_page: dict[int, list[PdfTextBlock]] = {}
    for block in layout.blocks:
        blocks_by_page.setdefault(block.page_index, []).append(block)

    for page_index, (page_width, page_height) in enumerate(layout.page_sizes):
        overlay_canvas.setPageSize((page_width, page_height))
        page_blocks = blocks_by_page.get(page_index, [])
        render_items: list[tuple[PdfTextBlock, str]] = []
        for block in page_blocks:
            translated_text = _normalize_translation(translations.get(block.id, ""))
            if not translated_text:
                raise RuntimeError(f"文本块 {block.id} 缺少有效译文")
            if translated_text == _normalize_translation(block.source_text):
                continue
            if block.source_type in {"native", "native-recovered"}:
                native_redactions_by_page.setdefault(page_index, []).append(
                    block.bbox
                )
            else:
                _draw_original_text_mask(
                    overlay_canvas,
                    block,
                    page_height,
                    vision_mask_padding,
                )
            render_items.append((block, translated_text))

        for block, translated_text in render_items:
            did_overflow, used_minimum_font = _draw_translated_text(
                overlay_canvas,
                block,
                translated_text,
                page_height,
            )
            replaced_block_count += 1
            replaced_block_ids.append(block.id)
            overflow_block_count += int(did_overflow)
            if did_overflow:
                overflow_block_ids.append(block.id)
            if used_minimum_font:
                minimum_font_block_ids.append(block.id)
        overlay_canvas.showPage()
    overlay_canvas.save()
    overlay_stream.seek(0)

    overlay_reader = PdfReader(overlay_stream)
    with tempfile.TemporaryDirectory(prefix="docweave-redacted-") as temporary_dir:
        redacted_source_path = Path(temporary_dir) / "text-redacted.pdf"
        _write_text_redacted_pdf(
            source_path,
            redacted_source_path,
            native_redactions_by_page,
        )
        source_reader = PdfReader(str(redacted_source_path))
        writer = PdfWriter(clone_from=str(redacted_source_path))
        writer.pdf_header = source_reader.pdf_header
        if len(writer.pages) != len(overlay_reader.pages):
            raise RuntimeError("原 PDF 与译文覆盖层页数不一致")

        for page_index, source_page in enumerate(writer.pages):
            source_page.merge_page(overlay_reader.pages[page_index])
        with result_path.open("wb") as output:
            writer.write(output)

    _validate_preserved_pdf(source_path, result_path)
    return LayoutRenderReport(
        page_count=len(layout.page_sizes),
        block_count=len(layout.blocks),
        replaced_block_count=replaced_block_count,
        overflow_block_count=overflow_block_count,
        source_type=layout.source_type,
        replaced_block_ids=tuple(replaced_block_ids),
        overflow_block_ids=tuple(overflow_block_ids),
        minimum_font_block_ids=tuple(minimum_font_block_ids),
    )


def _write_text_redacted_pdf(
    source_path: Path,
    destination_path: Path,
    redactions_by_page: dict[
        int,
        list[tuple[float, float, float, float]],
    ],
) -> None:
    """透明删除指定坐标中的原生文字，不改动图片和矢量图形。

    Args:
        source_path: 原始 PDF 路径。
        destination_path: 删除原文字后的临时 PDF 路径。
        redactions_by_page: 页索引到待删除文字框的映射。

    Returns:
        无返回值，处理结果写入 ``destination_path``。

    Raises:
        RuntimeError: PDF 文字删除失败时抛出，禁止回退到有损矩形遮盖。
    """

    try:
        document = pymupdf.open(source_path)
        try:
            for page_index, redaction_boxes in redactions_by_page.items():
                if page_index < 0 or page_index >= document.page_count:
                    raise RuntimeError(f"文字删除页索引越界：{page_index}")
                page = document[page_index]
                page_rectangle = page.rect
                valid_redaction_count = 0
                for x0, top, x1, bottom in redaction_boxes:
                    redaction_rectangle = pymupdf.Rect(
                        max(page_rectangle.x0, x0),
                        max(page_rectangle.y0, top),
                        min(page_rectangle.x1, x1),
                        min(page_rectangle.y1, bottom),
                    )
                    if redaction_rectangle.is_empty:
                        continue
                    page.add_redact_annot(
                        redaction_rectangle,
                        fill=False,
                        cross_out=False,
                    )
                    valid_redaction_count += 1
                if valid_redaction_count:
                    page.apply_redactions(
                        images=0,
                        graphics=0,
                        text=0,
                    )
            document.save(
                destination_path,
                garbage=4,
                deflate=True,
            )
        finally:
            document.close()
    except Exception as error:
        raise RuntimeError(
            f"原生文字透明删除失败，已停止生成以避免有损遮盖：{error}"
        ) from error


def build_full_page_quality_regions(
    layout: PdfLayoutDocument,
) -> tuple[PdfImageRegion, ...]:
    """为每一页构建视觉复检区域，确保质检不再只覆盖嵌入图片。"""

    placeholder = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(placeholder, format="PNG")
    placeholder_png = placeholder.getvalue()
    return tuple(
        PdfImageRegion(
            id=f"p{page_index + 1:04d}-full",
            page_index=page_index,
            bbox=(0.0, 0.0, page_width, page_height),
            image_png=placeholder_png,
        )
        for page_index, (page_width, page_height) in enumerate(layout.page_sizes)
    )


def validate_rendered_pdf(
    source_path: Path,
    result_path: Path,
    layout: PdfLayoutDocument,
    render_report: LayoutRenderReport,
    *,
    resolution: int = 96,
) -> AutomatedQualityReport:
    """渲染并检查全部页面的可读性和非文本内容保持情况。

    自动检查页数、页面尺寸、字号下限、溢出、全页可渲染性，以及译文
    覆盖区域之外的像素变化比例。任何错误均应进入 ``needs_review``，
    作为严格的结构化复核记录，同时保留已生成结果供正常下载。
    """

    issues: list[LayoutQualityIssue] = []
    for block_id in render_report.overflow_block_ids:
        block = next(
            (candidate for candidate in layout.blocks if candidate.id == block_id),
            None,
        )
        issues.append(
            LayoutQualityIssue(
                code="text_overflow",
                severity="error",
                message="译文超过原区域容量，已裁切到区域内，禁止自动交付",
                page_index=block.page_index if block else None,
                block_id=block_id,
            )
        )
    for block in layout.blocks:
        if (
            block.preserve_reason
            and block.preserve_reason
            != "图表中的企业、品牌或产品名称按原字形保留"
        ):
            issues.append(
                LayoutQualityIssue(
                    code="unstable_source_fragment",
                    severity="warning",
                    message=f"原文字形不稳定，自动保留原字形：{block.preserve_reason}",
                    page_index=block.page_index,
                    block_id=block.id,
                )
            )
        page_width, page_height = layout.page_sizes[block.page_index]
        block_area_ratio = (
            max(0.0, block.bbox[2] - block.bbox[0])
            * max(0.0, block.bbox[3] - block.bbox[1])
            / max(1.0, page_width * page_height)
        )
        if (
            block.id in render_report.replaced_block_ids
            and block_area_ratio >= 0.05
            and block.geometry_fill_ratio < 0.35
        ):
            issues.append(
                LayoutQualityIssue(
                    code="unsafe_mask_geometry",
                    severity="error",
                    message=(
                        "译文遮盖框跨越大面积空白或图形区域，"
                        "可能由离散图表标签错误合并导致"
                    ),
                    page_index=block.page_index,
                    block_id=block.id,
                )
            )
        if _is_coarse_native_text_block(
            block,
            render_report.replaced_block_ids,
        ):
            issues.append(
                LayoutQualityIssue(
                    code="coarse_native_text_block",
                    severity="error",
                    message="原生文本块跨越多行，无法保证逐行原位回填",
                    page_index=block.page_index,
                    block_id=block.id,
                )
            )
        if block.source_type == "vision" and any(
            candidate.id != block.id
            and candidate.page_index == block.page_index
            and not candidate.source_type.startswith("vision")
            and _bbox_overlap_ratio(block.bbox, candidate.bbox) >= 0.15
            for candidate in layout.blocks
        ):
            issues.append(
                LayoutQualityIssue(
                    code="duplicate_visual_overlay",
                    severity="error",
                    message="视觉补译区域与已有坐标文本重叠，可能产生叠字",
                    page_index=block.page_index,
                    block_id=block.id,
                )
            )

    maximum_change_ratio = 0.0
    rendered_page_count = 0
    try:
        _validate_preserved_pdf(source_path, result_path)
        with _PDF_EXTRACTION_LOCK, pdfplumber.open(
            source_path
        ) as source_document, pdfplumber.open(result_path) as result_document:
            for page_index, (source_page, result_page) in enumerate(
                zip(source_document.pages, result_document.pages)
            ):
                source_image = source_page.to_image(
                    resolution=resolution,
                    antialias=True,
                ).original.convert("RGB")
                result_image = result_page.to_image(
                    resolution=resolution,
                    antialias=True,
                ).original.convert("RGB")
                rendered_page_count += 1
                change_ratio = _non_text_change_ratio(
                    source_image,
                    result_image,
                    source_page.width,
                    source_page.height,
                    tuple(
                        block
                        for block in layout.blocks
                        if block.page_index == page_index
                    ),
                )
                maximum_change_ratio = max(maximum_change_ratio, change_ratio)
                if change_ratio > MAXIMUM_NON_TEXT_CHANGE_RATIO:
                    issues.append(
                        LayoutQualityIssue(
                            code="non_text_content_changed",
                            severity="error",
                            message=(
                                f"译文区域外像素变化比例 {change_ratio:.3%}，"
                                f"超过阈值 {MAXIMUM_NON_TEXT_CHANGE_RATIO:.3%}"
                            ),
                            page_index=page_index,
                        )
                    )
                colored_loss_ratio, dark_loss_ratio = _graphic_loss_ratios(
                    source_image,
                    result_image,
                )
                if (
                    colored_loss_ratio
                    > MAXIMUM_COLORED_GRAPHIC_LOSS_RATIO
                    or dark_loss_ratio > MAXIMUM_DARK_GRAPHIC_LOSS_RATIO
                ):
                    issues.append(
                        LayoutQualityIssue(
                            code="graphical_content_erased",
                            severity="error",
                            message=(
                                "检测到大面积原图形被浅色遮盖："
                                f"彩色图形损失 {colored_loss_ratio:.2%}，"
                                f"深色图形损失 {dark_loss_ratio:.2%}"
                            ),
                            page_index=page_index,
                        )
                    )
    except Exception as error:
        issues.append(
            LayoutQualityIssue(
                code="page_render_failed",
                severity="error",
                message=f"全页渲染质检失败：{error}",
            )
        )
    if rendered_page_count != len(layout.page_sizes):
        issues.append(
            LayoutQualityIssue(
                code="incomplete_page_review",
                severity="error",
                message=(
                    f"仅渲染检查 {rendered_page_count}/"
                    f"{len(layout.page_sizes)} 页"
                ),
            )
        )
    return AutomatedQualityReport(
        page_count=len(layout.page_sizes),
        rendered_page_count=rendered_page_count,
        issues=tuple(issues),
        maximum_non_text_change_ratio=maximum_change_ratio,
    )


def _is_coarse_native_text_block(
    block: PdfTextBlock,
    replaced_block_ids: tuple[str, ...],
) -> bool:
    """判断多行原生文本块是否真的存在非表格粗粒度覆盖风险。"""

    return (
        block.id in replaced_block_ids
        and block.region_type != "table"
        and block.source_type.startswith("native")
        and block.source_text.count("\n") >= 3
        and (block.bbox[3] - block.bbox[1]) >= block.font_size * 3
    )


def _non_text_change_ratio(
    source_image: Image.Image,
    result_image: Image.Image,
    page_width: float,
    page_height: float,
    blocks: tuple[PdfTextBlock, ...],
) -> float:
    """计算允许覆盖区域以外发生明显变化的像素比例。"""

    if source_image.size != result_image.size:
        return 1.0
    scale_x = source_image.width / float(page_width)
    scale_y = source_image.height / float(page_height)
    allowed_mask = Image.new("L", source_image.size, 0)
    drawer = ImageDraw.Draw(allowed_mask)
    for block in blocks:
        bbox = block.mask_bbox or block.bbox
        padding = 28.0 if block.source_type == "vision" else 4.0
        drawer.rectangle(
            (
                max(0, round((bbox[0] - padding) * scale_x)),
                max(0, round((bbox[1] - padding) * scale_y)),
                min(source_image.width, round((bbox[2] + padding) * scale_x)),
                min(source_image.height, round((bbox[3] + padding) * scale_y)),
            ),
            fill=255,
        )
    difference = ImageChops.difference(source_image, result_image).convert("L")
    changed_pixels = difference.point(lambda value: 255 if value > 18 else 0)
    outside_changes = ImageChops.multiply(
        changed_pixels,
        ImageChops.invert(allowed_mask),
    )
    histogram = outside_changes.histogram()
    return histogram[255] / max(1, source_image.width * source_image.height)


def _graphic_loss_ratios(
    source_image: Image.Image,
    result_image: Image.Image,
) -> tuple[float, float]:
    """计算原页面彩色或深色图形被浅色遮盖的页面比例。"""

    if source_image.size != result_image.size:
        return (1.0, 1.0)
    source_rgb = source_image.convert("RGB")
    result_rgb = result_image.convert("RGB")
    source_luminance = source_rgb.convert("L")
    result_luminance = result_rgb.convert("L")

    source_saturation = source_rgb.convert("HSV").getchannel("S").point(
        lambda value: 255 if value >= 45 else 0
    )
    moderate_brightness_gain = ImageChops.subtract(
        result_luminance,
        source_luminance,
    ).point(lambda value: 255 if value > 30 else 0)
    colored_loss = ImageChops.multiply(
        source_saturation,
        moderate_brightness_gain,
    )

    source_dark_pixels = source_luminance.point(
        lambda value: 255 if value < 130 else 0
    )
    strong_brightness_gain = ImageChops.subtract(
        result_luminance,
        source_luminance,
    ).point(lambda value: 255 if value > 60 else 0)
    dark_loss = ImageChops.multiply(
        source_dark_pixels,
        strong_brightness_gain,
    )

    pixel_count = max(1, source_image.width * source_image.height)
    return (
        colored_loss.histogram()[255] / pixel_count,
        dark_loss.histogram()[255] / pixel_count,
    )


def _extract_native_layout(source_path: Path) -> PdfLayoutDocument:
    """使用 pdfplumber 提取机器生成 PDF 的文本框与表格单元格。"""

    page_sizes: list[tuple[float, float]] = []
    blocks: list[PdfTextBlock] = []
    image_regions: list[PdfImageRegion] = []
    native_character_count = 0
    covered_native_character_count = 0
    laparams = {
        "line_margin": 0.45,
        "word_margin": 0.1,
        "char_margin": 1.8,
        "boxes_flow": 0.5,
        "detect_vertical": True,
    }
    try:
        with pdfplumber.open(source_path, laparams=laparams) as document:
            for page_index, page in enumerate(document.pages):
                page_width = float(page.width)
                page_height = float(page.height)
                page_sizes.append((page_width, page_height))
                text_lines = [
                    *page.objects.get("textlinehorizontal", []),
                    *page.objects.get("textlinevertical", []),
                ]
                text_boxes = text_lines or [
                    *page.objects.get("textboxhorizontal", []),
                    *page.objects.get("textboxvertical", []),
                ]
                graphical_edges = tuple(page.edges)
                visible_graphical_edges = tuple(
                    edge
                    for edge in graphical_edges
                    if _is_visible_graphical_edge(edge)
                )
                table_cells = _find_table_cells(page)
                graphical_table_edges = (
                    visible_graphical_edges
                    if _has_credible_graphical_grid(visible_graphical_edges)
                    else ()
                )
                page_image = _safe_page_image(page)
                native_text_boxes: list[_NativeTextBox] = []
                for text_box in text_boxes:
                    source_text = _normalize_source_text(
                        str(text_box.get("text", ""))
                    )
                    if not source_text:
                        continue
                    bbox = (
                        float(text_box["x0"]),
                        float(text_box["top"]),
                        float(text_box["x1"]),
                        float(text_box["bottom"]),
                    )
                    block_characters = _characters_in_bbox(page.chars, bbox)
                    table_cell = (
                        _containing_table_cell(bbox, table_cells)
                        or _infer_graphical_table_cell(
                            bbox,
                            graphical_table_edges,
                        )
                    )
                    rotation = _infer_text_rotation(source_text, bbox)
                    native_text_boxes.append(
                        _NativeTextBox(
                            text=source_text,
                            bbox=bbox,
                            characters=tuple(block_characters),
                            table_cell=table_cell,
                            rotation=rotation,
                        )
                    )

                recovered_text_boxes = _find_uncovered_word_boxes(
                    page,
                    native_text_boxes,
                    table_cells,
                    graphical_table_edges,
                )
                native_text_boxes.extend(recovered_text_boxes)
                page_block_start = len(blocks)
                for block_index, text_box_group in enumerate(
                    _group_native_text_boxes(native_text_boxes)
                ):
                    source_text = _join_layout_text(
                        [text_box.text for text_box in text_box_group]
                    )
                    bbox = _union_bbox(
                        [text_box.bbox for text_box in text_box_group]
                    )
                    block_characters = [
                        character
                        for text_box in text_box_group
                        for character in text_box.characters
                    ]
                    table_cell = text_box_group[0].table_cell
                    font_size = _dominant_font_size(block_characters, bbox)
                    font_name = _dominant_font_name(block_characters)
                    font_weight = _infer_font_weight(font_name)
                    rotation = _dominant_rotation(text_box_group)
                    render_bbox = (
                        _table_render_bbox(bbox, table_cell) if table_cell else bbox
                    )
                    alignment = _infer_alignment(
                        bbox,
                        table_cell or (0.0, 0.0, page_width, page_height),
                        source_text,
                        font_size,
                    )
                    is_recovered = any(
                        text_box.source_type == "native-recovered"
                        for text_box in text_box_group
                    )
                    region_type = _infer_region_type(
                        source_text,
                        bbox,
                        page_width,
                        page_height,
                        font_size,
                        table_cell,
                        is_recovered,
                    )
                    fragment_confidence, preserve_reason = _classify_fragment_stability(
                        text_box_group,
                        source_text,
                        bbox,
                        font_size,
                        rotation,
                    )
                    geometry_fill_ratio = _group_geometry_fill_ratio(
                        text_box_group,
                        bbox,
                    )
                    if (
                        len(text_box_group) > 1
                        and geometry_fill_ratio < 0.18
                        and (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                        >= font_size * font_size * 18
                    ):
                        fragment_confidence = min(fragment_confidence, 0.3)
                        preserve_reason = "离散文字被错误合并，禁止跨图形区域遮盖"
                    if (
                        region_type == "chart"
                        and _should_preserve_chart_proper_name(source_text)
                    ):
                        fragment_confidence = min(fragment_confidence, 0.4)
                        preserve_reason = "图表中的企业、品牌或产品名称按原字形保留"
                    # 原生/图形对象文字仍属于矢量文字。此前对复杂背景采用高斯
                    # 模糊补片，会把图例和密集表格中的原文字变成灰色拖影；这些
                    # 拖影既影响阅读，也会被视觉质检误判为外语残留。这里统一使用
                    # 区域主背景色精确遮盖。真正的位图文字仍由 vision 块单独处理。
                    mask_bbox, mask_image_png = (None, None)
                    blocks.append(
                        PdfTextBlock(
                            id=f"p{page_index + 1:04d}-b{block_index + 1:04d}",
                            page_index=page_index,
                            source_text=source_text,
                            bbox=bbox,
                            render_bbox=_inset_bbox(
                                render_bbox,
                                1.5,
                                # 密集表格的原始行高常不足 9pt。水平留白用于避开
                                # 单元格边线，垂直方向仅保留极小安全边距，否则会把
                                # 两字中文也误判为溢出。
                                vertical_amount=0.25 if table_cell else 0.0,
                            ),
                            font_size=font_size,
                            alignment=alignment,
                            background_rgb=_sample_background(
                                page_image,
                                bbox,
                                sampling_region=table_cell,
                            ),
                            text_rgb=_dominant_text_color(block_characters),
                            source_type=(
                                "native-recovered"
                                if is_recovered
                                else "native"
                            ),
                            mask_bbox=mask_bbox,
                            mask_image_png=mask_image_png,
                            font_name=font_name,
                            font_weight=font_weight,
                            rotation=rotation,
                            region_type=region_type,
                            table_cell=table_cell,
                            reading_order=block_index,
                            fragment_confidence=fragment_confidence,
                            preserve_reason=preserve_reason,
                            geometry_fill_ratio=geometry_fill_ratio,
                        )
                    )
                page_blocks = blocks[page_block_start:]
                visible_characters = [
                    character
                    for character in page.chars
                    if any(
                        value.isalpha()
                        for value in str(character.get("text", ""))
                    )
                ]
                native_character_count += len(visible_characters)
                covered_native_character_count += sum(
                    _character_in_any_bbox(
                        character,
                        [block.bbox for block in page_blocks],
                    )
                    for character in visible_characters
                )
                image_regions.extend(
                    _extract_embedded_image_regions(page, page_index)
                )
    except Exception as error:
        LOGGER.warning("原生 PDF 坐标提取失败，将尝试 MinerU 坐标：%s", error)
        return _page_sizes_only(source_path)
    return PdfLayoutDocument(
        page_sizes=tuple(page_sizes),
        blocks=tuple(blocks),
        source_type="native",
        image_regions=tuple(image_regions),
        native_character_count=native_character_count,
        covered_native_character_count=covered_native_character_count,
    )


def _extract_mineru_layout(
    page_sizes: tuple[tuple[float, float], ...],
    content_list: Iterable[dict[str, object]],
    image_regions: tuple[PdfImageRegion, ...] = (),
) -> PdfLayoutDocument:
    """将 MinerU 的千分制区块坐标转换为 PDF 页面坐标。"""

    blocks: list[PdfTextBlock] = []
    for index, item in enumerate(content_list):
        page_index = _safe_integer(item.get("page_idx"))
        raw_bbox = item.get("bbox")
        if (
            page_index is None
            or page_index < 0
            or page_index >= len(page_sizes)
            or not isinstance(raw_bbox, list)
            or len(raw_bbox) != 4
        ):
            continue
        source_text = _mineru_item_text(item)
        if not source_text:
            continue
        page_width, page_height = page_sizes[page_index]
        try:
            normalized_bbox = tuple(float(value) for value in raw_bbox)
        except (TypeError, ValueError):
            continue
        bbox = (
            normalized_bbox[0] / 1000 * page_width,
            normalized_bbox[1] / 1000 * page_height,
            normalized_bbox[2] / 1000 * page_width,
            normalized_bbox[3] / 1000 * page_height,
        )
        estimated_font_size = max(
            6.0,
            min(18.0, (bbox[3] - bbox[1]) / max(source_text.count("\n") + 1, 1)),
        )
        region_type = _mineru_region_type(item)
        block_id = f"p{page_index + 1:04d}-m{index + 1:04d}"
        table_html = _mineru_table_html(item)
        if region_type == "table" and table_html:
            matching_image_region = _matching_image_region(
                page_index,
                bbox,
                image_regions,
            )
            if matching_image_region is not None:
                recovery = recover_image_table_blocks(
                    table_id=block_id,
                    page_index=page_index,
                    table_bbox=bbox,
                    table_html=table_html,
                    image_region=matching_image_region,
                    reading_order_start=index * 500,
                )
                if recovery.succeeded:
                    blocks.extend(recovery.blocks)
                    LOGGER.info(
                        "图片表格单元格恢复成功：table=%s region=%s "
                        "rows=%s columns=%s blocks=%s confidence=%.3f",
                        block_id,
                        matching_image_region.id,
                        recovery.row_count,
                        recovery.column_count,
                        len(recovery.blocks),
                        recovery.confidence,
                    )
                    continue
                LOGGER.warning(
                    "图片表格单元格恢复失败并保留原图：table=%s region=%s "
                    "reason=%s",
                    block_id,
                    matching_image_region.id,
                    recovery.failure_reason,
                )
        blocks.append(
            PdfTextBlock(
                id=block_id,
                page_index=page_index,
                source_text=source_text,
                bbox=bbox,
                render_bbox=_inset_bbox(bbox, 2),
                font_size=estimated_font_size,
                alignment="left",
                background_rgb=(1.0, 1.0, 1.0),
                text_rgb=(0.0, 0.0, 0.0),
                source_type="mineru",
                region_type=region_type,
                reading_order=index,
                fragment_confidence=0.85,
            )
        )
    return PdfLayoutDocument(
        page_sizes=page_sizes,
        blocks=tuple(blocks),
        source_type="mineru",
    )


def _page_sizes_only(source_path: Path) -> PdfLayoutDocument:
    """读取 PDF 页面尺寸，用于 MinerU 坐标兜底。"""

    reader = PdfReader(str(source_path))
    page_sizes = tuple(
        (float(page.mediabox.width), float(page.mediabox.height))
        for page in reader.pages
    )
    return PdfLayoutDocument(page_sizes=page_sizes, blocks=(), source_type="native")


def _find_table_cells(page: pdfplumber.page.Page) -> list[tuple[float, float, float, float]]:
    """返回页面中检测到的表格单元格坐标。"""

    try:
        graphical_edges = [
            edge
            for edge in page.edges
            if _is_visible_graphical_edge(edge)
        ]
        horizontal_edge_count = sum(
            abs(float(edge.get("x1", 0)) - float(edge.get("x0", 0)))
            >= abs(
                float(edge.get("bottom", 0)) - float(edge.get("top", 0))
            )
            * 3
            for edge in graphical_edges
        )
        vertical_edge_count = sum(
            abs(float(edge.get("bottom", 0)) - float(edge.get("top", 0)))
            >= abs(float(edge.get("x1", 0)) - float(edge.get("x0", 0))) * 3
            for edge in graphical_edges
        )
    except (TypeError, ValueError):
        return []
    if horizontal_edge_count < 2 or vertical_edge_count < 2:
        return []
    try:
        detected_tables = page.find_tables()
    except Exception as error:
        LOGGER.debug("表格单元格检测失败：%s", error)
        return []
    credible_cells: list[tuple[float, float, float, float]] = []
    seen_cells: set[tuple[float, float, float, float]] = set()
    for table in detected_tables:
        normalized_cells = [
            tuple(float(value) for value in cell)
            for cell in table.cells
            if cell is not None
        ]
        if not _is_credible_detected_table(normalized_cells):
            continue
        for cell in normalized_cells:
            if not _is_cell_supported_by_edges(cell, graphical_edges):
                continue
            rounded_cell = tuple(round(value, 3) for value in cell)
            if rounded_cell in seen_cells:
                continue
            seen_cells.add(rounded_cell)
            credible_cells.append(cell)
    return credible_cells


def _is_visible_graphical_edge(edge: dict[str, object]) -> bool:
    """判断线段是否会在最终页面上可见，排除 Word 文字框的白色填充边。"""

    if bool(edge.get("stroke")):
        return not _is_white_pdf_color(edge.get("stroking_color"))
    if bool(edge.get("fill")):
        # Word 会为文字行生成与单元格背景同色的无描边矩形。其 rect_edge
        # 只是填充区域的几何边界，视觉上不存在；只有深色细矩形才可视为表格线。
        return _is_dark_pdf_color(edge.get("non_stroking_color"))
    return True


def _is_white_pdf_color(color: object) -> bool:
    """判断 PDF 灰度或 RGB/CMYK 颜色是否接近白色。"""

    if color is None:
        return False
    if isinstance(color, (int, float)):
        return float(color) >= 0.95
    if isinstance(color, (tuple, list)) and color:
        try:
            components = [float(component) for component in color]
        except (TypeError, ValueError):
            return False
        if len(components) == 4:
            return max(components) <= 0.05
        return min(components) >= 0.95
    return False


def _is_dark_pdf_color(color: object) -> bool:
    """判断 PDF 灰度或 RGB/CMYK 填充是否足以形成可见深色线条。"""

    if color is None:
        return False
    if isinstance(color, (int, float)):
        return float(color) <= 0.2
    if isinstance(color, (tuple, list)) and color:
        try:
            components = [float(component) for component in color]
        except (TypeError, ValueError):
            return False
        if len(components) == 4:
            return max(components) >= 0.7
        return max(components) <= 0.3
    return False


def _is_cell_supported_by_edges(
    cell: tuple[float, float, float, float],
    graphical_edges: Iterable[dict[str, object]],
    *,
    tolerance: float = 1.2,
) -> bool:
    """确认候选单元格四边均由可见线段支撑。"""

    x0, top, x1, bottom = cell
    width = max(1.0, x1 - x0)
    height = max(1.0, bottom - top)
    horizontal_support = {top: False, bottom: False}
    vertical_support = {x0: False, x1: False}
    for edge in graphical_edges:
        try:
            edge_x0 = float(edge["x0"])
            edge_x1 = float(edge["x1"])
            edge_top = float(edge["top"])
            edge_bottom = float(edge["bottom"])
        except (KeyError, TypeError, ValueError):
            continue
        edge_width = abs(edge_x1 - edge_x0)
        edge_height = abs(edge_bottom - edge_top)
        if edge_width >= max(3.0, edge_height * 3):
            edge_y = (edge_top + edge_bottom) / 2
            overlap = max(0.0, min(x1, max(edge_x0, edge_x1)) - max(x0, min(edge_x0, edge_x1)))
            for boundary in horizontal_support:
                if (
                    abs(edge_y - boundary) <= tolerance
                    and overlap >= width * 0.65
                ):
                    horizontal_support[boundary] = True
        elif edge_height >= max(3.0, edge_width * 3):
            edge_x = (edge_x0 + edge_x1) / 2
            overlap = max(0.0, min(bottom, max(edge_top, edge_bottom)) - max(top, min(edge_top, edge_bottom)))
            for boundary in vertical_support:
                if (
                    abs(edge_x - boundary) <= tolerance
                    and overlap >= height * 0.65
                ):
                    vertical_support[boundary] = True
    return all(horizontal_support.values()) and all(vertical_support.values())


def _is_credible_detected_table(
    cells: list[tuple[float, float, float, float]],
) -> bool:
    """过滤图表框和表格内部被重复检测出的单列伪表格。

    Args:
        cells: 一个候选表格包含的单元格坐标。

    Returns:
        候选同时具有至少两行、两列时返回 True。
    """

    if len(cells) < 4:
        return False
    x_boundaries = {
        round(coordinate, 1)
        for x0, _, x1, _ in cells
        for coordinate in (x0, x1)
    }
    y_boundaries = {
        round(coordinate, 1)
        for _, top, _, bottom in cells
        for coordinate in (top, bottom)
    }
    return len(x_boundaries) >= 3 and len(y_boundaries) >= 3


def _has_credible_graphical_grid(
    graphical_edges: Iterable[dict[str, object]],
    *,
    coordinate_tolerance: float = 1.0,
) -> bool:
    """判断页面线段是否形成可用于单元格恢复的重复网格。

    饼图外框和标签引线同样包含水平、垂直线段，但其坐标通常只出现一两次。
    真正的表格会在多行、多列重复使用相同的边界坐标。只有横纵方向均至少存在
    三组重复边界时，才允许使用图形几何兜底推断单元格，避免把并排图表误判为
    一个巨大单元格。

    Args:
        graphical_edges: 当前页面的 PDF 图形边集合。
        coordinate_tolerance: 边界坐标聚类精度，单位为点。

    Returns:
        页面具有可信重复表格网格时返回 True。
    """

    vertical_coordinates: Counter[int] = Counter()
    horizontal_coordinates: Counter[int] = Counter()
    scale = 1.0 / max(0.1, coordinate_tolerance)
    for edge in graphical_edges:
        try:
            edge_x0 = float(edge["x0"])
            edge_x1 = float(edge["x1"])
            edge_top = float(edge["top"])
            edge_bottom = float(edge["bottom"])
        except (KeyError, TypeError, ValueError):
            continue
        width = abs(edge_x1 - edge_x0)
        height = abs(edge_bottom - edge_top)
        if height >= max(5.0, width * 3):
            vertical_coordinates[
                round(((edge_x0 + edge_x1) / 2) * scale)
            ] += 1
        elif width >= max(5.0, height * 3):
            horizontal_coordinates[
                round(((edge_top + edge_bottom) / 2) * scale)
            ] += 1

    repeated_vertical_boundaries = sum(
        occurrence_count >= 3
        for occurrence_count in vertical_coordinates.values()
    )
    repeated_horizontal_boundaries = sum(
        occurrence_count >= 3
        for occurrence_count in horizontal_coordinates.values()
    )
    return (
        repeated_vertical_boundaries >= 3
        and repeated_horizontal_boundaries >= 3
    )


def _find_uncovered_word_boxes(
    page: pdfplumber.page.Page,
    existing_boxes: list[_NativeTextBox],
    table_cells: list[tuple[float, float, float, float]],
    graphical_edges: tuple[dict[str, object], ...] = (),
) -> list[_NativeTextBox]:
    """补齐位于 Form、图表等对象内且未形成 LTTextBox 的可翻译文字。"""

    existing_bboxes = [text_box.bbox for text_box in existing_boxes]
    recovered: list[_NativeTextBox] = []
    try:
        words = page.extract_words(
            return_chars=True,
            use_text_flow=True,
            keep_blank_chars=False,
        )
    except Exception as error:
        LOGGER.debug("补充词坐标提取失败：%s", error)
        return recovered

    for word in words:
        source_text = _normalize_source_text(str(word.get("text", "")))
        characters = word.get("chars")
        if (
            not source_text
            or not any(character.isalpha() for character in source_text)
            or not isinstance(characters, list)
            or not characters
        ):
            continue
        uncovered_count = sum(
            not _character_in_any_bbox(character, existing_bboxes)
            for character in characters
        )
        if uncovered_count / len(characters) < 0.5:
            continue
        try:
            bbox = (
                float(word["x0"]),
                float(word["top"]),
                float(word["x1"]),
                float(word["bottom"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        recovered.append(
            _NativeTextBox(
                text=source_text,
                bbox=bbox,
                characters=tuple(characters),
                table_cell=(
                    _containing_table_cell(bbox, table_cells)
                    or _infer_graphical_table_cell(bbox, graphical_edges)
                ),
                source_type="native-recovered",
            )
        )
    return recovered


def _infer_graphical_table_cell(
    bbox: tuple[float, float, float, float],
    graphical_edges: Iterable[dict[str, object]],
    *,
    boundary_tolerance: float = 2.0,
) -> tuple[float, float, float, float] | None:
    """根据真实线段几何关系推断文字所在表格单元格。

    部分 Word 导出的 PDF 会把横线错误标记为 ``orientation=v``，导致
    ``pdfplumber.find_tables`` 返回空结果。本方法不信任方向标签，只按线段
    宽高和覆盖范围寻找包围文字中心的上下左右边界。

    Args:
        bbox: 文字区域左、上、右、下坐标。
        graphical_edges: 当前页面的 PDF 图形边集合。
        boundary_tolerance: 文字与单元格边界允许的坐标误差。

    Returns:
        推断出的单元格坐标；边界不足时返回 ``None``。
    """

    x0, top, x1, bottom = bbox
    center_x = (x0 + x1) / 2
    center_y = (top + bottom) / 2
    vertical_segments: list[tuple[float, float, float]] = []
    horizontal_segments: list[tuple[float, float, float]] = []
    for edge in graphical_edges:
        try:
            edge_x0 = float(edge["x0"])
            edge_x1 = float(edge["x1"])
            edge_top = float(edge["top"])
            edge_bottom = float(edge["bottom"])
        except (KeyError, TypeError, ValueError):
            continue
        width = abs(edge_x1 - edge_x0)
        height = abs(edge_bottom - edge_top)
        if height >= max(5.0, width * 3):
            vertical_segments.append(
                (
                    (edge_x0 + edge_x1) / 2,
                    min(edge_top, edge_bottom),
                    max(edge_top, edge_bottom),
                )
            )
        elif width >= max(5.0, height * 3):
            horizontal_segments.append(
                (
                    (edge_top + edge_bottom) / 2,
                    min(edge_x0, edge_x1),
                    max(edge_x0, edge_x1),
                )
            )

    left_candidates = [
        x
        for x, segment_top, segment_bottom in vertical_segments
        if segment_top - boundary_tolerance
        <= center_y
        <= segment_bottom + boundary_tolerance
        and x <= x0 + boundary_tolerance
    ]
    right_candidates = [
        x
        for x, segment_top, segment_bottom in vertical_segments
        if segment_top - boundary_tolerance
        <= center_y
        <= segment_bottom + boundary_tolerance
        and x >= x1 - boundary_tolerance
    ]
    top_candidates = [
        y
        for y, segment_x0, segment_x1 in horizontal_segments
        if segment_x0 - boundary_tolerance
        <= center_x
        <= segment_x1 + boundary_tolerance
        and y <= top + boundary_tolerance
    ]
    bottom_candidates = [
        y
        for y, segment_x0, segment_x1 in horizontal_segments
        if segment_x0 - boundary_tolerance
        <= center_x
        <= segment_x1 + boundary_tolerance
        and y >= bottom - boundary_tolerance
    ]
    if not (
        left_candidates
        and right_candidates
        and top_candidates
        and bottom_candidates
    ):
        return None
    inferred = (
        max(left_candidates),
        max(top_candidates),
        min(right_candidates),
        min(bottom_candidates),
    )
    if (
        inferred[2] - inferred[0] < max(3.0, x1 - x0 - boundary_tolerance * 2)
        or inferred[3] - inferred[1]
        < max(3.0, bottom - top - boundary_tolerance * 2)
    ):
        return None
    return inferred


def _extract_embedded_image_regions(
    page: pdfplumber.page.Page,
    page_index: int,
) -> list[PdfImageRegion]:
    """提取占据有效页面面积的嵌入图片，供视觉模型识别图中文字。"""

    page_width = float(page.width)
    page_height = float(page.height)
    page_area = max(1.0, page_width * page_height)
    candidates: list[tuple[float, float, float, float]] = []
    for image in page.images:
        try:
            bbox = (
                float(image["x0"]),
                float(image["top"]),
                float(image["x1"]),
                float(image["bottom"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        width = max(0.0, bbox[2] - bbox[0])
        height = max(0.0, bbox[3] - bbox[1])
        if (
            width * height / page_area >= MINIMUM_IMAGE_AREA_RATIO
            and width >= 80
            and height >= 40
        ):
            candidates.append(bbox)
    if not candidates:
        return []

    try:
        rendered_page = page.to_image(
            resolution=144,
            antialias=True,
        ).original.convert("RGB")
    except Exception as error:
        LOGGER.warning("嵌入图片区域渲染失败：%s", error)
        return []

    scale_x = rendered_page.width / page_width
    scale_y = rendered_page.height / page_height
    regions: list[PdfImageRegion] = []
    for region_index, bbox in enumerate(candidates, start=1):
        crop_box = (
            max(0, round(bbox[0] * scale_x)),
            max(0, round(bbox[1] * scale_y)),
            min(rendered_page.width, round(bbox[2] * scale_x)),
            min(rendered_page.height, round(bbox[3] * scale_y)),
        )
        if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
            continue
        output = io.BytesIO()
        rendered_page.crop(crop_box).save(output, format="PNG", optimize=True)
        regions.append(
            PdfImageRegion(
                id=f"p{page_index + 1:04d}-i{region_index:04d}",
                page_index=page_index,
                bbox=bbox,
                image_png=output.getvalue(),
            )
        )
    return regions


def _character_in_any_bbox(
    character: dict[str, object],
    boxes: list[tuple[float, float, float, float]],
) -> bool:
    """判断字符中心点是否位于任意文本框内。"""

    try:
        center_x = (float(character["x0"]) + float(character["x1"])) / 2
        center_y = (float(character["top"]) + float(character["bottom"])) / 2
    except (KeyError, TypeError, ValueError):
        return False
    return any(
        x0 - 0.5 <= center_x <= x1 + 0.5
        and top - 0.5 <= center_y <= bottom + 0.5
        for x0, top, x1, bottom in boxes
    )


def _characters_in_bbox(
    characters: list[dict[str, object]],
    bbox: tuple[float, float, float, float],
) -> list[dict[str, object]]:
    """筛选中心点位于文本框内的字符。"""

    x0, top, x1, bottom = bbox
    return [
        character
        for character in characters
        if x0 - 0.5 <= (float(character["x0"]) + float(character["x1"])) / 2 <= x1 + 0.5
        and top - 0.5
        <= (float(character["top"]) + float(character["bottom"])) / 2
        <= bottom + 0.5
    ]


def _group_native_text_boxes(
    text_boxes: list[_NativeTextBox],
) -> list[list[_NativeTextBox]]:
    """合并同一表格单元格，表格外保持视觉行粒度。"""

    table_groups: dict[
        tuple[float, float, float, float],
        list[_NativeTextBox],
    ] = {}
    non_table_boxes: list[_NativeTextBox] = []
    for text_box in text_boxes:
        if text_box.table_cell is None:
            non_table_boxes.append(text_box)
        else:
            table_groups.setdefault(text_box.table_cell, []).append(text_box)

    groups = _group_non_table_text_boxes(non_table_boxes)
    groups.extend(
        sorted(
            (
                sorted(group, key=lambda item: (item.bbox[1], item.bbox[0]))
                for group in table_groups.values()
            ),
            key=lambda group: (group[0].bbox[1], group[0].bbox[0]),
        )
    )
    return sorted(groups, key=lambda group: (group[0].bbox[1], group[0].bbox[0]))


def _group_non_table_text_boxes(
    text_boxes: list[_NativeTextBox],
) -> list[list[_NativeTextBox]]:
    """只合并同一视觉行的相邻片段，禁止跨行形成大覆盖框。"""

    groups: list[list[_NativeTextBox]] = []
    for text_box in sorted(text_boxes, key=lambda item: (item.bbox[1], item.bbox[0])):
        if not groups:
            groups.append([text_box])
            continue
        previous = groups[-1][-1]
        previous_height = max(1.0, previous.bbox[3] - previous.bbox[1])
        current_height = max(1.0, text_box.bbox[3] - text_box.bbox[1])
        similar_height = (
            max(previous_height, current_height)
            / min(previous_height, current_height)
            <= 1.35
        )
        horizontal_gap = text_box.bbox[0] - previous.bbox[2]
        same_line_continuation = (
            similar_height
            and abs(text_box.bbox[1] - previous.bbox[1])
            <= min(previous_height, current_height) * 0.4
            and -1.0
            <= horizontal_gap
            <= max(4.0, min(previous_height, current_height) * 1.5)
        )
        if same_line_continuation:
            groups[-1].append(text_box)
        else:
            groups.append([text_box])
    return _merge_orphan_non_table_continuations(groups)


def _merge_orphan_non_table_continuations(
    groups: list[list[_NativeTextBox]],
) -> list[list[_NativeTextBox]]:
    """把图表中被换行拆出的单个日文词尾接回上一行。

    Args:
        groups: 已按视觉行完成初步分组的非表格文本框。

    Returns:
        合并可靠孤立词尾后的文本框组。
    """

    merged_groups = [list(group) for group in groups]
    removed_indexes: set[int] = set()
    orphan_pattern = re.compile(r"^[\u3041-\u30ff\u3400-\u9fff]$")
    for current_index, current_group in enumerate(merged_groups):
        if (
            len(current_group) != 1
            or not orphan_pattern.fullmatch(current_group[0].text.strip())
        ):
            continue
        current_box = current_group[0].bbox
        current_center_x = (current_box[0] + current_box[2]) / 2
        current_height = max(1.0, current_box[3] - current_box[1])
        candidate_index: int | None = None
        candidate_gap = math.inf
        for previous_index, previous_group in enumerate(
            merged_groups[:current_index]
        ):
            if previous_index in removed_indexes:
                continue
            previous_bbox = _union_bbox(
                [text_box.bbox for text_box in previous_group]
            )
            vertical_gap = current_box[1] - previous_bbox[3]
            if not (
                0.0 <= vertical_gap <= current_height * 0.65
                and previous_bbox[0] - current_height * 0.5
                <= current_center_x
                <= previous_bbox[2] + current_height * 0.5
            ):
                continue
            previous_text = _join_layout_text(
                [text_box.text for text_box in previous_group]
            )
            if (
                not re.search(
                    r"[\u3041-\u30ff\u3400-\u9fff]$",
                    previous_text,
                )
                or previous_text.endswith(("。", "！", "？", "、", "：", "；"))
            ):
                continue
            if vertical_gap < candidate_gap:
                candidate_index = previous_index
                candidate_gap = vertical_gap
        if candidate_index is not None:
            merged_groups[candidate_index].extend(current_group)
            removed_indexes.add(current_index)
    return [
        sorted(group, key=lambda item: (item.bbox[1], item.bbox[0]))
        for index, group in enumerate(merged_groups)
        if index not in removed_indexes
    ]


def _join_layout_text(lines: list[str]) -> str:
    """将 PDF 视觉换行恢复为适合整体翻译的自然文本。"""

    result = ""
    for line in lines:
        normalized = _normalize_source_text(line)
        if not normalized:
            continue
        if not result:
            result = normalized
            continue
        previous_character = result[-1]
        next_character = normalized[0]
        if normalized.startswith(("•", "・", "-", "–")):
            separator = "\n"
        elif previous_character in ".!?。！？:;" and next_character.isupper():
            separator = "\n"
        elif previous_character.isascii() and next_character.isascii():
            separator = " "
        else:
            separator = ""
        result = f"{result}{separator}{normalized}"
    return result.strip()


def _union_bbox(
    boxes: list[tuple[float, float, float, float]],
) -> tuple[float, float, float, float]:
    """返回覆盖全部输入文字框的最小矩形。"""

    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _group_geometry_fill_ratio(
    text_boxes: list[_NativeTextBox],
    union_bbox: tuple[float, float, float, float],
) -> float:
    """返回分组中文字框面积占联合外框面积的比例。"""

    union_area = max(
        0.1,
        (union_bbox[2] - union_bbox[0]) * (union_bbox[3] - union_bbox[1]),
    )
    occupied_area = sum(
        max(0.0, text_box.bbox[2] - text_box.bbox[0])
        * max(0.0, text_box.bbox[3] - text_box.bbox[1])
        for text_box in text_boxes
    )
    return min(1.0, occupied_area / union_area)


def _bbox_overlap_ratio(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    """返回两个矩形交集占较小矩形面积的比例。"""

    intersection_width = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    intersection_height = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    intersection_area = intersection_width * intersection_height
    first_area = max(0.1, (first[2] - first[0]) * (first[3] - first[1]))
    second_area = max(0.1, (second[2] - second[0]) * (second[3] - second[1]))
    return intersection_area / min(first_area, second_area)


def _dominant_font_size(
    characters: list[dict[str, object]],
    bbox: tuple[float, float, float, float],
) -> float:
    """使用文本框内字符字号的中位数，缺失时根据框高估算。"""

    sizes = [
        float(character["size"])
        for character in characters
        if isinstance(character.get("size"), (int, float))
        and float(character["size"]) > 0
    ]
    if sizes:
        return max(5.0, min(36.0, statistics.median(sizes)))
    return max(6.0, min(18.0, bbox[3] - bbox[1]))


def _dominant_font_name(characters: list[dict[str, object]]) -> str:
    """返回文本框中出现频率最高的原字体名称。"""

    names = [
        str(character.get("fontname", "")).strip()
        for character in characters
        if str(character.get("fontname", "")).strip()
    ]
    return Counter(names).most_common(1)[0][0] if names else ""


def _infer_font_weight(font_name: str) -> int:
    """根据 PDF 字体名称推断常规或粗体字重。"""

    normalized = font_name.casefold()
    return 700 if any(token in normalized for token in ("bold", "black", "heavy")) else 400


def _infer_text_rotation(
    source_text: str,
    bbox: tuple[float, float, float, float],
) -> int:
    """根据文字框长宽比识别常见竖排或旋转文字。"""

    width = max(0.1, bbox[2] - bbox[0])
    height = max(0.1, bbox[3] - bbox[1])
    compact_length = len(re.sub(r"\s+", "", source_text))
    if height > width * 2.5 and compact_length >= 2:
        return 90
    return 0


def _dominant_rotation(text_boxes: list[_NativeTextBox]) -> int:
    """返回分组内占比最高的旋转角度。"""

    rotations = [text_box.rotation for text_box in text_boxes]
    return Counter(rotations).most_common(1)[0][0] if rotations else 0


def _infer_region_type(
    source_text: str,
    bbox: tuple[float, float, float, float],
    page_width: float,
    page_height: float,
    font_size: float,
    table_cell: tuple[float, float, float, float] | None,
    is_recovered: bool,
) -> str:
    """根据表格、位置、字号和对象来源推断渲染区域类型。"""

    if table_cell is not None:
        return "table"
    if bbox[1] <= page_height * 0.06:
        return "header"
    if bbox[3] >= page_height * 0.94:
        return "footer"
    if is_recovered:
        return "chart"
    if font_size >= 14 or (
        len(source_text) <= 80
        and abs((bbox[0] + bbox[2]) / 2 - page_width / 2) <= page_width * 0.08
    ):
        return "heading"
    return "body"


def _should_preserve_chart_proper_name(source_text: str) -> bool:
    """判断图表拉丁文字是否属于应原样保留的企业或品牌标签。"""

    normalized = re.sub(r"\s+", " ", source_text).strip()
    if (
        not normalized
        or re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", normalized)
        or not re.search(r"[A-Za-z]", normalized)
    ):
        return False
    folded = normalized.casefold().strip(" .,:;()[]")
    generic_exact_labels = {
        "amount",
        "category",
        "manufacturer",
        "manufacturers",
        "market",
        "others",
        "share",
        "total",
        "volume",
    }
    if folded in generic_exact_labels:
        return False
    generic_phrases = (
        "market size forecast",
        "manufacturers' share",
        "manufacturer share",
        "single-sided and double-sided",
    )
    if any(phrase in folded for phrase in generic_phrases):
        return False
    company_suffix = re.compile(
        r"\b(?:co|corp|corporation|inc|ltd|limited|llc|plc|gmbh)\b",
        re.IGNORECASE,
    )
    proper_name_marker = re.compile(
        r"(?:[A-Z]{3,}|(?:^|[\s(])[A-Z][a-zA-Z-]{2,})"
    )
    return (
        company_suffix.search(normalized) is not None
        or proper_name_marker.search(normalized) is not None
    )


def _classify_fragment_stability(
    text_boxes: list[_NativeTextBox],
    source_text: str,
    bbox: tuple[float, float, float, float],
    font_size: float,
    rotation: int,
) -> tuple[float, str | None]:
    """评估原文字形是否可安全独立遮盖和回写。"""

    compact_text = re.sub(r"\s+", "", source_text)
    width = max(0.1, bbox[2] - bbox[0])
    height = max(0.1, bbox[3] - bbox[1])
    if rotation and height > font_size * 1.7 and width < font_size * 3.5:
        return 0.3, "竖排或旋转文字无法稳定恢复基线"
    single_letter_tokens = re.findall(r"(?<!\w)[A-Za-z](?!\w)", source_text)
    if len(single_letter_tokens) >= 4:
        return 0.4, "原 PDF 将单词拆成独立字形"
    if (
        len(compact_text) == 1
        and compact_text.isalpha()
        and len(text_boxes) == 1
    ):
        return 0.45, "孤立字母缺少可靠语义上下文"
    if (
        compact_text.isascii()
        and compact_text.isalpha()
        and len(compact_text) <= 2
        and width <= font_size * 1.6
    ):
        return 0.5, "拉丁文字片段过短，可能属于拆分单词"
    return 1.0, None


def _mineru_region_type(item: dict[str, object]) -> str:
    """将 MinerU 区块类型映射为统一页面区域类型。"""

    raw_type = str(item.get("type") or item.get("category") or "").casefold()
    if "table" in raw_type:
        return "table"
    if any(token in raw_type for token in ("image", "figure", "chart")):
        return "chart"
    if "caption" in raw_type:
        return "caption"
    if any(token in raw_type for token in ("title", "header")):
        return "heading"
    if "footer" in raw_type:
        return "footer"
    return "body"


def _containing_table_cell(
    bbox: tuple[float, float, float, float],
    table_cells: list[tuple[float, float, float, float]],
) -> tuple[float, float, float, float] | None:
    """返回包含文本框中心点的最小表格单元格。"""

    center_x = (bbox[0] + bbox[2]) / 2
    center_y = (bbox[1] + bbox[3]) / 2
    candidates = [
        cell
        for cell in table_cells
        if cell[0] <= center_x <= cell[2] and cell[1] <= center_y <= cell[3]
    ]
    return min(
        candidates,
        key=lambda cell: (cell[2] - cell[0]) * (cell[3] - cell[1]),
        default=None,
    )


def _table_render_bbox(
    text_bbox: tuple[float, float, float, float],
    table_cell: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """保留原文字起始高度，同时允许译文使用单元格剩余空间换行。"""

    return (
        table_cell[0],
        max(table_cell[1], text_bbox[1] - 0.5),
        table_cell[2],
        table_cell[3],
    )


def _infer_alignment(
    bbox: tuple[float, float, float, float],
    region: tuple[float, float, float, float],
    text: str,
    font_size: float,
) -> str:
    """根据文本框位置和字号推断标题居中，其余内容左对齐。"""

    region_width = region[2] - region[0]
    center_offset = abs((bbox[0] + bbox[2]) / 2 - (region[0] + region[2]) / 2)
    is_heading = font_size >= 14 or len(text) <= 40
    if is_heading and center_offset <= region_width * 0.08:
        return "center"
    return "left"


def _dominant_text_color(
    characters: list[dict[str, object]],
) -> tuple[float, float, float]:
    """从字符绘制色中取中位数，保留标题和深色底区域的文字颜色。"""

    colors = [
        normalized
        for character in characters
        if (normalized := _normalize_pdf_color(character.get("non_stroking_color")))
        is not None
    ]
    if not colors:
        return (0.0, 0.0, 0.0)
    return tuple(statistics.median(color[channel] for color in colors) for channel in range(3))


def _normalize_pdf_color(value: object) -> tuple[float, float, float] | None:
    """将 PDF 灰度或 RGB 颜色统一为 0 到 1 的 RGB。"""

    if isinstance(value, (int, float)):
        level = min(max(float(value), 0.0), 1.0)
        return (level, level, level)
    if not isinstance(value, (tuple, list)):
        return None
    numeric_values = [
        float(component)
        for component in value
        if isinstance(component, (int, float))
    ]
    if len(numeric_values) == 1:
        level = min(max(numeric_values[0], 0.0), 1.0)
        return (level, level, level)
    if len(numeric_values) >= 3:
        return tuple(min(max(component, 0.0), 1.0) for component in numeric_values[:3])
    return None


def _safe_page_image(
    page: pdfplumber.page.Page,
    resolution: int = 72,
):
    """渲染页面用于采样文字背景色，失败时返回 None。"""

    try:
        return page.to_image(
            resolution=resolution,
            antialias=resolution > 72,
        ).original.convert("RGB")
    except Exception as error:
        LOGGER.debug("页面背景采样不可用：%s", error)
        return None


def _sample_background(
    page_image,
    bbox: tuple[float, float, float, float],
    *,
    sampling_region: tuple[float, float, float, float] | None = None,
) -> tuple[float, float, float]:
    """采样文字所在区域的主背景色，避开表格边框和抗锯齿字形。

    Args:
        page_image: 72 DPI 页面渲染图。
        bbox: 原文字区域。
        sampling_region: 可选的表格单元格区域。

    Returns:
        归一化 RGB 主背景色。
    """

    if page_image is None:
        return (1.0, 1.0, 1.0)
    width, height = page_image.size
    if sampling_region is not None:
        region_x0, region_top, region_x1, region_bottom = sampling_region
        inset = min(
            2.0,
            max(
                0.5,
                min(region_x1 - region_x0, region_bottom - region_top) * 0.08,
            ),
        )
        sample_bbox = (
            region_x0 + inset,
            region_top + inset,
            region_x1 - inset,
            region_bottom - inset,
        )
    else:
        x0, top, x1, bottom = bbox
        sample_bbox = (
            max(0.0, x0 - 4.0),
            max(0.0, top - 3.0),
            min(float(width), x1 + 4.0),
            min(float(height), bottom + 3.0),
        )
    pixel_bbox = (
        min(max(round(sample_bbox[0]), 0), width - 1),
        min(max(round(sample_bbox[1]), 0), height - 1),
        min(max(round(sample_bbox[2]), 1), width),
        min(max(round(sample_bbox[3]), 1), height),
    )
    if pixel_bbox[2] <= pixel_bbox[0] or pixel_bbox[3] <= pixel_bbox[1]:
        return (1.0, 1.0, 1.0)
    crop = page_image.crop(pixel_bbox).convert("RGB")
    sample_stride = max(1, min(crop.size) // 24)
    quantized_pixels = Counter(
        tuple(round(channel / 8) * 8 for channel in pixel)
        for y in range(0, crop.height, sample_stride)
        for x in range(0, crop.width, sample_stride)
        if (pixel := crop.getpixel((x, y)))
    )
    if not quantized_pixels:
        return (1.0, 1.0, 1.0)
    background = quantized_pixels.most_common(1)[0][0]
    return tuple(min(255, channel) / 255 for channel in background)


def _draw_original_text_mask(
    output_canvas: canvas.Canvas,
    block: PdfTextBlock,
    page_height: float,
    vision_mask_padding: float = 12.0,
) -> None:
    """使用采样到的背景色精确遮盖原文本框。

    视觉文字的 OCR 框需要适当外扩，但外扩范围必须限制在其来源图片
    区域内，避免覆盖图片外紧邻的表格边线和矢量图形。
    """

    if block.mask_image_png is not None and block.mask_bbox is not None:
        x0, top, x1, bottom = block.mask_bbox
        output_canvas.saveState()
        output_canvas.drawImage(
            ImageReader(io.BytesIO(block.mask_image_png)),
            x0,
            page_height - bottom,
            width=max(0.1, x1 - x0),
            height=max(0.1, bottom - top),
            preserveAspectRatio=False,
            mask="auto",
        )
        output_canvas.restoreState()
        return

    if block.source_type == "vision":
        padding_x = max(0.0, vision_mask_padding)
        padding_y = padding_x
    elif block.source_type == "image-table":
        # 图片表格的 bbox 已按真实网格向单元格内部收缩。这里不能再次外扩，
        # 否则抗锯齿后的 1px/2px 边线会被单元格背景色覆盖成短缺口。
        padding_x = 0.0
        padding_y = 0.0
    elif block.region_type == "table":
        padding_x = 0.45
        padding_y = 0.2
    else:
        padding_x = 0.8
        padding_y = 0.45
    x0, top, x1, bottom = block.bbox
    mask_left = max(0.0, x0 - padding_x)
    mask_top = max(0.0, top - padding_y)
    mask_right = x1 + padding_x
    mask_bottom = bottom + padding_y
    if block.source_type == "vision" and block.mask_bbox is not None:
        clip_left, clip_top, clip_right, clip_bottom = block.mask_bbox
        mask_left = max(mask_left, clip_left)
        mask_top = max(mask_top, clip_top)
        mask_right = min(mask_right, clip_right)
        mask_bottom = min(mask_bottom, clip_bottom)
    if mask_right <= mask_left or mask_bottom <= mask_top:
        return
    output_canvas.saveState()
    output_canvas.setFillColorRGB(*block.background_rgb)
    output_canvas.setStrokeColorRGB(*block.background_rgb)
    output_canvas.rect(
        mask_left,
        max(0.0, page_height - mask_bottom),
        max(0.1, mask_right - mask_left),
        max(0.1, mask_bottom - mask_top),
        stroke=0,
        fill=1,
    )
    output_canvas.restoreState()


def _draw_translated_text(
    output_canvas: canvas.Canvas,
    block: PdfTextBlock,
    text: str,
    page_height: float,
) -> tuple[bool, bool]:
    """在原文本框或表格单元格内自适应写入译文。"""

    x0, top, x1, bottom = block.render_bbox
    available_width = max(1.0, x1 - x0)
    available_height = max(1.0, bottom - top)
    minimum_font_size = _minimum_font_size_for_block(block)
    font_size, lines, overflow = _fit_text(
        text,
        available_width,
        available_height,
        block.font_size,
        minimum_font_size=minimum_font_size,
    )
    leading = font_size * 1.12
    first_baseline = page_height - top - font_size * 0.86
    font_name = (
        DEFAULT_BOLD_FONT_NAME
        if block.font_weight >= 600
        else DEFAULT_FONT_NAME
    )
    maximum_line_count = max(
        1,
        math.floor((available_height + font_size * 0.2) / leading),
    )
    output_canvas.saveState()
    vertical_clip_padding = min(4.0, max(2.0, font_size * 0.35))
    clipping_path = output_canvas.beginPath()
    clipping_path.rect(
        x0,
        page_height - bottom - vertical_clip_padding,
        available_width,
        available_height + vertical_clip_padding * 2,
    )
    output_canvas.clipPath(clipping_path, stroke=0, fill=0)
    output_canvas.setFillColorRGB(*block.text_rgb)
    output_canvas.setFont(font_name, font_size)
    for line_index, line in enumerate(lines[:maximum_line_count]):
        baseline = first_baseline - line_index * leading
        if block.alignment == "center":
            output_canvas.drawCentredString((x0 + x1) / 2, baseline, line)
        else:
            output_canvas.drawString(x0, baseline, line)
    output_canvas.restoreState()
    return overflow, font_size <= minimum_font_size + 0.01


def _fit_text(
    text: str,
    width: float,
    height: float,
    preferred_font_size: float,
    *,
    minimum_font_size: float = MINIMUM_FONT_SIZE,
) -> tuple[float, list[str], bool]:
    """逐级缩小字号，使译文尽量完整进入原区域。"""

    maximum_font_size = max(minimum_font_size, preferred_font_size)
    font_size = maximum_font_size
    while font_size >= minimum_font_size:
        lines = _wrap_text(text, width, font_size)
        if len(lines) * font_size * 1.12 <= height + font_size * 0.35:
            return font_size, lines, False
        font_size -= 0.5
    lines = _wrap_text(text, width, minimum_font_size)
    return minimum_font_size, lines, True


def _minimum_font_size_for_block(block: PdfTextBlock) -> float:
    """按正文、表格和图表区域设定可读字号下限。"""

    if block.source_type == "image-table":
        return MINIMUM_IMAGE_TABLE_FONT_SIZE
    if block.region_type == "table":
        return MINIMUM_TABLE_FONT_SIZE
    if block.region_type in {"chart", "image", "caption"}:
        return MINIMUM_CHART_FONT_SIZE
    return MINIMUM_BODY_FONT_SIZE


def _wrap_text(text: str, width: float, font_size: float) -> list[str]:
    """按实际字体宽度换行，兼容中英文混排。"""

    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for character in paragraph:
            candidate = f"{current}{character}"
            if (
                current
                and pdfmetrics.stringWidth(candidate, DEFAULT_FONT_NAME, font_size)
                > width
            ):
                lines.append(current.rstrip())
                current = character.lstrip()
            else:
                current = candidate
        if current or not lines:
            lines.append(current.rstrip())
    return lines or [""]


def _validate_preserved_pdf(source_path: Path, result_path: Path) -> None:
    """确认输出 PDF 页数和页面尺寸与原文一致。"""

    if not result_path.is_file() or result_path.stat().st_size == 0:
        raise RuntimeError("原版式 PDF 生成失败")
    source_reader = PdfReader(str(source_path))
    result_reader = PdfReader(str(result_path))
    if len(source_reader.pages) != len(result_reader.pages):
        raise RuntimeError("复原结果页数与原 PDF 不一致")
    for page_index, (source_page, result_page) in enumerate(
        zip(source_reader.pages, result_reader.pages)
    ):
        source_size = (
            round(float(source_page.mediabox.width), 2),
            round(float(source_page.mediabox.height), 2),
        )
        result_size = (
            round(float(result_page.mediabox.width), 2),
            round(float(result_page.mediabox.height), 2),
        )
        if source_size != result_size:
            raise RuntimeError(f"第 {page_index + 1} 页尺寸与原 PDF 不一致")


def _mineru_item_text(item: dict[str, object]) -> str:
    """从 MinerU 文本、表格和公式区块中提取纯文本，禁止 HTML 标签外泄。"""

    for key in ("text", "table_body", "content"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return _strip_markup(value)
    captions = item.get("table_caption")
    if isinstance(captions, list):
        return _normalize_source_text(
            " ".join(str(value) for value in captions if value)
        )
    return ""


def _mineru_table_html(item: dict[str, object]) -> str:
    """返回 MinerU 表格 HTML，非结构化表格不进入网格恢复。"""

    value = item.get("table_body")
    if (
        isinstance(value, str)
        and "<table" in value.casefold()
        and "</table>" in value.casefold()
    ):
        return value
    return ""


def _matching_image_region(
    page_index: int,
    table_bbox: tuple[float, float, float, float],
    image_regions: tuple[PdfImageRegion, ...],
) -> PdfImageRegion | None:
    """返回与 MinerU 图片表格坐标重合度最高的嵌入图片区域。"""

    candidates = [
        region
        for region in image_regions
        if region.page_index == page_index
        and _bbox_overlap_ratio(table_bbox, region.bbox) >= 0.8
    ]
    return min(
        candidates,
        key=lambda region: (
            (region.bbox[2] - region.bbox[0])
            * (region.bbox[3] - region.bbox[1])
        ),
        default=None,
    )


def _strip_markup(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", "\n", value)
    return _normalize_source_text(html.unescape(without_tags))


def _normalize_source_text(value: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _normalize_translation(value: str) -> str:
    return _normalize_source_text(_strip_markup(value))


def _inset_bbox(
    bbox: tuple[float, float, float, float],
    amount: float,
    *,
    vertical_amount: float | None = None,
) -> tuple[float, float, float, float]:
    """缩小文本渲染区域；视觉行可仅水平内缩以保留下伸字形。"""

    x0, top, x1, bottom = bbox
    vertical_inset = amount if vertical_amount is None else vertical_amount
    if x1 - x0 <= amount * 2 or bottom - top <= vertical_inset * 2:
        return bbox
    return (
        x0 + amount,
        top + vertical_inset,
        x1 - amount,
        bottom - vertical_inset,
    )


def _safe_integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
