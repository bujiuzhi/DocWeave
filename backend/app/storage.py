"""使用 SQLite 和本地文件目录持久化任务及文档产物。"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Iterable
from uuid import uuid4

from app.models import (
    CreateJobRequest,
    GlossaryCandidate,
    GlossaryCandidateStatus,
    GlossaryLearningMode,
    GlossaryTerm,
    GlossaryTermInput,
    GlossaryTermSource,
    JobDetails,
    JobLogEntry,
    JobLogLevel,
    JobMetrics,
    JobStatus,
    LocalizationJob,
    QualityIssue,
    TranslationPolicy,
)
from app.version import LEGACY_PIPELINE_VERSION, PDF_PIPELINE_VERSION


def _translation_memory_policy_fingerprint(
    policy: TranslationPolicy,
) -> str:
    """返回与当前流水线和完整翻译策略绑定的稳定记忆指纹。"""

    payload = {
        "pipeline_version": PDF_PIPELINE_VERSION,
        "translation_policy": policy.model_dump(mode="json"),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class JobRepository:
    """持久化任务元数据并管理上传、结果文件路径。"""

    def __init__(self, data_directory: Path) -> None:
        """初始化数据路径。

        Args:
            data_directory: SQLite、上传文件和结果文件的共同根目录。
        """

        self.data_directory = data_directory
        self.upload_directory = data_directory / "uploads"
        self.result_directory = data_directory / "results"
        self.legacy_result_directory = self.result_directory / "legacy"
        self.database_path = data_directory / "docweave.sqlite3"
        self._write_lock = Lock()

    def initialize(self) -> None:
        """创建运行目录、任务、批次、术语、日志和质检表。"""

        self.upload_directory.mkdir(parents=True, exist_ok=True)
        self.result_directory.mkdir(parents=True, exist_ok=True)
        self.legacy_result_directory.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS localization_jobs (
                    id TEXT PRIMARY KEY,
                    file_name TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    translation_policy TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    source_path TEXT NOT NULL,
                    result_path TEXT,
                    result_file_name TEXT,
                    pipeline_version TEXT NOT NULL,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(
                connection,
                "localization_jobs",
                "pipeline_version",
                f"TEXT NOT NULL DEFAULT '{LEGACY_PIPELINE_VERSION}'",
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_localization_jobs_created_at "
                "ON localization_jobs(created_at DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS job_metrics (
                    job_id TEXT PRIMARY KEY,
                    started_at TEXT,
                    finished_at TEXT,
                    mineru_duration_ms INTEGER NOT NULL DEFAULT 0,
                    llm_duration_ms INTEGER NOT NULL DEFAULT 0,
                    render_duration_ms INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    token_usage_available INTEGER NOT NULL DEFAULT 0,
                    result_file_size INTEGER,
                    validation_duration_ms INTEGER NOT NULL DEFAULT 0,
                    translation_batch_count INTEGER NOT NULL DEFAULT 0,
                    resumed_translation_batch_count INTEGER NOT NULL DEFAULT 0,
                    translation_memory_hit_count INTEGER NOT NULL DEFAULT 0,
                    quality_issue_count INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (job_id) REFERENCES localization_jobs(id) ON DELETE CASCADE
                )
                """
            )
            self._ensure_column(
                connection,
                "job_metrics",
                "validation_duration_ms",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                "job_metrics",
                "translation_batch_count",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                "job_metrics",
                "resumed_translation_batch_count",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                "job_metrics",
                "translation_memory_hit_count",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                "job_metrics",
                "quality_issue_count",
                "INTEGER NOT NULL DEFAULT 0",
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS job_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    progress INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (job_id) REFERENCES localization_jobs(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_job_logs_job_created_at "
                "ON job_logs(job_id, created_at ASC, id ASC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS glossary_terms (
                    id TEXT PRIMARY KEY,
                    source_text TEXT NOT NULL COLLATE NOCASE,
                    target_text TEXT NOT NULL,
                    category TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_job_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_text)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS job_glossary_snapshot (
                    job_id TEXT NOT NULL,
                    glossary_term_id TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    target_text TEXT NOT NULL,
                    category TEXT NOT NULL,
                    PRIMARY KEY (job_id, source_text),
                    FOREIGN KEY (job_id) REFERENCES localization_jobs(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS glossary_candidates (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    proposed_target_text TEXT NOT NULL,
                    category TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    occurrences INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(job_id, source_text),
                    FOREIGN KEY (job_id) REFERENCES localization_jobs(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS translation_memory (
                    source_hash TEXT PRIMARY KEY,
                    source_text TEXT NOT NULL,
                    target_text TEXT NOT NULL,
                    source_language TEXT NOT NULL,
                    target_language TEXT NOT NULL,
                    policy_fingerprint TEXT NOT NULL,
                    model TEXT NOT NULL,
                    occurrences INTEGER NOT NULL DEFAULT 1,
                    last_job_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(
                connection,
                "translation_memory",
                "policy_fingerprint",
                "TEXT NOT NULL DEFAULT 'legacy'",
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS translation_batches (
                    job_id TEXT NOT NULL,
                    batch_key TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    page_index INTEGER NOT NULL,
                    block_ids TEXT NOT NULL,
                    translations TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    token_usage_available INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (job_id, batch_key),
                    FOREIGN KEY (job_id) REFERENCES localization_jobs(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_translation_batches_job_status "
                "ON translation_batches(job_id, status, page_index)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS job_quality_issues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    code TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    page_index INTEGER,
                    block_id TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (job_id) REFERENCES localization_jobs(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_job_quality_issues_job "
                "ON job_quality_issues(job_id, id)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO job_metrics (job_id) "
                "SELECT id FROM localization_jobs"
            )
            connection.execute(
                """
                UPDATE localization_jobs
                SET pipeline_version = ?
                WHERE pipeline_version = ?
                  AND id IN (
                      SELECT job_id
                      FROM job_metrics
                      WHERE validation_duration_ms > 0
                        AND translation_batch_count > 0
                  )
                """,
                (PDF_PIPELINE_VERSION, LEGACY_PIPELINE_VERSION),
            )

    def create(self, job_id: str, request: CreateJobRequest, source_path: Path) -> LocalizationJob:
        """新增真实上传任务。

        Args:
            job_id: 服务端生成的任务 ID。
            request: 已校验的任务业务参数。
            source_path: 已落盘的源 PDF 路径。

        Returns:
            新创建的任务。
        """

        now = datetime.now(timezone.utc)
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO localization_jobs (
                    id, file_name, file_size, model, strategy, translation_policy,
                    status, progress, source_path, pipeline_version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    request.file_name,
                    request.file_size,
                    request.model,
                    request.strategy,
                    request.translation_policy.model_dump_json(),
                    JobStatus.QUEUED.value,
                    0,
                    str(source_path),
                    PDF_PIPELINE_VERSION,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            connection.execute(
                "INSERT INTO job_metrics (job_id) VALUES (?)",
                (job_id,),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO job_glossary_snapshot (
                    job_id, glossary_term_id, source_text, target_text, category
                )
                SELECT ?, id, source_text, target_text, category
                FROM glossary_terms
                """,
                (job_id,),
            )
            connection.execute(
                """
                INSERT INTO job_logs (
                    job_id, stage, level, message, progress, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    "queued",
                    JobLogLevel.INFO.value,
                    "任务已创建，等待并发调度",
                    0,
                    now.isoformat(),
                ),
            )
        return self.get(job_id)

    def list_all(self) -> list[LocalizationJob]:
        """按创建时间倒序返回全部真实任务。"""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM localization_jobs ORDER BY created_at DESC"
            ).fetchall()
        return [self._to_job(row) for row in rows]

    def get(self, job_id: str) -> LocalizationJob:
        """按 ID 返回任务，不存在时抛出 KeyError。"""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM localization_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._to_job(row)

    def get_details(self, job_id: str) -> JobDetails:
        """返回任务状态、阶段日志和资源指标详情。"""

        return JobDetails(
            job=self.get(job_id),
            logs=self.list_logs(job_id),
            metrics=self.get_metrics(job_id),
            quality_issues=self.list_quality_issues(job_id),
        )

    def list_logs(self, job_id: str) -> list[JobLogEntry]:
        """按时间正序返回任务的持久化运行日志。"""

        self.get(job_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, stage, level, message, progress, created_at
                FROM job_logs
                WHERE job_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (job_id,),
            ).fetchall()
        return [self._to_log_entry(row) for row in rows]

    def append_log(
        self,
        job_id: str,
        stage: str,
        level: JobLogLevel,
        message: str,
        progress: int | None = None,
    ) -> JobLogEntry:
        """追加一条任务阶段日志并返回持久化结果。"""

        created_at = datetime.now(timezone.utc)
        normalized_progress = (
            max(0, min(progress, 100)) if progress is not None else None
        )
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO job_logs (
                    job_id, stage, level, message, progress, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    stage[:64],
                    level.value,
                    message.strip()[:1000],
                    normalized_progress,
                    created_at.isoformat(),
                ),
            )
            log_id = cursor.lastrowid
            row = connection.execute(
                """
                SELECT id, stage, level, message, progress, created_at
                FROM job_logs WHERE id = ?
                """,
                (log_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._to_log_entry(row)

    def get_metrics(self, job_id: str) -> JobMetrics:
        """返回任务累计资源指标，运行中的总耗时按当前时间计算。"""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM job_metrics WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        started_at = (
            datetime.fromisoformat(row["started_at"]) if row["started_at"] else None
        )
        finished_at = (
            datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None
        )
        duration_ms = 0
        if started_at:
            end_time = finished_at or datetime.now(timezone.utc)
            duration_ms = max(0, int((end_time - started_at).total_seconds() * 1000))
        return JobMetrics(
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            mineru_duration_ms=row["mineru_duration_ms"],
            llm_duration_ms=row["llm_duration_ms"],
            render_duration_ms=row["render_duration_ms"],
            validation_duration_ms=row["validation_duration_ms"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            total_tokens=row["total_tokens"],
            token_usage_available=bool(row["token_usage_available"]),
            result_file_size=row["result_file_size"],
            translation_batch_count=row["translation_batch_count"],
            resumed_translation_batch_count=row["resumed_translation_batch_count"],
            translation_memory_hit_count=row["translation_memory_hit_count"],
            quality_issue_count=row["quality_issue_count"],
        )

    def start_metrics(self, job_id: str) -> None:
        """记录任务首次开始执行时间。"""

        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE job_metrics
                SET started_at = COALESCE(started_at, ?), finished_at = NULL
                WHERE job_id = ?
                """,
                (datetime.now(timezone.utc).isoformat(), job_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(job_id)

    def add_metrics(
        self,
        job_id: str,
        *,
        mineru_duration_ms: int = 0,
        llm_duration_ms: int = 0,
        render_duration_ms: int = 0,
        validation_duration_ms: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        token_usage_available: bool = False,
        translation_batch_count: int = 0,
        resumed_translation_batch_count: int = 0,
        translation_memory_hit_count: int = 0,
        quality_issue_count: int = 0,
    ) -> None:
        """累加任务阶段耗时和 Token 用量。"""

        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE job_metrics
                SET mineru_duration_ms = mineru_duration_ms + ?,
                    llm_duration_ms = llm_duration_ms + ?,
                    render_duration_ms = render_duration_ms + ?,
                    validation_duration_ms = validation_duration_ms + ?,
                    input_tokens = input_tokens + ?,
                    output_tokens = output_tokens + ?,
                    total_tokens = total_tokens + ?,
                    token_usage_available = MAX(token_usage_available, ?),
                    translation_batch_count = translation_batch_count + ?,
                    resumed_translation_batch_count =
                        resumed_translation_batch_count + ?,
                    translation_memory_hit_count =
                        translation_memory_hit_count + ?,
                    quality_issue_count = quality_issue_count + ?
                WHERE job_id = ?
                """,
                (
                    max(0, mineru_duration_ms),
                    max(0, llm_duration_ms),
                    max(0, render_duration_ms),
                    max(0, validation_duration_ms),
                    max(0, input_tokens),
                    max(0, output_tokens),
                    max(0, total_tokens),
                    int(token_usage_available),
                    max(0, translation_batch_count),
                    max(0, resumed_translation_batch_count),
                    max(0, translation_memory_hit_count),
                    max(0, quality_issue_count),
                    job_id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(job_id)

    def finish_metrics(self, job_id: str, result_file_size: int | None = None) -> None:
        """记录任务结束时间和可选结果文件大小。"""

        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE job_metrics
                SET finished_at = ?,
                    result_file_size = COALESCE(?, result_file_size)
                WHERE job_id = ?
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    result_file_size,
                    job_id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(job_id)

    def get_source_path(self, job_id: str) -> Path:
        """返回任务源文件路径。"""

        return Path(self._get_path_value(job_id, "source_path"))

    def get_result_path(self, job_id: str) -> Path | None:
        """返回任务结果文件路径；尚无结果时返回 None。"""

        value = self._get_path_value(job_id, "result_path")
        return Path(value) if value else None

    def update_status(
        self,
        job_id: str,
        status: JobStatus,
        progress: int,
        *,
        error_message: str | None = None,
        result_path: Path | None = None,
        result_file_name: str | None = None,
    ) -> LocalizationJob:
        """更新任务执行状态和结果信息。"""

        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE localization_jobs
                SET status = ?, progress = ?, error_message = ?,
                    result_path = COALESCE(?, result_path),
                    result_file_name = COALESCE(?, result_file_name),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    max(0, min(progress, 100)),
                    error_message,
                    str(result_path) if result_path else None,
                    result_file_name,
                    datetime.now(timezone.utc).isoformat(),
                    job_id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(job_id)
        return self.get(job_id)

    def recover_interrupted_jobs(self) -> list[str]:
        """恢复进程重启时中断的任务并返回可重新提交的任务 ID。

        Returns:
            源文件仍存在、已重置为排队状态的任务 ID。
        """

        with self._write_lock, self._connect() as connection:
            active_statuses = (
                JobStatus.QUEUED,
                JobStatus.ANALYZING,
                JobStatus.SEGMENTING,
                JobStatus.TRANSLATING,
                JobStatus.RENDERING,
                JobStatus.VALIDATING,
                JobStatus.REPAIRING,
                JobStatus.PROCESSING,
            )
            interrupted_rows = connection.execute(
                """
                SELECT id, source_path FROM localization_jobs
                WHERE status IN (?, ?, ?, ?, ?, ?, ?, ?)
                   OR (
                       status = ?
                       AND error_message = ?
                   )
                """,
                (
                    *(status.value for status in active_statuses),
                    JobStatus.FAILED.value,
                    "服务重启导致任务中断，请重新上传文档",
                ),
            ).fetchall()
            now = datetime.now(timezone.utc).isoformat()
            recovered_job_ids: list[str] = []
            for row in interrupted_rows:
                if not Path(row["source_path"]).is_file():
                    connection.execute(
                        """
                        UPDATE localization_jobs
                        SET status = ?, error_message = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            JobStatus.FAILED.value,
                            "服务恢复任务失败：源文件不存在",
                            now,
                            row["id"],
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO job_logs (
                            job_id, stage, level, message, progress, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            row["id"],
                            "recovery",
                            JobLogLevel.ERROR.value,
                            "服务恢复任务失败：源文件不存在",
                            0,
                            now,
                        ),
                    )
                    continue
                connection.execute(
                    """
                    UPDATE localization_jobs
                    SET status = ?, progress = 0, error_message = NULL,
                        result_path = NULL, result_file_name = NULL,
                        pipeline_version = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        JobStatus.QUEUED.value,
                        PDF_PIPELINE_VERSION,
                        now,
                        row["id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE job_metrics
                    SET finished_at = NULL, result_file_size = NULL
                    WHERE job_id = ?
                    """,
                    (row["id"],),
                )
                connection.execute(
                    """
                    INSERT INTO job_logs (
                        job_id, stage, level, message, progress, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        "recovery",
                        JobLogLevel.WARNING.value,
                        "检测到服务中断，已自动重新加入队列，无需重新上传；"
                        "已完成翻译批次会直接复用",
                        0,
                        now,
                    ),
                )
                recovered_job_ids.append(row["id"])
        return recovered_job_ids

    def prepare_reprocess(self, job_id: str) -> LocalizationJob:
        """归档旧产物并复用原文件按当前流水线重新排队。

        Args:
            job_id: 需要重新处理的任务 ID。

        Returns:
            已重置为排队状态的任务。

        Raises:
            KeyError: 任务不存在。
            FileNotFoundError: 服务器保留的原 PDF 不存在。
            RuntimeError: 任务仍在运行，不能重复提交。
        """

        with self._write_lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT status, source_path, result_path, pipeline_version
                FROM localization_jobs
                WHERE id = ?
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            active_statuses = {
                JobStatus.QUEUED.value,
                JobStatus.ANALYZING.value,
                JobStatus.SEGMENTING.value,
                JobStatus.TRANSLATING.value,
                JobStatus.RENDERING.value,
                JobStatus.VALIDATING.value,
                JobStatus.REPAIRING.value,
                JobStatus.PROCESSING.value,
            }
            if row["status"] in active_statuses:
                raise RuntimeError("任务正在运行，不能重复提交")
            source_path = Path(row["source_path"])
            if not source_path.is_file():
                raise FileNotFoundError("服务器保留的原 PDF 不存在，无法重新处理")

            result_path = Path(row["result_path"]) if row["result_path"] else None
            if result_path and result_path.is_file():
                archived_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                archive_path = (
                    self.legacy_result_directory / f"{job_id}-{archived_at}.pdf"
                )
                result_path.replace(archive_path)

            previous_pipeline_version = row["pipeline_version"]
            now = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """
                UPDATE localization_jobs
                SET status = ?, progress = 0, result_path = NULL,
                    result_file_name = NULL, error_message = NULL,
                    pipeline_version = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    JobStatus.QUEUED.value,
                    PDF_PIPELINE_VERSION,
                    now,
                    job_id,
                ),
            )
            connection.execute(
                """
                UPDATE job_metrics
                SET started_at = NULL, finished_at = NULL,
                    mineru_duration_ms = 0, llm_duration_ms = 0,
                    render_duration_ms = 0, validation_duration_ms = 0,
                    input_tokens = 0, output_tokens = 0, total_tokens = 0,
                    token_usage_available = 0, result_file_size = NULL,
                    translation_batch_count = 0,
                    resumed_translation_batch_count = 0,
                    translation_memory_hit_count = 0,
                    quality_issue_count = 0
                WHERE job_id = ?
                """,
                (job_id,),
            )
            connection.execute(
                "DELETE FROM job_quality_issues WHERE job_id = ?",
                (job_id,),
            )
            if previous_pipeline_version != PDF_PIPELINE_VERSION:
                connection.execute(
                    "DELETE FROM translation_batches WHERE job_id = ?",
                    (job_id,),
                )
            connection.execute(
                """
                INSERT INTO job_logs (
                    job_id, stage, level, message, progress, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    "reprocess",
                    JobLogLevel.WARNING.value,
                    (
                        f"旧产物已归档，任务已切换到当前流水线 "
                        f"{PDF_PIPELINE_VERSION} 重新处理"
                    ),
                    0,
                    now,
                ),
            )
        return self.get(job_id)

    def cancel(self, job_id: str) -> LocalizationJob:
        """将未完成任务标记为已取消。"""

        job = self.get(job_id)
        if job.status in {
            JobStatus.COMPLETED,
            JobStatus.NEEDS_REVIEW,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }:
            return job
        cancelled_job = self.update_status(
            job_id,
            JobStatus.CANCELLED,
            job.progress,
            error_message="用户已取消",
        )
        self.append_log(
            job_id,
            "cancelled",
            JobLogLevel.WARNING,
            "任务已取消",
            job.progress,
        )
        self.finish_metrics(job_id)
        return cancelled_job

    def list_glossary_terms(self) -> list[GlossaryTerm]:
        """返回后端正式术语库中的全部条目。"""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM glossary_terms
                ORDER BY updated_at DESC, source_text COLLATE NOCASE
                """
            ).fetchall()
        return [self._to_glossary_term(row) for row in rows]

    def upsert_glossary_term(
        self,
        term: GlossaryTermInput,
        *,
        source: GlossaryTermSource = GlossaryTermSource.MANUAL,
        source_job_id: str | None = None,
    ) -> GlossaryTerm:
        """按原文不区分大小写新增或更新正式术语。"""

        now = datetime.now(timezone.utc).isoformat()
        term_id = str(uuid4())
        source_text = term.source_text.strip()
        target_text = term.target_text.strip()
        category = term.category.strip() or "专业术语"
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO glossary_terms (
                    id, source_text, target_text, category, source,
                    source_job_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_text) DO UPDATE SET
                    target_text = excluded.target_text,
                    category = excluded.category,
                    source = excluded.source,
                    source_job_id = excluded.source_job_id,
                    updated_at = excluded.updated_at
                """,
                (
                    term_id,
                    source_text,
                    target_text,
                    category,
                    source.value,
                    source_job_id,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM glossary_terms WHERE source_text = ? COLLATE NOCASE",
                (source_text,),
            ).fetchone()
        if row is None:
            raise RuntimeError("术语写入失败")
        return self._to_glossary_term(row)

    def delete_glossary_term(self, term_id: str) -> None:
        """删除一个正式术语，不影响既有任务的术语快照。"""

        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM glossary_terms WHERE id = ?",
                (term_id,),
            )
            if cursor.rowcount == 0:
                raise KeyError(term_id)

    def get_glossary_snapshot(
        self,
        job_id: str,
    ) -> list[dict[str, str]]:
        """返回任务创建时冻结的术语快照。"""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT source_text, target_text, category
                FROM job_glossary_snapshot
                WHERE job_id = ?
                ORDER BY LENGTH(source_text) DESC, source_text
                """,
                (job_id,),
            ).fetchall()
        return [
            {
                "source_text": row["source_text"],
                "target_text": row["target_text"],
                "category": row["category"],
            }
            for row in rows
        ]

    def get_translation_batch(
        self,
        job_id: str,
        batch_key: str,
    ) -> dict[str, object] | None:
        """返回批次持久化结果，用于进程重启后续跑。"""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM translation_batches
                WHERE job_id = ? AND batch_key = ?
                """,
                (job_id, batch_key),
            ).fetchone()
        if row is None:
            return None
        return {
            "stage": row["stage"],
            "page_index": row["page_index"],
            "block_ids": json.loads(row["block_ids"]),
            "translations": json.loads(row["translations"]),
            "status": row["status"],
            "attempts": row["attempts"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "total_tokens": row["total_tokens"],
            "token_usage_available": bool(row["token_usage_available"]),
            "error_message": row["error_message"],
        }

    def save_translation_batch_progress(
        self,
        job_id: str,
        batch_key: str,
        *,
        stage: str,
        page_index: int,
        block_ids: Iterable[str],
        translations: dict[str, str],
        status: str,
        attempts_increment: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        token_usage_available: bool = False,
        error_message: str | None = None,
    ) -> None:
        """原子合并批次译文并记录尝试、Token 和执行状态。"""

        now = datetime.now(timezone.utc).isoformat()
        with self._write_lock, self._connect() as connection:
            existing = connection.execute(
                """
                SELECT translations FROM translation_batches
                WHERE job_id = ? AND batch_key = ?
                """,
                (job_id, batch_key),
            ).fetchone()
            merged_translations = (
                json.loads(existing["translations"]) if existing else {}
            )
            merged_translations.update(translations)
            connection.execute(
                """
                INSERT INTO translation_batches (
                    job_id, batch_key, stage, page_index, block_ids,
                    translations, status, attempts, input_tokens,
                    output_tokens, total_tokens, token_usage_available,
                    error_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, batch_key) DO UPDATE SET
                    translations = excluded.translations,
                    status = excluded.status,
                    attempts = translation_batches.attempts + excluded.attempts,
                    input_tokens =
                        translation_batches.input_tokens + excluded.input_tokens,
                    output_tokens =
                        translation_batches.output_tokens + excluded.output_tokens,
                    total_tokens =
                        translation_batches.total_tokens + excluded.total_tokens,
                    token_usage_available = MAX(
                        translation_batches.token_usage_available,
                        excluded.token_usage_available
                    ),
                    error_message = excluded.error_message,
                    updated_at = excluded.updated_at
                """,
                (
                    job_id,
                    batch_key,
                    stage,
                    max(0, page_index),
                    json.dumps(list(block_ids), ensure_ascii=False),
                    json.dumps(merged_translations, ensure_ascii=False),
                    status,
                    max(0, attempts_increment),
                    max(0, input_tokens),
                    max(0, output_tokens),
                    max(0, total_tokens),
                    int(token_usage_available),
                    error_message[:500] if error_message else None,
                    now,
                    now,
                ),
            )

    def clear_quality_issues(self, job_id: str) -> None:
        """清除任务旧质检问题，便于重试后写入最新报告。"""

        with self._write_lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM job_quality_issues WHERE job_id = ?",
                (job_id,),
            )
            connection.execute(
                "UPDATE job_metrics SET quality_issue_count = 0 WHERE job_id = ?",
                (job_id,),
            )

    def replace_quality_issues(
        self,
        job_id: str,
        issues: Iterable[dict[str, object]],
    ) -> list[QualityIssue]:
        """使用最新自动质检报告替换任务问题清单。"""

        issue_list = list(issues)
        now = datetime.now(timezone.utc).isoformat()
        with self._write_lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM job_quality_issues WHERE job_id = ?",
                (job_id,),
            )
            for issue in issue_list:
                connection.execute(
                    """
                    INSERT INTO job_quality_issues (
                        job_id, stage, code, severity, message,
                        page_index, block_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        str(issue.get("stage") or "validation")[:64],
                        str(issue.get("code") or "unknown")[:64],
                        str(issue.get("severity") or "error")[:16],
                        str(issue.get("message") or "未说明的质检问题")[:1000],
                        issue.get("page_index"),
                        str(issue["block_id"])[:128]
                        if issue.get("block_id")
                        else None,
                        now,
                    ),
                )
            connection.execute(
                """
                UPDATE job_metrics
                SET quality_issue_count = ?
                WHERE job_id = ?
                """,
                (len(issue_list), job_id),
            )
        return self.list_quality_issues(job_id)

    def list_quality_issues(self, job_id: str) -> list[QualityIssue]:
        """返回任务最新自动质检问题。"""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, stage, code, severity, message,
                       page_index, block_id, created_at
                FROM job_quality_issues
                WHERE job_id = ?
                ORDER BY id
                """,
                (job_id,),
            ).fetchall()
        return [
            QualityIssue(
                id=row["id"],
                stage=row["stage"],
                code=row["code"],
                severity=row["severity"],
                message=row["message"],
                page_index=row["page_index"],
                block_id=row["block_id"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def list_glossary_candidates(
        self,
        status: GlossaryCandidateStatus | None = None,
    ) -> list[GlossaryCandidate]:
        """返回自动学习术语候选，可按状态筛选。"""

        query = "SELECT * FROM glossary_candidates"
        parameters: tuple[object, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            parameters = (status.value,)
        query += " ORDER BY created_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            GlossaryCandidate(
                id=row["id"],
                job_id=row["job_id"],
                source_text=row["source_text"],
                proposed_target_text=row["proposed_target_text"],
                category=row["category"],
                confidence=row["confidence"],
                occurrences=row["occurrences"],
                status=GlossaryCandidateStatus(row["status"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def get_translation_memory(
        self,
        policy: TranslationPolicy,
        source_texts: Iterable[str],
    ) -> dict[str, str]:
        """按语言方向读取完全匹配的历史译文。

        只复用原文和语言方向完全一致的记录，避免模糊匹配把相似型号、数字或不同
        语境的短语错误套用。返回值以原文为键，同一文档内重复文本可共享译文。
        """

        normalized_texts = list(
            dict.fromkeys(
                text.strip()
                for text in source_texts
                if text.strip()
            )
        )
        if not normalized_texts:
            return {}
        policy_fingerprint = _translation_memory_policy_fingerprint(policy)
        translations: dict[str, str] = {}
        with self._connect() as connection:
            for start in range(0, len(normalized_texts), 400):
                text_chunk = normalized_texts[start : start + 400]
                query = """
                    SELECT source_text, target_text
                    FROM translation_memory
                    WHERE source_language = ?
                      AND target_language = ?
                      AND policy_fingerprint = ?
                      AND source_text IN (SELECT value FROM json_each(?))
                    """
                rows = connection.execute(
                    query,
                    (
                        policy.source_language.value,
                        policy.target_language.value,
                        policy_fingerprint,
                        json.dumps(text_chunk, ensure_ascii=False),
                    ),
                ).fetchall()
                translations.update(
                    {
                        row["source_text"]: row["target_text"]
                        for row in rows
                        if row["target_text"].strip()
                    }
                )
        return translations

    def record_translation_learning(
        self,
        job: LocalizationJob,
        translation_pairs: Iterable[tuple[str, str]],
    ) -> int:
        """写入翻译记忆，并按任务策略生成或自动录入术语。"""

        normalized_pairs = [
            (source.strip(), target.strip())
            for source, target in translation_pairs
            if source.strip() and target.strip() and source.strip() != target.strip()
        ]
        if not normalized_pairs:
            return 0
        occurrence_map: dict[tuple[str, str], int] = {}
        for pair in normalized_pairs:
            occurrence_map[pair] = occurrence_map.get(pair, 0) + 1
        now = datetime.now(timezone.utc).isoformat()
        policy_fingerprint = _translation_memory_policy_fingerprint(
            job.translation_policy
        )
        candidate_count = 0
        with self._write_lock, self._connect() as connection:
            for (source_text, target_text), occurrences in occurrence_map.items():
                source_hash = hashlib.sha256(
                    (
                        f"{job.translation_policy.source_language.value}\0"
                        f"{job.translation_policy.target_language.value}\0"
                        f"{policy_fingerprint}\0"
                        f"{source_text}"
                    ).encode("utf-8")
                ).hexdigest()
                connection.execute(
                    """
                    INSERT INTO translation_memory (
                        source_hash, source_text, target_text, source_language,
                        target_language, policy_fingerprint, model, occurrences,
                        last_job_id, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_hash) DO UPDATE SET
                        target_text = excluded.target_text,
                        model = excluded.model,
                        occurrences =
                            translation_memory.occurrences + excluded.occurrences,
                        last_job_id = excluded.last_job_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        source_hash,
                        source_text,
                        target_text,
                        job.translation_policy.source_language.value,
                        job.translation_policy.target_language.value,
                        policy_fingerprint,
                        job.model,
                        occurrences,
                        job.id,
                        now,
                    ),
                )
                if (
                    job.translation_policy.glossary_learning_mode
                    == GlossaryLearningMode.OFF
                    or not self._is_glossary_candidate(source_text, occurrences)
                ):
                    continue
                confidence = 0.92 if occurrences >= 2 else 0.82
                candidate_status = (
                    GlossaryCandidateStatus.APPROVED
                    if job.translation_policy.glossary_learning_mode
                    == GlossaryLearningMode.AUTO
                    else GlossaryCandidateStatus.PENDING
                )
                connection.execute(
                    """
                    INSERT INTO glossary_candidates (
                        id, job_id, source_text, proposed_target_text,
                        category, confidence, occurrences, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id, source_text) DO UPDATE SET
                        proposed_target_text = excluded.proposed_target_text,
                        confidence = excluded.confidence,
                        occurrences = excluded.occurrences,
                        status = excluded.status
                    """,
                    (
                        str(uuid4()),
                        job.id,
                        source_text,
                        target_text,
                        "任务学习",
                        confidence,
                        occurrences,
                        candidate_status.value,
                        now,
                    ),
                )
                candidate_count += 1
                if candidate_status == GlossaryCandidateStatus.APPROVED:
                    connection.execute(
                        """
                        INSERT INTO glossary_terms (
                            id, source_text, target_text, category, source,
                            source_job_id, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source_text) DO UPDATE SET
                            target_text = excluded.target_text,
                            category = excluded.category,
                            source = excluded.source,
                            source_job_id = excluded.source_job_id,
                            updated_at = excluded.updated_at
                        """,
                        (
                            str(uuid4()),
                            source_text,
                            target_text,
                            "任务学习",
                            GlossaryTermSource.TASK.value,
                            job.id,
                            now,
                            now,
                        ),
                    )
        return candidate_count

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table_name: str,
        column_name: str,
        definition: str,
    ) -> None:
        """为旧数据库补充字段，保持原数据可原位升级。"""

        existing_columns = {
            row["name"]
            for row in connection.execute(
                f"PRAGMA table_info({table_name})"
            ).fetchall()
        }
        if column_name not in existing_columns:
            connection.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
            )

    def _get_path_value(self, job_id: str, column: str) -> str | None:
        if column not in {"source_path", "result_path"}:
            raise ValueError("不允许读取的路径字段")
        query = (
            "SELECT source_path FROM localization_jobs WHERE id = ?"
            if column == "source_path"
            else "SELECT result_path FROM localization_jobs WHERE id = ?"
        )
        with self._connect() as connection:
            row = connection.execute(
                query,
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return row[column]

    def _to_job(self, row: sqlite3.Row) -> LocalizationJob:
        result_path = Path(row["result_path"]) if row["result_path"] else None
        return LocalizationJob(
            id=row["id"],
            file_name=row["file_name"],
            file_size=row["file_size"],
            model=row["model"],
            strategy=row["strategy"],
            translation_policy=TranslationPolicy.model_validate(
                json.loads(row["translation_policy"])
            ),
            status=JobStatus(row["status"]),
            progress=row["progress"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            result_file_name=row["result_file_name"],
            result_available=bool(
                row["status"]
                in {
                    JobStatus.COMPLETED.value,
                    JobStatus.NEEDS_REVIEW.value,
                }
                and row["pipeline_version"] == PDF_PIPELINE_VERSION
                and result_path
                and result_path.is_file()
            ),
            result_downloadable=bool(
                (
                    (
                        row["status"] == JobStatus.COMPLETED.value
                        and row["pipeline_version"] == PDF_PIPELINE_VERSION
                    )
                    or (
                        row["status"] == JobStatus.NEEDS_REVIEW.value
                        and row["pipeline_version"] != LEGACY_PIPELINE_VERSION
                    )
                )
                and result_path
                and result_path.is_file()
            ),
            pipeline_version=row["pipeline_version"],
            result_outdated=bool(
                row["status"] == JobStatus.COMPLETED.value
                and row["pipeline_version"] != PDF_PIPELINE_VERSION
                and result_path
                and result_path.is_file()
            ),
            error_message=row["error_message"],
        )

    @staticmethod
    def _to_log_entry(row: sqlite3.Row) -> JobLogEntry:
        """将 SQLite 行转换为任务日志模型。"""

        return JobLogEntry(
            id=row["id"],
            stage=row["stage"],
            level=JobLogLevel(row["level"]),
            message=row["message"],
            progress=row["progress"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _to_glossary_term(row: sqlite3.Row) -> GlossaryTerm:
        """将 SQLite 行转换为术语条目。"""

        return GlossaryTerm(
            id=row["id"],
            source_text=row["source_text"],
            target_text=row["target_text"],
            category=row["category"],
            source=GlossaryTermSource(row["source"]),
            source_job_id=row["source_job_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _is_glossary_candidate(source_text: str, occurrences: int) -> bool:
        """筛选适合沉淀为术语、而非整句翻译记忆的短文本。"""

        compact_text = " ".join(source_text.split())
        if not 2 <= len(compact_text) <= 60:
            return False
        if any(mark in compact_text for mark in ("。", "！", "？", ". ", "! ", "? ")):
            return False
        has_latin_identifier = any(character.isupper() for character in compact_text)
        has_digit = any(character.isdigit() for character in compact_text)
        return occurrences >= 2 or has_latin_identifier or has_digit
