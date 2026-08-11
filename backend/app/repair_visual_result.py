"""对已生成且存在视觉复核建议的 PDF 继续坐标补译，避免整份文档重跑。"""

import argparse
import os
from pathlib import Path
from time import perf_counter

from app.domain.pdf import PdfLayoutDocument
from app.models import JobLogLevel, JobStatus
from app.services.job_processor import (
    _build_visual_residual_repairs,
    _elapsed_milliseconds,
    _filter_preserved_visual_residuals,
    review_visual_residuals_with_llm,
)
from app.services.pdf_layout import (
    extract_native_pdf_layout,
    render_translated_pdf,
)
from app.storage import JobRepository


def repair_visual_result(
    repository: JobRepository,
    job_id: str,
    result_file_name: str,
    maximum_attempts: int = 4,
) -> bool:
    """继续修复已有结果 PDF 的图片文字残留。

    Args:
        repository: 任务及文件持久化仓库。
        job_id: 已生成候选 PDF 的任务 ID。
        result_file_name: 通过浏览器下载时使用的中文文件名。
        maximum_attempts: 最多追加的视觉坐标补译次数。

    Returns:
        最终视觉复检通过时返回 True，否则返回 False。
    """

    job = repository.get(job_id)
    source_path = repository.get_source_path(job_id)
    result_path = repository.result_directory / f"{job_id}.pdf"
    repair_path = repository.result_directory / f"{job_id}.repair.pdf"
    if not source_path.is_file():
        raise RuntimeError("视觉续修失败：源 PDF 不存在")
    if not result_path.is_file():
        raise RuntimeError("视觉续修失败：候选结果 PDF 不存在")

    original_layout = extract_native_pdf_layout(source_path)
    visual_context = tuple(
        block.source_text
        for block in original_layout.blocks
        if block.source_type != "vision"
    )
    repository.update_status(job_id, JobStatus.PROCESSING, 97)
    repository.append_log(
        job_id,
        "quality-repair",
        JobLogLevel.WARNING,
        "沿用已有候选 PDF 继续最终页面视觉复检，无需重新翻译正文",
        97,
    )

    try:
        normalized_maximum_attempts = max(1, maximum_attempts)
        for review_index in range(normalized_maximum_attempts + 1):
            quality_started_at = perf_counter()
            residuals, usage = review_visual_residuals_with_llm(
                result_path,
                job,
                original_layout.image_regions,
            )
            repository.add_metrics(
                job_id,
                llm_duration_ms=_elapsed_milliseconds(quality_started_at),
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                token_usage_available=usage.available,
            )
            repairable_texts = _filter_preserved_visual_residuals(
                [residual.source_text for residual in residuals],
                visual_context,
                job,
            )
            repairable_keys = {
                residual.casefold()
                for residual in repairable_texts
            }
            residuals = [
                residual
                for residual in residuals
                if residual.source_text.casefold() in repairable_keys
            ]
            if not residuals:
                result_file_size = result_path.stat().st_size
                repository.update_status(
                    job_id,
                    JobStatus.COMPLETED,
                    100,
                    result_path=result_path,
                    result_file_name=result_file_name,
                )
                repository.finish_metrics(job_id, result_file_size)
                repository.append_log(
                    job_id,
                    "completed",
                    JobLogLevel.SUCCESS,
                    (
                        "已有候选 PDF 经最终页面视觉续修后通过，"
                        f"结果 {result_file_name}"
                    ),
                    100,
                )
                return True

            if review_index >= normalized_maximum_attempts:
                break

            attempt = review_index + 1
            repair_blocks, translations = _build_visual_residual_repairs(
                residuals,
                attempt,
            )
            repair_layout = PdfLayoutDocument(
                page_sizes=original_layout.page_sizes,
                blocks=repair_blocks,
                source_type="hybrid",
                image_regions=original_layout.image_regions,
            )
            render_started_at = perf_counter()
            render_translated_pdf(
                result_path,
                repair_path,
                repair_layout,
                translations,
                vision_mask_padding=12.0 + attempt * 8.0,
            )
            repair_path.replace(result_path)
            repository.add_metrics(
                job_id,
                render_duration_ms=_elapsed_milliseconds(render_started_at),
            )
            repository.append_log(
                job_id,
                "quality-repair",
                JobLogLevel.WARNING,
                (
                    f"视觉续修第 {attempt} 次补建 {len(repair_blocks)} "
                    "个坐标文本块，正在复检"
                ),
                97,
            )

        remaining_text = "、".join(
            residual.source_text
            for residual in residuals[:10]
        )
        error_message = f"视觉续修达到上限，仍有残留：{remaining_text}"
        repository.update_status(
            job_id,
            JobStatus.FAILED,
            97,
            error_message=error_message,
        )
        repository.finish_metrics(job_id)
        repository.append_log(
            job_id,
            "failed",
            JobLogLevel.ERROR,
            error_message,
            97,
        )
        return False
    except Exception as error:
        repair_path.unlink(missing_ok=True)
        error_message = f"视觉续修失败：{error}"
        repository.update_status(
            job_id,
            JobStatus.FAILED,
            97,
            error_message=error_message,
        )
        repository.finish_metrics(job_id)
        repository.append_log(
            job_id,
            "failed",
            JobLogLevel.ERROR,
            error_message,
            97,
        )
        raise
    finally:
        repair_path.unlink(missing_ok=True)


def main() -> int:
    """解析命令行参数并执行单个任务的视觉续修。"""

    parser = argparse.ArgumentParser(
        description="沿用已有候选 PDF 继续视觉坐标补译",
    )
    parser.add_argument("job_id", help="任务 ID")
    parser.add_argument("result_file_name", help="中文下载文件名")
    parser.add_argument(
        "--maximum-attempts",
        type=int,
        default=4,
        help="最大视觉续修次数，默认 4",
    )
    arguments = parser.parse_args()
    data_directory = Path(
        os.getenv("DOCWEAVE_DATA_DIR", "/app/data")
    )
    repository = JobRepository(data_directory)
    repository.initialize()
    succeeded = repair_visual_result(
        repository,
        arguments.job_id,
        arguments.result_file_name,
        arguments.maximum_attempts,
    )
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
