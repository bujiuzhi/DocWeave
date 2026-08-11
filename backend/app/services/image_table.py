"""从 MinerU 表格结构和嵌入图片网格恢复可安全翻译的单元格坐标。"""

from __future__ import annotations

import io
import logging
import re
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser

from PIL import Image

from app.domain.pdf import PdfImageRegion, PdfTextBlock

LOGGER = logging.getLogger(__name__)
MAXIMUM_TABLE_ROWS = 80
MAXIMUM_TABLE_COLUMNS = 20
MAXIMUM_TABLE_CELLS = 400
MINIMUM_GRID_LINE_SCORE = 0.12
MINIMUM_GRID_COLOR_TRANSITION = 20
# 图片表格中的真实边线通常横跨绝大部分表格。低于该阈值的候选多为
# 文字笔画或数据行基线，不能据此推断单元格边界。
MINIMUM_SELECTED_GRID_LINE_SCORE = 0.40
MINIMUM_GRID_SEGMENT_RATIO = 0.35


@dataclass(frozen=True)
class StructuredTableCell:
    """MinerU HTML 表格中的一个逻辑单元格。"""

    row_index: int
    column_index: int
    row_span: int
    column_span: int
    source_text: str
    is_header: bool


@dataclass(frozen=True)
class ImageTableRecovery:
    """密集图片表格坐标恢复结果。"""

    blocks: tuple[PdfTextBlock, ...]
    row_count: int = 0
    column_count: int = 0
    confidence: float = 0.0
    failure_reason: str | None = None

    @property
    def succeeded(self) -> bool:
        """返回表格是否已恢复出可翻译单元格。"""

        return bool(self.blocks) and self.failure_reason is None


@dataclass(frozen=True)
class _RawTableCell:
    """尚未计算行列坐标的 HTML 单元格。"""

    source_text: str
    row_span: int
    column_span: int
    is_header: bool


class _TableHtmlParser(HTMLParser):
    """把 MinerU 表格 HTML 解析成保留合并关系的行。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[_RawTableCell]] = []
        self._current_row: list[_RawTableCell] | None = None
        self._current_parts: list[str] | None = None
        self._current_row_span = 1
        self._current_column_span = 1
        self._current_is_header = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        """处理表格行、单元格和换行起始标签。"""

        normalized_tag = tag.casefold()
        if normalized_tag == "tr":
            if self._current_row:
                self.rows.append(self._current_row)
            self._current_row = []
            return
        if normalized_tag not in {"td", "th"}:
            if normalized_tag == "br" and self._current_parts is not None:
                self._current_parts.append("\n")
            return
        if self._current_row is None:
            self._current_row = []
        attributes = {name.casefold(): value for name, value in attrs}
        self._current_parts = []
        self._current_row_span = _positive_span(attributes.get("rowspan"))
        self._current_column_span = _positive_span(attributes.get("colspan"))
        self._current_is_header = normalized_tag == "th"

    def handle_data(self, data: str) -> None:
        """收集当前单元格的文字内容。"""

        if self._current_parts is not None:
            self._current_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        """完成当前单元格或当前行。"""

        normalized_tag = tag.casefold()
        if normalized_tag in {"td", "th"} and self._current_parts is not None:
            if self._current_row is None:
                self._current_row = []
            self._current_row.append(
                _RawTableCell(
                    source_text=_normalize_cell_text("".join(self._current_parts)),
                    row_span=self._current_row_span,
                    column_span=self._current_column_span,
                    is_header=self._current_is_header,
                )
            )
            self._current_parts = None
            return
        if normalized_tag == "tr" and self._current_row is not None:
            if self._current_row:
                self.rows.append(self._current_row)
            self._current_row = None

    def close(self) -> None:
        """完成解析并提交没有显式结束标签的最后一行。"""

        super().close()
        if self._current_row:
            self.rows.append(self._current_row)
        self._current_row = None


def recover_image_table_blocks(
    *,
    table_id: str,
    page_index: int,
    table_bbox: tuple[float, float, float, float],
    table_html: str,
    image_region: PdfImageRegion,
    reading_order_start: int,
) -> ImageTableRecovery:
    """恢复密集图片表格的单元格页面坐标。

    Args:
        table_id: MinerU 表格区块稳定 ID。
        page_index: 表格所在页索引。
        table_bbox: MinerU 表格在 PDF 页面上的坐标。
        table_html: MinerU 返回的 HTML 表格结构。
        image_region: 表格所在的原始嵌入图片。
        reading_order_start: 单元格阅读顺序起点。

    Returns:
        可翻译单元格文本块或带原因的安全失败结果。
    """

    cells, row_count, column_count = parse_structured_table(table_html)
    if not cells:
        return ImageTableRecovery(
            blocks=(),
            failure_reason="MinerU 表格结构为空或无法解析",
        )
    if (
        row_count < 2
        or column_count < 2
        or row_count > MAXIMUM_TABLE_ROWS
        or column_count > MAXIMUM_TABLE_COLUMNS
        or len(cells) > MAXIMUM_TABLE_CELLS
    ):
        return ImageTableRecovery(
            blocks=(),
            row_count=row_count,
            column_count=column_count,
            failure_reason=(
                f"表格规模不在安全范围内：{row_count} 行 × {column_count} 列，"
                f"{len(cells)} 个单元格"
            ),
        )

    crop_geometry = _table_crop_geometry(
        table_bbox,
        image_region.bbox,
        image_region.image_png,
    )
    if crop_geometry is None:
        return ImageTableRecovery(
            blocks=(),
            row_count=row_count,
            column_count=column_count,
            failure_reason="MinerU 表格坐标与嵌入图片不匹配",
        )
    image, crop_bbox = crop_geometry
    table_image = image.crop(crop_bbox)
    column_boundaries, column_confidence = _detect_axis_boundaries(
        table_image,
        axis="vertical",
        expected_segment_count=column_count,
    )
    row_boundaries, row_confidence = _detect_axis_boundaries(
        table_image,
        axis="horizontal",
        expected_segment_count=row_count,
    )
    if not column_boundaries or not row_boundaries:
        return ImageTableRecovery(
            blocks=(),
            row_count=row_count,
            column_count=column_count,
            confidence=min(column_confidence, row_confidence),
            failure_reason=(
                "图片网格线与 MinerU 行列结构无法可靠对应，已保留原图"
            ),
        )

    blocks = _build_cell_blocks(
        table_id=table_id,
        page_index=page_index,
        table_bbox=table_bbox,
        table_image=table_image,
        cells=cells,
        column_boundaries=column_boundaries,
        row_boundaries=row_boundaries,
        reading_order_start=reading_order_start,
    )
    if not blocks:
        return ImageTableRecovery(
            blocks=(),
            row_count=row_count,
            column_count=column_count,
            confidence=min(column_confidence, row_confidence),
            failure_reason="表格中没有可翻译的自然语言单元格",
        )
    return ImageTableRecovery(
        blocks=tuple(blocks),
        row_count=row_count,
        column_count=column_count,
        confidence=min(column_confidence, row_confidence),
    )


def parse_structured_table(
    table_html: str,
) -> tuple[tuple[StructuredTableCell, ...], int, int]:
    """解析 HTML 表格并计算包含合并单元格的逻辑行列坐标。

    Args:
        table_html: MinerU 返回的 HTML 表格。

    Returns:
        逻辑单元格、总行数和总列数。
    """

    if "<table" not in table_html.casefold():
        return (), 0, 0
    parser = _TableHtmlParser()
    try:
        parser.feed(table_html)
        parser.close()
    except Exception as error:
        LOGGER.warning("MinerU 表格 HTML 解析失败：%s", error)
        return (), 0, 0
    occupied: set[tuple[int, int]] = set()
    cells: list[StructuredTableCell] = []
    maximum_column = 0
    maximum_row = 0
    for row_index, row in enumerate(parser.rows):
        column_index = 0
        for raw_cell in row:
            while (row_index, column_index) in occupied:
                column_index += 1
            cell = StructuredTableCell(
                row_index=row_index,
                column_index=column_index,
                row_span=raw_cell.row_span,
                column_span=raw_cell.column_span,
                source_text=raw_cell.source_text,
                is_header=raw_cell.is_header or row_index == 0,
            )
            cells.append(cell)
            for occupied_row in range(row_index, row_index + cell.row_span):
                for occupied_column in range(
                    column_index,
                    column_index + cell.column_span,
                ):
                    occupied.add((occupied_row, occupied_column))
            maximum_column = max(
                maximum_column,
                column_index + cell.column_span,
            )
            maximum_row = max(maximum_row, row_index + cell.row_span)
            column_index += cell.column_span
    return tuple(cells), maximum_row, maximum_column


def _table_crop_geometry(
    table_bbox: tuple[float, float, float, float],
    region_bbox: tuple[float, float, float, float],
    image_png: bytes,
) -> tuple[Image.Image, tuple[int, int, int, int]] | None:
    """把页面表格坐标转换为嵌入图片像素裁剪框。"""

    region_width = max(0.1, region_bbox[2] - region_bbox[0])
    region_height = max(0.1, region_bbox[3] - region_bbox[1])
    intersection_width = max(
        0.0,
        min(table_bbox[2], region_bbox[2]) - max(table_bbox[0], region_bbox[0]),
    )
    intersection_height = max(
        0.0,
        min(table_bbox[3], region_bbox[3]) - max(table_bbox[1], region_bbox[1]),
    )
    table_area = max(
        0.1,
        (table_bbox[2] - table_bbox[0]) * (table_bbox[3] - table_bbox[1]),
    )
    if intersection_width * intersection_height / table_area < 0.8:
        return None
    try:
        image = Image.open(io.BytesIO(image_png)).convert("RGB")
    except Exception as error:
        LOGGER.warning("图片表格解码失败：%s", error)
        return None
    left = round((table_bbox[0] - region_bbox[0]) / region_width * image.width)
    top = round((table_bbox[1] - region_bbox[1]) / region_height * image.height)
    right = round((table_bbox[2] - region_bbox[0]) / region_width * image.width)
    bottom = round((table_bbox[3] - region_bbox[1]) / region_height * image.height)
    crop_bbox = (
        min(max(left, 0), image.width - 1),
        min(max(top, 0), image.height - 1),
        min(max(right, 1), image.width),
        min(max(bottom, 1), image.height),
    )
    if crop_bbox[2] - crop_bbox[0] < 20 or crop_bbox[3] - crop_bbox[1] < 20:
        return None
    return image, crop_bbox


def _detect_axis_boundaries(
    image: Image.Image,
    *,
    axis: str,
    expected_segment_count: int,
) -> tuple[tuple[int, ...], float]:
    """按全行或全列的颜色突变检测表格网格边界。"""

    if expected_segment_count < 1:
        return (), 0.0
    axis_length = image.width if axis == "vertical" else image.height
    orthogonal_length = image.height if axis == "vertical" else image.width
    if axis_length < expected_segment_count * 3 or orthogonal_length < 8:
        return (), 0.0
    pixels = image.load()
    sample_stride = max(1, orthogonal_length // 600)
    candidates: list[tuple[int, float]] = []
    for position in range(1, axis_length - 1):
        dark_count = 0
        transition_count = 0
        sample_count = 0
        for orthogonal_position in range(0, orthogonal_length, sample_stride):
            if axis == "vertical":
                previous_pixel = pixels[position - 1, orthogonal_position]
                current_pixel = pixels[position, orthogonal_position]
            else:
                previous_pixel = pixels[orthogonal_position, position - 1]
                current_pixel = pixels[orthogonal_position, position]
            sample_count += 1
            if max(current_pixel) <= 105:
                dark_count += 1
            if max(
                abs(current_pixel[channel] - previous_pixel[channel])
                for channel in range(3)
            ) >= MINIMUM_GRID_COLOR_TRANSITION:
                transition_count += 1
        score = max(
            dark_count / max(1, sample_count),
            transition_count / max(1, sample_count),
        )
        if score >= MINIMUM_GRID_LINE_SCORE:
            candidates.append((position, score))
    collapsed_candidates = _collapse_line_candidates(candidates)
    # MinerU 的表格外框常比真实图片表格多出几像素留白，不能把裁剪框的
    # 0 和末端直接当作表格外边界。把裁剪边缘作为较低优先级的兜底候选，
    # 再与图片中检测到的真实边线共同选择 expected + 1 条边界。
    boundary_candidates = list(collapsed_candidates)
    edge_fallback_score = MINIMUM_SELECTED_GRID_LINE_SCORE
    boundary_candidates.extend(
        ((0, edge_fallback_score), (axis_length, edge_fallback_score))
    )
    required_boundary_count = expected_segment_count + 1
    if len(boundary_candidates) < required_boundary_count:
        return (), 0.0
    average_segment_length = axis_length / expected_segment_count
    minimum_gap = max(
        2,
        round(average_segment_length * MINIMUM_GRID_SEGMENT_RATIO),
    )
    selected: list[tuple[int, float]] = []
    for candidate in sorted(
        boundary_candidates,
        key=lambda value: value[1],
        reverse=True,
    ):
        if any(
            abs(candidate[0] - existing[0]) < minimum_gap
            for existing in selected
        ):
            continue
        selected.append(candidate)
        if len(selected) == required_boundary_count:
            break
    if len(selected) != required_boundary_count:
        return (), 0.0
    selected.sort(key=lambda value: value[0])
    boundaries = tuple(position for position, _ in selected)
    if any(
        boundaries[index + 1] - boundaries[index] < minimum_gap
        for index in range(len(boundaries) - 1)
    ):
        return (), 0.0
    confidence = min(score for _, score in selected) if selected else 1.0
    if confidence < MINIMUM_SELECTED_GRID_LINE_SCORE:
        return (), confidence
    return tuple(boundaries), confidence


def _collapse_line_candidates(
    candidates: list[tuple[int, float]],
) -> list[tuple[int, float]]:
    """把同一条宽网格线的连续候选像素合并为一个中心点。"""

    if not candidates:
        return []
    groups: list[list[tuple[int, float]]] = [[candidates[0]]]
    for candidate in candidates[1:]:
        if candidate[0] - groups[-1][-1][0] <= 2:
            groups[-1].append(candidate)
        else:
            groups.append([candidate])
    collapsed: list[tuple[int, float]] = []
    for group in groups:
        strongest = max(group, key=lambda value: value[1])
        collapsed.append(strongest)
    return collapsed


def _build_cell_blocks(
    *,
    table_id: str,
    page_index: int,
    table_bbox: tuple[float, float, float, float],
    table_image: Image.Image,
    cells: tuple[StructuredTableCell, ...],
    column_boundaries: tuple[int, ...],
    row_boundaries: tuple[int, ...],
    reading_order_start: int,
) -> list[PdfTextBlock]:
    """把逻辑单元格和像素网格转换为页面坐标文本块。"""

    table_width = max(0.1, table_bbox[2] - table_bbox[0])
    table_height = max(0.1, table_bbox[3] - table_bbox[1])
    blocks: list[PdfTextBlock] = []
    for cell_index, cell in enumerate(cells, start=1):
        if not cell.source_text or not _contains_natural_language(cell.source_text):
            continue
        if (
            cell.column_index + cell.column_span >= len(column_boundaries)
            or cell.row_index + cell.row_span >= len(row_boundaries)
        ):
            continue
        pixel_bbox = (
            column_boundaries[cell.column_index],
            row_boundaries[cell.row_index],
            column_boundaries[cell.column_index + cell.column_span],
            row_boundaries[cell.row_index + cell.row_span],
        )
        cell_bbox = (
            table_bbox[0] + pixel_bbox[0] / table_image.width * table_width,
            table_bbox[1] + pixel_bbox[1] / table_image.height * table_height,
            table_bbox[0] + pixel_bbox[2] / table_image.width * table_width,
            table_bbox[1] + pixel_bbox[3] / table_image.height * table_height,
        )
        horizontal_inset = min(1.5, max(0.6, (cell_bbox[2] - cell_bbox[0]) * 0.02))
        vertical_inset = min(1.2, max(0.5, (cell_bbox[3] - cell_bbox[1]) * 0.08))
        text_bbox = (
            cell_bbox[0] + horizontal_inset,
            cell_bbox[1] + vertical_inset,
            cell_bbox[2] - horizontal_inset,
            cell_bbox[3] - vertical_inset,
        )
        if text_bbox[2] <= text_bbox[0] or text_bbox[3] <= text_bbox[1]:
            continue
        background_rgb = _sample_cell_background(table_image, pixel_bbox)
        luminance = (
            background_rgb[0] * 0.2126
            + background_rgb[1] * 0.7152
            + background_rgb[2] * 0.0722
        )
        line_count = max(1, cell.source_text.count("\n") + 1)
        cell_height = text_bbox[3] - text_bbox[1]
        maximum_font_size = 11.0 if cell.is_header else 8.0
        preferred_font_size = max(
            4.5,
            min(maximum_font_size, cell_height / line_count * 0.66),
        )
        blocks.append(
            PdfTextBlock(
                id=f"{table_id}-c{cell_index:04d}",
                page_index=page_index,
                source_text=cell.source_text,
                bbox=text_bbox,
                render_bbox=text_bbox,
                font_size=preferred_font_size,
                alignment=(
                    "center"
                    if cell.is_header or cell.column_index > 0
                    else "left"
                ),
                background_rgb=background_rgb,
                text_rgb=(
                    (1.0, 1.0, 1.0)
                    if luminance < 0.42
                    else (0.0, 0.0, 0.0)
                ),
                source_type="image-table",
                font_weight=600 if cell.is_header else 400,
                region_type="table",
                table_cell=cell_bbox,
                reading_order=reading_order_start + cell_index,
                fragment_confidence=0.92,
                geometry_fill_ratio=1.0,
            )
        )
    return blocks


def _sample_cell_background(
    image: Image.Image,
    pixel_bbox: tuple[int, int, int, int],
) -> tuple[float, float, float]:
    """采样单元格主背景色，避开边框和少量原文字形。"""

    left, top, right, bottom = pixel_bbox
    inset_x = min(max(1, (right - left) // 12), 6)
    inset_y = min(max(1, (bottom - top) // 8), 5)
    sample_bbox = (
        min(right - 1, left + inset_x),
        min(bottom - 1, top + inset_y),
        max(left + 1, right - inset_x),
        max(top + 1, bottom - inset_y),
    )
    if sample_bbox[2] <= sample_bbox[0] or sample_bbox[3] <= sample_bbox[1]:
        return (1.0, 1.0, 1.0)
    crop = image.crop(sample_bbox).convert("RGB")
    stride = max(1, min(crop.size) // 24)
    colors = Counter(
        tuple(round(channel / 8) * 8 for channel in crop.getpixel((x, y)))
        for y in range(0, crop.height, stride)
        for x in range(0, crop.width, stride)
    )
    if not colors:
        return (1.0, 1.0, 1.0)
    dominant = colors.most_common(1)[0][0]
    return tuple(min(255, channel) / 255 for channel in dominant)


def _contains_natural_language(value: str) -> bool:
    """判断单元格是否包含需要参与翻译的自然语言字符。"""

    return bool(
        re.search(
            r"[A-Za-z\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]",
            value,
        )
    )


def _positive_span(value: str | None) -> int:
    """把 rowspan 或 colspan 转换为安全正整数。"""

    try:
        return max(1, min(100, int(value or "1")))
    except ValueError:
        return 1


def _normalize_cell_text(value: str) -> str:
    """规范化单元格空白，同时保留显式换行。"""

    lines = [re.sub(r"\s+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()
