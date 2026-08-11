"""调用 MinerU 与通用 LLM API，生成可下载的真实本地化 PDF。"""

import base64
import hashlib
import io
import json
import logging
import os
import re
import statistics
import unicodedata
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from collections import Counter
from dataclasses import replace
from pathlib import Path
from time import perf_counter, sleep
from urllib.parse import urlsplit

import httpx
from PIL import Image

from app.domain.pdf import PdfImageRegion, PdfLayoutDocument, PdfTextBlock
from app.pipeline.contracts import (
    DocumentParseResult,
    LayoutTranslationResult,
    LlmTextResult,
    ParseDocument,
    RenderDocument,
    TokenUsage,
    TranslateDocument,
    TranslateFileName,
    ValidateDocument,
    VisualResidual,
)

from app.models import JobLogLevel, JobStatus, LocalizationJob
from app.services.pdf_layout import (
    _bbox_overlap_ratio,
    build_full_page_quality_regions,
    extract_pdf_layout,
    extract_mineru_pdf_layout,
    extract_native_pdf_layout,
    render_pdf_image_regions,
    render_translated_pdf,
    should_use_mineru,
    validate_rendered_pdf,
)
from app.services.translation_policy import build_translation_instruction
from app.storage import JobRepository

LOGGER = logging.getLogger(__name__)
GENERIC_ENGLISH_TRANSLATIONS_ZH = {
    "address": "地址",
    "application": "应用",
    "amount": "数量",
    "capacity": "产能",
    "category": "类别",
    "company": "公司",
    "country": "国家",
    "coverlay": "覆盖膜",
    "manufacturer": "制造商",
    "market": "市场",
    "method": "方法",
    "name": "名称",
    "others": "其他",
    "product": "产品",
    "production": "生产",
    "quantity": "数量",
    "region": "地区",
    "sales": "销售额",
    "share": "份额",
    "single": "单面",
    "stage": "阶段",
    "summary": "概要",
    "test": "测试",
    "total": "合计",
    "type": "类型",
    "value": "数值",
    "volume": "数量",
    "year": "年份",
}
GENERIC_ENGLISH_PHRASES_ZH = {
    "company name": "公司名称",
    "market share": "市场份额",
    "product name": "产品名称",
    "production capacity": "生产能力",
    "production items": "生产项目",
    "sales volume": "销售量",
    "single-sided": "单面",
    "test method": "测试方法",
}
COMMON_TABLE_TRANSLATIONS_ZH = {
    "single-sided": "单面",
    "double-sided": "双面",
    "single-sided (captive)": "单面（自用）",
    "double-sided (captive)": "双面（自用）",
    "single-sided (merchant)": "单面（外售）",
    "double-sided (merchant)": "双面（外售）",
    "single-sided (in-house production)": "单面（自制）",
    "double-sided (in-house production)": "双面（自制）",
    "single-sided (external sales)": "单面（外售）",
    "double-sided (external sales)": "双面（外售）",
    "単面": "单面",
    "両面": "双面",
    "内製": "自制",
    "外販": "外售",
    "小計": "小计",
    "合計": "合计",
    "自社": "本公司",
    "内訳": "明细",
    "金額": "金额",
}
MAXIMUM_SAFE_VISION_ITEMS_PER_REGION = 24
RETRYABLE_HTTP_STATUS_CODES = {408, 429, 500, 502, 503, 504}
SIMPLIFIED_CHINESE_FILENAME_MARKERS = frozenset(
    "为与业场层应发后产频目录总览译图国华东亚门间线体简数测试"
)
DETERMINISTIC_FILENAME_TRANSLATIONS_ZH = {
    "目次": "目录",
}


class LlmExecutionError(RuntimeError):
    """携带失败前已产生 Token 用量的大模型调用异常。"""

    def __init__(self, message: str, usage: TokenUsage) -> None:
        super().__init__(message)
        self.usage = usage


class JobProcessor:
    """在后台线程执行文档解析、翻译和结果 PDF 生成。"""

    def __init__(
        self,
        repository: JobRepository,
        parse_document: ParseDocument | None = None,
        translate_document: TranslateDocument | None = None,
        translate_file_name: TranslateFileName | None = None,
        render_document: RenderDocument | None = None,
        validate_document: ValidateDocument | None = None,
    ) -> None:
        """初始化任务处理器。

        Args:
            repository: 任务和文件持久化仓库。
            parse_document: 可替换的 MinerU 解析函数，主要用于测试。
            translate_document: 可替换的 LLM 翻译函数，主要用于测试。
            translate_file_name: 可替换的产物文件名翻译函数，主要用于测试。
            render_document: 可替换的原版式 PDF 渲染实现。
            validate_document: 可替换的全页质量校验实现。
        """

        self.repository = repository
        self.parse_document = parse_document or parse_pdf_with_mineru
        self.translate_document = translate_document
        self.translate_file_name = translate_file_name or translate_file_name_with_llm
        self.render_document = render_document or render_translated_pdf
        self.validate_document = validate_document or validate_rendered_pdf
        self.worker_count = max(1, int(os.getenv("DOCWEAVE_WORKERS", "4")))
        self.executor = ThreadPoolExecutor(
            max_workers=self.worker_count,
            thread_name_prefix="docweave-job",
        )
        self.futures: dict[str, Future[None]] = {}

    def submit(self, job_id: str) -> None:
        """提交后台任务并记录异常。"""

        future = self.executor.submit(self.process_now, job_id)
        self.futures[job_id] = future
        future.add_done_callback(lambda completed: self._handle_future(job_id, completed))

    def process_now(self, job_id: str) -> None:
        """同步执行一个任务，便于后台线程和测试共用。"""

        result_path: Path | None = None
        result_file_name: str | None = None
        try:
            job = self.repository.get(job_id)
            if job.status == JobStatus.CANCELLED:
                return
            self.repository.start_metrics(job_id)
            self.repository.update_status(job_id, JobStatus.ANALYZING, 10)
            self.repository.append_log(
                job_id,
                "started",
                JobLogLevel.INFO,
                f"开始处理，模型 {job.model}，策略 {job.strategy}",
                10,
            )
            source_path = self.repository.get_source_path(job_id)

            self.repository.update_status(job_id, JobStatus.ANALYZING, 15)
            self.repository.append_log(
                job_id,
                "mineru",
                JobLogLevel.INFO,
                "正在检测原 PDF 文字坐标、表格、图片和碎片稳定性",
                15,
            )
            mineru_started_at = perf_counter()
            try:
                native_layout = extract_native_pdf_layout(source_path)
                use_mineru = should_use_mineru(native_layout, job.strategy)
                if use_mineru:
                    self.repository.update_status(
                        job_id,
                        JobStatus.SEGMENTING,
                        25,
                    )
                    self.repository.append_log(
                        job_id,
                        "mineru",
                        JobLogLevel.INFO,
                        (
                            "已按任务策略和页面复杂度启用 MinerU 结构解析；"
                            "最终仍优先复用原生精确文字坐标"
                        ),
                        25,
                    )
                    parse_result = _normalize_parse_result(
                        self.parse_document(source_path, job.strategy)
                    )
                    layout_document = extract_mineru_pdf_layout(
                        source_path,
                        parse_result.content_list,
                        native_layout=native_layout,
                    )
                else:
                    layout_document = native_layout
            finally:
                mineru_duration_ms = _elapsed_milliseconds(mineru_started_at)
                self.repository.add_metrics(
                    job_id,
                    mineru_duration_ms=mineru_duration_ms,
                )
            if not layout_document.blocks:
                raise RuntimeError("PDF 中不存在可翻译文本块")
            granularity_issue = _find_layout_granularity_issue(layout_document)
            if granularity_issue:
                raise RuntimeError(granularity_issue)
            if self._is_cancelled(job_id):
                return
            self.repository.update_status(job_id, JobStatus.SEGMENTING, 40)
            self.repository.append_log(
                job_id,
                "mineru",
                JobLogLevel.SUCCESS,
                (
                    f"结构解析完成，提取 {layout_document.character_count} 个字符、"
                    f"{len(layout_document.blocks)} 个坐标文本块，"
                    f"使用 {_layout_source_label(layout_document.source_type)}，"
                    f"原生文字覆盖率 {layout_document.native_text_coverage:.1%}，"
                    f"表格块 {layout_document.table_block_count} 个，"
                    f"图片表格单元格 {layout_document.image_table_block_count} 个，"
                    f"不稳定碎片 {layout_document.unsafe_block_count} 个，"
                    f"待视觉识别图片 {len(layout_document.image_regions)} 个，"
                    f"耗时 {_format_duration(mineru_duration_ms)}"
                ),
                40,
            )

            self.repository.update_status(job_id, JobStatus.TRANSLATING, 45)
            self.repository.append_log(
                job_id,
                "translation",
                JobLogLevel.INFO,
                "正在调用任务模型翻译正文",
                45,
            )
            translation_started_at = perf_counter()
            try:
                raw_translation_result = (
                    self.translate_document(layout_document, job)
                    if self.translate_document is not None
                    else translate_layout_with_llm(
                        layout_document,
                        job,
                        repository=self.repository,
                    )
                )
                translation_result = _normalize_layout_translation_result(
                    raw_translation_result
                )
            except Exception as error:
                translation_duration_ms = _elapsed_milliseconds(
                    translation_started_at
                )
                failure_usage = _get_error_token_usage(error)
                self.repository.add_metrics(
                    job_id,
                    llm_duration_ms=translation_duration_ms,
                    input_tokens=failure_usage.input_tokens,
                    output_tokens=failure_usage.output_tokens,
                    total_tokens=failure_usage.total_tokens,
                    token_usage_available=failure_usage.available,
                )
                raise
            translation_duration_ms = _elapsed_milliseconds(translation_started_at)
            self.repository.add_metrics(
                job_id,
                llm_duration_ms=translation_duration_ms,
                input_tokens=translation_result.usage.input_tokens,
                output_tokens=translation_result.usage.output_tokens,
                total_tokens=translation_result.usage.total_tokens,
                token_usage_available=translation_result.usage.available,
                translation_memory_hit_count=translation_result.memory_hit_count,
            )
            if translation_result.memory_hit_count:
                self.repository.append_log(
                    job_id,
                    "translation-memory",
                    JobLogLevel.SUCCESS,
                    (
                        f"翻译记忆精确命中 {translation_result.memory_hit_count} "
                        "个文本块，已跳过对应模型请求"
                    ),
                    73,
                )
            if translation_result.additional_blocks:
                layout_document = replace(
                    layout_document,
                    blocks=(
                        *layout_document.blocks,
                        *translation_result.additional_blocks,
                    ),
                    source_type="hybrid",
                )
            missing_block_ids = [
                block.id
                for block in layout_document.blocks
                if block.is_translatable
                and not translation_result.translations.get(block.id, "").strip()
            ]
            if missing_block_ids:
                raise RuntimeError(
                    f"大模型缺少 {len(missing_block_ids)} 个文本块的翻译结果"
                )
            quality_issues = _validate_translation_quality(
                layout_document,
                translation_result.translations,
                job,
            )
            quality_retry_limit = max(
                0,
                int(os.getenv("LLM_QUALITY_RETRIES", "2")),
            )
            quality_retry_count = 0
            while quality_issues and quality_retry_count < quality_retry_limit:
                quality_retry_count += 1
                self.repository.update_status(
                    job_id,
                    JobStatus.REPAIRING,
                    78,
                )
                self.repository.append_log(
                    job_id,
                    "quality",
                    JobLogLevel.WARNING,
                    (
                        f"译文质量校验发现 {len(quality_issues)} 个问题，"
                        f"正在执行第 {quality_retry_count} 次定向修复"
                    ),
                    78,
                )
                repair_started_at = perf_counter()
                repair_result = repair_translation_quality_with_llm(
                    layout_document,
                    job,
                    quality_issues,
                )
                repair_duration_ms = _elapsed_milliseconds(repair_started_at)
                translation_duration_ms += repair_duration_ms
                self.repository.add_metrics(
                    job_id,
                    llm_duration_ms=repair_duration_ms,
                    input_tokens=repair_result.usage.input_tokens,
                    output_tokens=repair_result.usage.output_tokens,
                    total_tokens=repair_result.usage.total_tokens,
                    token_usage_available=repair_result.usage.available,
                )
                translation_result = replace(
                    translation_result,
                    translations={
                        **translation_result.translations,
                        **repair_result.translations,
                    },
                    usage=translation_result.usage + repair_result.usage,
                )
                quality_issues = _validate_translation_quality(
                    layout_document,
                    translation_result.translations,
                    job,
                )
            deterministic_repairs = _repair_generic_english_residuals(
                layout_document,
                translation_result.translations,
                quality_issues,
                job,
            )
            if deterministic_repairs:
                translation_result = replace(
                    translation_result,
                    translations={
                        **translation_result.translations,
                        **deterministic_repairs,
                    },
                )
                self.repository.append_log(
                    job_id,
                    "quality",
                    JobLogLevel.WARNING,
                    (
                        "大模型定向重译后仍有普通英文短标签，"
                        f"已确定性修复 {len(deterministic_repairs)} 个文本块"
                    ),
                    79,
                )
                quality_issues = _validate_translation_quality(
                    layout_document,
                    translation_result.translations,
                    job,
                )
            common_table_repairs = _repair_common_table_terms(
                layout_document,
                translation_result.translations,
                job,
            )
            if common_table_repairs:
                translation_result = replace(
                    translation_result,
                    translations={
                        **translation_result.translations,
                        **common_table_repairs,
                    },
                )
                self.repository.append_log(
                    job_id,
                    "quality",
                    JobLogLevel.SUCCESS,
                    (
                        "已按受控词典统一 "
                        f"{len(common_table_repairs)} 个高频表格标签"
                    ),
                    79,
                )
                quality_issues = _validate_translation_quality(
                    layout_document,
                    translation_result.translations,
                    job,
                )
            blocking_translation_issues = list(quality_issues)
            if blocking_translation_issues:
                self.repository.append_log(
                    job_id,
                    "quality",
                    JobLogLevel.WARNING,
                    (
                        f"仍有 {len(blocking_translation_issues)} 个译文问题；"
                        "将继续生成草稿并由最终自动质检阻止正式交付"
                    ),
                    79,
                )
            if self._is_cancelled(job_id):
                return
            self.repository.update_status(job_id, JobStatus.TRANSLATING, 80)
            self.repository.append_log(
                job_id,
                "translation",
                (
                    JobLogLevel.WARNING
                    if blocking_translation_issues
                    else JobLogLevel.SUCCESS
                ),
                (
                    f"正文翻译完成，共 {len(layout_document.blocks)} 个坐标文本块，"
                    f"其中视觉识别 {len(translation_result.additional_blocks)} 个，"
                    f"耗时 {_format_duration(translation_duration_ms)}，"
                    f"{_format_token_usage(translation_result.usage)}"
                ),
                80,
            )

            self.repository.update_status(job_id, JobStatus.TRANSLATING, 85)
            self.repository.append_log(
                job_id,
                "filename",
                JobLogLevel.INFO,
                "正在翻译产物文件名并保留章节、型号和专名",
                85,
            )
            file_name_started_at = perf_counter()
            try:
                file_name_result = _normalize_llm_result(
                    self.translate_file_name(job)
                )
            except Exception as error:
                file_name_duration_ms = _elapsed_milliseconds(
                    file_name_started_at
                )
                failure_usage = _get_error_token_usage(error)
                self.repository.add_metrics(
                    job_id,
                    llm_duration_ms=file_name_duration_ms,
                    input_tokens=failure_usage.input_tokens,
                    output_tokens=failure_usage.output_tokens,
                    total_tokens=failure_usage.total_tokens,
                    token_usage_available=failure_usage.available,
                )
                raise
            file_name_duration_ms = _elapsed_milliseconds(file_name_started_at)
            self.repository.add_metrics(
                job_id,
                llm_duration_ms=file_name_duration_ms,
                input_tokens=file_name_result.usage.input_tokens,
                output_tokens=file_name_result.usage.output_tokens,
                total_tokens=file_name_result.usage.total_tokens,
                token_usage_available=file_name_result.usage.available,
            )
            result_file_name = build_result_file_name(job, file_name_result.text)
            self.repository.update_status(job_id, JobStatus.RENDERING, 90)
            self.repository.append_log(
                job_id,
                "filename",
                JobLogLevel.SUCCESS,
                f"产物文件名已确定：{result_file_name}",
                90,
            )

            self.repository.update_status(job_id, JobStatus.RENDERING, 92)
            self.repository.append_log(
                job_id,
                "render",
                JobLogLevel.INFO,
                "正在原 PDF 页面上遮盖原文字并按原坐标写入译文",
                92,
            )
            result_path = self.repository.result_directory / f"{job.id}.pdf"
            render_started_at = perf_counter()
            try:
                render_report = self.render_document(
                    source_path,
                    result_path,
                    layout_document,
                    translation_result.translations,
                )
            finally:
                render_duration_ms = _elapsed_milliseconds(render_started_at)
                self.repository.add_metrics(
                    job_id,
                    render_duration_ms=render_duration_ms,
                )
            layout_repair_retry_limit = max(
                0,
                int(os.getenv("LLM_LAYOUT_REPAIR_RETRIES", "2")),
            )
            layout_repair_retry_count = 0
            while (
                render_report.overflow_block_ids
                and layout_repair_retry_count < layout_repair_retry_limit
                and self.translate_document is None
            ):
                layout_repair_retry_count += 1
                self.repository.update_status(
                    job_id,
                    JobStatus.REPAIRING,
                    94,
                )
                self.repository.append_log(
                    job_id,
                    "layout-repair",
                    JobLogLevel.WARNING,
                    (
                        f"发现 {len(render_report.overflow_block_ids)} 个译文框溢出，"
                        f"正在执行第 {layout_repair_retry_count} 次限长重译并重新排版"
                    ),
                    94,
                )
                layout_repair_started_at = perf_counter()
                layout_repair_result = repair_layout_overflow_with_llm(
                    layout_document,
                    job,
                    translation_result.translations,
                    render_report.overflow_block_ids,
                )
                layout_repair_duration_ms = _elapsed_milliseconds(
                    layout_repair_started_at
                )
                translation_result = replace(
                    translation_result,
                    translations={
                        **translation_result.translations,
                        **layout_repair_result.translations,
                    },
                    usage=translation_result.usage + layout_repair_result.usage,
                )
                self.repository.add_metrics(
                    job_id,
                    llm_duration_ms=layout_repair_duration_ms,
                    input_tokens=layout_repair_result.usage.input_tokens,
                    output_tokens=layout_repair_result.usage.output_tokens,
                    total_tokens=layout_repair_result.usage.total_tokens,
                    token_usage_available=layout_repair_result.usage.available,
                )
                repair_render_started_at = perf_counter()
                render_report = self.render_document(
                    source_path,
                    result_path,
                    layout_document,
                    translation_result.translations,
                )
                self.repository.add_metrics(
                    job_id,
                    render_duration_ms=_elapsed_milliseconds(
                        repair_render_started_at
                    ),
                )
            if render_report.overflow_block_count:
                self.repository.append_log(
                    job_id,
                    "render",
                    JobLogLevel.WARNING,
                    (
                        f"{render_report.overflow_block_count} 个文本块的译文长度超过"
                        "原区域容量；已保留草稿并交由最终质量门禁阻止正式下载"
                    ),
                    96,
                )
            elif layout_repair_retry_count:
                self.repository.append_log(
                    job_id,
                    "layout-repair",
                    JobLogLevel.SUCCESS,
                    (
                        f"文本框溢出已自动修复，共执行 "
                        f"{layout_repair_retry_count} 次限长重译"
                    ),
                    96,
                )
            quality_regions = (
                build_full_page_quality_regions(layout_document)
                if (
                    self.translate_document is None
                    and os.getenv(
                        "DOCWEAVE_FULL_PAGE_VISUAL_REVIEW",
                        "true",
                    ).strip().casefold()
                    not in {"0", "false", "no"}
                )
                else ()
            )
            if quality_regions:
                self.repository.update_status(
                    job_id,
                    JobStatus.VALIDATING,
                    97,
                )
                self.repository.append_log(
                    job_id,
                    "quality",
                    JobLogLevel.INFO,
                    "正在渲染全部页面并执行可翻译外语残留复检",
                    97,
                )
                visual_sources = tuple(
                    block.source_text
                    for block in translation_result.additional_blocks
                )
                visual_residual_context = (
                    visual_sources
                    + tuple(
                        block.source_text
                        for block in layout_document.blocks
                        if block.source_type != "vision"
                    )
                )
                visual_quality_retry_limit = max(
                    0,
                    int(os.getenv("LLM_VISUAL_QUALITY_RETRIES", "2")),
                )
                visual_auto_repair_enabled = (
                    os.getenv(
                        "DOCWEAVE_FULL_PAGE_VISUAL_AUTO_REPAIR",
                        "false",
                    )
                    .strip()
                    .casefold()
                    in {"1", "true", "yes"}
                )
                visual_quality_retry_count = 0
                visual_quality_issue_message: str | None = None
                while True:
                    quality_started_at = perf_counter()
                    (
                        visual_residuals,
                        quality_usage,
                    ) = review_visual_residuals_with_llm(
                        result_path,
                        job,
                        quality_regions,
                        visual_sources,
                    )
                    repairable_residual_texts = (
                        _filter_preserved_visual_residuals(
                            [
                                residual.source_text
                                for residual in visual_residuals
                            ],
                            visual_residual_context,
                            job,
                        )
                    )
                    repairable_residual_keys = {
                        residual.casefold()
                        for residual in repairable_residual_texts
                    }
                    visual_residuals = [
                        residual
                        for residual in visual_residuals
                        if residual.source_text.casefold()
                        in repairable_residual_keys
                    ]
                    quality_duration_ms = _elapsed_milliseconds(
                        quality_started_at
                    )
                    self.repository.add_metrics(
                        job_id,
                        llm_duration_ms=quality_duration_ms,
                        input_tokens=quality_usage.input_tokens,
                        output_tokens=quality_usage.output_tokens,
                        total_tokens=quality_usage.total_tokens,
                        token_usage_available=quality_usage.available,
                    )
                    if not visual_residuals:
                        break
                    if not visual_auto_repair_enabled:
                        visual_quality_issue_message = (
                            "只读视觉质检发现疑似未翻译文字："
                            + "、".join(
                                residual.source_text
                                for residual in visual_residuals[:10]
                            )
                            + "；为避免误遮盖原版内容，未执行自动坐标覆盖"
                        )
                        break
                    if (
                        visual_quality_retry_count
                        >= visual_quality_retry_limit
                    ):
                        visual_quality_issue_message = (
                            "译后视觉质检发现未翻译文字："
                            + "、".join(
                                residual.source_text
                                for residual in visual_residuals[:10]
                            )
                        )
                        break
                    visual_quality_retry_count += 1
                    overlapping_block_ids = (
                        _visual_residual_overlapping_block_ids(
                            visual_residuals,
                            layout_document.blocks,
                        )
                    )
                    if overlapping_block_ids:
                        overlap_repair_started_at = perf_counter()
                        overlap_repair_result = (
                            repair_translation_quality_with_llm(
                                layout_document,
                                job,
                                [
                                    f"{block_id} 视觉残留需消除"
                                    for block_id in sorted(
                                        overlapping_block_ids
                                    )
                                ],
                            )
                        )
                        self.repository.add_metrics(
                            job_id,
                            llm_duration_ms=_elapsed_milliseconds(
                                overlap_repair_started_at
                            ),
                            input_tokens=(
                                overlap_repair_result.usage.input_tokens
                            ),
                            output_tokens=(
                                overlap_repair_result.usage.output_tokens
                            ),
                            total_tokens=(
                                overlap_repair_result.usage.total_tokens
                            ),
                            token_usage_available=(
                                overlap_repair_result.usage.available
                            ),
                        )
                        translation_result = replace(
                            translation_result,
                            translations={
                                **translation_result.translations,
                                **overlap_repair_result.translations,
                            },
                            usage=(
                                translation_result.usage
                                + overlap_repair_result.usage
                            ),
                        )
                    uncovered_visual_residuals = [
                        residual
                        for residual in visual_residuals
                        if not _visual_residual_overlaps_block_ids(
                            residual,
                            layout_document.blocks,
                            overlapping_block_ids,
                        )
                    ]
                    repair_blocks, repair_translations = (
                        _build_visual_residual_repairs(
                            uncovered_visual_residuals,
                            visual_quality_retry_count,
                        )
                    )
                    if repair_blocks:
                        layout_document = replace(
                            layout_document,
                            blocks=(
                                *layout_document.blocks,
                                *repair_blocks,
                            ),
                            source_type="hybrid",
                        )
                    translation_result = replace(
                        translation_result,
                        translations={
                            **translation_result.translations,
                            **repair_translations,
                        },
                        additional_blocks=(
                            *translation_result.additional_blocks,
                            *repair_blocks,
                        ),
                    )
                    vision_mask_padding = (
                        12.0 + visual_quality_retry_count * 8.0
                    )
                    self.repository.append_log(
                        job_id,
                        "quality",
                        JobLogLevel.WARNING,
                        (
                            "译后视觉质检发现可翻译文字残留："
                            + "、".join(
                                residual.source_text
                                for residual in visual_residuals[:5]
                            )
                            + f"；正在执行第 {visual_quality_retry_count} "
                            f"次修复（定向重译 {len(overlapping_block_ids)} 块，"
                            f"新建坐标补译 {len(repair_blocks)} 块）"
                            "并重新复检"
                        ),
                        97,
                    )
                    repair_render_started_at = perf_counter()
                    render_report = self.render_document(
                        source_path,
                        result_path,
                        layout_document,
                        translation_result.translations,
                        vision_mask_padding=vision_mask_padding,
                    )
                    self.repository.add_metrics(
                        job_id,
                        render_duration_ms=_elapsed_milliseconds(
                            repair_render_started_at
                        ),
                    )
                self.repository.append_log(
                    job_id,
                    "quality",
                    (
                        JobLogLevel.WARNING
                        if visual_quality_issue_message
                        else JobLogLevel.SUCCESS
                    ),
                    (
                        visual_quality_issue_message
                        or (
                            f"视觉残留复检通过，检查 {len(quality_regions)} 页"
                            + (
                                f"，自动修复 {visual_quality_retry_count} 次"
                                if visual_quality_retry_count
                                else ""
                            )
                            + f"，末次复检耗时 "
                            f"{_format_duration(quality_duration_ms)}"
                        )
                    ),
                    98,
                )
            else:
                visual_quality_issue_message = None

            self.repository.update_status(
                job_id,
                JobStatus.VALIDATING,
                99,
            )
            self.repository.append_log(
                job_id,
                "validation",
                JobLogLevel.INFO,
                "正在对全部页面执行尺寸、溢出、字号和非文本内容保持检查",
                99,
            )
            validation_started_at = perf_counter()
            automated_quality_report = self.validate_document(
                source_path,
                result_path,
                layout_document,
                render_report,
            )
            validation_duration_ms = _elapsed_milliseconds(validation_started_at)
            self.repository.add_metrics(
                job_id,
                validation_duration_ms=validation_duration_ms,
            )
            quality_issue_dicts = [
                issue.as_dict()
                for issue in automated_quality_report.issues
            ]
            quality_issue_dicts.extend(
                {
                    "stage": "translation-quality",
                    "code": "translation_quality_failed",
                    "severity": "error",
                    "message": issue,
                    "block_id": issue.split(" ", 1)[0],
                }
                for issue in blocking_translation_issues
            )
            if visual_quality_issue_message:
                quality_issue_dicts.append(
                    {
                        "stage": "visual-quality",
                        "code": "untranslated_visual_text",
                        "severity": "error",
                        "message": visual_quality_issue_message,
                    }
                )
            persisted_quality_issues = self.repository.replace_quality_issues(
                job_id,
                quality_issue_dicts,
            )
            result_file_size = result_path.stat().st_size
            blocking_quality_issues = [
                issue
                for issue in persisted_quality_issues
                if issue.severity == "error"
            ]
            if blocking_quality_issues:
                review_message = (
                    f"严格质检记录 {len(blocking_quality_issues)} 项待复核细节，"
                    "结果文件已生成并可正常下载；"
                    "问题清单已保留用于人工抽检和持续优化"
                )
                self.repository.update_status(
                    job_id,
                    JobStatus.NEEDS_REVIEW,
                    100,
                    error_message=review_message,
                    result_path=result_path,
                    result_file_name=result_file_name,
                )
                self.repository.finish_metrics(job_id, result_file_size)
                self.repository.append_log(
                    job_id,
                    "needs-review",
                    JobLogLevel.WARNING,
                    review_message,
                    100,
                )
                return

            learned_candidate_count = self.repository.record_translation_learning(
                job,
                (
                    (block.source_text, translation_result.translations[block.id])
                    for block in layout_document.blocks
                    if block.id in translation_result.translations
                    and block.is_translatable
                ),
            )
            self.repository.update_status(
                job_id,
                JobStatus.COMPLETED,
                100,
                result_path=result_path,
                result_file_name=result_file_name,
            )
            self.repository.finish_metrics(job_id, result_file_size)
            self.repository.append_log(
                job_id,
                "completed",
                JobLogLevel.SUCCESS,
                (
                    f"任务成功完成，结果 {result_file_name}，"
                    f"保留原始 {render_report.page_count} 页页面尺寸与非文本内容，"
                    f"原坐标替换 {render_report.replaced_block_count} 个文本块，"
                    f"自动质检渲染 {automated_quality_report.rendered_page_count} 页，"
                    f"学习术语候选 {learned_candidate_count} 条，"
                    f"文件大小 {_format_bytes(result_file_size)}"
                ),
                100,
            )
        except KeyError:
            LOGGER.warning("任务不存在或已被移除：%s", job_id)
        except Exception as error:
            LOGGER.exception("任务处理失败：%s", job_id)
            try:
                if not self._is_cancelled(job_id):
                    failed_job = self.repository.get(job_id)
                    safe_error_message = _safe_error_message(error)
                    if (
                        result_path is not None
                        and result_path.is_file()
                        and result_path.stat().st_size > 0
                        and result_file_name
                    ):
                        review_message = (
                            f"质量评估服务未完整返回：{safe_error_message}；"
                            "结果文件已生成并可正常下载，"
                            "已保留待复核记录"
                        )
                        self.repository.replace_quality_issues(
                            job_id,
                            [
                                {
                                    "stage": "visual-quality",
                                    "code": "quality_evaluation_failed",
                                    "severity": "error",
                                    "message": review_message,
                                }
                            ],
                        )
                        self.repository.update_status(
                            job_id,
                            JobStatus.NEEDS_REVIEW,
                            100,
                            error_message=review_message,
                            result_path=result_path,
                            result_file_name=result_file_name,
                        )
                        self.repository.finish_metrics(
                            job_id,
                            result_path.stat().st_size,
                        )
                        self.repository.append_log(
                            job_id,
                            "needs-review",
                            JobLogLevel.WARNING,
                            review_message,
                            100,
                        )
                    else:
                        self.repository.update_status(
                            job_id,
                            JobStatus.FAILED,
                            failed_job.progress,
                            error_message=safe_error_message,
                        )
                        self.repository.finish_metrics(job_id)
                        self.repository.append_log(
                            job_id,
                            "failed",
                            JobLogLevel.ERROR,
                            f"任务失败：{safe_error_message}",
                            failed_job.progress,
                        )
            except KeyError:
                LOGGER.warning("记录失败状态时任务已不存在：%s", job_id)

    def cancel(self, job_id: str) -> LocalizationJob:
        """取消尚未开始的线程任务，并更新持久化状态。"""

        future = self.futures.get(job_id)
        if future:
            future.cancel()
        return self.repository.cancel(job_id)

    def shutdown(self) -> None:
        """关闭线程池，不等待远程处理完成。"""

        self.executor.shutdown(wait=False, cancel_futures=True)

    def _is_cancelled(self, job_id: str) -> bool:
        return self.repository.get(job_id).status == JobStatus.CANCELLED

    def _handle_future(self, job_id: str, future: Future[None]) -> None:
        self.futures.pop(job_id, None)
        if future.cancelled():
            LOGGER.info("任务已在执行前取消：%s", job_id)


def _find_layout_granularity_issue(
    layout_document: PdfLayoutDocument,
) -> str | None:
    """检测原生文字是否被错误合并为无法原位回填的大段文本。

    Args:
        layout_document: 解析后的 PDF 版面中间模型。

    Returns:
        发现异常时返回可读错误信息，否则返回 ``None``。
    """

    translatable_blocks = [
        block for block in layout_document.blocks if block.is_translatable
    ]
    if (
        not translatable_blocks
        or layout_document.native_text_coverage < 0.8
    ):
        return None
    compact_lengths = [
        len(re.sub(r"\s+", "", block.source_text))
        for block in translatable_blocks
    ]
    average_length = sum(compact_lengths) / len(compact_lengths)
    oversized_count = sum(length > 260 for length in compact_lengths)
    multiline_count = sum(
        block.source_text.count("\n") >= 2 and length > 160
        for block, length in zip(translatable_blocks, compact_lengths)
    )
    unsafe_geometry_blocks = []
    for block in translatable_blocks:
        page_width, page_height = layout_document.page_sizes[block.page_index]
        block_area_ratio = (
            max(0.0, block.bbox[2] - block.bbox[0])
            * max(0.0, block.bbox[3] - block.bbox[1])
            / max(1.0, page_width * page_height)
        )
        if block_area_ratio >= 0.05 and block.geometry_fill_ratio < 0.35:
            unsafe_geometry_blocks.append(block)
    page_count = max(1, len(layout_document.page_sizes))
    too_sparse = (
        average_length > 220
        and len(translatable_blocks) < page_count * 20
    )
    too_many_oversized = oversized_count / len(translatable_blocks) >= 0.2
    too_many_multiline = multiline_count / len(translatable_blocks) >= 0.1
    if not (
        too_sparse
        or too_many_oversized
        or too_many_multiline
        or unsafe_geometry_blocks
    ):
        return None
    return (
        "版面分块异常：原生文字被合并为大段文本，禁止进入覆盖渲染；"
        f"共 {page_count} 页、{len(translatable_blocks)} 个可翻译块、"
        f"平均 {average_length:.0f} 字符/块、"
        f"超大文本块 {oversized_count} 个、"
        f"低密度大遮盖框 {len(unsafe_geometry_blocks)} 个。"
        "请使用逐行坐标重新解析"
    )


def parse_pdf_with_mineru(source_path: Path, strategy: str) -> DocumentParseResult:
    """调用 MinerU 同步解析接口并提取 Markdown 与坐标内容列表。

    Args:
        source_path: 已持久化的源 PDF。
        strategy: 前端选择的处理策略。

    Returns:
        MinerU 输出的语义文本和结构化内容列表。
    """

    host = os.getenv("MINERU_HOST", "127.0.0.1").strip()
    port = os.getenv("MINERU_PORT", "8000").strip()
    base_url = os.getenv("MINERU_BASE_URL", f"http://{host}:{port}").rstrip("/")
    parse_method = "txt" if strategy == "原生文字优先" else "auto"
    request_data = {
        "backend": os.getenv("MINERU_BACKEND", "pipeline"),
        "effort": "medium",
        "parse_method": parse_method,
        "return_md": "true",
        "return_middle_json": "true",
        "return_content_list": "true",
        "response_format_zip": "true",
        "return_original_file": "false",
    }
    timeout_seconds = float(os.getenv("MINERU_TIMEOUT_SECONDS", "900"))
    with source_path.open("rb") as source_file, httpx.Client(
        timeout=timeout_seconds,
        trust_env=False,
    ) as client:
        response = client.post(
            f"{base_url}/file_parse",
            data=request_data,
            files=[("files", (source_path.name, source_file, "application/pdf"))],
        )
        if response.is_error:
            raise RuntimeError(_remote_error("MinerU", response))
    return _extract_mineru_result(response)


def translate_layout_with_llm(
    layout: PdfLayoutDocument,
    job: LocalizationJob,
    *,
    repository: JobRepository | None = None,
) -> LayoutTranslationResult:
    """调用 OpenAI 兼容的通用 LLM API 按坐标文本块翻译。

    Args:
        layout: 带页面和坐标信息的文本块。
        job: 包含模型与翻译规则的真实任务。

    Returns:
        文本块 ID 到译文的映射及累计 Token 用量。
    """

    base_url = _get_llm_base_url()
    api_key = os.getenv("LLM_API_KEY", "")
    if not base_url or not api_key:
        raise RuntimeError("LLM_BASE_URL 或 LLM_API_KEY 未配置")

    all_translatable_blocks = tuple(
        block for block in layout.blocks if block.is_translatable
    )
    memory_by_source = (
        repository.get_translation_memory(
            job.translation_policy,
            (block.source_text for block in all_translatable_blocks),
        )
        if repository is not None
        else {}
    )
    memory_translations = {
        block.id: memory_by_source[block.source_text.strip()]
        for block in all_translatable_blocks
        if block.source_text.strip() in memory_by_source
    }
    translatable_blocks = tuple(
        block
        for block in all_translatable_blocks
        if block.id not in memory_translations
    )
    batches = _chunk_layout_blocks(translatable_blocks)
    glossary_snapshot = (
        repository.get_glossary_snapshot(job.id)
        if repository is not None
        else []
    )
    instruction = build_translation_instruction(
        job.translation_policy,
        glossary_snapshot,
    )
    translations: dict[str, str] = {
        block.id: block.source_text
        for block in layout.blocks
        if not block.is_translatable
    }
    translations.update(memory_translations)
    visual_blocks: list[PdfTextBlock] = []
    accumulated_usage = TokenUsage()
    resumed_batch_count = 0
    timeout_seconds = float(os.getenv("LLM_TIMEOUT_SECONDS", "300"))
    batch_worker_count = max(
        1,
        min(
            len(batches),
            int(os.getenv("LLM_BATCH_WORKERS", "3")),
        ),
    )
    try:
        with ThreadPoolExecutor(
            max_workers=batch_worker_count,
            thread_name_prefix="docweave-llm-batch",
        ) as batch_executor:
            futures = [
                batch_executor.submit(
                    _translate_persisted_batch,
                    base_url=base_url,
                    api_key=api_key,
                    model=job.model,
                    instruction=instruction,
                    batch=batch,
                    batch_index=index,
                    batch_count=len(batches),
                    timeout_seconds=timeout_seconds,
                    repository=repository,
                    job_id=job.id,
                    stage="translation",
                )
                for index, batch in enumerate(batches, start=1)
            ]
            batch_errors: list[Exception] = []
            for future in as_completed(futures):
                try:
                    (
                        batch_translations,
                        batch_usage,
                        resumed,
                    ) = future.result()
                except Exception as error:
                    accumulated_usage += _get_error_token_usage(error)
                    batch_errors.append(error)
                    continue
                translations.update(batch_translations)
                accumulated_usage += batch_usage
                resumed_batch_count += int(resumed)
            if batch_errors:
                raise batch_errors[0]
        if repository is not None:
            repository.add_metrics(
                job.id,
                translation_batch_count=len(batches),
                resumed_translation_batch_count=resumed_batch_count,
            )
        safe_image_regions, preserved_image_regions = (
            _select_visual_translation_regions(layout)
        )
        if preserved_image_regions:
            message = (
                f"检测到 {len(preserved_image_regions)} 个密集图片表格缺少可靠"
                "单元格坐标，已保留原图，避免整表文字错位覆盖"
            )
            LOGGER.warning("%s：job=%s", message, job.id)
            if repository is not None:
                repository.append_log(
                    job.id,
                    "vision",
                    JobLogLevel.WARNING,
                    message,
                    68,
                )
        if safe_image_regions:
            vision_worker_count = max(
                1,
                min(
                    len(safe_image_regions),
                    int(os.getenv("LLM_VISION_WORKERS", "2")),
                ),
            )
            with ThreadPoolExecutor(
                max_workers=vision_worker_count,
                thread_name_prefix="docweave-llm-vision",
            ) as vision_executor:
                vision_futures = [
                    vision_executor.submit(
                        _translate_image_region,
                        base_url=base_url,
                        api_key=api_key,
                        model=job.model,
                        instruction=instruction,
                        region=region,
                        timeout_seconds=timeout_seconds,
                    )
                    for region in safe_image_regions
                ]
                for future in as_completed(vision_futures):
                    region_blocks, region_translations, region_usage = future.result()
                    visual_blocks.extend(region_blocks)
                    translations.update(region_translations)
                    accumulated_usage += region_usage
    except Exception as error:
        raise LlmExecutionError(str(error), accumulated_usage) from error
    return LayoutTranslationResult(
        translations=translations,
        usage=accumulated_usage,
        additional_blocks=tuple(
            sorted(
                visual_blocks,
                key=lambda block: (
                    block.page_index,
                    block.bbox[1],
                    block.bbox[0],
                ),
            )
        ),
        memory_hit_count=len(memory_translations),
    )


def repair_translation_quality_with_llm(
    layout: PdfLayoutDocument,
    job: LocalizationJob,
    issues: list[str],
) -> LayoutTranslationResult:
    """只重译未通过质量门禁的文本块，避免整份文档重复消耗。"""

    base_url = _get_llm_base_url()
    api_key = os.getenv("LLM_API_KEY", "")
    if not base_url or not api_key:
        raise RuntimeError("LLM_BASE_URL 或 LLM_API_KEY 未配置")
    issue_ids = {
        issue.split(" ", 1)[0]
        for issue in issues
        if issue.strip()
    }
    issue_blocks = tuple(
        block
        for block in layout.blocks
        if block.id in issue_ids
    )
    if not issue_blocks:
        raise RuntimeError("质量修复未定位到对应文本块")

    batches = _chunk_layout_blocks(
        issue_blocks,
        character_limit=4_000,
        block_limit=40,
    )
    instruction = (
        build_translation_instruction(job.translation_policy)
        + "\n这是质量门禁后的定向重译。必须消除日文假名、韩文和普通英文残留；"
        "所有数字、百分比、型号与标准号必须逐字保持，不得新增、删除或重复。"
    )
    translations: dict[str, str] = {}
    usage = TokenUsage()
    timeout_seconds = float(os.getenv("LLM_TIMEOUT_SECONDS", "300"))
    worker_count = max(
        1,
        min(len(batches), int(os.getenv("LLM_BATCH_WORKERS", "3"))),
    )
    try:
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="docweave-quality-repair",
        ) as executor:
            futures = [
                executor.submit(
                    _translate_layout_batch,
                    base_url=base_url,
                    api_key=api_key,
                    model=job.model,
                    instruction=instruction,
                    batch=batch,
                    batch_index=index,
                    batch_count=len(batches),
                    timeout_seconds=timeout_seconds,
                )
                for index, batch in enumerate(batches, start=1)
            ]
            for future in as_completed(futures):
                batch_translations, batch_usage = future.result()
                translations.update(batch_translations)
                usage += batch_usage
    except Exception as error:
        raise LlmExecutionError(str(error), usage) from error
    return LayoutTranslationResult(
        translations=translations,
        usage=usage,
    )


def repair_layout_overflow_with_llm(
    layout: PdfLayoutDocument,
    job: LocalizationJob,
    translations: dict[str, str],
    overflow_block_ids: tuple[str, ...],
) -> LayoutTranslationResult:
    """对超出原文本框容量的译文执行限长改写。

    Args:
        layout: 原始坐标与版式信息。
        job: 当前任务及模型配置。
        translations: 当前完整译文映射。
        overflow_block_ids: 渲染阶段确认溢出的文本块 ID。

    Returns:
        仅包含已压缩文本块的新译文和 Token 用量。
    """

    base_url = _get_llm_base_url()
    api_key = os.getenv("LLM_API_KEY", "")
    if not base_url or not api_key:
        raise RuntimeError("LLM_BASE_URL 或 LLM_API_KEY 未配置")
    overflow_ids = set(overflow_block_ids)
    compact_blocks = tuple(
        replace(
            block,
            source_text=(
                f"硬性上限：{_estimate_layout_character_capacity(block)} 个中文字符。"
                f"当前译文：{translations.get(block.id, block.source_text)}"
            ),
        )
        for block in layout.blocks
        if block.id in overflow_ids
    )
    if not compact_blocks:
        raise RuntimeError("版式修复未定位到对应文本块")
    batches = _chunk_layout_blocks(
        compact_blocks,
        character_limit=2_000,
        block_limit=24,
    )
    instruction = (
        build_translation_instruction(job.translation_policy)
        + "\n这是原版式文本框溢出的自动修复。输入已包含“硬性上限”和“当前译文”。"
        "请在不改变事实、数字、百分比、型号、标准号和专名的前提下压缩中文表述，"
        "输出不得超过每条给定的字符上限，不得输出解释或省略号。"
    )
    repaired_translations: dict[str, str] = {}
    usage = TokenUsage()
    timeout_seconds = float(os.getenv("LLM_TIMEOUT_SECONDS", "300"))
    worker_count = max(
        1,
        min(len(batches), int(os.getenv("LLM_BATCH_WORKERS", "3"))),
    )
    try:
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="docweave-layout-repair",
        ) as executor:
            futures = [
                executor.submit(
                    _translate_layout_batch,
                    base_url=base_url,
                    api_key=api_key,
                    model=job.model,
                    instruction=instruction,
                    batch=batch,
                    batch_index=index,
                    batch_count=len(batches),
                    timeout_seconds=timeout_seconds,
                )
                for index, batch in enumerate(batches, start=1)
            ]
            for future in as_completed(futures):
                batch_translations, batch_usage = future.result()
                repaired_translations.update(batch_translations)
                usage += batch_usage
    except Exception as error:
        raise LlmExecutionError(str(error), usage) from error
    return LayoutTranslationResult(
        translations=repaired_translations,
        usage=usage,
    )


def _estimate_layout_character_capacity(block: PdfTextBlock) -> int:
    """按文本框面积和区域字号下限估算可读中文字符容量。"""

    x0, top, x1, bottom = block.render_bbox
    width = max(1.0, x1 - x0)
    height = max(1.0, bottom - top)
    minimum_font_size = (
        6.5
        if block.region_type == "table"
        else 6.0
        if block.region_type in {"chart", "image", "caption"}
        else 7.0
    )
    characters_per_line = max(1, int(width / minimum_font_size))
    line_count = max(1, int(height / (minimum_font_size * 1.12)))
    return max(4, characters_per_line * line_count)


def _translate_persisted_batch(
    *,
    base_url: str,
    api_key: str,
    model: str,
    instruction: str,
    batch: list[PdfTextBlock],
    batch_index: int,
    batch_count: int,
    timeout_seconds: float,
    repository: JobRepository | None,
    job_id: str,
    stage: str,
) -> tuple[dict[str, str], TokenUsage, bool]:
    """翻译并持久化一个页面批次，失败时递归拆分到单块。"""

    batch_key = _translation_batch_key(stage, batch)
    persisted = (
        repository.get_translation_batch(job_id, batch_key)
        if repository is not None
        else None
    )
    persisted_translations = (
        dict(persisted.get("translations", {}))
        if persisted is not None
        else {}
    )
    if (
        persisted is not None
        and persisted.get("status") == "completed"
        and all(block.id in persisted_translations for block in batch)
    ):
        return persisted_translations, TokenUsage(), True

    pending_blocks = [
        block for block in batch if block.id not in persisted_translations
    ]
    accumulated_translations = dict(persisted_translations)
    accumulated_usage = TokenUsage()

    def translate_subset(subset: list[PdfTextBlock]) -> None:
        nonlocal accumulated_usage
        if not subset:
            return
        try:
            subset_translations, subset_usage = _translate_layout_batch(
                base_url=base_url,
                api_key=api_key,
                model=model,
                instruction=instruction,
                batch=subset,
                batch_index=batch_index,
                batch_count=batch_count,
                timeout_seconds=timeout_seconds,
            )
        except Exception as error:
            failure_usage = _get_error_token_usage(error)
            accumulated_usage += failure_usage
            if repository is not None:
                repository.save_translation_batch_progress(
                    job_id,
                    batch_key,
                    stage=stage,
                    page_index=batch[0].page_index,
                    block_ids=(block.id for block in batch),
                    translations={},
                    status="partial",
                    attempts_increment=1,
                    input_tokens=failure_usage.input_tokens,
                    output_tokens=failure_usage.output_tokens,
                    total_tokens=failure_usage.total_tokens,
                    token_usage_available=failure_usage.available,
                    error_message=_safe_error_message(error),
                )
            if len(subset) == 1:
                raise
            midpoint = max(1, len(subset) // 2)
            translate_subset(subset[:midpoint])
            translate_subset(subset[midpoint:])
            return
        accumulated_translations.update(subset_translations)
        accumulated_usage += subset_usage
        if repository is not None:
            repository.save_translation_batch_progress(
                job_id,
                batch_key,
                stage=stage,
                page_index=batch[0].page_index,
                block_ids=(block.id for block in batch),
                translations=subset_translations,
                status="partial",
                attempts_increment=1,
                input_tokens=subset_usage.input_tokens,
                output_tokens=subset_usage.output_tokens,
                total_tokens=subset_usage.total_tokens,
                token_usage_available=subset_usage.available,
            )

    translate_subset(pending_blocks)
    if repository is not None:
        repository.save_translation_batch_progress(
            job_id,
            batch_key,
            stage=stage,
            page_index=batch[0].page_index,
            block_ids=(block.id for block in batch),
            translations={},
            status="completed",
            error_message=None,
        )
    return accumulated_translations, accumulated_usage, bool(
        persisted_translations
    )


def _translation_batch_key(stage: str, batch: list[PdfTextBlock]) -> str:
    """根据阶段、页码、块 ID 与原文生成稳定批次键。"""

    digest = hashlib.sha256()
    digest.update(stage.encode("utf-8"))
    for block in batch:
        digest.update(
            (
                f"\0{block.page_index}\0{block.id}\0{block.source_text}"
            ).encode("utf-8")
        )
    return digest.hexdigest()


def _translate_layout_batch(
    *,
    base_url: str,
    api_key: str,
    model: str,
    instruction: str,
    batch: list[PdfTextBlock],
    batch_index: int,
    batch_count: int,
    timeout_seconds: float,
) -> tuple[dict[str, str], TokenUsage]:
    """独立翻译一个文本块批次，供受控并发执行。"""

    source_blocks = [
        {"id": block.id, "text": block.source_text}
        for block in batch
    ]
    with httpx.Client(timeout=timeout_seconds, trust_env=False) as client:
        response = _post_llm_request(
            client,
            url=f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            payload={
                "model": model,
                "temperature": 0.1,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            f"{instruction}\n"
                            "你正在翻译 PDF 坐标文本块。"
                            "只返回严格 JSON，不要 Markdown、代码围栏或解释。"
                            "输出格式必须为 "
                            '{"translations":[{"id":"原ID","text":"译文"}]}。'
                            "ID 必须原样保留，每个输入 ID 必须且只能返回一次。"
                            "只翻译 text 的自然语言，不生成 HTML 标签；"
                            "数字、型号、标准号、公式和任务要求保留的词不得改写。"
                            "图表标签、表头和短语必须采用紧凑译法；"
                            "日文“～用/～向け”优先译为“～用”，不要扩写成“用于～”。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"以下是第 {batch_index}/{batch_count} 批文本块：\n"
                            f"{json.dumps(source_blocks, ensure_ascii=False)}"
                        ),
                    },
                ],
            },
            service_name="大模型",
        )
    payload = response.json()
    usage = _extract_llm_usage(payload)
    try:
        translations = _extract_block_translations(
            _extract_llm_content(payload),
            batch,
        )
    except Exception as error:
        raise LlmExecutionError(str(error), usage) from error
    return translations, usage


def _translate_image_region(
    *,
    base_url: str,
    api_key: str,
    model: str,
    instruction: str,
    region: PdfImageRegion,
    timeout_seconds: float,
) -> tuple[list[PdfTextBlock], dict[str, str], TokenUsage]:
    """识别并翻译嵌入图片内的自然语言文字，同时返回原图相对坐标。"""

    image_url = (
        "data:image/png;base64,"
        + base64.b64encode(region.image_png).decode("ascii")
    )
    with httpx.Client(timeout=timeout_seconds, trust_env=False) as client:
        response = _post_llm_request(
            client,
            url=f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            payload={
                "model": model,
                "temperature": 0,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            f"{instruction}\n"
                            "你还负责识别嵌入图片内的文字。必须找出图片中全部需要翻译的"
                            "自然语言文字，并给出紧贴原文字的矩形坐标。"
                            "坐标使用图片左上角为原点的 0 到 1000 归一化值。"
                            "不要返回纯数字、百分比、公式、公司名、品牌名、产品型号或无需"
                            "翻译的符号。译文必须简短，图表中的“～用/～向け”优先译成"
                            "“～用”，不要扩写成“用于～”。只返回严格 JSON，格式为 "
                            '{"items":[{"source_text":"原文","translated_text":"译文",'
                            '"bbox":[x0,y0,x1,y1]}]}。不要返回解释或代码围栏。'
                        ),
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    f"识别并翻译第 {region.page_index + 1} 页图片区域"
                                    f" {region.id} 内的全部可翻译文字。"
                                ),
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": image_url},
                            },
                        ],
                    },
                ],
            },
            service_name="大模型图片文字识别",
        )
    payload = response.json()
    usage = _extract_llm_usage(payload)
    parsed = _extract_json_payload(_extract_llm_content(payload))
    items = parsed.get("items")
    if not isinstance(items, list):
        raise LlmExecutionError("图片文字识别结果缺少 items 数组", usage)

    return _build_vision_blocks(items, region, usage)


def _build_vision_blocks(
    items: list[object],
    region: PdfImageRegion,
    usage: TokenUsage,
) -> tuple[list[PdfTextBlock], dict[str, str], TokenUsage]:
    """跳过单个无效视觉条目并把其余 OCR 结果转换为页面文本块。"""

    if len(items) > MAXIMUM_SAFE_VISION_ITEMS_PER_REGION:
        LOGGER.warning(
            "图片区域 %s 返回 %s 个视觉文字项，超过安全阈值 %s，保留原图",
            region.id,
            len(items),
            MAXIMUM_SAFE_VISION_ITEMS_PER_REGION,
        )
        return [], {}, usage

    blocks: list[PdfTextBlock] = []
    translations: dict[str, str] = {}
    invalid_item_count = 0
    for item_index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            invalid_item_count += 1
            continue
        source_text = str(item.get("source_text", "")).strip()
        translated_text = str(item.get("translated_text", "")).strip()
        normalized_bbox = _normalize_vision_bbox(item.get("bbox"))
        if not source_text or not translated_text or normalized_bbox is None:
            invalid_item_count += 1
            continue
        page_bbox = _vision_bbox_to_page(region.bbox, normalized_bbox)
        block_id = f"{region.id}-v{item_index:04d}"
        block_height = max(1.0, page_bbox[3] - page_bbox[1])
        block = PdfTextBlock(
            id=block_id,
            page_index=region.page_index,
            source_text=source_text,
            bbox=page_bbox,
            render_bbox=page_bbox,
            font_size=max(
                5.0,
                min(
                    18.0,
                    block_height / max(source_text.count("\n") + 1, 1) * 0.78,
                ),
            ),
            alignment="center",
            background_rgb=_sample_vision_background(
                region.image_png,
                normalized_bbox,
            ),
            text_rgb=(0.0, 0.0, 0.0),
            source_type="vision",
            # 视觉框允许外扩以清除 OCR 字形边缘，但遮罩不得越过原始
            # 图片区域，否则会误擦紧邻图片的表格边线或其他矢量图形。
            mask_bbox=region.bbox,
        )
        blocks.append(block)
        translations[block_id] = translated_text
    if items and not blocks and invalid_item_count:
        raise LlmExecutionError(
            "图片文字识别返回的条目全部为空或坐标无效",
            usage,
        )
    return blocks, translations, usage


def _select_visual_translation_regions(
    layout: PdfLayoutDocument,
) -> tuple[tuple[PdfImageRegion, ...], tuple[PdfImageRegion, ...]]:
    """筛选可安全进行视觉文字覆盖的图片区域。

    Args:
        layout: 已融合原生坐标和 MinerU 结构的页面模型。

    Returns:
        可视觉翻译区域与因整表粗粒度坐标而保留的区域。
    """

    safe_regions: list[PdfImageRegion] = []
    preserved_regions: list[PdfImageRegion] = []
    for region in layout.image_regions:
        has_recovered_image_table = any(
            block.page_index == region.page_index
            and block.source_type == "image-table"
            and _bbox_overlap_ratio(block.bbox, region.bbox) >= 0.8
            for block in layout.blocks
        )
        has_coarse_table_block = any(
            block.page_index == region.page_index
            and block.source_type == "mineru"
            and block.region_type == "table"
            and block.preserve_reason is not None
            and _bbox_overlap_ratio(block.bbox, region.bbox) >= 0.8
            for block in layout.blocks
        )
        if has_recovered_image_table:
            continue
        if has_coarse_table_block:
            preserved_regions.append(region)
        else:
            safe_regions.append(region)
    return tuple(safe_regions), tuple(preserved_regions)


def _build_visual_residual_repairs(
    residuals: list[VisualResidual],
    retry_index: int,
) -> tuple[tuple[PdfTextBlock, ...], dict[str, str]]:
    """把最终页面视觉质检返回的残留坐标转换为补译文本块。"""

    blocks: list[PdfTextBlock] = []
    translations: dict[str, str] = {}
    for item_index, residual in enumerate(residuals, start=1):
        page_bbox = _vision_bbox_to_page(
            residual.region.bbox,
            residual.bbox,
        )
        block_height = max(1.0, page_bbox[3] - page_bbox[1])
        block_id = (
            f"{residual.region.id}-q{retry_index:02d}-v{item_index:04d}"
        )
        block = PdfTextBlock(
            id=block_id,
            page_index=residual.region.page_index,
            source_text=residual.source_text,
            bbox=page_bbox,
            render_bbox=page_bbox,
            font_size=max(
                5.0,
                min(
                    18.0,
                    block_height
                    / max(residual.source_text.count("\n") + 1, 1)
                    * 0.78,
                ),
            ),
            alignment="center",
            background_rgb=_sample_vision_background(
                residual.region.image_png,
                residual.bbox,
            ),
            text_rgb=(0.0, 0.0, 0.0),
            source_type="vision",
            mask_bbox=residual.region.bbox,
        )
        blocks.append(block)
        translations[block_id] = residual.translated_text
    return tuple(blocks), translations


def _visual_residual_overlapping_block_ids(
    residuals: list[VisualResidual],
    blocks: tuple[PdfTextBlock, ...],
) -> set[str]:
    """定位视觉残留所在的既有文本块，避免重复叠加新译文。"""

    overlapping_ids: set[str] = set()
    for residual in residuals:
        page_bbox = _vision_bbox_to_page(
            residual.region.bbox,
            residual.bbox,
        )
        candidates = [
            block
            for block in blocks
            if block.page_index == residual.region.page_index
            and _bbox_overlap_ratio(page_bbox, block.bbox) >= 0.15
        ]
        if candidates:
            overlapping_ids.add(
                max(
                    candidates,
                    key=lambda block: _bbox_overlap_ratio(
                        page_bbox,
                        block.bbox,
                    ),
                ).id
            )
    return overlapping_ids


def _visual_residual_overlaps_block_ids(
    residual: VisualResidual,
    blocks: tuple[PdfTextBlock, ...],
    block_ids: set[str],
) -> bool:
    """判断视觉残留是否已由既有文本块的定向重译接管。"""

    if not block_ids:
        return False
    page_bbox = _vision_bbox_to_page(
        residual.region.bbox,
        residual.bbox,
    )
    return any(
        block.id in block_ids
        and block.page_index == residual.region.page_index
        and _bbox_overlap_ratio(page_bbox, block.bbox) >= 0.15
        for block in blocks
    )


def review_visual_residuals_with_llm(
    result_path: Path,
    job: LocalizationJob,
    image_regions: tuple[PdfImageRegion, ...],
    expected_source_texts: tuple[str, ...] = (),
) -> tuple[list[VisualResidual], TokenUsage]:
    """渲染结果页面后复检图片区域，返回可坐标修复的真实残留。"""

    base_url = _get_llm_base_url()
    api_key = os.getenv("LLM_API_KEY", "")
    if not base_url or not api_key:
        raise RuntimeError("LLM_BASE_URL 或 LLM_API_KEY 未配置")
    rendered_regions = render_pdf_image_regions(
        result_path,
        image_regions,
    )
    if not rendered_regions:
        return [], TokenUsage()

    timeout_seconds = float(os.getenv("LLM_TIMEOUT_SECONDS", "300"))
    worker_count = max(
        1,
        min(
            len(rendered_regions),
            int(os.getenv("LLM_VISION_WORKERS", "2")),
        ),
    )
    residuals: list[VisualResidual] = []
    accumulated_usage = TokenUsage()
    try:
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="docweave-visual-review",
        ) as executor:
            futures = [
                executor.submit(
                    _review_image_region,
                    base_url=base_url,
                    api_key=api_key,
                    model=job.model,
                    target_language=job.translation_policy.target_language.value,
                    protected_terms=job.translation_policy.protected_terms,
                    expected_source_texts=expected_source_texts,
                    region=region,
                    timeout_seconds=timeout_seconds,
                )
                for region in rendered_regions
            ]
            for future in as_completed(futures):
                region_residuals, region_usage = future.result()
                residuals.extend(region_residuals)
                accumulated_usage += region_usage
    except Exception as error:
        raise LlmExecutionError(str(error), accumulated_usage) from error
    return residuals, accumulated_usage


def _review_image_region(
    *,
    base_url: str,
    api_key: str,
    model: str,
    target_language: str,
    protected_terms: list[str],
    expected_source_texts: tuple[str, ...],
    region: PdfImageRegion,
    timeout_seconds: float,
) -> tuple[list[VisualResidual], TokenUsage]:
    """复检最终页面裁剪区域并返回残留的译文和坐标。"""

    image_url = (
        "data:image/png;base64,"
        + base64.b64encode(region.image_png).decode("ascii")
    )
    protected_text = "、".join(protected_terms) or "无"
    with httpx.Client(timeout=timeout_seconds, trust_env=False) as client:
        response = _post_llm_request(
            client,
            url=f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            payload={
                "model": model,
                "temperature": 0,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "你是 PDF 译后视觉质检员。检查图片中是否仍存在应该翻译但尚未"
                            f"翻译为目标语言 {target_language} 的自然语言文字。"
                            "公司名、品牌名、产品型号、数字、百分比、单位、公式和用户保留词"
                            f"不算问题。用户保留词：{protected_text}。"
                            "只能根据当前图片中清晰、完整、实际可见的文字判断，不得根据原文"
                            "候选、语义上下文或模糊笔画猜测残留。文字已是目标语言、仅有模糊"
                            "边缘或无法可靠辨认时不要报告。目标为简体中文时，“自制、外售、"
                            "单面、双面、合计、小计”均是合格中文，不得误认成日文近形字。"
                            "只报告高置信度、可用紧贴坐标定位的完整外语单词或短语。"
                            "只返回严格 JSON："
                            '{"residuals":[{"text":"仍可见的漏译原文",'
                            '"translated_text":"目标语言译文",'
                            '"bbox":[x0,y0,x1,y1]}]}。'
                            "bbox 必须紧贴残留原文，使用图片左上角为原点的 0 到 1000"
                            "归一化坐标。"
                            "没有漏译时返回 {\"residuals\":[]}。不要解释。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": f"复检图片区域 {region.id}。",
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": image_url},
                            },
                        ],
                    },
                ],
            },
            service_name="大模型视觉质检",
        )
    payload = response.json()
    usage = _extract_llm_usage(payload)
    parsed = _extract_json_payload(_extract_llm_content(payload))
    items = parsed.get("residuals")
    if not isinstance(items, list):
        raise LlmExecutionError("视觉质检结果缺少 residuals 数组", usage)
    residuals: list[VisualResidual] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        source_text = str(item.get("text", "")).strip()
        translated_text = str(item.get("translated_text", "")).strip()
        normalized_bbox = _normalize_vision_bbox(item.get("bbox"))
        if not source_text:
            continue
        # 视觉模型偶尔会额外返回“模糊残留”等无坐标判断。该条目无法
        # 安全覆盖，应忽略而不是让已经生成的整份 PDF 失败。
        if not translated_text or normalized_bbox is None:
            LOGGER.warning(
                "忽略缺少译文或有效坐标的视觉质检条目：%s",
                source_text,
            )
            continue
        residuals.append(
            VisualResidual(
                region=region,
                source_text=source_text,
                translated_text=translated_text,
                bbox=normalized_bbox,
            )
        )
    return residuals, usage


def translate_file_name_with_llm(job: LocalizationJob) -> LlmTextResult:
    """使用任务所选模型翻译产物文件名，同时保留章节号和专名。

    Args:
        job: 包含源文件名、目标语言、模型和保留词的真实任务。

    Returns:
        不含扩展名和语言后缀的目标语言文件名及 Token 用量。
    """

    original_stem = Path(job.file_name).stem
    target_language = job.translation_policy.target_language.value
    deterministic_translation = (
        DETERMINISTIC_FILENAME_TRANSLATIONS_ZH.get(original_stem.strip())
        if target_language == "zh-CN"
        else None
    )
    if deterministic_translation:
        return LlmTextResult(text=deterministic_translation)
    if _filename_already_matches_target_language(original_stem, target_language):
        return LlmTextResult(text=original_stem)
    base_url = _get_llm_base_url()
    api_key = os.getenv("LLM_API_KEY", "")
    if not base_url or not api_key:
        raise RuntimeError("LLM_BASE_URL 或 LLM_API_KEY 未配置")
    protected_terms = "、".join(job.translation_policy.protected_terms) or "无"
    timeout_seconds = float(os.getenv("LLM_TIMEOUT_SECONDS", "300"))
    usage = TokenUsage()
    try:
        with httpx.Client(timeout=timeout_seconds, trust_env=False) as client:
            response = _post_llm_request(
                client,
                url=f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                payload={
                    "model": job.model,
                    "temperature": 0,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "你负责翻译文档文件名。只返回翻译后的文件名主体，不要扩展名、"
                                "语言后缀、引号、解释或代码围栏。必须完整保留原文件名中的章节号、"
                                "卷号、序号、年份、型号、标准号、缩写和专有名词；仅翻译自然语言。"
                                "目标语言为 zh-CN 时必须输出中文；原文件名主体已经是中文时不得"
                                "改写成英文或其他语言。"
                                "结合技术材料语境选择术语：日文“フィルム”或英文“film”指材料"
                                "片材时译为“薄膜”，只有电影或摄影胶片语境才译为“影片/胶片”。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"目标语言代码：{target_language}\n"
                                f"必须原样保留的任务词：{protected_terms}\n"
                                f"原文件名主体：{original_stem}"
                            ),
                        },
                    ],
                },
                service_name="大模型文件名翻译",
            )
        payload = response.json()
        usage = _extract_llm_usage(payload)
        translated_stem = _extract_llm_content(payload)
    except Exception as error:
        raise LlmExecutionError(str(error), usage) from error
    preserved_stem = _ensure_preserved_filename_tokens(
        original_stem,
        translated_stem,
        job.translation_policy.protected_terms,
    )
    return LlmTextResult(
        text=_ensure_filename_target_language(
            original_stem,
            preserved_stem,
            target_language,
        ),
        usage=usage,
    )


def _filename_already_matches_target_language(
    file_name_stem: str,
    target_language: str,
) -> bool:
    """判断文件名自然语言是否已经是目标语言，避免模型反向翻译。"""

    if target_language == "zh-CN":
        return (
            bool(re.search(r"[\u3400-\u9fff]", file_name_stem))
            and not bool(re.search(r"[\u3040-\u30ff\uac00-\ud7af]", file_name_stem))
            and not bool(re.search(r"[A-Za-z]{2,}", file_name_stem))
            and any(
                character in SIMPLIFIED_CHINESE_FILENAME_MARKERS
                for character in file_name_stem
            )
        )
    if target_language == "ja":
        return bool(re.search(r"[\u3040-\u30ff]", file_name_stem))
    if target_language == "ko":
        return bool(re.search(r"[\uac00-\ud7af]", file_name_stem))
    if target_language == "en":
        return (
            bool(re.search(r"[A-Za-z]{2,}", file_name_stem))
            and not bool(
                re.search(
                    r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]",
                    file_name_stem,
                )
            )
        )
    return False


def _ensure_filename_target_language(
    original_stem: str,
    translated_stem: str,
    target_language: str,
) -> str:
    """拒绝明显偏离目标语言的文件名结果，并安全回退原主体。"""

    if target_language != "zh-CN":
        return translated_stem
    original_chinese_count = len(re.findall(r"[\u3400-\u9fff]", original_stem))
    translated_chinese_count = len(re.findall(r"[\u3400-\u9fff]", translated_stem))
    original_latin_words = re.findall(r"[A-Za-z]{2,}", original_stem)
    translated_latin_words = re.findall(r"[A-Za-z]{2,}", translated_stem)
    if original_chinese_count and not translated_chinese_count:
        return original_stem
    if (
        original_chinese_count >= 2
        and not original_latin_words
        and translated_latin_words
    ):
        return original_stem
    return translated_stem


def build_result_file_name(job: LocalizationJob, translated_stem: str) -> str:
    """根据已翻译主体和目标语言构造安全下载文件名。"""

    suffixes = {
        "zh-CN": "cn",
        "en": "en",
        "ja": "ja",
        "ko": "ko",
        "de": "de",
        "fr": "fr",
    }
    suffix = suffixes.get(job.translation_policy.target_language.value, "localized")
    normalized_stem = _sanitize_file_name_stem(translated_stem)
    normalized_stem = re.sub(
        rf"(?i)(?:[\s_-]*(?:{re.escape(suffix)}|中文版|中文))$",
        "",
        normalized_stem,
    ).strip(" ._-")
    if not normalized_stem:
        raise RuntimeError("文件名翻译结果为空")
    maximum_stem_bytes = 255 - len(f"_{suffix}.pdf".encode("utf-8"))
    normalized_stem = _truncate_utf8(normalized_stem, maximum_stem_bytes)
    return f"{normalized_stem}_{suffix}.pdf"


def _extract_mineru_result(response: httpx.Response) -> DocumentParseResult:
    """从 MinerU ZIP 或 JSON 响应提取 Markdown 和标准内容列表。"""

    buffer = io.BytesIO(response.content)
    if zipfile.is_zipfile(buffer):
        with zipfile.ZipFile(buffer) as archive:
            markdown_files = sorted(
                (name for name in archive.namelist() if name.lower().endswith(".md")),
                key=len,
            )
            content_list_files = sorted(
                (
                    name
                    for name in archive.namelist()
                    if name.lower().endswith("_content_list.json")
                    and not name.lower().endswith("_content_list_v2.json")
                ),
                key=len,
            )
            markdown = (
                archive.read(markdown_files[0]).decode("utf-8")
                if markdown_files
                else ""
            )
            content_list: tuple[dict[str, object], ...] = ()
            if content_list_files:
                try:
                    raw_content_list = json.loads(
                        archive.read(content_list_files[0]).decode("utf-8")
                    )
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise RuntimeError("MinerU 内容列表不是有效 JSON") from error
                content_list = _normalize_content_list(raw_content_list)
            if not markdown.strip() and not content_list:
                raise RuntimeError("MinerU ZIP 结果中不存在可用文本或坐标内容列表")
            return DocumentParseResult(
                markdown=markdown,
                content_list=content_list,
            )

    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError("MinerU 返回了无法识别的结果格式") from error
    markdown = _find_markdown(payload) or ""
    content_list = _find_content_list(payload)
    if not markdown.strip() and not content_list:
        raise RuntimeError("MinerU 响应中不存在可用文本或坐标内容列表")
    return DocumentParseResult(markdown=markdown, content_list=content_list)


def _find_markdown(value: object) -> str | None:
    if isinstance(value, dict):
        preferred_keys = ("md_content", "markdown", "full_md")
        for key in preferred_keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate
        for candidate in value.values():
            found = _find_markdown(candidate)
            if found:
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = _find_markdown(candidate)
            if found:
                return found
    return None


def _find_content_list(value: object) -> tuple[dict[str, object], ...]:
    """递归寻找同时带 bbox 和 page_idx 的 MinerU 标准内容列表。"""

    if isinstance(value, dict):
        for key in ("content_list", "content_list_data"):
            if key in value:
                normalized = _normalize_content_list(value[key])
                if normalized:
                    return normalized
        for candidate in value.values():
            found = _find_content_list(candidate)
            if found:
                return found
    elif isinstance(value, list):
        normalized = _normalize_content_list(value)
        if normalized:
            return normalized
        for candidate in value:
            found = _find_content_list(candidate)
            if found:
                return found
    return ()


def _normalize_content_list(value: object) -> tuple[dict[str, object], ...]:
    """校验并标准化 MinerU 内容列表。"""

    if not isinstance(value, list):
        return ()
    return tuple(
        item
        for item in value
        if isinstance(item, dict)
        and isinstance(item.get("bbox"), list)
        and isinstance(item.get("page_idx"), int)
    )


def _extract_llm_content(payload: object) -> str:
    if not isinstance(payload, dict):
        raise RuntimeError("大模型响应格式错误")
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError("大模型响应缺少翻译内容") from error
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        text_parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") in {"text", "output_text"}
        ]
        combined = "".join(text_parts).strip()
        if combined:
            return combined
    raise RuntimeError("大模型返回了空翻译内容")


def _extract_json_payload(content: str) -> dict[str, object]:
    """从模型文本中提取单个 JSON 对象并拒绝其他格式。"""

    normalized = content.strip()
    if normalized.startswith("```"):
        normalized = re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            normalized,
            flags=re.IGNORECASE,
        ).strip()
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError:
        start = normalized.find("{")
        end = normalized.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError("大模型返回内容不是有效 JSON")
        try:
            payload = json.loads(normalized[start : end + 1])
        except json.JSONDecodeError as error:
            raise RuntimeError("大模型返回内容不是有效 JSON") from error
    if not isinstance(payload, dict):
        raise RuntimeError("大模型返回 JSON 顶层必须是对象")
    return payload


def _normalize_vision_bbox(
    value: object,
) -> tuple[float, float, float, float] | None:
    """校验视觉模型返回的千分制矩形坐标。"""

    if not isinstance(value, list) or len(value) != 4:
        return None
    try:
        x0, top, x1, bottom = (float(component) for component in value)
    except (TypeError, ValueError):
        return None
    x0 = min(max(x0, 0.0), 1000.0)
    top = min(max(top, 0.0), 1000.0)
    x1 = min(max(x1, 0.0), 1000.0)
    bottom = min(max(bottom, 0.0), 1000.0)
    if x1 - x0 < 2 or bottom - top < 2:
        return None
    return (x0, top, x1, bottom)


def _vision_bbox_to_page(
    region_bbox: tuple[float, float, float, float],
    normalized_bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """将图片千分制坐标映射回原 PDF 页面坐标。"""

    region_x0, region_top, region_x1, region_bottom = region_bbox
    region_width = region_x1 - region_x0
    region_height = region_bottom - region_top
    x0, top, x1, bottom = normalized_bbox
    return (
        region_x0 + x0 / 1000 * region_width,
        region_top + top / 1000 * region_height,
        region_x0 + x1 / 1000 * region_width,
        region_top + bottom / 1000 * region_height,
    )


def _sample_vision_background(
    image_png: bytes,
    normalized_bbox: tuple[float, float, float, float],
) -> tuple[float, float, float]:
    """从文字框内侧边缘选取主背景色，避免表格线把白底采成灰色。"""

    try:
        image = Image.open(io.BytesIO(image_png)).convert("RGB")
    except Exception:
        return (1.0, 1.0, 1.0)
    x0, top, x1, bottom = normalized_bbox
    pixel_bbox = (
        round(x0 / 1000 * image.width),
        round(top / 1000 * image.height),
        round(x1 / 1000 * image.width),
        round(bottom / 1000 * image.height),
    )
    left = min(max(pixel_bbox[0] + 3, 0), image.width - 1)
    right = min(max(pixel_bbox[2] - 3, left), image.width - 1)
    top_y = min(max(pixel_bbox[1] + 3, 0), image.height - 1)
    bottom_y = min(max(pixel_bbox[3] - 3, top_y), image.height - 1)
    horizontal_step = max(1, (right - left) // 48)
    vertical_step = max(1, (bottom_y - top_y) // 24)
    sample_points = [
        *((x, top_y) for x in range(left, right + 1, horizontal_step)),
        *((x, bottom_y) for x in range(left, right + 1, horizontal_step)),
        *((left, y) for y in range(top_y, bottom_y + 1, vertical_step)),
        *((right, y) for y in range(top_y, bottom_y + 1, vertical_step)),
    ]
    pixels = [image.getpixel(point) for point in sample_points]
    light_pixels = [
        pixel
        for pixel in pixels
        if sum(pixel) >= 192
    ]
    if light_pixels:
        pixels = light_pixels
    color_buckets = Counter(
        tuple(channel // 16 for channel in pixel)
        for pixel in pixels
    )
    dominant_bucket, _ = color_buckets.most_common(1)[0]
    dominant_pixels = [
        pixel
        for pixel in pixels
        if tuple(channel // 16 for channel in pixel) == dominant_bucket
    ]
    return tuple(
        statistics.median(pixel[channel] for pixel in dominant_pixels) / 255
        for channel in range(3)
    )


def _extract_llm_usage(payload: object) -> TokenUsage:
    """从 OpenAI 兼容响应提取 Token 用量，不存在时明确标记不可用。"""

    if not isinstance(payload, dict) or not isinstance(payload.get("usage"), dict):
        return TokenUsage()
    usage = payload["usage"]
    input_tokens = _non_negative_integer(
        usage.get("prompt_tokens", usage.get("input_tokens"))
    )
    output_tokens = _non_negative_integer(
        usage.get("completion_tokens", usage.get("output_tokens"))
    )
    reported_total = _non_negative_integer(usage.get("total_tokens"))
    available = any(
        value is not None for value in (input_tokens, output_tokens, reported_total)
    )
    normalized_input = input_tokens or 0
    normalized_output = output_tokens or 0
    total_tokens = (
        reported_total
        if reported_total is not None
        else normalized_input + normalized_output
    )
    return TokenUsage(
        input_tokens=normalized_input,
        output_tokens=normalized_output,
        total_tokens=total_tokens,
        available=available,
    )


def _normalize_llm_result(result: LlmTextResult | str) -> LlmTextResult:
    """兼容测试注入的纯文本函数并统一为带用量的结果。"""

    return result if isinstance(result, LlmTextResult) else LlmTextResult(text=result)


def _normalize_parse_result(
    result: DocumentParseResult | str,
) -> DocumentParseResult:
    """兼容测试注入的纯 Markdown 解析函数。"""

    if isinstance(result, DocumentParseResult):
        return result
    return DocumentParseResult(markdown=result)


def _normalize_layout_translation_result(
    result: LayoutTranslationResult | dict[str, str],
) -> LayoutTranslationResult:
    """兼容测试注入的文本块映射函数。"""

    if isinstance(result, LayoutTranslationResult):
        return result
    return LayoutTranslationResult(translations=result)


def _validate_translation_quality(
    layout: PdfLayoutDocument,
    translations: dict[str, str],
    job: LocalizationJob,
) -> list[str]:
    """检查源语言残留和数字改写，返回可定位到文本块的质量问题。"""

    issues: list[str] = []
    target_language = job.translation_policy.target_language.value
    protected_terms = [
        term
        for term in job.translation_policy.protected_terms
        if term.strip()
    ]
    generic_english_words = set(GENERIC_ENGLISH_TRANSLATIONS_ZH)
    for block in layout.blocks:
        if not block.is_translatable:
            continue
        source_text = block.source_text.strip()
        translated_text = translations.get(block.id, "").strip()
        if not source_text or not translated_text:
            continue
        check_text = translated_text
        for protected_term in protected_terms:
            check_text = check_text.replace(protected_term, "")
        if target_language == "zh-CN":
            if re.search(
                r"[\u3041-\u3096\u30a1-\u30fa\u30fd-\u30ff\uac00-\ud7af]",
                check_text,
            ):
                issues.append(f"{block.id} 仍含日文或韩文：{translated_text[:40]}")
            source_english_text = source_text
            target_english_text = check_text
            if job.translation_policy.preserve_models_and_standards:
                source_english_text = _remove_model_and_standard_tokens(
                    source_english_text
                )
                target_english_text = _remove_model_and_standard_tokens(
                    target_english_text
                )
            source_words = {
                value.casefold()
                for value in re.findall(r"[A-Za-z]+", source_english_text)
            }
            target_words = {
                value.casefold()
                for value in re.findall(r"[A-Za-z]+", target_english_text)
            }
            residual_generic_words = (
                source_words & target_words & generic_english_words
            )
            if residual_generic_words:
                issues.append(
                    f"{block.id} 仍含普通英文："
                    + ",".join(sorted(residual_generic_words))
                )
        if job.translation_policy.preserve_models_and_standards:
            source_numbers = _normalized_number_tokens(source_text)
            target_numbers = _normalized_number_tokens(translated_text)
            if source_numbers != target_numbers:
                issues.append(f"{block.id} 数字或百分比与原文不一致")
    return issues


def _filter_preserved_visual_residuals(
    residuals: list[str],
    source_texts: tuple[str, ...],
    job: LocalizationJob,
) -> list[str]:
    """过滤模型误报的保留词、公司名和专名片段，保留真实漏译项。

    Args:
        residuals: 视觉模型报告的残留文字。
        source_texts: 图片识别阶段提取的原文候选。
        job: 当前任务及其翻译保留策略。

    Returns:
        去重后仍需自动修复的真实残留文字。
    """

    protected_terms = tuple(
        term.casefold()
        for term in job.translation_policy.protected_terms
        if term.strip()
    )
    filtered: list[str] = []
    seen: set[str] = set()
    for residual in residuals:
        normalized_residual = residual.strip()
        folded_residual = normalized_residual.casefold()
        if not normalized_residual or folded_residual in seen:
            continue
        seen.add(folded_residual)
        if re.search(
            r"(?:模糊|疑似).*(?:残留|文字)|(?:残留).*(?:模糊|疑似)",
            normalized_residual,
        ):
            continue
        if any(
            folded_residual == protected_term
            or folded_residual in protected_term
            for protected_term in protected_terms
        ):
            continue
        if (
            job.translation_policy.preserve_proper_nouns
            and _is_company_name_fragment(
                normalized_residual,
                source_texts,
            )
        ):
            continue
        if (
            job.translation_policy.preserve_models_and_standards
            and _is_model_or_standard_residual(normalized_residual)
        ):
            continue
        filtered.append(normalized_residual)
    return filtered


def _is_company_name_fragment(
    residual: str,
    source_texts: tuple[str, ...],
) -> bool:
    """判断拉丁字母残留是否属于带公司后缀的完整专名片段。"""

    if "®" in residual or "™" in residual:
        return True
    if not re.fullmatch(r"[A-Za-z][A-Za-z .,&'®™/-]*", residual):
        return False
    company_suffix = re.compile(
        r"\b(?:co|company|corp|corporation|inc|incorporated|ltd|limited|"
        r"llc|plc|gmbh)\b",
        re.IGNORECASE,
    )
    if company_suffix.search(residual):
        return False
    folded_residual = residual.casefold()
    if any(
        folded_residual in source_text.casefold()
        and company_suffix.search(source_text)
        for source_text in source_texts
    ):
        return True
    # 表格经常把公司简称拆成独立单元格，不再携带 Co./Ltd. 后缀。
    # 对源文中真实存在、首字母大写且不属于受控普通词典的单个拉丁词，
    # 按专名保留，避免视觉模型反复把企业简称当作漏译词覆盖。
    return (
        re.fullmatch(r"[A-Z][a-zA-Z]{2,}", residual) is not None
        and folded_residual not in GENERIC_ENGLISH_TRANSLATIONS_ZH
    )


def _is_model_or_standard_residual(residual: str) -> bool:
    """识别应保留的全大写缩写、材料指标、型号和标准号。"""

    normalized = residual.strip()
    if len(normalized) < 2:
        return False
    if re.fullmatch(r"[A-Z](?:[&/][A-Z])+", normalized):
        return True
    if re.fullmatch(
        r"(?:Low|High)\s+(?:CTE|Df|Dk|HAST|MIT|RTI|Td|Tg)"
        r"(?:\s+(?:PI|FCCL|FPC|LCP))?",
        normalized,
    ):
        return True
    if re.fullmatch(
        r"[A-Z][A-Z0-9]*(?:[-./][A-Z0-9]+)*",
        normalized,
    ):
        return True
    if (
        "-" in normalized
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ./-]*", normalized)
        and sum(character.isupper() for character in normalized) >= 2
    ):
        return True
    if (
        re.search(r"\b(?:ASTM|IEC|IPC|ISO|JIS|UL)\b", normalized)
        and not re.search(
            r"[\u3041-\u30ff\u3400-\u9fff\uac00-\ud7af]",
            normalized,
        )
    ):
        return True
    material_metrics = {
        "CTE",
        "Df",
        "Dk",
        "HAST",
        "MIT",
        "RTI",
        "Td",
        "Tg",
    }
    metric_match = re.match(
        r"^\[?\s*([A-Za-z]+)\b(.*)$",
        normalized,
    )
    if (
        metric_match
        and metric_match.group(1) in material_metrics
    ):
        metric_words = set(re.findall(r"[A-Za-z]+", normalized))
        allowed_metric_words = material_metrics | {
            "C",
            "F",
            "RH",
            "T",
            "g",
            "ppm",
        }
        if metric_words <= allowed_metric_words:
            return True
    chemical_symbols = {
        "Ag",
        "Al",
        "Au",
        "Cu",
        "Fe",
        "Ni",
        "Pb",
        "Si",
        "Sn",
        "Zn",
    }
    if normalized in chemical_symbols:
        return True
    if not re.fullmatch(r"[A-Za-z0-9µμΩ°%().,+\-/\s]+", normalized):
        return False
    unit_words = {
        "a",
        "b",
        "c",
        "f",
        "g",
        "ghz",
        "hz",
        "in",
        "k",
        "khz",
        "lb",
        "mhz",
        "mil",
        "mm",
        "n",
        "nm",
        "um",
    }
    latin_words = re.findall(r"[A-Za-z]+", normalized)
    has_measurement_marker = bool(
        re.search(r"\d|%|°|/|µ|μ|Ω", normalized)
    )
    return (
        has_measurement_marker
        and bool(latin_words)
        and all(word.casefold() in unit_words for word in latin_words)
    )


def _remove_model_and_standard_tokens(text: str) -> str:
    """移除普通英文残留检查中应原样保留的缩写和连字符型号。"""

    return re.sub(
        r"\b(?:[A-Za-z][A-Za-z0-9]*(?:[-_/][A-Za-z0-9]+)+|"
        r"[A-Z]{2,}[A-Z0-9]*)\b",
        " ",
        text,
    )


def _repair_generic_english_residuals(
    layout: PdfLayoutDocument,
    translations: dict[str, str],
    issues: list[str],
    job: LocalizationJob,
) -> dict[str, str]:
    """对模型多次遗漏的独立普通英文短标签执行确定性中文替换。"""

    if job.translation_policy.target_language.value != "zh-CN":
        return {}
    issue_ids = {
        issue.split(" ", 1)[0]
        for issue in issues
        if "仍含普通英文" in issue
    }
    repairs: dict[str, str] = {}
    for block in layout.blocks:
        if block.id not in issue_ids:
            continue
        source_word = block.source_text.strip().casefold()
        translated_word = translations.get(block.id, "").strip().casefold()
        replacement = (
            GENERIC_ENGLISH_PHRASES_ZH.get(source_word)
            or GENERIC_ENGLISH_TRANSLATIONS_ZH.get(source_word)
        )
        if replacement and translated_word == source_word:
            repairs[block.id] = replacement
    return repairs


def _repair_common_table_terms(
    layout: PdfLayoutDocument,
    translations: dict[str, str],
    job: LocalizationJob,
) -> dict[str, str]:
    """统一高频中英日表格标签，避免短词被模型回写为日文或近义歧义词。

    Args:
        layout: 当前 PDF 的坐标文本块。
        translations: 已完成的块级译文。
        job: 用于确认目标语言的任务配置。

    Returns:
        需要覆盖到最终译文映射的确定性修正。
    """

    if job.translation_policy.target_language.value != "zh-CN":
        return {}
    repairs: dict[str, str] = {}
    for block in layout.blocks:
        if not block.is_translatable or block.id not in translations:
            continue
        source_text = block.source_text.strip()
        replacement = (
            COMMON_TABLE_TRANSLATIONS_ZH.get(source_text)
            or COMMON_TABLE_TRANSLATIONS_ZH.get(source_text.casefold())
        )
        if replacement and translations[block.id].strip() != replacement:
            repairs[block.id] = replacement
    return repairs


def _normalized_number_tokens(text: str) -> Counter[str]:
    """提取并标准化需要严格保持的数字和百分比。"""

    return Counter(
        token.replace(",", "")
        for token in re.findall(r"\d[\d,]*(?:\.\d+)?%?", text)
        if token.strip()
    )


def _layout_source_label(source_type: str) -> str:
    """返回用于任务日志的坐标来源说明。"""

    labels = {
        "native": "原 PDF 精确文字坐标",
        "hybrid": "原生坐标与视觉识别混合",
        "mineru": "MinerU 区块坐标",
    }
    return labels.get(source_type, source_type)


def _get_error_token_usage(error: Exception) -> TokenUsage:
    """返回异常携带的已消耗 Token，用于失败任务资源统计。"""

    return error.usage if isinstance(error, LlmExecutionError) else TokenUsage()


def _non_negative_integer(value: object) -> int | None:
    """将合法非负整数返回，否则返回 None。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _elapsed_milliseconds(started_at: float) -> int:
    """返回从高精度起点到当前时刻的非负毫秒数。"""

    return max(0, round((perf_counter() - started_at) * 1000))


def _format_duration(duration_ms: int) -> str:
    """将毫秒耗时格式化为日志可读文本。"""

    if duration_ms < 1000:
        return f"{duration_ms} 毫秒"
    return f"{duration_ms / 1000:.2f} 秒"


def _format_token_usage(usage: TokenUsage) -> str:
    """将 Token 用量格式化为日志文本。"""

    if not usage.available:
        return "服务未返回 Token 用量"
    return (
        f"输入 {usage.input_tokens}、输出 {usage.output_tokens}、"
        f"合计 {usage.total_tokens} Token"
    )


def _format_bytes(size: int) -> str:
    """将字节数格式化为日志可读文本。"""

    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def _chunk_layout_blocks(
    blocks: tuple[PdfTextBlock, ...],
    character_limit: int = 2_400,
    block_limit: int = 24,
) -> list[list[PdfTextBlock]]:
    """按页、字符数和文本块数分批，禁止跨页混合语境。"""

    batches: list[list[PdfTextBlock]] = []
    current: list[PdfTextBlock] = []
    current_character_count = 0
    current_page_index: int | None = None
    for block in sorted(
        blocks,
        key=lambda item: (
            item.page_index,
            item.reading_order,
            item.bbox[1],
            item.bbox[0],
        ),
    ):
        block_character_count = len(block.source_text)
        exceeds_limit = current and (
            block.page_index != current_page_index
            or current_character_count + block_character_count > character_limit
            or len(current) >= block_limit
        )
        if exceeds_limit:
            batches.append(current)
            current = []
            current_character_count = 0
        current_page_index = block.page_index
        current.append(block)
        current_character_count += block_character_count
    if current:
        batches.append(current)
    return batches


def _extract_block_translations(
    content: str,
    expected_blocks: list[PdfTextBlock],
) -> dict[str, str]:
    """解析文本块 JSON，空译文按原文透传并拒绝真正缺失的正文。"""

    expected_by_id = {block.id: block for block in expected_blocks}
    expected_ids = set(expected_by_id)
    normalized_content = content.strip()
    if normalized_content.startswith("```"):
        normalized_content = re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            normalized_content,
            flags=re.IGNORECASE,
        ).strip()
    try:
        payload = json.loads(normalized_content)
    except json.JSONDecodeError:
        start = normalized_content.find("{")
        end = normalized_content.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError("大模型返回的文本块翻译不是有效 JSON")
        try:
            payload = json.loads(normalized_content[start : end + 1])
        except json.JSONDecodeError as error:
            raise RuntimeError("大模型返回的文本块翻译不是有效 JSON") from error
    if not isinstance(payload, dict) or not isinstance(
        payload.get("translations"), list
    ):
        raise RuntimeError("大模型文本块翻译缺少 translations 数组")

    translations: dict[str, str] = {}
    unknown_items: list[dict[str, object]] = []
    for item in payload["translations"]:
        if not isinstance(item, dict):
            raise RuntimeError("大模型文本块翻译包含无效条目")
        block_id = item.get("id")
        translated_text = item.get("text")
        if not isinstance(block_id, str) or block_id not in expected_ids:
            unknown_items.append(item)
            continue
        if block_id in translations:
            raise RuntimeError(f"大模型重复返回文本块 {block_id}")
        translations[block_id] = (
            translated_text.strip()
            if isinstance(translated_text, str) and translated_text.strip()
            else expected_by_id[block_id].source_text
        )

    missing_ids = expected_ids - translations.keys()
    if missing_ids and len(payload["translations"]) == len(expected_blocks):
        positional_translations = _recover_translations_by_position(
            payload["translations"],
            expected_blocks,
        )
        if positional_translations is not None:
            return positional_translations
    if unknown_items:
        raise RuntimeError(
            f"大模型返回 {len(unknown_items)} 个未知文本块 ID"
        )
    if missing_ids:
        raise RuntimeError(f"大模型缺少 {len(missing_ids)} 个文本块的翻译结果")
    return translations


def _recover_translations_by_position(
    items: list[object],
    expected_blocks: list[PdfTextBlock],
) -> dict[str, str] | None:
    """仅在数量完全一致时按原顺序恢复被模型轻微改写的块 ID。"""

    recovered: dict[str, str] = {}
    for item, block in zip(items, expected_blocks):
        if not isinstance(item, dict):
            return None
        translated_text = item.get("text")
        recovered[block.id] = (
            translated_text.strip()
            if isinstance(translated_text, str) and translated_text.strip()
            else block.source_text
        )
    return recovered


def _safe_error_message(error: Exception) -> str:
    message = str(error).strip() or error.__class__.__name__
    return message[:500]


def _ensure_preserved_filename_tokens(
    original_stem: str,
    translated_stem: str,
    protected_terms: list[str],
) -> str:
    translated_stem = translated_stem.strip()
    if not translated_stem:
        raise RuntimeError("大模型返回了空文件名")
    candidates = re.findall(
        r"\d+(?:[._-]\d+)*|[A-Za-z]+(?:[A-Za-z0-9._-]*[A-Za-z0-9])?",
        original_stem,
    )
    preserved_tokens = [
        token
        for token in candidates
        if token[0].isdigit()
        or any(character.isdigit() for character in token)
        or (len(token) >= 2 and token.isupper())
        or any(separator in token for separator in ("-", "_", "."))
    ]
    preserved_tokens.extend(
        term for term in protected_terms if term.casefold() in original_stem.casefold()
    )
    missing_tokens = [
        token
        for token in preserved_tokens
        if token.casefold() not in translated_stem.casefold()
    ]
    if missing_tokens:
        translated_stem = f"{translated_stem} {' '.join(missing_tokens)}"
    return translated_stem


def _sanitize_file_name_stem(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = normalized.strip().strip("`'\"")
    normalized = re.sub(r"(?i)\.pdf$", "", normalized).strip()
    normalized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip(" .")


def _truncate_utf8(value: str, maximum_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    truncated = encoded[:maximum_bytes]
    while truncated:
        try:
            return truncated.decode("utf-8").rstrip(" ._-")
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    raise RuntimeError("文件名在截断后为空")


def _remote_error(service_name: str, response: httpx.Response) -> str:
    """生成不包含上游响应正文的错误信息，避免持久化提示词或文档内容。"""

    return f"{service_name} 请求失败（HTTP {response.status_code}）"


def _get_llm_base_url() -> str:
    """读取并校验 LLM 服务地址，避免将 API Key 发送到不可信明文连接。"""

    base_url = os.getenv("LLM_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        return ""
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise RuntimeError("LLM_BASE_URL 必须是未携带凭据的 HTTP(S) 地址")
    if parsed.scheme == "https":
        return base_url

    hostname = parsed.hostname.casefold()
    is_local_or_test = hostname in {"localhost", "127.0.0.1", "::1"} or hostname.endswith(
        ".test"
    )
    allow_insecure_http = os.getenv(
        "DOCWEAVE_ALLOW_INSECURE_LLM_HTTP", "false"
    ).strip().casefold() in {"1", "true", "yes"}
    if not is_local_or_test and not allow_insecure_http:
        raise RuntimeError(
            "LLM_BASE_URL 的远程地址必须使用 HTTPS；"
            "仅在可信隔离网络中才可显式启用 DOCWEAVE_ALLOW_INSECURE_LLM_HTTP"
        )
    return base_url


def _post_llm_request(
    client: httpx.Client,
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, object],
    service_name: str,
) -> httpx.Response:
    """发送可重试的大模型请求，吸收瞬时超时、限流和网关错误。

    Args:
        client: 已配置超时和代理策略的 HTTP 客户端。
        url: OpenAI 兼容的 Chat Completions 地址。
        headers: 鉴权请求头。
        payload: 请求 JSON。
        service_name: 写入真实错误信息的调用阶段名称。

    Returns:
        成功的 HTTP 响应。

    Raises:
        RuntimeError: 达到重试上限或遇到不可重试的响应。
    """

    retry_limit = max(0, int(os.getenv("LLM_HTTP_RETRIES", "2")))
    for attempt in range(retry_limit + 1):
        try:
            response = client.post(
                url,
                headers=headers,
                json=payload,
            )
        except (httpx.TimeoutException, httpx.TransportError) as error:
            if attempt >= retry_limit:
                raise RuntimeError(
                    f"{service_name} 网络请求失败：{error}"
                ) from error
        else:
            if not response.is_error:
                return response
            if (
                response.status_code not in RETRYABLE_HTTP_STATUS_CODES
                or attempt >= retry_limit
            ):
                raise RuntimeError(_remote_error(service_name, response))
        sleep(min(4.0, 0.5 * (2**attempt)))
    raise RuntimeError(f"{service_name} 请求重试流程异常结束")
