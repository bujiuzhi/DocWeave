"""验证失败候选 PDF 可以续修完成而无需重新翻译正文。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.models import CreateJobRequest, JobStatus, TranslationPolicy
from app.repair_visual_result import repair_visual_result
from app.services.job_processor import TokenUsage, VisualResidual
from app.services.pdf_layout import PdfImageRegion, PdfLayoutDocument
from app.storage import JobRepository


class VisualRepairTest(unittest.TestCase):
    """视觉续修通过后必须恢复真实下载产物状态。"""

    def test_existing_result_can_complete_without_full_translation(self) -> None:
        """无真实残留时应直接把已有候选 PDF 标记为可下载。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = JobRepository(Path(temporary_directory))
            repository.initialize()
            source_path = repository.upload_directory / "job-repair.pdf"
            result_path = repository.result_directory / "job-repair.pdf"
            self._write_pdf(source_path)
            self._write_pdf(result_path)
            repository.create(
                "job-repair",
                CreateJobRequest(
                    file_name="source.pdf",
                    file_size=source_path.stat().st_size,
                    model="test-model",
                    translation_policy=TranslationPolicy(),
                ),
                source_path,
            )
            repository.update_status(
                "job-repair",
                JobStatus.FAILED,
                97,
                error_message="视觉质检失败",
            )
            empty_layout = PdfLayoutDocument(
                page_sizes=(A4,),
                blocks=(),
                source_type="native",
                image_regions=(),
            )

            with (
                patch(
                    "app.repair_visual_result.extract_native_pdf_layout",
                    return_value=empty_layout,
                ),
                patch(
                    "app.repair_visual_result.review_visual_residuals_with_llm",
                    return_value=([], TokenUsage()),
                ),
            ):
                succeeded = repair_visual_result(
                    repository,
                    "job-repair",
                    "修复结果_cn.pdf",
                )

            job = repository.get("job-repair")
            self.assertTrue(succeeded)
            self.assertEqual(job.status, JobStatus.COMPLETED)
            self.assertEqual(job.result_file_name, "修复结果_cn.pdf")
            self.assertTrue(job.result_available)

    def test_last_repair_is_reviewed_before_marking_job_failed(self) -> None:
        """达到补译次数上限后仍须复检最后一次生成的结果。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = JobRepository(Path(temporary_directory))
            repository.initialize()
            source_path = repository.upload_directory / "job-last-review.pdf"
            result_path = repository.result_directory / "job-last-review.pdf"
            self._write_pdf(source_path)
            self._write_pdf(result_path)
            repository.create(
                "job-last-review",
                CreateJobRequest(
                    file_name="source.pdf",
                    file_size=source_path.stat().st_size,
                    model="test-model",
                    translation_policy=TranslationPolicy(),
                ),
                source_path,
            )
            repository.update_status(
                "job-last-review",
                JobStatus.FAILED,
                97,
                error_message="视觉质检失败",
            )
            image_region = PdfImageRegion(
                id="image-1",
                page_index=0,
                bbox=(0.0, 0.0, 100.0, 100.0),
                image_png=b"not-used",
            )
            source_block = PdfLayoutDocument(
                page_sizes=(A4,),
                blocks=(),
                source_type="native",
                image_regions=(image_region,),
            )
            residual = VisualResidual(
                region=image_region,
                source_text="未翻译",
                translated_text="已翻译",
                bbox=(0.0, 0.0, 1000.0, 1000.0),
            )

            with (
                patch(
                    "app.repair_visual_result.extract_native_pdf_layout",
                    return_value=source_block,
                ),
                patch(
                    "app.repair_visual_result.review_visual_residuals_with_llm",
                    side_effect=[
                        ([residual], TokenUsage()),
                        ([], TokenUsage()),
                    ],
                ) as review,
                patch(
                    "app.repair_visual_result.render_translated_pdf",
                    side_effect=lambda source, destination, *_args, **_kwargs: (
                        self._write_pdf(destination)
                    ),
                ),
            ):
                succeeded = repair_visual_result(
                    repository,
                    "job-last-review",
                    "最终复检_cn.pdf",
                    maximum_attempts=1,
                )

            job = repository.get("job-last-review")
            self.assertTrue(succeeded)
            self.assertEqual(review.call_count, 2)
            self.assertEqual(job.status, JobStatus.COMPLETED)

    @staticmethod
    def _write_pdf(destination: Path) -> None:
        """生成视觉续修测试使用的最小有效 PDF。"""

        pdf_canvas = canvas.Canvas(str(destination), pagesize=A4)
        pdf_canvas.drawString(72, 720, "Source")
        pdf_canvas.save()
