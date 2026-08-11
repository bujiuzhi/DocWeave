"""验证密集图片表格只能在结构和网格一致时执行单元格级回填。"""

import io
import unittest

from PIL import Image, ImageDraw

from app.domain.pdf import PdfImageRegion
from app.services.image_table import (
    parse_structured_table,
    recover_image_table_blocks,
)


class ImageTableRecoveryTest(unittest.TestCase):
    """图片表格恢复必须保持单元格边界并安全失败。"""

    def test_html_rowspan_and_colspan_are_mapped_to_logical_grid(self) -> None:
        """HTML 合并单元格必须转换为稳定逻辑行列坐标。"""

        cells, row_count, column_count = parse_structured_table(
            """
            <table>
              <tr><th rowspan="2">属性</th><th colspan="2">典型值</th></tr>
              <tr><th>A</th><th>B</th></tr>
              <tr><td>结果</td><td>合格</td><td>合格</td></tr>
            </table>
            """
        )

        self.assertEqual(row_count, 3)
        self.assertEqual(column_count, 3)
        self.assertEqual(cells[0].row_span, 2)
        self.assertEqual(cells[1].column_span, 2)
        self.assertEqual((cells[-1].row_index, cells[-1].column_index), (2, 2))

    def test_unequal_grid_columns_are_recovered_from_image_lines(self) -> None:
        """非等宽表格必须依据真实网格线恢复，而不是平均切分。"""

        region = self._build_grid_region(
            width=400,
            height=120,
            columns=(0, 200, 300, 399),
            rows=(0, 40, 80, 119),
        )
        recovery = recover_image_table_blocks(
            table_id="p0001-m0001",
            page_index=0,
            table_bbox=(0.0, 0.0, 400.0, 120.0),
            table_html=(
                "<table>"
                "<tr><th>Property</th><th>A</th><th>B</th></tr>"
                "<tr><td>Loss tangent</td><td>0.1</td><td>0.2</td></tr>"
                "<tr><td>Result</td><td>Pass</td><td>Pass</td></tr>"
                "</table>"
            ),
            image_region=region,
            reading_order_start=0,
        )

        self.assertTrue(recovery.succeeded)
        self.assertEqual(recovery.row_count, 3)
        self.assertEqual(recovery.column_count, 3)
        property_block = recovery.blocks[0]
        self.assertEqual(property_block.source_type, "image-table")
        self.assertAlmostEqual(property_block.table_cell[2], 200.0, delta=2.0)
        pass_blocks = [
            block for block in recovery.blocks if block.source_text == "Pass"
        ]
        self.assertEqual(len(pass_blocks), 2)

    def test_gridless_dense_table_is_preserved(self) -> None:
        """图片中没有可信网格线时不得使用平均坐标强行覆盖。"""

        image_stream = io.BytesIO()
        Image.new("RGB", (400, 120), "white").save(
            image_stream,
            format="PNG",
        )
        region = PdfImageRegion(
            id="p0001-i0001",
            page_index=0,
            bbox=(0.0, 0.0, 400.0, 120.0),
            image_png=image_stream.getvalue(),
        )

        recovery = recover_image_table_blocks(
            table_id="p0001-m0001",
            page_index=0,
            table_bbox=region.bbox,
            table_html=(
                "<table><tr><th>Property</th><th>Value</th></tr>"
                "<tr><td>Result</td><td>Pass</td></tr></table>"
            ),
            image_region=region,
            reading_order_start=0,
        )

        self.assertFalse(recovery.succeeded)
        self.assertIn("网格线", recovery.failure_reason or "")

    def test_dark_header_background_uses_white_translation_text(self) -> None:
        """深色表头恢复后必须使用白色译文，保持原表视觉对比度。"""

        region = self._build_grid_region(
            width=300,
            height=80,
            columns=(0, 180, 299),
            rows=(0, 40, 79),
            header_color=(200, 20, 35),
        )
        recovery = recover_image_table_blocks(
            table_id="p0001-m0001",
            page_index=0,
            table_bbox=region.bbox,
            table_html=(
                "<table><tr><th>Property</th><th>Value</th></tr>"
                "<tr><td>Result</td><td>Pass</td></tr></table>"
            ),
            image_region=region,
            reading_order_start=0,
        )

        self.assertTrue(recovery.succeeded)
        self.assertEqual(recovery.blocks[0].text_rgb, (1.0, 1.0, 1.0))

    def test_real_grid_boundaries_override_image_crop_margin(self) -> None:
        """MinerU 外框含留白时必须使用图片真实外边线，不能从裁剪边缘起算。"""

        region = self._build_grid_region(
            width=300,
            height=100,
            columns=(8, 160, 292),
            rows=(6, 50, 94),
        )
        recovery = recover_image_table_blocks(
            table_id="p0001-m0001",
            page_index=0,
            table_bbox=region.bbox,
            table_html=(
                "<table><tr><th>Property</th><th>Value</th></tr>"
                "<tr><td>Result</td><td>Pass</td></tr></table>"
            ),
            image_region=region,
            reading_order_start=0,
        )

        self.assertTrue(recovery.succeeded)
        first_cell = recovery.blocks[0].table_cell
        self.assertIsNotNone(first_cell)
        assert first_cell is not None
        self.assertAlmostEqual(first_cell[0], 8.0, delta=2.0)
        self.assertAlmostEqual(first_cell[1], 6.0, delta=2.0)

    def test_low_contrast_white_grid_on_gray_cells_is_recovered(self) -> None:
        """浅灰底上的白色细网格仍应按连续颜色突变识别。"""

        image = Image.new("RGB", (300, 100), (220, 220, 220))
        drawing = ImageDraw.Draw(image)
        for x_coordinate in (5, 160, 294):
            drawing.line(
                (x_coordinate, 5, x_coordinate, 94),
                fill="white",
                width=2,
            )
        for y_coordinate in (5, 50, 94):
            drawing.line(
                (5, y_coordinate, 294, y_coordinate),
                fill="white",
                width=2,
            )
        image_stream = io.BytesIO()
        image.save(image_stream, format="PNG")
        region = PdfImageRegion(
            id="p0001-i0001",
            page_index=0,
            bbox=(0.0, 0.0, 300.0, 100.0),
            image_png=image_stream.getvalue(),
        )

        recovery = recover_image_table_blocks(
            table_id="p0001-m0001",
            page_index=0,
            table_bbox=region.bbox,
            table_html=(
                "<table><tr><th>Property</th><th>Value</th></tr>"
                "<tr><td>Result</td><td>Pass</td></tr></table>"
            ),
            image_region=region,
            reading_order_start=0,
        )

        self.assertTrue(recovery.succeeded)
        self.assertGreaterEqual(recovery.confidence, 0.40)

    def test_interrupted_text_like_strokes_are_not_treated_as_grid(self) -> None:
        """覆盖率不足的断续文字笔画不能被误判为表格边线。"""

        image = Image.new("RGB", (300, 100), "white")
        drawing = ImageDraw.Draw(image)
        for y_coordinate in (5, 50, 94):
            for x_coordinate in range(5, 295, 30):
                drawing.line(
                    (x_coordinate, y_coordinate, x_coordinate + 8, y_coordinate),
                    fill="black",
                    width=2,
                )
        for x_coordinate in (5, 160, 294):
            for y_coordinate in range(5, 95, 18):
                drawing.line(
                    (x_coordinate, y_coordinate, x_coordinate, y_coordinate + 5),
                    fill="black",
                    width=2,
                )
        image_stream = io.BytesIO()
        image.save(image_stream, format="PNG")
        region = PdfImageRegion(
            id="p0001-i0001",
            page_index=0,
            bbox=(0.0, 0.0, 300.0, 100.0),
            image_png=image_stream.getvalue(),
        )

        recovery = recover_image_table_blocks(
            table_id="p0001-m0001",
            page_index=0,
            table_bbox=region.bbox,
            table_html=(
                "<table><tr><th>Property</th><th>Value</th></tr>"
                "<tr><td>Result</td><td>Pass</td></tr></table>"
            ),
            image_region=region,
            reading_order_start=0,
        )

        self.assertFalse(recovery.succeeded)
        self.assertIn("网格线", recovery.failure_reason or "")

    def test_tall_body_cell_does_not_use_oversized_font(self) -> None:
        """高行表体不能因单元格高度较大而使用接近标题的字号。"""

        region = self._build_grid_region(
            width=300,
            height=200,
            columns=(0, 180, 299),
            rows=(0, 40, 199),
        )
        recovery = recover_image_table_blocks(
            table_id="p0001-m0001",
            page_index=0,
            table_bbox=region.bbox,
            table_html=(
                "<table><tr><th>Property</th><th>Value</th></tr>"
                "<tr><td>Dimensional stability after bake</td>"
                "<td>Pass</td></tr></table>"
            ),
            image_region=region,
            reading_order_start=0,
        )

        self.assertTrue(recovery.succeeded)
        body_block = next(
            block
            for block in recovery.blocks
            if block.source_text == "Dimensional stability after bake"
        )
        self.assertLessEqual(body_block.font_size, 8.0)

    @staticmethod
    def _build_grid_region(
        *,
        width: int,
        height: int,
        columns: tuple[int, ...],
        rows: tuple[int, ...],
        header_color: tuple[int, int, int] | None = None,
    ) -> PdfImageRegion:
        """生成具有指定非等距网格的图片表格测试区域。"""

        image = Image.new("RGB", (width, height), "white")
        drawing = ImageDraw.Draw(image)
        if header_color is not None:
            drawing.rectangle(
                (0, 0, width - 1, rows[1]),
                fill=header_color,
            )
        for x_coordinate in columns:
            drawing.line(
                (x_coordinate, 0, x_coordinate, height - 1),
                fill="black",
                width=2,
            )
        for y_coordinate in rows:
            drawing.line(
                (0, y_coordinate, width - 1, y_coordinate),
                fill="black",
                width=2,
            )
        image_stream = io.BytesIO()
        image.save(image_stream, format="PNG")
        return PdfImageRegion(
            id="p0001-i0001",
            page_index=0,
            bbox=(0.0, 0.0, float(width), float(height)),
            image_png=image_stream.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
