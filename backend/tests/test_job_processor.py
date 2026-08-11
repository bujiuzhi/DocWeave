"""验证真实任务处理器会生成非空 PDF 产物。"""

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from PIL import Image
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.models import (
    CreateJobRequest,
    JobLogLevel,
    JobStatus,
    LocalizationJob,
    TranslationPolicy,
)
from app.services.job_processor import (
    JobProcessor,
    LayoutTranslationResult,
    LlmTextResult,
    TokenUsage,
    VisualResidual,
    _build_visual_residual_repairs,
    _build_vision_blocks,
    _chunk_layout_blocks,
    _extract_block_translations,
    _estimate_layout_character_capacity,
    _ensure_filename_target_language,
    _filename_already_matches_target_language,
    _filter_preserved_visual_residuals,
    _find_layout_granularity_issue,
    _get_llm_base_url,
    _is_model_or_standard_residual,
    _post_llm_request,
    _repair_common_table_terms,
    _repair_generic_english_residuals,
    _review_image_region,
    _sample_vision_background,
    _select_visual_translation_regions,
    _translate_persisted_batch,
    _validate_translation_quality,
    _visual_residual_overlapping_block_ids,
    build_result_file_name,
    translate_layout_with_llm,
    translate_file_name_with_llm,
)
from app.services.pdf_layout import PdfImageRegion, PdfLayoutDocument, PdfTextBlock
from app.storage import JobRepository
from app.version import PDF_PIPELINE_VERSION


class JobProcessorTest(unittest.TestCase):
    """解析与翻译结果必须落成真实可下载文件。"""

    def test_remote_llm_http_requires_explicit_opt_in(self) -> None:
        """远程明文 LLM 地址不得在未授权时接收 API Key。"""

        with patch.dict(
            "os.environ",
            {
                "LLM_BASE_URL": "http://llm.example.com/v1",
                "DOCWEAVE_ALLOW_INSECURE_LLM_HTTP": "false",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "必须使用 HTTPS"):
                _get_llm_base_url()

    def test_loopback_llm_http_is_allowed_for_local_development(self) -> None:
        """本机开发服务可使用环回 HTTP 地址。"""

        with patch.dict(
            "os.environ",
            {"LLM_BASE_URL": "http://127.0.0.1:9000/v1"},
            clear=False,
        ):
            self.assertEqual(
                _get_llm_base_url(),
                "http://127.0.0.1:9000/v1",
            )

    def test_vision_background_ignores_table_lines(self) -> None:
        """白色表格单元格的黑线和文字不能把补译背景采样成灰色。"""

        image = Image.new("RGB", (200, 100), "white")
        for x in range(20, 181):
            image.putpixel((x, 20), (0, 0, 0))
            image.putpixel((x, 80), (0, 0, 0))
        for y in range(20, 81):
            image.putpixel((20, y), (0, 0, 0))
            image.putpixel((180, y), (0, 0, 0))
        for x in range(70, 130):
            image.putpixel((x, 50), (0, 0, 0))
        image_buffer = io.BytesIO()
        image.save(image_buffer, format="PNG")

        background = _sample_vision_background(
            image_buffer.getvalue(),
            (100.0, 200.0, 900.0, 800.0),
        )

        self.assertEqual(background, (1.0, 1.0, 1.0))

    def test_layout_gate_rejects_sparse_page_spanning_mask(self) -> None:
        """跨越大面积图形的低密度文字框必须在翻译前被阻断。"""

        block = PdfTextBlock(
            id="p0001-b0001",
            page_index=0,
            source_text="Manufacturers labels",
            bbox=(40.0, 40.0, 500.0, 500.0),
            render_bbox=(40.0, 40.0, 500.0, 500.0),
            font_size=10.0,
            alignment="left",
            background_rgb=(1.0, 1.0, 1.0),
            text_rgb=(0.0, 0.0, 0.0),
            geometry_fill_ratio=0.08,
        )
        layout = PdfLayoutDocument(
            page_sizes=(A4,),
            blocks=(block,),
            source_type="native",
            native_character_count=20,
            covered_native_character_count=20,
        )

        issue = _find_layout_granularity_issue(layout)

        self.assertIsNotNone(issue)
        self.assertIn("低密度大遮盖框 1 个", issue or "")

    def test_processor_generates_real_pdf(self) -> None:
        """成功任务必须对应磁盘上的非空 PDF。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = JobRepository(Path(temporary_directory))
            repository.initialize()
            source_path = repository.upload_directory / "job-1.pdf"
            self._write_source_pdf(source_path)
            repository.create(
                "job-1",
                CreateJobRequest(
                    file_name="source.pdf",
                    file_size=source_path.stat().st_size,
                    model="test-model",
                    translation_policy=TranslationPolicy(),
                ),
                source_path,
            )
            processor = JobProcessor(
                repository,
                parse_document=lambda _path, _strategy: "# English title\n\nSource text",
                translate_document=lambda layout, _job: LayoutTranslationResult(
                    translations={
                        block.id: (
                            "中文标题"
                            if "title" in block.source_text.lower()
                            else "真实翻译内容"
                        )
                        for block in layout.blocks
                    },
                    usage=TokenUsage(120, 45, 165, True),
                ),
                translate_file_name=lambda _job: LlmTextResult(
                    "源文件",
                    TokenUsage(12, 5, 17, True),
                ),
            )

            processor.process_now("job-1")
            processor.shutdown()

            job = repository.get("job-1")
            result_path = repository.get_result_path("job-1")
            self.assertEqual(job.status, JobStatus.COMPLETED)
            self.assertEqual(job.progress, 100)
            self.assertTrue(job.result_available)
            self.assertEqual(job.result_file_name, "源文件_cn.pdf")
            self.assertIsNotNone(result_path)
            assert result_path is not None
            self.assertGreater(result_path.stat().st_size, 500)
            self.assertEqual(result_path.read_bytes()[:5], b"%PDF-")
            source_reader = PdfReader(str(source_path))
            result_reader = PdfReader(str(result_path))
            self.assertEqual(len(source_reader.pages), len(result_reader.pages))
            self.assertEqual(
                float(source_reader.pages[0].mediabox.width),
                float(result_reader.pages[0].mediabox.width),
            )
            self.assertNotIn(
                "DocWeave 本地化结果",
                result_reader.pages[0].extract_text(),
            )
            details = repository.get_details("job-1")
            self.assertEqual(details.metrics.input_tokens, 132)
            self.assertEqual(details.metrics.output_tokens, 50)
            self.assertEqual(details.metrics.total_tokens, 182)
            self.assertTrue(details.metrics.token_usage_available)
            self.assertEqual(details.metrics.result_file_size, result_path.stat().st_size)
            self.assertIsNotNone(details.metrics.finished_at)
            self.assertEqual(details.logs[-1].stage, "completed")
            self.assertEqual(details.logs[-1].level.value, "success")

    def test_pathological_large_text_blocks_are_rejected(self) -> None:
        """目录页被合并成少量大文本框时不得进入覆盖渲染。"""

        blocks = tuple(
            PdfTextBlock(
                **{
                    **self._build_text_block(
                        f"p{index // 8 + 1:04d}-b{index + 1:04d}",
                        "目录条目" * 90,
                    ).__dict__,
                    "page_index": index // 8,
                }
            )
            for index in range(54)
        )
        layout = PdfLayoutDocument(
            page_sizes=tuple((595.0, 842.0) for _ in range(7)),
            blocks=blocks,
            source_type="native",
            native_character_count=sum(len(block.source_text) for block in blocks),
            covered_native_character_count=sum(
                len(block.source_text) for block in blocks
            ),
        )

        issue = _find_layout_granularity_issue(layout)

        self.assertIsNotNone(issue)
        self.assertIn("禁止进入覆盖渲染", issue or "")

    def test_processor_persists_failure_details(self) -> None:
        """处理异常必须保留失败原因、阶段日志和已消耗时间。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = JobRepository(Path(temporary_directory))
            repository.initialize()
            source_path = repository.upload_directory / "job-failed.pdf"
            blank_canvas = canvas.Canvas(str(source_path), pagesize=A4)
            blank_canvas.showPage()
            blank_canvas.save()
            repository.create(
                "job-failed",
                CreateJobRequest(
                    file_name="failed.pdf",
                    file_size=source_path.stat().st_size,
                    model="test-model",
                    translation_policy=TranslationPolicy(),
                ),
                source_path,
            )

            def fail_parsing(_path: Path, _strategy: str) -> str:
                raise RuntimeError("MinerU 测试错误")

            processor = JobProcessor(repository, parse_document=fail_parsing)
            processor.process_now("job-failed")
            processor.shutdown()

            details = repository.get_details("job-failed")
            self.assertEqual(details.job.status, JobStatus.FAILED)
            self.assertIn("MinerU 测试错误", details.job.error_message or "")
            self.assertEqual(details.logs[-1].stage, "failed")
            self.assertIn("MinerU 测试错误", details.logs[-1].message)
            self.assertIsNotNone(details.metrics.finished_at)

    def test_quality_failure_preserves_downloadable_candidate_pdf(self) -> None:
        """PDF 已生成后质检异常时应正常交付并保留结构化复核记录。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = JobRepository(Path(temporary_directory))
            repository.initialize()
            source_path = repository.upload_directory / "job-review.pdf"
            self._write_source_pdf(source_path)
            repository.create(
                "job-review",
                CreateJobRequest(
                    file_name="source.pdf",
                    file_size=source_path.stat().st_size,
                    model="test-model",
                    translation_policy=TranslationPolicy(),
                ),
                source_path,
            )
            processor = JobProcessor(
                repository,
                translate_file_name=lambda _job: LlmTextResult("源文件"),
            )

            def translate_layout(
                layout: PdfLayoutDocument,
                _job: LocalizationJob,
                *,
                repository: JobRepository,
            ) -> LayoutTranslationResult:
                del repository
                return LayoutTranslationResult(
                    translations={
                        block.id: "中文内容"
                        for block in layout.blocks
                    }
                )

            with (
                patch(
                    "app.services.job_processor.translate_layout_with_llm",
                    side_effect=translate_layout,
                ),
                patch(
                    "app.services.job_processor.review_visual_residuals_with_llm",
                    side_effect=RuntimeError("视觉服务测试异常"),
                ),
            ):
                processor.process_now("job-review")
            processor.shutdown()

            details = repository.get_details("job-review")
            result_path = repository.get_result_path("job-review")
            self.assertEqual(details.job.status, JobStatus.NEEDS_REVIEW)
            self.assertEqual(details.job.progress, 100)
            self.assertTrue(details.job.result_downloadable)
            self.assertTrue(details.job.result_available)
            self.assertIsNotNone(result_path)
            assert result_path is not None
            self.assertGreater(result_path.stat().st_size, 500)
            self.assertIn("视觉服务测试异常", details.job.error_message or "")
            self.assertIn(
                "结果文件已生成并可正常下载",
                details.job.error_message or "",
            )
            self.assertEqual(details.logs[-1].stage, "needs-review")
            self.assertEqual(details.logs[-1].level, JobLogLevel.WARNING)
            self.assertEqual(
                details.quality_issues[0].code,
                "quality_evaluation_failed",
            )

    def test_result_file_name_translates_meaning_and_keeps_cn_suffix(self) -> None:
        """中文产物名应保留章节和专名信息，并统一添加 cn 后缀。"""

        job = self._build_job("第4章 2層FCCLの市場分析.pdf")

        result = build_result_file_name(job, "第4章 双层FCCL市场分析.pdf")

        self.assertEqual(result, "第4章 双层FCCL市场分析_cn.pdf")

    def test_result_file_name_removes_invalid_path_characters(self) -> None:
        """模型返回的路径符号和重复后缀不能进入下载文件名。"""

        job = self._build_job("Chapter 3: Film Market.pdf")

        result = build_result_file_name(job, '"第3章/薄膜:市场_中文.pdf"')

        self.assertEqual(result, "第3章_薄膜_市场_cn.pdf")

    def test_chinese_file_name_is_not_reverse_translated(self) -> None:
        """已经是中文的文件名不得被模型反向改写为英文。"""

        self.assertTrue(
            _filename_already_matches_target_language(
                "第3章 薄膜市场",
                "zh-CN",
            )
        )
        self.assertEqual(
            _ensure_filename_target_language(
                "自动化升级验证",
                "Automation Upgrade Verification",
                "zh-CN",
            ),
            "自动化升级验证",
        )

    def test_han_only_japanese_table_of_contents_name_is_translated(self) -> None:
        """纯汉字日文“目次”不得误判为已经是简体中文。"""

        job = self._build_job("目次.pdf").model_copy(
            update={"status": JobStatus.TRANSLATING}
        )

        result = translate_file_name_with_llm(job)

        self.assertEqual(result.text, "目录")
        self.assertFalse(result.usage.available)

    def test_layout_character_capacity_respects_region_size(self) -> None:
        """限长重译的字符上限应随文本框面积增加。"""

        compact_block = self._build_text_block("p0001-b0001", "原文")
        large_block = PdfTextBlock(
            **{
                **compact_block.__dict__,
                "id": "p0001-b0002",
                "render_bbox": (0, 0, 200, 80),
            }
        )

        self.assertGreater(
            _estimate_layout_character_capacity(large_block),
            _estimate_layout_character_capacity(compact_block),
        )

    def test_block_translation_accepts_empty_symbol_and_recovers_changed_ids(
        self,
    ) -> None:
        """模型对符号返回空值或轻微改写 ID 时不应导致正文错位。"""

        blocks = [
            self._build_text_block("p0001-b0001", "市場動向"),
            self._build_text_block("p0001-b0002", "●"),
        ]
        content = (
            '{"translations":['
            '{"id":"block-1","text":"市场动向"},'
            '{"id":"block-2","text":""}'
            "]}"
        )

        translations = _extract_block_translations(content, blocks)

        self.assertEqual(translations["p0001-b0001"], "市场动向")
        self.assertEqual(translations["p0001-b0002"], "●")

    def test_quality_gate_rejects_language_residual_and_number_change(self) -> None:
        """日文残留或百分比被改写时不得把任务标记为成功。"""

        blocks = (
            self._build_text_block("p0001-b0001", "カバーレイ用"),
            self._build_text_block("p0001-b0002", "市場比率 12.1%"),
        )
        layout = PdfLayoutDocument(
            page_sizes=(A4,),
            blocks=blocks,
            source_type="native",
        )

        issues = _validate_translation_quality(
            layout,
            {
                "p0001-b0001": "カバーレイ用",
                "p0001-b0002": "市场占比 12.7%",
            },
            self._build_job("source.pdf"),
        )

        self.assertTrue(any("仍含日文" in issue for issue in issues))
        self.assertTrue(any("数字或百分比" in issue for issue in issues))

    def test_visual_quality_keeps_company_name_but_repairs_real_residuals(
        self,
    ) -> None:
        """公司专名片段不能误报，普通英文和日文残留仍须进入修复。"""

        residuals = _filter_preserved_visual_residuals(
            [
                "Electro",
                "MIT",
                "ASTM-D-2520",
                "Pyralux® AP",
                "Cu",
                "N/mm (lb/in)",
                "288°C (550°F)",
                "B %",
                "1MHz",
                "Df",
                "Tg [",
                "Td [5%",
                "g/",
                "IPC TM-660",
                "CTE (ppm/C) T<Tg",
                "Hi-FlexRA",
                "Hi-Flex RA",
                "Df [25℃, 45%RH]",
                "Coverlay",
                "カバーレイ",
                "coverlay",
                "Tonggan",
                "Towada",
                "BukKang",
                "Volume",
                "模糊残留文字",
            ],
            (
                "DOOSAN Electro-Materials Co.,Ltd.",
                "MIT",
                "ASTM-D-2520",
                "Pyralux® AP",
                "Cu",
                "N/mm (lb/in)",
                "288°C (550°F)",
                "B %",
                "1MHz",
                "Df",
                "Tg [",
                "Td [5%",
                "g/",
                "IPC TM-660",
                "CTE (ppm/C) T<Tg",
                "Hi-FlexRA",
                "Hi-Flex RA",
                "Df [25℃, 45%RH]",
                "Coverlay application",
                "カバーレイ用",
                "Tonggan",
                "Volume",
                "TOWADA",
            ),
            self._build_job("source.pdf"),
        )

        self.assertEqual(residuals, ["Coverlay", "カバーレイ", "Volume"])

    def test_visual_quality_keeps_industry_abbreviations_and_metrics(self) -> None:
        """行业缩写与指标组合不得被视觉质检误报为普通英文残留。"""

        self.assertTrue(_is_model_or_standard_residual("R&D"))
        self.assertTrue(_is_model_or_standard_residual("Low CTE PI"))
        self.assertTrue(_is_model_or_standard_residual("High Dk"))
        self.assertFalse(_is_model_or_standard_residual("Coverlay"))

    def test_visual_residual_reuses_existing_text_block(self) -> None:
        """视觉残留与原生文本框重叠时应定向重译，不能新增叠字块。"""

        region = PdfImageRegion(
            id="p0001-full",
            page_index=0,
            bbox=(0, 0, 100, 100),
            image_png=b"image",
        )
        residual = VisualResidual(
            region=region,
            source_text="Dock",
            translated_text="底座",
            bbox=(100, 100, 400, 250),
        )
        block = PdfTextBlock(
            **{
                **self._build_text_block(
                    "p0001-b0001",
                    "Dock flexible board",
                ).__dict__,
                "bbox": (10, 10, 40, 25),
                "render_bbox": (10, 10, 40, 25),
            }
        )

        self.assertEqual(
            _visual_residual_overlapping_block_ids(
                [residual],
                (block,),
            ),
            {"p0001-b0001"},
        )

    def test_generic_english_short_label_has_deterministic_fallback(
        self,
    ) -> None:
        """模型重复漏译独立普通英文标签时应使用受控词典收尾。"""

        block = self._build_text_block("p0001-b0001", "type")
        layout = PdfLayoutDocument(
            page_sizes=(A4,),
            blocks=(block,),
            source_type="native",
        )

        repairs = _repair_generic_english_residuals(
            layout,
            {block.id: "type"},
            [f"{block.id} 仍含普通英文：type"],
            self._build_job("source.pdf"),
        )

        self.assertEqual(repairs, {block.id: "类型"})

    def test_common_table_terms_use_deterministic_chinese(self) -> None:
        """高频表格短词不得因模型波动回写为日文或歧义表达。"""

        blocks = (
            self._build_text_block("p0001-b0001", "Single-sided"),
            self._build_text_block("p0001-b0002", "内製"),
            self._build_text_block("p0001-b0003", "外販"),
        )
        layout = PdfLayoutDocument(
            page_sizes=(A4,),
            blocks=blocks,
            source_type="native",
        )

        repairs = _repair_common_table_terms(
            layout,
            {
                "p0001-b0001": "単面",
                "p0001-b0002": "自社",
                "p0001-b0003": "外销",
            },
            self._build_job("source.pdf"),
        )

        self.assertEqual(
            repairs,
            {
                "p0001-b0001": "单面",
                "p0001-b0002": "自制",
                "p0001-b0003": "外售",
            },
        )

    def test_quality_gate_preserves_type_c_model_name(self) -> None:
        """USB Type-C 等型号中的普通英文片段不得被当作漏译。"""

        type_c_block = self._build_text_block(
            "p0001-b0001",
            "ノート PC 向け USB Type-C 用 FPC",
        )
        lk_type_block = self._build_text_block(
            "p0001-b0002",
            "LK-Type（両面）",
        )
        layout = PdfLayoutDocument(
            page_sizes=(A4,),
            blocks=(type_c_block, lk_type_block),
            source_type="native",
        )

        issues = _validate_translation_quality(
            layout,
            {
                type_c_block.id: "笔记本电脑用 USB Type-C FPC",
                lk_type_block.id: "LK-Type（双面）",
            },
            self._build_job("source.pdf"),
        )

        self.assertEqual(issues, [])

    def test_llm_request_retries_transient_http_error(self) -> None:
        """大模型 408 等瞬时错误应在任务内重试而不是直接失败。"""

        request = httpx.Request("POST", "http://llm.test/chat/completions")
        client = MagicMock()
        client.post.side_effect = [
            httpx.Response(408, request=request),
            httpx.Response(200, request=request, json={"choices": []}),
        ]

        with patch("app.services.job_processor.sleep"):
            response = _post_llm_request(
                client,
                url=str(request.url),
                headers={"Authorization": "Bearer test"},
                payload={"model": "test-model"},
                service_name="大模型",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.post.call_count, 2)

    def test_translation_batches_never_cross_pages(self) -> None:
        """页面级批次必须保持页内语境，禁止把两页文本混在一次请求。"""

        first_page = self._build_text_block("p0001-b0001", "Page one")
        second_page = PdfTextBlock(
            **{
                **first_page.__dict__,
                "id": "p0002-b0001",
                "page_index": 1,
                "source_text": "Page two",
            }
        )

        batches = _chunk_layout_blocks((first_page, second_page))

        self.assertEqual(len(batches), 2)
        self.assertEqual({block.page_index for block in batches[0]}, {0})
        self.assertEqual({block.page_index for block in batches[1]}, {1})

    def test_completed_translation_batch_is_resumed_without_llm_call(self) -> None:
        """已完成批次必须从 SQLite 直接恢复，不能重复消耗模型。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = JobRepository(Path(temporary_directory))
            repository.initialize()
            source_path = repository.upload_directory / "batch.pdf"
            self._write_source_pdf(source_path)
            repository.create(
                "batch-job",
                CreateJobRequest(
                    file_name="batch.pdf",
                    file_size=source_path.stat().st_size,
                    model="test-model",
                    translation_policy=TranslationPolicy(),
                ),
                source_path,
            )
            block = self._build_text_block("p0001-b0001", "Product name")
            with patch(
                "app.services.job_processor._translate_layout_batch",
                return_value=(
                    {block.id: "产品名称"},
                    TokenUsage(20, 5, 25, True),
                ),
            ) as translate_mock:
                first_result = _translate_persisted_batch(
                    base_url="http://llm.test",
                    api_key="test",
                    model="test-model",
                    instruction="test",
                    batch=[block],
                    batch_index=1,
                    batch_count=1,
                    timeout_seconds=10,
                    repository=repository,
                    job_id="batch-job",
                    stage="translation",
                )
                second_result = _translate_persisted_batch(
                    base_url="http://llm.test",
                    api_key="test",
                    model="test-model",
                    instruction="test",
                    batch=[block],
                    batch_index=1,
                    batch_count=1,
                    timeout_seconds=10,
                    repository=repository,
                    job_id="batch-job",
                    stage="translation",
                )

        self.assertEqual(translate_mock.call_count, 1)
        self.assertEqual(first_result[0][block.id], "产品名称")
        self.assertTrue(second_result[2])

    def test_translation_memory_skips_exactly_matched_blocks(self) -> None:
        """同语言方向的完全匹配记忆应跳过模型，未命中块仍正常翻译。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = JobRepository(Path(temporary_directory))
            repository.initialize()
            source_path = repository.upload_directory / "memory.pdf"
            self._write_source_pdf(source_path)
            job = repository.create(
                "memory-job",
                CreateJobRequest(
                    file_name="memory.pdf",
                    file_size=source_path.stat().st_size,
                    model="test-model",
                    translation_policy=TranslationPolicy(),
                ),
                source_path,
            )
            repository.record_translation_learning(
                job,
                [("Product name", "产品名称")],
            )
            memory_block = self._build_text_block(
                "p0001-b0001",
                "Product name",
            )
            pending_block = self._build_text_block(
                "p0001-b0002",
                "Supplier",
            )
            layout = PdfLayoutDocument(
                page_sizes=(A4,),
                blocks=(memory_block, pending_block),
                source_type="native",
            )
            with (
                patch.dict(
                    "app.services.job_processor.os.environ",
                    {
                        "LLM_BASE_URL": "http://llm.test/v1",
                        "LLM_API_KEY": "test",
                    },
                ),
                patch(
                    "app.services.job_processor._translate_persisted_batch",
                    return_value=(
                        {pending_block.id: "供应商"},
                        TokenUsage(20, 5, 25, True),
                        False,
                    ),
                ) as translate_mock,
            ):
                result = translate_layout_with_llm(
                    layout,
                    job,
                    repository=repository,
                )

        self.assertEqual(result.translations[memory_block.id], "产品名称")
        self.assertEqual(result.translations[pending_block.id], "供应商")
        self.assertEqual(result.memory_hit_count, 1)
        translated_batch = translate_mock.call_args.kwargs["batch"]
        self.assertEqual(
            [block.id for block in translated_batch],
            [pending_block.id],
        )

    def test_translation_memory_isolated_by_complete_policy(self) -> None:
        """翻译策略变化后不得复用旧策略生成的记忆译文。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = JobRepository(Path(temporary_directory))
            repository.initialize()
            source_path = repository.upload_directory / "memory-policy.pdf"
            self._write_source_pdf(source_path)
            original_policy = TranslationPolicy(preserve_proper_nouns=False)
            job = repository.create(
                "memory-policy-job",
                CreateJobRequest(
                    file_name="memory-policy.pdf",
                    file_size=source_path.stat().st_size,
                    model="test-model",
                    translation_policy=original_policy,
                ),
                source_path,
            )
            repository.record_translation_learning(
                job,
                [("All Star Tech Co., Ltd.", "全明星科技有限公司")],
            )

            original_memory = repository.get_translation_memory(
                original_policy,
                ["All Star Tech Co., Ltd."],
            )
            protected_policy_memory = repository.get_translation_memory(
                TranslationPolicy(preserve_proper_nouns=True),
                ["All Star Tech Co., Ltd."],
            )

        self.assertEqual(
            original_memory,
            {"All Star Tech Co., Ltd.": "全明星科技有限公司"},
        )
        self.assertEqual(protected_policy_memory, {})

    def test_vision_blocks_skip_single_invalid_item(self) -> None:
        """图片 OCR 的单个空条目不得拖垮同一区域的有效翻译。"""

        image_stream = io.BytesIO()
        Image.new("RGB", (100, 100), "white").save(
            image_stream,
            format="PNG",
        )
        region = PdfImageRegion(
            id="p0001-i0001",
            page_index=0,
            bbox=(0.0, 0.0, 100.0, 100.0),
            image_png=image_stream.getvalue(),
        )

        blocks, translations, _ = _build_vision_blocks(
            [
                {
                    "source_text": "",
                    "translated_text": "",
                    "bbox": None,
                },
                {
                    "source_text": "カバーレイ用",
                    "translated_text": "覆盖膜用",
                    "bbox": [100, 100, 500, 250],
                },
            ],
            region,
            TokenUsage(),
        )

        self.assertEqual(len(blocks), 1)
        self.assertEqual(translations[blocks[0].id], "覆盖膜用")
        self.assertEqual(blocks[0].mask_bbox, region.bbox)

    def test_dense_vision_table_is_preserved_instead_of_overlaid(self) -> None:
        """密集图片表格不得按大量不可靠视觉坐标逐项覆盖。"""

        image_stream = io.BytesIO()
        Image.new("RGB", (400, 300), "white").save(
            image_stream,
            format="PNG",
        )
        region = PdfImageRegion(
            id="p0001-i0001",
            page_index=0,
            bbox=(0.0, 0.0, 400.0, 300.0),
            image_png=image_stream.getvalue(),
        )
        items = [
            {
                "source_text": f"Property {item_index}",
                "translated_text": f"性能 {item_index}",
                "bbox": [10, item_index * 20, 400, item_index * 20 + 18],
            }
            for item_index in range(25)
        ]

        blocks, translations, _ = _build_vision_blocks(
            items,
            region,
            TokenUsage(),
        )

        self.assertEqual(blocks, [])
        self.assertEqual(translations, {})

    def test_coarse_mineru_image_table_skips_visual_translation(self) -> None:
        """已判定整表坐标不可靠的图片区域不得再次提交视觉覆盖。"""

        image_stream = io.BytesIO()
        Image.new("RGB", (400, 300), "white").save(
            image_stream,
            format="PNG",
        )
        region = PdfImageRegion(
            id="p0001-i0001",
            page_index=0,
            bbox=(0.0, 0.0, 400.0, 300.0),
            image_png=image_stream.getvalue(),
        )
        coarse_block = PdfTextBlock(
            id="p0001-m0001",
            page_index=0,
            source_text="Property\nTypical Value\nDielectric Constant\nLoss Tangent",
            bbox=(0.0, 0.0, 400.0, 300.0),
            render_bbox=(2.0, 2.0, 398.0, 298.0),
            font_size=8.0,
            alignment="left",
            background_rgb=(1.0, 1.0, 1.0),
            text_rgb=(0.0, 0.0, 0.0),
            source_type="mineru",
            region_type="table",
            preserve_reason="整表坐标不可靠",
        )
        layout = PdfLayoutDocument(
            page_sizes=((400.0, 300.0),),
            blocks=(coarse_block,),
            source_type="hybrid",
            image_regions=(region,),
        )

        safe_regions, preserved_regions = _select_visual_translation_regions(
            layout
        )

        self.assertEqual(safe_regions, ())
        self.assertEqual(preserved_regions, (region,))

    def test_visual_review_returns_translation_and_coordinates(self) -> None:
        """译后视觉复检必须返回可直接补译的文本、译文和坐标。"""

        image_stream = io.BytesIO()
        Image.new("RGB", (100, 100), "white").save(
            image_stream,
            format="PNG",
        )
        region = PdfImageRegion(
            id="p0001-i0001",
            page_index=0,
            bbox=(10.0, 20.0, 110.0, 120.0),
            image_png=image_stream.getvalue(),
        )
        request = httpx.Request("POST", "http://llm.test/chat/completions")
        response = httpx.Response(
            200,
            request=request,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"residuals":[{"text":"Test Method",'
                                '"translated_text":"测试方法",'
                                '"bbox":[100,200,600,400]}]}'
                            )
                        }
                    }
                ]
            },
        )

        with patch(
            "app.services.job_processor._post_llm_request",
            return_value=response,
        ) as request_mock:
            residuals, _ = _review_image_region(
                base_url="http://llm.test",
                api_key="test",
                model="test-model",
                target_language="zh-CN",
                protected_terms=[],
                expected_source_texts=("自社", "Test Method"),
                region=region,
                timeout_seconds=10,
            )

        self.assertEqual(len(residuals), 1)
        self.assertEqual(residuals[0].translated_text, "测试方法")
        self.assertEqual(residuals[0].bbox, (100.0, 200.0, 600.0, 400.0))
        system_prompt = request_mock.call_args.kwargs["payload"]["messages"][0][
            "content"
        ]
        self.assertNotIn("原始图片文字候选", system_prompt)
        self.assertIn("不得根据原文", system_prompt)
        self.assertIn("自制", system_prompt)

    def test_visual_review_skips_unlocatable_fuzzy_item(self) -> None:
        """无有效坐标的模糊视觉判断不得使已生成 PDF 整体失败。"""

        image_stream = io.BytesIO()
        Image.new("RGB", (100, 100), "white").save(
            image_stream,
            format="PNG",
        )
        region = PdfImageRegion(
            id="p0001-i0001",
            page_index=0,
            bbox=(0.0, 0.0, 100.0, 100.0),
            image_png=image_stream.getvalue(),
        )
        request = httpx.Request("POST", "http://llm.test/chat/completions")
        response = httpx.Response(
            200,
            request=request,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"residuals":['
                                '{"text":"模糊残留文字","translated_text":"",'
                                '"bbox":null},'
                                '{"text":"Volume","translated_text":"数量",'
                                '"bbox":[100,200,600,400]}'
                                "]}"
                            )
                        }
                    }
                ]
            },
        )

        with patch(
            "app.services.job_processor._post_llm_request",
            return_value=response,
        ):
            residuals, _ = _review_image_region(
                base_url="http://llm.test",
                api_key="test",
                model="test-model",
                target_language="zh-CN",
                protected_terms=[],
                expected_source_texts=(),
                region=region,
                timeout_seconds=10,
            )

        self.assertEqual([item.source_text for item in residuals], ["Volume"])

    def test_visual_residual_builds_page_repair_block(self) -> None:
        """视觉残留坐标应转换为原页面上的真实补译文本块。"""

        image_stream = io.BytesIO()
        Image.new("RGB", (100, 100), "white").save(
            image_stream,
            format="PNG",
        )
        region = PdfImageRegion(
            id="p0001-i0001",
            page_index=0,
            bbox=(10.0, 20.0, 110.0, 120.0),
            image_png=image_stream.getvalue(),
        )
        residual = VisualResidual(
            region=region,
            source_text="Test Method",
            translated_text="测试方法",
            bbox=(100.0, 200.0, 600.0, 400.0),
        )

        blocks, translations = _build_visual_residual_repairs(
            [residual],
            retry_index=1,
        )

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].bbox, (20.0, 40.0, 70.0, 60.0))
        self.assertEqual(blocks[0].mask_bbox, region.bbox)
        self.assertEqual(translations[blocks[0].id], "测试方法")

    @staticmethod
    def _build_job(file_name: str) -> LocalizationJob:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        return LocalizationJob(
            id="name-test",
            file_name=file_name,
            file_size=128,
            model="test-model",
            strategy="智能路由",
            translation_policy=TranslationPolicy(),
            status=JobStatus.PROCESSING,
            progress=85,
            created_at=now,
            updated_at=now,
            pipeline_version=PDF_PIPELINE_VERSION,
        )

    @staticmethod
    def _write_source_pdf(destination: Path) -> None:
        """生成带明确版式的有效测试 PDF。"""

        pdf_canvas = canvas.Canvas(str(destination), pagesize=A4)
        pdf_canvas.setTitle("原始测试文档")
        pdf_canvas.setFont("Helvetica-Bold", 18)
        pdf_canvas.drawString(72, 770, "English title")
        pdf_canvas.setFont("Helvetica", 11)
        pdf_canvas.drawString(72, 720, "Source paragraph")
        pdf_canvas.save()

    @staticmethod
    def _build_text_block(block_id: str, source_text: str) -> PdfTextBlock:
        """构造 JSON 解析测试所需的最小文本块。"""

        return PdfTextBlock(
            id=block_id,
            page_index=0,
            source_text=source_text,
            bbox=(0, 0, 100, 20),
            render_bbox=(0, 0, 100, 20),
            font_size=10,
            alignment="left",
            background_rgb=(1, 1, 1),
            text_rgb=(0, 0, 0),
        )


if __name__ == "__main__":
    unittest.main()
