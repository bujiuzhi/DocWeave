"""DocWeave 后端入口：接收真实 PDF、持久化任务并提供结果下载。"""

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, AsyncIterator
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import ValidationError

from app.models import (
    CreateJobRequest,
    GlossaryCandidate,
    GlossaryCandidateStatus,
    GlossaryLearningMode,
    GlossaryTerm,
    GlossaryTermInput,
    JobDetails,
    JobStatus,
    LanguageCode,
    LocalizationJob,
    TranslationPolicy,
)
from app.services.job_processor import JobProcessor
from app.services.translation_policy import build_translation_instruction
from app.storage import JobRepository
from app.version import APP_VERSION

LOGGER = logging.getLogger(__name__)
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024


def create_app(
    data_directory: Path | None = None,
    processor_factory: type[JobProcessor] = JobProcessor,
) -> FastAPI:
    """创建可注入数据目录的 FastAPI 应用。

    Args:
        data_directory: 任务数据根目录；测试可传入临时目录。
        processor_factory: 后台处理器类型；测试可注入不联网实现。

    Returns:
        配置完成的 FastAPI 应用。
    """

    resolved_data_directory = data_directory or Path(
        os.getenv("DOCWEAVE_DATA_DIR", "/app/data")
    )
    repository = JobRepository(resolved_data_directory)
    processor = processor_factory(repository)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        repository.initialize()
        for recovered_job_id in repository.recover_interrupted_jobs():
            processor.submit(recovered_job_id)
        yield
        processor.shutdown()

    application = FastAPI(title="DocWeave API", version=APP_VERSION, lifespan=lifespan)
    application.state.repository = repository
    application.state.processor = processor

    @application.get("/api/v1/health")
    def health_check() -> dict[str, str]:
        """返回服务健康状态。"""

        return {"status": "ok", "service": "docweave-api"}

    @application.get("/api/v1/jobs", response_model=list[LocalizationJob])
    def list_jobs() -> list[LocalizationJob]:
        """返回 SQLite 中持久化的全部真实任务。"""

        return repository.list_all()

    @application.post(
        "/api/v1/jobs",
        response_model=LocalizationJob,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_job(
        file: Annotated[UploadFile, File(description="待本地化 PDF")],
        model: Annotated[str, Form(min_length=1, max_length=200)],
        strategy: Annotated[str, Form(min_length=1, max_length=64)],
        translation_policy: Annotated[str, Form()],
    ) -> LocalizationJob:
        """保存真实上传文件、创建持久化任务并启动后台处理。"""

        file_name = Path(file.filename or "").name
        if not file_name or not file_name.lower().endswith(".pdf"):
            raise HTTPException(status_code=422, detail="只支持 PDF 文件")
        try:
            policy = TranslationPolicy.model_validate(json.loads(translation_policy))
        except (json.JSONDecodeError, ValidationError) as error:
            raise HTTPException(status_code=422, detail="翻译规则格式错误") from error

        job_id = str(uuid4())
        source_path = repository.upload_directory / f"{job_id}.pdf"
        file_size = await _save_upload(file, source_path)
        request = CreateJobRequest(
            file_name=file_name,
            file_size=file_size,
            model=model.strip(),
            strategy=strategy.strip(),
            translation_policy=policy,
        )
        try:
            job = repository.create(job_id, request, source_path)
        except Exception:
            source_path.unlink(missing_ok=True)
            LOGGER.exception("创建任务失败：%s", job_id)
            raise
        processor.submit(job_id)
        return job

    @application.get("/api/v1/jobs/{job_id}", response_model=LocalizationJob)
    def get_job(job_id: str) -> LocalizationJob:
        """按任务 ID 查询真实任务。"""

        return _get_job_or_404(repository, job_id)

    @application.get("/api/v1/jobs/{job_id}/details", response_model=JobDetails)
    def get_job_details(job_id: str) -> JobDetails:
        """返回任务运行日志、错误详情和资源消耗报告。"""

        _get_job_or_404(repository, job_id)
        return repository.get_details(job_id)

    @application.delete("/api/v1/jobs/{job_id}", response_model=LocalizationJob)
    def cancel_job(job_id: str) -> LocalizationJob:
        """取消未完成任务。"""

        _get_job_or_404(repository, job_id)
        return processor.cancel(job_id)

    @application.post(
        "/api/v1/jobs/{job_id}/reprocess",
        response_model=LocalizationJob,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def reprocess_job(job_id: str) -> LocalizationJob:
        """复用服务器保留的原 PDF，按当前流水线重新处理。"""

        _get_job_or_404(repository, job_id)
        try:
            job = repository.prepare_reprocess(job_id)
        except FileNotFoundError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        processor.submit(job_id)
        return job

    @application.get("/api/v1/jobs/{job_id}/download")
    def download_job(job_id: str) -> FileResponse:
        """下载任务已生成的 PDF 结果。"""

        job = _get_job_or_404(repository, job_id)
        result_path = repository.get_result_path(job_id)
        if (
            not job.result_downloadable
            or result_path is None
            or not result_path.is_file()
        ):
            raise HTTPException(status_code=409, detail="任务尚无可下载结果")
        download_file_name = job.result_file_name or result_path.name
        quality_status = "passed"
        if job.status == JobStatus.NEEDS_REVIEW:
            quality_status = "review-recommended"
        return FileResponse(
            path=result_path,
            media_type="application/pdf",
            filename=download_file_name,
            headers={"X-DocWeave-Quality-Status": quality_status},
        )

    @application.get("/api/v1/jobs/{job_id}/translation-instruction")
    def get_translation_instruction(job_id: str) -> dict[str, str]:
        """返回该任务实际传递给翻译模型的约束指令。"""

        job = _get_job_or_404(repository, job_id)
        return {
            "instruction": build_translation_instruction(
                job.translation_policy,
                repository.get_glossary_snapshot(job_id),
            )
        }

    @application.get("/api/v1/glossary", response_model=list[GlossaryTerm])
    def list_glossary_terms() -> list[GlossaryTerm]:
        """返回服务端正式术语库。"""

        return repository.list_glossary_terms()

    @application.post(
        "/api/v1/glossary",
        response_model=GlossaryTerm,
        status_code=status.HTTP_201_CREATED,
    )
    def upsert_glossary_term(term: GlossaryTermInput) -> GlossaryTerm:
        """新增术语；原文已存在时更新标准译文。"""

        return repository.upsert_glossary_term(term)

    @application.delete(
        "/api/v1/glossary/{term_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def delete_glossary_term(term_id: str) -> None:
        """删除正式术语，不影响既有任务快照。"""

        try:
            repository.delete_glossary_term(term_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="术语不存在") from error

    @application.get(
        "/api/v1/glossary-candidates",
        response_model=list[GlossaryCandidate],
    )
    def list_glossary_candidates(
        candidate_status: GlossaryCandidateStatus | None = None,
    ) -> list[GlossaryCandidate]:
        """返回任务自动学习的术语候选。"""

        return repository.list_glossary_candidates(candidate_status)

    return application


async def _save_upload(file: UploadFile, destination: Path) -> int:
    """流式保存并校验 PDF 上传内容。"""

    destination.parent.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    first_chunk = True
    try:
        with destination.open("wb") as output:
            while chunk := await file.read(UPLOAD_CHUNK_BYTES):
                if first_chunk:
                    first_chunk = False
                    if not chunk.startswith(b"%PDF-"):
                        raise HTTPException(status_code=422, detail="文件内容不是有效 PDF")
                total_bytes += len(chunk)
                if total_bytes > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="PDF 文件不能超过 200 MB")
                output.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    if total_bytes == 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail="PDF 文件为空")
    return total_bytes


def _get_job_or_404(repository: JobRepository, job_id: str) -> LocalizationJob:
    try:
        return repository.get(job_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="任务不存在") from error


app = create_app()

__all__ = [
    "CreateJobRequest",
    "GlossaryLearningMode",
    "JobDetails",
    "JobStatus",
    "LanguageCode",
    "LocalizationJob",
    "TranslationPolicy",
    "app",
    "create_app",
]
