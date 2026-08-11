"""验证译文只覆盖原文字区域，并保留 PDF 页面与非文本版式。"""

import io
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from time import sleep
from unittest.mock import MagicMock
from unittest.mock import patch

import pdfplumber
from PIL import Image, ImageDraw
from pypdf import PdfReader
from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.services.pdf_layout import (
    PdfImageRegion,
    PdfLayoutDocument,
    PdfTextBlock,
    _NativeTextBox,
    _draw_original_text_mask,
    _draw_translated_text,
    _fit_text,
    _graphic_loss_ratios,
    _has_credible_graphical_grid,
    _infer_graphical_table_cell,
    _inset_bbox,
    _is_credible_detected_table,
    _is_coarse_image_table_block,
    _is_coarse_native_text_block,
    _is_visible_graphical_edge,
    _merge_orphan_non_table_continuations,
    _register_translation_fonts,
    _sample_background,
    _should_preserve_chart_proper_name,
    _table_render_bbox,
    extract_pdf_layout,
    extract_mineru_pdf_layout,
    extract_native_pdf_layout,
    render_pdf_image_regions,
    render_translated_pdf,
    should_use_mineru,
)


class PdfLayoutTest(unittest.TestCase):
    """原坐标回写必须保留页面尺寸、图形和表格边框。"""

    def test_table_cell_is_inferred_when_edge_orientation_is_wrong(self) -> None:
        """Word PDF 线段方向标签错误时仍应按几何关系恢复单元格。"""

        edges = (
            {
                "x0": 100.0,
                "x1": 100.0,
                "top": 10.0,
                "bottom": 40.0,
                "orientation": "h",
            },
            {
                "x0": 140.0,
                "x1": 140.0,
                "top": 10.0,
                "bottom": 40.0,
                "orientation": "h",
            },
            {
                "x0": 100.0,
                "x1": 140.0,
                "top": 10.0,
                "bottom": 10.0,
                "orientation": "v",
            },
            {
                "x0": 100.0,
                "x1": 140.0,
                "top": 40.0,
                "bottom": 40.0,
                "orientation": "v",
            },
        )

        cell = _infer_graphical_table_cell(
            (112.0, 20.0, 124.0, 29.0),
            edges,
        )

        self.assertEqual(cell, (100.0, 10.0, 140.0, 40.0))

    def test_chart_frame_is_not_treated_as_repeating_table_grid(self) -> None:
        """图表外框和标签引线不得触发表格单元格几何兜底。"""

        chart_edges = (
            {"x0": 50.0, "x1": 50.0, "top": 30.0, "bottom": 300.0},
            {"x0": 450.0, "x1": 450.0, "top": 30.0, "bottom": 300.0},
            {"x0": 50.0, "x1": 450.0, "top": 30.0, "bottom": 30.0},
            {"x0": 50.0, "x1": 450.0, "top": 300.0, "bottom": 300.0},
            {"x0": 180.0, "x1": 250.0, "top": 120.0, "bottom": 121.0},
            {"x0": 250.0, "x1": 250.0, "top": 120.0, "bottom": 170.0},
        )

        self.assertFalse(_has_credible_graphical_grid(chart_edges))

    def test_two_chart_frames_are_not_treated_as_table_grid(self) -> None:
        """并排图表的重复外框仍不能被误判为多行多列表格。"""

        chart_edges: list[dict[str, float]] = []
        for left, right in ((50.0, 250.0), (270.0, 470.0)):
            for _ in range(3):
                chart_edges.extend(
                    [
                        {
                            "x0": left,
                            "x1": left,
                            "top": 30.0,
                            "bottom": 300.0,
                        },
                        {
                            "x0": right,
                            "x1": right,
                            "top": 30.0,
                            "bottom": 300.0,
                        },
                        {
                            "x0": left,
                            "x1": right,
                            "top": 30.0,
                            "bottom": 30.0,
                        },
                        {
                            "x0": left,
                            "x1": right,
                            "top": 300.0,
                            "bottom": 300.0,
                        },
                    ]
                )

        self.assertFalse(_has_credible_graphical_grid(chart_edges))

    def test_nested_single_column_detection_is_not_credible_table(self) -> None:
        """表格内的单列换行框不得替代真正父单元格。"""

        nested_cells = [
            (443.8, 246.8, 494.6, 265.8),
            (443.8, 265.8, 494.6, 284.9),
        ]
        parent_cells = [
            (40.0, 200.0, 260.0, 240.0),
            (260.0, 200.0, 500.0, 240.0),
            (40.0, 240.0, 260.0, 290.0),
            (260.0, 240.0, 500.0, 290.0),
        ]

        self.assertFalse(_is_credible_detected_table(nested_cells))
        self.assertTrue(_is_credible_detected_table(parent_cells))

    def test_invisible_word_rectangle_is_not_table_edge(self) -> None:
        """Word 导出的白色文字框边缘不能参与表格单元格识别。"""

        invisible_edge = {
            "stroke": False,
            "fill": True,
            "non_stroking_color": 1.0,
        }
        visible_edge = {
            "stroke": False,
            "fill": True,
            "non_stroking_color": 0.0,
        }
        colored_background_edge = {
            "stroke": False,
            "fill": True,
            "non_stroking_color": (0.851, 0.949, 0.816),
        }

        self.assertFalse(_is_visible_graphical_edge(invisible_edge))
        self.assertTrue(_is_visible_graphical_edge(visible_edge))
        self.assertFalse(_is_visible_graphical_edge(colored_background_edge))

    def test_orphan_japanese_suffix_is_merged_with_chart_label(self) -> None:
        """图表标签换行产生的单字词尾应接回同列上一行。"""

        groups = [
            [
                _NativeTextBox(
                    text="放熱シート",
                    bbox=(128.5, 163.2, 174.3, 173.4),
                    characters=(),
                    table_cell=None,
                )
            ],
            [
                _NativeTextBox(
                    text="放熱シート用",
                    bbox=(450.8, 172.3, 506.6, 182.5),
                    characters=(),
                    table_cell=None,
                )
            ],
            [
                _NativeTextBox(
                    text="用",
                    bbox=(146.4, 176.4, 156.5, 186.5),
                    characters=(),
                    table_cell=None,
                )
            ],
        ]

        merged = _merge_orphan_non_table_continuations(groups)

        self.assertEqual(len(merged), 2)
        self.assertEqual(
            [text_box.text for text_box in merged[0]],
            ["放熱シート", "用"],
        )

    def test_multiline_table_cell_is_not_coarse_native_block(self) -> None:
        """表格单元格内的多行公司清单属于正常结构，不得阻断交付。"""

        table_block = PdfTextBlock(
            id="p0001-b0007",
            page_index=0,
            source_text="Company A\nCompany B\nCompany C\nCompany D",
            bbox=(100.0, 100.0, 240.0, 190.0),
            render_bbox=(100.0, 100.0, 240.0, 190.0),
            font_size=10.0,
            alignment="left",
            background_rgb=(1.0, 1.0, 1.0),
            text_rgb=(0.0, 0.0, 0.0),
            region_type="table",
        )
        body_block = PdfTextBlock(
            **{
                **table_block.__dict__,
                "id": "p0001-b0008",
                "region_type": "body",
            }
        )

        self.assertFalse(
            _is_coarse_native_text_block(
                table_block,
                (table_block.id,),
            )
        )
        self.assertTrue(
            _is_coarse_native_text_block(
                body_block,
                (body_block.id,),
            )
        )
        self.assertFalse(
            _is_coarse_native_text_block(
                body_block,
                (),
            )
        )

    def test_repeated_table_boundaries_are_credible_grid(self) -> None:
        """多行多列表格的重复边界应允许几何单元格恢复。"""

        table_edges: list[dict[str, float]] = []
        for left, right in ((100.0, 140.0), (140.0, 180.0), (180.0, 220.0)):
            for top, bottom in ((10.0, 30.0), (30.0, 50.0), (50.0, 70.0)):
                table_edges.extend(
                    [
                        {"x0": left, "x1": left, "top": top, "bottom": bottom},
                        {"x0": right, "x1": right, "top": top, "bottom": bottom},
                        {"x0": left, "x1": right, "top": top, "bottom": top},
                        {
                            "x0": left,
                            "x1": right,
                            "top": bottom,
                            "bottom": bottom,
                        },
                    ]
                )

        self.assertTrue(_has_credible_graphical_grid(table_edges))

    def test_native_chart_labels_keep_tight_independent_boxes(self) -> None:
        """饼图标签必须保持紧凑文字框，不能合并为覆盖整张图的区域。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / "chart.pdf"
            pdf_canvas = canvas.Canvas(str(source_path), pagesize=A4)
            page_width, page_height = A4
            pdf_canvas.rect(50, 300, page_width - 100, 450)
            pdf_canvas.wedge(180, 380, 360, 560, 0, 210, fill=1)
            pdf_canvas.line(270, 470, 410, 560)
            pdf_canvas.line(270, 470, 125, 400)
            pdf_canvas.setFont("Helvetica", 10)
            pdf_canvas.drawString(380, 565, "Alpha Tech Co., Ltd. 53.7%")
            pdf_canvas.drawString(70, 390, "Beta Materials Corp. 26.9%")
            pdf_canvas.save()

            layout = extract_native_pdf_layout(source_path)

            chart_blocks = [
                block
                for block in layout.blocks
                if "Alpha" in block.source_text or "Beta" in block.source_text
            ]
            self.assertEqual(len(chart_blocks), 2)
            self.assertTrue(all(block.table_cell is None for block in chart_blocks))
            self.assertTrue(
                all(
                    (block.bbox[2] - block.bbox[0])
                    * (block.bbox[3] - block.bbox[1])
                    < page_width * page_height * 0.02
                    for block in chart_blocks
                )
            )

    def test_chart_company_fragments_are_preserved_but_title_is_translated(self) -> None:
        """图表中的拆分企业名应保留，通用标题和“其他”仍应翻译。"""

        self.assertTrue(
            _should_preserve_chart_proper_name("Alpha Tech Co., Ltd.")
        )
        self.assertTrue(
            _should_preserve_chart_proper_name("TECHNOLOGY CO.,")
        )
        self.assertFalse(
            _should_preserve_chart_proper_name(
                "Manufacturers' share 3-layer FCCL- Volume"
            )
        )
        self.assertFalse(_should_preserve_chart_proper_name("Others"))

    def test_large_colored_chart_erasure_is_detected(self) -> None:
        """大面积彩色图形被白色遮盖时必须触发结构损失指标。"""

        source = Image.new("RGB", (300, 200), "white")
        ImageDraw.Draw(source).ellipse(
            (80, 30, 250, 190),
            fill=(67, 110, 160),
        )
        result = source.copy()
        ImageDraw.Draw(result).rectangle((70, 20, 260, 195), fill="white")

        colored_loss_ratio, dark_loss_ratio = _graphic_loss_ratios(
            source,
            result,
        )

        self.assertGreater(colored_loss_ratio, 0.2)
        self.assertGreater(dark_loss_ratio, 0.05)

    def test_table_mask_does_not_cover_cell_borders(self) -> None:
        """表格文字遮盖边距必须小于普通正文，避免擦除单元格边线。"""

        output_canvas = MagicMock()
        block = PdfTextBlock(
            id="p0001-b0001",
            page_index=0,
            source_text="Double-sided",
            bbox=(100.0, 20.0, 140.0, 32.0),
            render_bbox=(100.0, 20.0, 140.0, 32.0),
            font_size=9.0,
            alignment="center",
            background_rgb=(1.0, 1.0, 0.8),
            text_rgb=(0.0, 0.0, 0.0),
            region_type="table",
            table_cell=(98.0, 18.0, 142.0, 34.0),
        )

        _draw_original_text_mask(output_canvas, block, page_height=200.0)

        output_canvas.rect.assert_called_once()
        mask_left, mask_bottom, mask_width, mask_height = (
            output_canvas.rect.call_args.args
        )
        self.assertAlmostEqual(mask_left, 99.55)
        self.assertAlmostEqual(mask_bottom, 167.8)
        self.assertAlmostEqual(mask_width, 40.9)
        self.assertAlmostEqual(mask_height, 12.4)
        self.assertEqual(
            output_canvas.rect.call_args.kwargs,
            {"stroke": 0, "fill": 1},
        )

    def test_image_table_mask_never_expands_towards_grid_lines(self) -> None:
        """图片表格文字框已内缩，遮盖时不得再次向网格线外扩。"""

        output_canvas = MagicMock()
        block = PdfTextBlock(
            id="p0001-m0001-c0001",
            page_index=0,
            source_text="Laminate",
            bbox=(101.0, 21.0, 139.0, 31.0),
            render_bbox=(101.0, 21.0, 139.0, 31.0),
            font_size=8.0,
            alignment="center",
            background_rgb=(1.0, 1.0, 0.8),
            text_rgb=(0.0, 0.0, 0.0),
            source_type="image-table",
            region_type="table",
            table_cell=(100.0, 20.0, 140.0, 32.0),
        )

        _draw_original_text_mask(output_canvas, block, page_height=200.0)

        output_canvas.rect.assert_called_once_with(
            101.0,
            169.0,
            38.0,
            10.0,
            stroke=0,
            fill=1,
        )

    def test_table_background_sampling_ignores_border_and_text(self) -> None:
        """表格遮盖色应取单元格主色，不能采到边框形成灰色块。"""

        image = Image.new("RGB", (80, 50), (224, 239, 243))
        draw = ImageDraw.Draw(image)
        draw.rectangle((5, 5, 75, 45), outline=(0, 0, 0), width=2)
        draw.rectangle((25, 18, 48, 26), fill=(20, 20, 20))

        background = _sample_background(
            image,
            (25.0, 18.0, 48.0, 26.0),
            sampling_region=(5.0, 5.0, 75.0, 45.0),
        )

        self.assertAlmostEqual(background[0], 224 / 255, places=2)
        self.assertAlmostEqual(background[1], 240 / 255, places=2)
        self.assertAlmostEqual(background[2], 240 / 255, places=2)

    def test_dense_table_cell_keeps_usable_line_height(self) -> None:
        """密集表格不能因垂直留白过大而把两字中文误判为溢出。"""

        _register_translation_fonts()
        text_bbox = (290.9, 125.9, 302.0, 131.4)
        table_cell = (276.0, 123.8, 316.8, 132.7)
        render_bbox = _inset_bbox(
            _table_render_bbox(text_bbox, table_cell),
            1.5,
            vertical_amount=0.25,
        )
        width = render_bbox[2] - render_bbox[0]
        height = render_bbox[3] - render_bbox[1]

        _, _, overflow = _fit_text(
            "内制",
            width,
            height,
            5.5,
            minimum_font_size=6.5,
        )

        self.assertFalse(overflow)

    def test_native_layout_extraction_is_serialized(self) -> None:
        """多任务并发时底层 PDF 渲染入口不得同时执行。"""

        counter_lock = Lock()
        active_count = 0
        maximum_active_count = 0

        def record_extraction(_: Path) -> str:
            nonlocal active_count, maximum_active_count
            with counter_lock:
                active_count += 1
                maximum_active_count = max(maximum_active_count, active_count)
            sleep(0.03)
            with counter_lock:
                active_count -= 1
            return "layout"

        with patch(
            "app.services.pdf_layout._extract_native_layout",
            side_effect=record_extraction,
        ):
            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(
                    executor.map(
                        extract_native_pdf_layout,
                        [Path(f"source-{index}.pdf") for index in range(4)],
                    )
                )

        self.assertEqual(results, ["layout"] * 4)
        self.assertEqual(maximum_active_count, 1)

    def test_mineru_strategy_is_honored_for_native_pdf(self) -> None:
        """用户选择 MinerU 时不能因存在原生文本层而静默跳过。"""

        layout = PdfLayoutDocument(
            page_sizes=(A4,),
            blocks=(
                PdfTextBlock(
                    id="p0001-b0001",
                    page_index=0,
                    source_text="Product name",
                    bbox=(10, 10, 120, 30),
                    render_bbox=(10, 10, 120, 30),
                    font_size=10,
                    alignment="left",
                    background_rgb=(1, 1, 1),
                    text_rgb=(0, 0, 0),
                ),
            ),
            source_type="native",
            native_character_count=11,
            covered_native_character_count=11,
        )

        self.assertTrue(should_use_mineru(layout, "MinerU 结构解析"))
        self.assertFalse(should_use_mineru(layout, "原生文字优先"))

    def test_mineru_coarse_block_is_not_appended_to_native_text_page(self) -> None:
        """原生坐标完整时 MinerU 的孤立粗框不得进入覆盖渲染。"""

        native_layout = PdfLayoutDocument(
            page_sizes=((600.0, 800.0),),
            blocks=tuple(
                PdfTextBlock(
                    id=f"p0001-b{index:04d}",
                    page_index=0,
                    source_text=text,
                    bbox=(40.0, 40.0 + index * 20, 180.0, 52.0 + index * 20),
                    render_bbox=(40.0, 40.0 + index * 20, 180.0, 52.0 + index * 20),
                    font_size=10.0,
                    alignment="left",
                    background_rgb=(1.0, 1.0, 1.0),
                    text_rgb=(0.0, 0.0, 0.0),
                )
                for index, text in enumerate(
                    ("Table title", "General purpose", "LCP resin"),
                    start=1,
                )
            ),
            source_type="native",
            native_character_count=34,
            covered_native_character_count=34,
        )
        content_list = [
            {
                "type": "image",
                "text": "JMS",
                # 模型框位于页面另一侧，因此旧逻辑会把它追加成实体白色遮罩。
                "bbox": [700, 100, 980, 900],
                "page_idx": 0,
            }
        ]

        layout = extract_mineru_pdf_layout(
            Path("unused.pdf"),
            content_list,
            native_layout=native_layout,
        )

        self.assertEqual(len(layout.blocks), 3)
        self.assertTrue(
            all(block.source_type == "native" for block in layout.blocks)
        )

    def test_vision_mask_adds_safe_margin_for_inaccurate_ocr_boxes(self) -> None:
        """视觉框偏紧时仍应扩大遮盖范围，清除原字形边缘残留。"""

        output_canvas = MagicMock()
        block = PdfTextBlock(
            id="p0001-i0001-v0001",
            page_index=0,
            source_text="フッ素樹脂",
            bbox=(100.0, 20.0, 140.0, 32.0),
            render_bbox=(100.0, 20.0, 140.0, 32.0),
            font_size=9.0,
            alignment="center",
            background_rgb=(1.0, 1.0, 1.0),
            text_rgb=(0.0, 0.0, 0.0),
            source_type="vision",
        )

        _draw_original_text_mask(output_canvas, block, page_height=200.0)

        output_canvas.rect.assert_called_once_with(
            88.0,
            156.0,
            64.0,
            36.0,
            stroke=0,
            fill=1,
        )

    def test_visual_quality_retry_can_expand_mask_margin(self) -> None:
        """视觉复检失败后应能定向扩大图片文字遮盖范围。"""

        output_canvas = MagicMock()
        block = PdfTextBlock(
            id="p0001-i0001-v0001",
            page_index=0,
            source_text="カバーレイ",
            bbox=(100.0, 20.0, 140.0, 32.0),
            render_bbox=(100.0, 20.0, 140.0, 32.0),
            font_size=9.0,
            alignment="center",
            background_rgb=(1.0, 1.0, 1.0),
            text_rgb=(0.0, 0.0, 0.0),
            source_type="vision",
        )

        _draw_original_text_mask(
            output_canvas,
            block,
            page_height=200.0,
            vision_mask_padding=20.0,
        )

        output_canvas.rect.assert_called_once_with(
            80.0,
            148.0,
            80.0,
            52.0,
            stroke=0,
            fill=1,
        )

    def test_vision_mask_is_clipped_to_source_image_region(self) -> None:
        """图片文字遮罩外扩不得越界擦除相邻表格底边。"""

        output_canvas = MagicMock()
        block = PdfTextBlock(
            id="p0008-i0001-v0001",
            page_index=7,
            source_text="【図 A】",
            bbox=(131.0, 327.0, 160.0, 336.0),
            render_bbox=(131.0, 327.0, 160.0, 336.0),
            font_size=8.0,
            alignment="center",
            background_rgb=(1.0, 1.0, 1.0),
            text_rgb=(0.0, 0.0, 0.0),
            source_type="vision",
            mask_bbox=(85.1, 320.4, 547.1, 466.7),
        )

        _draw_original_text_mask(output_canvas, block, page_height=595.0)

        output_canvas.rect.assert_called_once()
        mask_left, mask_bottom, mask_width, mask_height = (
            output_canvas.rect.call_args.args
        )
        self.assertAlmostEqual(mask_left, 119.0)
        self.assertAlmostEqual(mask_bottom, 247.0)
        self.assertAlmostEqual(mask_width, 53.0)
        self.assertAlmostEqual(mask_height, 27.6)
        self.assertEqual(
            output_canvas.rect.call_args.kwargs,
            {"stroke": 0, "fill": 1},
        )

    def test_translation_clip_keeps_latin_descenders_and_punctuation(self) -> None:
        """译文裁剪区必须保留 y、g 和逗号等下伸字形。"""

        output_canvas = MagicMock()
        clipping_path = output_canvas.beginPath.return_value
        block = PdfTextBlock(
            id="p0001-b0001",
            page_index=0,
            source_text="Galaxy, Google",
            bbox=(10.0, 20.0, 110.0, 30.0),
            render_bbox=(10.0, 20.0, 110.0, 30.0),
            font_size=10.0,
            alignment="left",
            background_rgb=(1.0, 1.0, 1.0),
            text_rgb=(0.0, 0.0, 0.0),
        )

        _draw_translated_text(
            output_canvas,
            block,
            "Galaxy, Google",
            page_height=200.0,
        )

        clipping_path.rect.assert_called_once_with(
            10.0,
            166.5,
            100.0,
            17.0,
        )

    def test_native_pdf_preserves_layout_and_non_text_content(self) -> None:
        """机器生成 PDF 应只在原文字区域叠加译文。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source_path = directory / "source.pdf"
            result_path = directory / "result.pdf"
            self._write_layout_pdf(source_path)

            layout = extract_pdf_layout(source_path)
            translations = {
                block.id: self._translation_for(block.source_text)
                for block in layout.blocks
            }
            report = render_translated_pdf(
                source_path,
                result_path,
                layout,
                translations,
            )

            source_reader = PdfReader(str(source_path))
            result_reader = PdfReader(str(result_path))
            self.assertEqual(layout.source_type, "native")
            self.assertGreaterEqual(report.replaced_block_count, 5)
            self.assertEqual(source_reader.pdf_header, result_reader.pdf_header)
            self.assertEqual(len(source_reader.pages), len(result_reader.pages))
            self.assertEqual(
                float(source_reader.pages[0].mediabox.width),
                float(result_reader.pages[0].mediabox.width),
            )
            self.assertEqual(
                float(source_reader.pages[0].mediabox.height),
                float(result_reader.pages[0].mediabox.height),
            )

            with pdfplumber.open(source_path) as source_document, pdfplumber.open(
                result_path
            ) as result_document:
                source_page = source_document.pages[0]
                result_page = result_document.pages[0]
                self.assertGreaterEqual(
                    len(result_page.lines),
                    len(source_page.lines),
                )
                self.assertGreaterEqual(
                    len(result_page.rects),
                    len(source_page.rects),
                )
                self.assertNotIn("<table>", result_page.extract_text() or "")

                source_image = source_page.to_image(resolution=72).original.convert(
                    "RGB"
                )
                result_image = result_page.to_image(resolution=72).original.convert(
                    "RGB"
                )
                self.assertEqual(
                    source_image.getpixel((530, 780)),
                    result_image.getpixel((530, 780)),
                )

    def test_native_text_redaction_preserves_mixed_chart_background(self) -> None:
        """跨越白底和彩色扇区的标签不得产生单色矩形覆盖块。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source_path = directory / "chart-source.pdf"
            result_path = directory / "chart-result.pdf"
            page_width, page_height = A4
            pdf_canvas = canvas.Canvas(str(source_path), pagesize=A4)
            pdf_canvas.setFillColor(HexColor("#4777a8"))
            pdf_canvas.rect(100, 350, 250, 180, stroke=0, fill=1)
            pdf_canvas.setFillColorRGB(0, 0, 0)
            pdf_canvas.setFont("Helvetica", 14)
            pdf_canvas.drawString(
                315,
                430,
                "Coverlay application",
            )
            pdf_canvas.save()

            layout = extract_native_pdf_layout(source_path)
            target_block = next(
                block
                for block in layout.blocks
                if "Coverlay application" in block.source_text
            )
            recovered_target_block = PdfTextBlock(
                **{
                    **target_block.__dict__,
                    "source_type": "native-recovered",
                }
            )
            layout = PdfLayoutDocument(
                page_sizes=layout.page_sizes,
                blocks=tuple(
                    recovered_target_block
                    if block.id == target_block.id
                    else block
                    for block in layout.blocks
                ),
                source_type=layout.source_type,
                image_regions=layout.image_regions,
                native_character_count=layout.native_character_count,
                covered_native_character_count=(
                    layout.covered_native_character_count
                ),
            )
            translations = {
                block.id: (
                    "覆盖膜应用"
                    if block.id == target_block.id
                    else block.source_text
                )
                for block in layout.blocks
            }

            render_translated_pdf(
                source_path,
                result_path,
                layout,
                translations,
            )

            with pdfplumber.open(source_path) as source_document, pdfplumber.open(
                result_path
            ) as result_document:
                source_page = source_document.pages[0]
                result_page = result_document.pages[0]
                self.assertNotIn(
                    "Coverlay application",
                    result_page.extract_text() or "",
                )
                source_image = source_page.to_image(resolution=72).original.convert(
                    "RGB"
                )
                result_image = result_page.to_image(resolution=72).original.convert(
                    "RGB"
                )
                colored_loss_ratio, _ = _graphic_loss_ratios(
                    source_image,
                    result_image,
                )
                self.assertLess(colored_loss_ratio, 0.005)

    def test_native_layout_keeps_visual_lines_as_separate_blocks(self) -> None:
        """目录和列表的相邻视觉行不得合并成跨行大文本块。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / "lines.pdf"
            pdf_canvas = canvas.Canvas(str(source_path), pagesize=A4)
            pdf_canvas.setFont("Helvetica", 11)
            pdf_canvas.drawString(60, 760, "First market item ........ 1")
            pdf_canvas.drawString(60, 746, "Second market item ....... 2")
            pdf_canvas.drawString(60, 732, "Third market item ........ 3")
            pdf_canvas.save()

            layout = extract_native_pdf_layout(source_path)

            self.assertEqual(len(layout.blocks), 3)
            self.assertEqual(
                [block.source_text for block in layout.blocks],
                [
                    "First market item ........ 1",
                    "Second market item ....... 2",
                    "Third market item ........ 3",
                ],
            )
            self.assertTrue(
                all(
                    block.render_bbox[1] == block.bbox[1]
                    and block.render_bbox[3] == block.bbox[3]
                    for block in layout.blocks
                )
            )

    def test_quality_region_uses_composited_result_page(self) -> None:
        """视觉复检裁剪必须包含最终 PDF 覆盖层而不是原始图片对象。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            result_path = directory / "result.pdf"
            image_stream = io.BytesIO()
            Image.new("RGB", (100, 50), "red").save(
                image_stream,
                format="PNG",
            )
            pdf_canvas = canvas.Canvas(str(result_path), pagesize=A4)
            pdf_canvas.drawImage(
                ImageReader(io.BytesIO(image_stream.getvalue())),
                100,
                400,
                width=100,
                height=50,
            )
            pdf_canvas.setFillColorRGB(0, 1, 0)
            pdf_canvas.rect(120, 410, 40, 20, stroke=0, fill=1)
            pdf_canvas.save()
            page_height = float(A4[1])
            region = PdfImageRegion(
                id="p0001-i0001",
                page_index=0,
                bbox=(
                    100.0,
                    page_height - 450.0,
                    200.0,
                    page_height - 400.0,
                ),
                image_png=image_stream.getvalue(),
            )

            rendered_regions = render_pdf_image_regions(
                result_path,
                (region,),
            )

            rendered_image = Image.open(
                io.BytesIO(rendered_regions[0].image_png)
            ).convert("RGB")
            red, green, blue = rendered_image.getpixel((80, 60))
            self.assertLess(red, 50)
            self.assertGreater(green, 200)
            self.assertLess(blue, 50)

    def test_mineru_table_markup_never_reaches_render_text(self) -> None:
        """扫描件兜底时应剥离 MinerU 表格 HTML 标签。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / "scan.pdf"
            pdf_canvas = canvas.Canvas(str(source_path), pagesize=A4)
            pdf_canvas.showPage()
            pdf_canvas.save()
            content_list = [
                {
                    "type": "table",
                    "table_body": (
                        "<table><tr><td>Product</td><td>Supplier</td></tr></table>"
                    ),
                    "bbox": [100, 100, 900, 300],
                    "page_idx": 0,
                }
            ]

            layout = extract_pdf_layout(source_path, content_list)

            self.assertEqual(layout.source_type, "mineru")
            self.assertEqual(len(layout.blocks), 1)
            self.assertNotIn("<table>", layout.blocks[0].source_text)
            self.assertIn("Product", layout.blocks[0].source_text)

    def test_coarse_mineru_image_table_is_preserved(self) -> None:
        """整张图片表格只有一个 MinerU 外框时不得作为普通文本覆盖。"""

        image_stream = io.BytesIO()
        Image.new("RGB", (600, 300), "white").save(
            image_stream,
            format="PNG",
        )
        native_layout = PdfLayoutDocument(
            page_sizes=((600.0, 800.0),),
            blocks=(),
            source_type="native",
            image_regions=(
                PdfImageRegion(
                    id="p0001-i0001",
                    page_index=0,
                    bbox=(60.0, 100.0, 540.0, 340.0),
                    image_png=image_stream.getvalue(),
                ),
            ),
        )
        content_list = [
            {
                "type": "table",
                "table_body": (
                    "<table><tr><td>Property</td><td>Typical Value</td></tr>"
                    "<tr><td>Dielectric constant at 10 GHz</td><td>3.2</td></tr>"
                    "<tr><td>Loss tangent at 10 GHz</td><td>0.003</td></tr>"
                    "<tr><td>Peel strength after solder</td><td>Pass</td></tr>"
                    "</table>"
                ),
                "bbox": [100, 125, 900, 425],
                "page_idx": 0,
            }
        ]

        layout = extract_mineru_pdf_layout(
            Path("unused.pdf"),
            content_list,
            native_layout=native_layout,
        )

        self.assertEqual(len(layout.blocks), 1)
        self.assertFalse(layout.blocks[0].is_translatable)
        self.assertIsNotNone(layout.blocks[0].preserve_reason)
        self.assertTrue(
            _is_coarse_image_table_block(
                layout.blocks[0],
                native_layout.image_regions[0],
            )
        )

    def test_mineru_image_table_with_matching_grid_becomes_cell_blocks(self) -> None:
        """MinerU 表格结构与图片网格一致时必须进入单元格级回填。"""

        image = Image.new("RGB", (480, 240), "white")
        drawing = ImageDraw.Draw(image)
        for x_coordinate in (0, 240, 479):
            drawing.line(
                (x_coordinate, 0, x_coordinate, 239),
                fill="black",
                width=2,
            )
        for y_coordinate in (0, 120, 239):
            drawing.line(
                (0, y_coordinate, 479, y_coordinate),
                fill="black",
                width=2,
            )
        image_stream = io.BytesIO()
        image.save(image_stream, format="PNG")
        native_layout = PdfLayoutDocument(
            page_sizes=((600.0, 800.0),),
            blocks=(),
            source_type="native",
            image_regions=(
                PdfImageRegion(
                    id="p0001-i0001",
                    page_index=0,
                    bbox=(60.0, 100.0, 540.0, 340.0),
                    image_png=image_stream.getvalue(),
                ),
            ),
        )
        content_list = [
            {
                "type": "table",
                "table_body": (
                    "<table><tr><th>Property</th><th>Supplier</th></tr>"
                    "<tr><td>Film type</td><td>Example Corp.</td></tr>"
                    "</table>"
                ),
                "bbox": [100, 125, 900, 425],
                "page_idx": 0,
            }
        ]

        layout = extract_mineru_pdf_layout(
            Path("unused.pdf"),
            content_list,
            native_layout=native_layout,
        )

        self.assertEqual(len(layout.blocks), 4)
        self.assertTrue(
            all(block.source_type == "image-table" for block in layout.blocks)
        )
        self.assertTrue(all(block.is_translatable for block in layout.blocks))
        self.assertEqual(layout.image_table_block_count, 4)

    def test_form_text_and_embedded_image_are_in_quality_scope(self) -> None:
        """图形对象文字必须补齐，大幅嵌入图片必须进入视觉识别范围。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source_path = directory / "form-and-image.pdf"
            image = Image.new("RGB", (400, 120), "white")
            ImageDraw.Draw(image).text((20, 40), "IMAGE TEXT", fill="black")
            image_path = directory / "diagram.png"
            image.save(image_path)

            pdf_canvas = canvas.Canvas(str(source_path), pagesize=A4)
            pdf_canvas.beginForm("chart-labels")
            pdf_canvas.setFont("Helvetica", 12)
            pdf_canvas.drawString(20, 20, "Other market segments")
            pdf_canvas.endForm()
            pdf_canvas.saveState()
            pdf_canvas.translate(100, 700)
            pdf_canvas.doForm("chart-labels")
            pdf_canvas.restoreState()
            pdf_canvas.drawImage(
                ImageReader(str(image_path)),
                100,
                450,
                width=300,
                height=90,
            )
            pdf_canvas.showPage()
            pdf_canvas.save()

            layout = extract_pdf_layout(source_path)

            self.assertEqual(layout.native_text_coverage, 1.0)
            self.assertTrue(
                any(
                    "Other" in block.source_text
                    and block.source_type == "native-recovered"
                    and block.mask_image_png is None
                    for block in layout.blocks
                )
            )
            self.assertEqual(len(layout.image_regions), 1)

    @staticmethod
    def _translation_for(source_text: str) -> str:
        translations = {
            "Market Overview": "市场概览",
            "Left column text stays here.": "左栏文字保持原位。",
            "Right column text stays here.": "右栏文字保持原位。",
            "Category": "类别",
            "Supplier": "供应商",
            "Film": "薄膜",
            "Example Corp.": "示例公司",
            "Original footer": "原始页脚",
        }
        return translations.get(source_text, f"译文：{source_text}")

    @staticmethod
    def _write_layout_pdf(destination: Path) -> None:
        """生成含深色标题栏、双栏、表格和装饰图形的版式样例。"""

        page_width, page_height = A4
        pdf_canvas = canvas.Canvas(str(destination), pagesize=A4)
        pdf_canvas.setTitle("Layout Source")
        pdf_canvas.setFillColor(HexColor("#123B66"))
        pdf_canvas.rect(40, page_height - 110, page_width - 80, 70, stroke=0, fill=1)
        pdf_canvas.setFillColor(white)
        pdf_canvas.setFont("Helvetica-Bold", 20)
        pdf_canvas.drawString(60, page_height - 82, "Market Overview")

        pdf_canvas.setFillColor(HexColor("#1F2937"))
        pdf_canvas.setFont("Helvetica", 11)
        pdf_canvas.drawString(60, page_height - 150, "Left column text stays here.")
        pdf_canvas.drawString(320, page_height - 150, "Right column text stays here.")

        table_left = 60
        table_bottom = page_height - 290
        table_width = 475
        row_height = 36
        pdf_canvas.setStrokeColor(HexColor("#64748B"))
        pdf_canvas.rect(table_left, table_bottom, table_width, row_height * 2)
        pdf_canvas.line(
            table_left,
            table_bottom + row_height,
            table_left + table_width,
            table_bottom + row_height,
        )
        pdf_canvas.line(
            table_left + 210,
            table_bottom,
            table_left + 210,
            table_bottom + row_height * 2,
        )
        pdf_canvas.setFillColor(HexColor("#111827"))
        pdf_canvas.drawString(70, table_bottom + 50, "Category")
        pdf_canvas.drawString(280, table_bottom + 50, "Supplier")
        pdf_canvas.drawString(70, table_bottom + 14, "Film")
        pdf_canvas.drawString(280, table_bottom + 14, "Example Corp.")

        pdf_canvas.setFillColor(HexColor("#E34A3B"))
        pdf_canvas.circle(530, 62, 16, stroke=0, fill=1)
        pdf_canvas.setFillColor(HexColor("#475569"))
        pdf_canvas.setFont("Helvetica", 9)
        pdf_canvas.drawString(60, 58, "Original footer")
        pdf_canvas.save()


if __name__ == "__main__":
    unittest.main()
