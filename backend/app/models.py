"""定义 DocWeave 任务、语言、术语和自动质检数据模型。"""

from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    """文档本地化任务状态。"""

    QUEUED = "queued"
    ANALYZING = "analyzing"
    SEGMENTING = "segmenting"
    TRANSLATING = "translating"
    RENDERING = "rendering"
    VALIDATING = "validating"
    REPAIRING = "repairing"
    PROCESSING = "processing"
    NEEDS_REVIEW = "needs_review"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobLogLevel(str, Enum):
    """任务运行日志级别。"""

    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


class LanguageCode(str, Enum):
    """任务可选择的输入和输出语言代码。"""

    AUTO = "auto"
    ZH_CN = "zh-CN"
    EN = "en"
    JA = "ja"
    KO = "ko"
    DE = "de"
    FR = "fr"


class GlossaryLearningMode(str, Enum):
    """任务完成后的术语学习策略。"""

    OFF = "off"
    REVIEW = "review"
    AUTO = "auto"


class GlossaryTermSource(str, Enum):
    """术语条目的产生来源。"""

    MANUAL = "manual"
    TASK = "task"


class GlossaryCandidateStatus(str, Enum):
    """自动学习术语候选的审核状态。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class TranslationPolicy(BaseModel):
    """控制自然语言翻译和内容保留的任务级规则。"""

    source_language: LanguageCode = LanguageCode.AUTO
    target_language: LanguageCode = LanguageCode.ZH_CN
    translate_all_translatable_text: bool = True
    preserve_proper_nouns: bool = True
    preserve_glossary_terms: bool = True
    preserve_models_and_standards: bool = True
    glossary_learning_mode: GlossaryLearningMode = GlossaryLearningMode.REVIEW
    protected_terms: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(
        default_factory=list,
        max_length=200,
    )


class CreateJobRequest(BaseModel):
    """创建本地化任务时保存的业务参数。"""

    file_name: Annotated[str, Field(min_length=1, max_length=255)]
    file_size: Annotated[int, Field(ge=1)]
    model: Annotated[str, Field(min_length=1, max_length=200)]
    strategy: Annotated[str, Field(min_length=1, max_length=64)] = "智能路由"
    translation_policy: TranslationPolicy = Field(default_factory=TranslationPolicy)


class LocalizationJob(BaseModel):
    """面向前端的真实本地化任务响应。"""

    id: str
    file_name: str
    file_size: int
    model: str
    strategy: str
    translation_policy: TranslationPolicy
    status: JobStatus
    progress: Annotated[int, Field(ge=0, le=100)]
    created_at: datetime
    updated_at: datetime
    result_file_name: str | None = None
    result_available: bool = False
    result_downloadable: bool = False
    pipeline_version: str
    result_outdated: bool = False
    error_message: str | None = None


class GlossaryTermInput(BaseModel):
    """新增或更新术语时使用的输入模型。"""

    source_text: Annotated[str, Field(min_length=1, max_length=160)]
    target_text: Annotated[str, Field(min_length=1, max_length=240)]
    category: Annotated[str, Field(min_length=1, max_length=80)] = "专业术语"


class GlossaryTerm(GlossaryTermInput):
    """后端持久化的正式术语条目。"""

    id: str
    source: GlossaryTermSource
    source_job_id: str | None = None
    created_at: datetime
    updated_at: datetime


class GlossaryCandidate(BaseModel):
    """任务自动提取、等待审核或自动通过的术语候选。"""

    id: str
    job_id: str
    source_text: str
    proposed_target_text: str
    category: str
    confidence: Annotated[float, Field(ge=0, le=1)]
    occurrences: Annotated[int, Field(ge=1)]
    status: GlossaryCandidateStatus
    created_at: datetime


class QualityIssue(BaseModel):
    """自动质检产生的结构化问题。"""

    id: int
    stage: str
    code: str
    severity: str
    message: str
    page_index: Annotated[int | None, Field(ge=0)] = None
    block_id: str | None = None
    created_at: datetime


class JobLogEntry(BaseModel):
    """任务执行过程中持久化的单条阶段日志。"""

    id: int
    stage: str
    level: JobLogLevel
    message: str
    progress: Annotated[int | None, Field(ge=0, le=100)] = None
    created_at: datetime


class JobMetrics(BaseModel):
    """任务的耗时、Token 与结果文件资源指标。"""

    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: Annotated[int, Field(ge=0)] = 0
    mineru_duration_ms: Annotated[int, Field(ge=0)] = 0
    llm_duration_ms: Annotated[int, Field(ge=0)] = 0
    render_duration_ms: Annotated[int, Field(ge=0)] = 0
    validation_duration_ms: Annotated[int, Field(ge=0)] = 0
    input_tokens: Annotated[int, Field(ge=0)] = 0
    output_tokens: Annotated[int, Field(ge=0)] = 0
    total_tokens: Annotated[int, Field(ge=0)] = 0
    token_usage_available: bool = False
    result_file_size: Annotated[int | None, Field(ge=0)] = None
    translation_batch_count: Annotated[int, Field(ge=0)] = 0
    resumed_translation_batch_count: Annotated[int, Field(ge=0)] = 0
    translation_memory_hit_count: Annotated[int, Field(ge=0)] = 0
    quality_issue_count: Annotated[int, Field(ge=0)] = 0


class JobDetails(BaseModel):
    """任务详情报告，包含状态、运行日志和资源指标。"""

    job: LocalizationJob
    logs: list[JobLogEntry]
    metrics: JobMetrics
    quality_issues: list[QualityIssue] = Field(default_factory=list)
