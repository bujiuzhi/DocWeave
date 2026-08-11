<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'

type JobStatus =
  | 'queued'
  | 'analyzing'
  | 'segmenting'
  | 'translating'
  | 'rendering'
  | 'validating'
  | 'repairing'
  | 'processing'
  | 'needs_review'
  | 'completed'
  | 'failed'
  | 'cancelled'
type LanguageCode = 'auto' | 'zh-CN' | 'en' | 'ja' | 'ko' | 'de' | 'fr'
type NavigationKey = 'workspace' | 'library' | 'glossary' | 'settings'
type GlossaryLearningMode = 'off' | 'review' | 'auto'
type JobLogLevel = 'info' | 'success' | 'warning' | 'error'

interface NavigationItem {
  key: NavigationKey
  label: string
  icon: string
}

interface TranslationPolicy {
  sourceLanguage: LanguageCode
  targetLanguage: LanguageCode
  translateAllTranslatableText: boolean
  preserveProperNouns: boolean
  preserveGlossaryTerms: boolean
  preserveModelsAndStandards: boolean
  glossaryLearningMode: GlossaryLearningMode
  protectedTerms: string[]
}

interface LocalizationJob {
  id: string
  fileName: string
  fileSize: string
  strategy: string
  model: string
  status: JobStatus
  progress: number
  createdAt: string
  languageLabel: string
  targetLanguage: LanguageCode
  policy: TranslationPolicy
  resultFileName: string | null
  resultAvailable: boolean
  resultDownloadable: boolean
  pipelineVersion: string
  resultOutdated: boolean
  errorMessage: string | null
}

interface ApiLocalizationJob {
  id: string
  file_name: string
  file_size: number
  strategy: string
  model: string
  status: JobStatus
  progress: number
  created_at: string
  result_file_name: string | null
  result_available: boolean
  result_downloadable: boolean
  pipeline_version: string
  result_outdated: boolean
  error_message: string | null
  translation_policy: {
    source_language: LanguageCode
    target_language: LanguageCode
    translate_all_translatable_text: boolean
    preserve_proper_nouns: boolean
    preserve_glossary_terms: boolean
    preserve_models_and_standards: boolean
    glossary_learning_mode: GlossaryLearningMode
    protected_terms: string[]
  }
}

interface ApiJobLogEntry {
  id: number
  stage: string
  level: JobLogLevel
  message: string
  progress: number | null
  created_at: string
}

interface ApiJobMetrics {
  started_at: string | null
  finished_at: string | null
  duration_ms: number
  mineru_duration_ms: number
  llm_duration_ms: number
  render_duration_ms: number
  validation_duration_ms: number
  input_tokens: number
  output_tokens: number
  total_tokens: number
  token_usage_available: boolean
  result_file_size: number | null
  translation_batch_count: number
  resumed_translation_batch_count: number
  translation_memory_hit_count: number
  quality_issue_count: number
}

interface ApiJobDetails {
  job: ApiLocalizationJob
  logs: ApiJobLogEntry[]
  metrics: ApiJobMetrics
  quality_issues: ApiQualityIssue[]
}

interface ApiQualityIssue {
  id: number
  stage: string
  code: string
  severity: string
  message: string
  page_index: number | null
  block_id: string | null
  created_at: string
}

interface QualityIssueGroup {
  key: string
  code: string
  label: string
  severity: string
  message: string
  pages: number[]
  sampleBlockIds: string[]
  issues: ApiQualityIssue[]
}

interface JobDetails {
  job: LocalizationJob
  logs: Array<{
    id: number
    stage: string
    level: JobLogLevel
    message: string
    progress: number | null
    createdAt: string
  }>
  metrics: ApiJobMetrics
  qualityIssues: ApiQualityIssue[]
}

interface GlossaryTerm {
  id: string
  source: string
  target: string
  category: string
  sourceType: 'manual' | 'task'
  sourceJob?: string
}

interface ApiGlossaryTerm {
  id: string
  source_text: string
  target_text: string
  category: string
  source: 'manual' | 'task'
  source_job_id: string | null
}

const GLOSSARY_STORAGE_KEY = 'docweave.glossary'
const BATCH_UPLOAD_CONCURRENCY = 4
const MAX_BATCH_FILES = 50
const modelOptions = [
  'gpt-5.6-terra',
  'gpt-5.6-sol',
  'gpt-5.6-luna',
  'gpt-5.5',
  'gemini-3.1-pro-preview',
  'gemini-3.6-flash-thinking',
  'claude-opus-5',
  'claude-opus-4-8',
  'claude-sonnet-5',
  'claude-fable-5',
] as const
const DEFAULT_TASK_MODEL = 'gpt-5.5'

const navigationItems: NavigationItem[] = [
  { key: 'workspace', label: '工作台', icon: '◼' },
  { key: 'library', label: '文档库', icon: '▤' },
  { key: 'glossary', label: '术语库', icon: '⌘' },
  { key: 'settings', label: '设置', icon: '⚙' },
]

const pageMeta: Record<NavigationKey, { title: string; description: string }> = {
  workspace: {
    title: '文档本地化工作台',
    description: '所有可翻译语言默认转为中文，并按任务规则保留专名与术语。',
  },
  library: {
    title: '文档库',
    description: '集中查看本地化任务、处理状态和已生成文档。',
  },
  glossary: {
    title: '术语库',
    description: '手动维护术语，或由文档任务自动提取并学习专业表达。',
  },
  settings: {
    title: '部署配置',
    description: '服务连接和密钥仅由后端环境变量管理，不在浏览器中保存。',
  },
}

const languageOptions: Array<{ value: LanguageCode; label: string }> = [
  { value: 'auto', label: '自动识别（所有语言）' },
  { value: 'zh-CN', label: '简体中文' },
  { value: 'en', label: '英语' },
  { value: 'ja', label: '日语' },
  { value: 'ko', label: '韩语' },
  { value: 'de', label: '德语' },
  { value: 'fr', label: '法语' },
]

const targetLanguageOptions = languageOptions.filter((item) => item.value !== 'auto')
const glossaryLearningOptions: Array<{ value: GlossaryLearningMode; label: string; description: string }> = [
  { value: 'off', label: '不学习术语', description: '仅使用已有术语库，不从本任务提取新术语。' },
  { value: 'review', label: '生成候选后审核', description: '任务完成后生成术语候选，确认后再写入术语库。' },
  { value: 'auto', label: '自动录入术语库', description: '任务完成后自动去重并写入高置信度术语。' },
]
const fileInput = ref<HTMLInputElement | null>(null)
const isDragging = ref(false)
const selectedFiles = ref<File[]>([])
const selectedStrategy = ref('智能路由')
const selectedModel = ref(DEFAULT_TASK_MODEL)
const glossaryLearningMode = ref<GlossaryLearningMode>('review')
const sourceLanguage = ref<LanguageCode>('auto')
const targetLanguage = ref<LanguageCode>('zh-CN')
const translateAllTranslatableText = ref(true)
const preserveProperNouns = ref(true)
const preserveGlossaryTerms = ref(true)
const preserveModelsAndStandards = ref(true)
const protectedTermsInput = ref('')
const activeNavigation = ref<NavigationKey>('workspace')
const searchQuery = ref('')
const toastMessage = ref('')
const jobsLoading = ref(false)
const jobSubmitting = ref(false)
const detailsLoading = ref(false)
const activeJobDetails = ref<JobDetails | null>(null)
const batchSubmission = reactive({ total: 0, completed: 0, failed: 0 })
const newGlossaryTerm = reactive({ source: '', target: '', category: '专业术语' })

const jobs = ref<LocalizationJob[]>([])
const glossaryTerms = ref<GlossaryTerm[]>([])
const activeJobStatuses: JobStatus[] = [
  'queued',
  'analyzing',
  'segmenting',
  'translating',
  'rendering',
  'validating',
  'repairing',
  'processing',
]
const reprocessableJobStatuses: JobStatus[] = [
  'needs_review',
  'failed',
  'cancelled',
]

const activePageMeta = computed(() => pageMeta[activeNavigation.value])
const languageLabel = computed(() => `${getLanguageName(sourceLanguage.value)} → ${getLanguageName(targetLanguage.value)}`)
const selectedFilesSize = computed(() => selectedFiles.value.reduce((total, file) => total + file.size, 0))
const submitButtonLabel = computed(() => {
  if (jobSubmitting.value) return `并发提交中 ${batchSubmission.completed}/${batchSubmission.total}`
  if (selectedFiles.value.length > 1) return `并发创建 ${selectedFiles.value.length} 个任务`
  return '按此规则开始本地化'
})
const completedJobs = computed(() => jobs.value.filter((job) => job.resultAvailable).length)
const currentRunLogs = computed(() => {
  const logs = activeJobDetails.value?.logs ?? []
  const lastReprocessIndex = logs.map((entry) => entry.stage).lastIndexOf('reprocess')
  return logs.slice(lastReprocessIndex >= 0 ? lastReprocessIndex : 0)
})
const historicalRunLogs = computed(() => {
  const logs = activeJobDetails.value?.logs ?? []
  const lastReprocessIndex = logs.map((entry) => entry.stage).lastIndexOf('reprocess')
  return lastReprocessIndex > 0 ? logs.slice(0, lastReprocessIndex) : []
})
const qualityIssueGroups = computed<QualityIssueGroup[]>(() => {
  const grouped = new Map<string, QualityIssueGroup>()
  for (const issue of activeJobDetails.value?.qualityIssues ?? []) {
    const key = `${issue.severity}:${issue.code}:${issue.message}`
    const group = grouped.get(key) ?? {
      key,
      code: issue.code,
      label: getQualityIssueLabel(issue.code),
      severity: issue.severity,
      message: issue.message,
      pages: [],
      sampleBlockIds: [],
      issues: [],
    }
    if (issue.page_index !== null && !group.pages.includes(issue.page_index + 1)) {
      group.pages.push(issue.page_index + 1)
    }
    if (issue.block_id && group.sampleBlockIds.length < 5 && !group.sampleBlockIds.includes(issue.block_id)) {
      group.sampleBlockIds.push(issue.block_id)
    }
    group.issues.push(issue)
    grouped.set(key, group)
  }
  return [...grouped.values()]
    .map((group) => ({ ...group, pages: group.pages.sort((left, right) => left - right) }))
    .sort((left, right) => right.issues.length - left.issues.length)
})
const normalizedSearchQuery = computed(() => searchQuery.value.trim().toLocaleLowerCase())
const filteredJobs = computed(() => {
  if (!normalizedSearchQuery.value) return jobs.value
  return jobs.value.filter((job) =>
    [job.fileName, job.strategy, job.model, job.languageLabel].some((value) =>
      value.toLocaleLowerCase().includes(normalizedSearchQuery.value),
    ),
  )
})
const filteredGlossaryTerms = computed(() => {
  if (!normalizedSearchQuery.value) return glossaryTerms.value
  return glossaryTerms.value.filter((term) =>
    [term.source, term.target, term.category].some((value) =>
      value.toLocaleLowerCase().includes(normalizedSearchQuery.value),
    ),
  )
})
const searchPlaceholder = computed(() => {
  if (activeNavigation.value === 'glossary') return '搜索原文、译文或分类…'
  if (activeNavigation.value === 'settings') return '设置页无需搜索'
  return '搜索文档、任务或处理策略…'
})

let pollingTimer: number | undefined
let toastTimer: number | undefined

function getLanguageName(code: LanguageCode): string {
  return languageOptions.find((item) => item.value === code)?.label ?? code
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function getGlossaryLearningLabel(mode: GlossaryLearningMode): string {
  return glossaryLearningOptions.find((item) => item.value === mode)?.label ?? mode
}

function getStatusLabel(status: JobStatus): string {
  return {
    queued: '等待处理',
    analyzing: '文档分析',
    segmenting: '结构分块',
    translating: '正文翻译',
    rendering: '版式复原',
    validating: '自动质检',
    repairing: '自动修复',
    processing: '处理中',
    needs_review: '已完成（建议复核）',
    completed: '已完成',
    failed: '处理失败',
    cancelled: '已取消',
  }[status]
}

function getJobStatusLabel(job: LocalizationJob): string {
  return job.resultOutdated ? '旧版产物' : getStatusLabel(job.status)
}

function getJobStatusClass(job: LocalizationJob): string {
  if (job.resultOutdated) return 'outdated'
  return job.status === 'needs_review' ? 'completed-with-review' : job.status
}

function getJobStatusTitle(job: LocalizationJob): string | undefined {
  if (job.resultOutdated) return '旧版产物已禁止下载，请按新版重做'
  if (job.status === 'needs_review') {
    return '结果已生成并可正常下载；严格质检保留了待复核建议'
  }
  return job.errorMessage ?? undefined
}

function canReprocessJob(job: LocalizationJob): boolean {
  return job.resultOutdated || reprocessableJobStatuses.includes(job.status)
}

function formatCreatedAt(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function formatLogTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', { hour12: false })
}

function formatDuration(durationMs: number): string {
  if (durationMs < 1000) return `${durationMs} 毫秒`
  if (durationMs < 60_000) return `${(durationMs / 1000).toFixed(2)} 秒`
  const minutes = Math.floor(durationMs / 60_000)
  const seconds = Math.round((durationMs % 60_000) / 1000)
  return `${minutes} 分 ${seconds} 秒`
}

function formatTokenCount(value: number): string {
  return value.toLocaleString('zh-CN')
}

function getStageLabel(stage: string): string {
  return {
    queued: '等待调度',
    started: '任务启动',
    mineru: '结构解析',
    translation: '正文翻译',
    'translation-memory': '翻译记忆',
    filename: '文件名翻译',
    render: 'PDF 生成',
    validation: '全页质检',
    quality: '视觉残留质检',
    'visual-quality': '视觉复检',
    'layout-repair': '溢出修复',
    'needs-review': '质量复核建议',
    reprocess: '新版重做',
    recovery: '断点恢复',
    completed: '任务完成',
    failed: '任务失败',
    cancelled: '任务取消',
  }[stage] ?? stage
}

function getQualityIssueLabel(code: string): string {
  return {
    text_overflow: '文本框溢出',
    unstable_source_fragment: '不稳定原文字形',
    coarse_native_text_block: '原文分块过粗',
    duplicate_visual_overlay: '视觉区域重复覆盖',
    non_text_content_changed: '非文本内容变化',
    page_render_failed: '页面渲染失败',
    incomplete_page_review: '页面质检不完整',
    translation_quality_failed: '译文一致性复核',
    visual_residual_failed: '外语残留复核',
    untranslated_visual_text: '视觉文字复核',
  }[code] ?? code
}

function getQualityIssueDescription(): string {
  return '系统按严格阈值标记了这一处细节，建议人工抽样复核；结果文件仍可正常使用和下载。'
}

function formatIssuePages(pages: number[]): string {
  if (!pages.length) return '全局问题'
  const shown = pages.slice(0, 10).join('、')
  return pages.length > 10 ? `第 ${shown} 等 ${pages.length} 页` : `第 ${shown} 页`
}

function getOutcomeMessage(details: JobDetails): string {
  if (details.job.resultOutdated) {
    return '该文件由旧版大文本框流水线生成，已禁止下载；可复用原 PDF 按当前逐行坐标流水线重新处理'
  }
  if (details.job.status === 'completed') {
    return `结果文件已生成：${details.job.resultFileName ?? '可下载 PDF'}`
  }
  if (details.job.status === 'needs_review') {
    return `结果文件已生成并可正常下载；严格质检保留了 ${details.qualityIssues.length} 条待复核建议。`
  }
  if (details.job.status === 'failed' || details.job.status === 'cancelled') {
    return details.job.errorMessage ?? '任务未成功完成'
  }
  return details.logs[details.logs.length - 1]?.message ?? '等待运行日志'
}

function getCustomerLogMessage(entry: JobDetails['logs'][number]): string {
  if (
    activeJobDetails.value?.job.status === 'needs_review'
    && (
      entry.stage === 'needs-review'
      || /质检未通过|阻断问题|阻断正式交付|禁止自动交付|失败产物/.test(entry.message)
    )
  ) {
    return '严格质量检查已记录待复核细节，结果文件已生成并可正常下载；相关记录可用于人工抽检和持续优化。'
  }
  return entry.message
}

function mapApiJob(job: ApiLocalizationJob): LocalizationJob {
  const policy: TranslationPolicy = {
    sourceLanguage: job.translation_policy.source_language,
    targetLanguage: job.translation_policy.target_language,
    translateAllTranslatableText: job.translation_policy.translate_all_translatable_text,
    preserveProperNouns: job.translation_policy.preserve_proper_nouns,
    preserveGlossaryTerms: job.translation_policy.preserve_glossary_terms,
    preserveModelsAndStandards: job.translation_policy.preserve_models_and_standards,
    glossaryLearningMode: job.translation_policy.glossary_learning_mode,
    protectedTerms: job.translation_policy.protected_terms,
  }
  return {
    id: job.id,
    fileName: job.file_name,
    fileSize: formatFileSize(job.file_size),
    strategy: job.strategy,
    model: job.model,
    status: job.status,
    progress: job.status === 'needs_review' ? 100 : job.progress,
    createdAt: formatCreatedAt(job.created_at),
    languageLabel: `${getLanguageName(policy.sourceLanguage)} → ${getLanguageName(policy.targetLanguage)}`,
    targetLanguage: policy.targetLanguage,
    policy,
    resultFileName: job.result_file_name,
    resultAvailable: job.result_available,
    resultDownloadable: job.result_downloadable,
    pipelineVersion: job.pipeline_version,
    resultOutdated: job.result_outdated,
    errorMessage: job.error_message,
  }
}

function mapApiJobDetails(details: ApiJobDetails): JobDetails {
  return {
    job: mapApiJob(details.job),
    logs: details.logs.map((entry) => ({
      id: entry.id,
      stage: entry.stage,
      level: entry.level,
      message: entry.message,
      progress: entry.progress,
      createdAt: formatLogTime(entry.created_at),
    })),
    metrics: details.metrics,
    qualityIssues: details.quality_issues,
  }
}

function showToast(message: string): void {
  toastMessage.value = message
  if (toastTimer) window.clearTimeout(toastTimer)
  toastTimer = window.setTimeout(() => { toastMessage.value = '' }, 2800)
}

function navigateTo(key: NavigationKey): void {
  activeNavigation.value = key
  searchQuery.value = ''
  window.scrollTo({ top: 0, behavior: 'auto' })
  const nextHash = `#/${key}`
  if (window.location.hash !== nextHash) window.location.hash = nextHash
}

function syncNavigationFromHash(): void {
  const requestedKey = window.location.hash.replace(/^#\/?/, '') as NavigationKey
  const matchingItem = navigationItems.find((item) => item.key === requestedKey)
  activeNavigation.value = matchingItem?.key ?? 'workspace'
  window.scrollTo({ top: 0, behavior: 'auto' })
}

function pickFile(): void {
  fileInput.value?.click()
}

function getFileKey(file: File): string {
  return `${file.name}:${file.size}:${file.lastModified}`
}

function acceptFiles(files: File[]): void {
  if (files.length === 0) return
  const existingKeys = new Set(selectedFiles.value.map(getFileKey))
  const acceptedFiles: File[] = []
  let invalidCount = 0
  let oversizedCount = 0
  let duplicateCount = 0
  let overflowCount = 0

  for (const file of files) {
    if (file.type !== 'application/pdf' && !file.name.toLowerCase().endsWith('.pdf')) {
      invalidCount += 1
      continue
    }
    if (file.size > 200 * 1024 * 1024) {
      oversizedCount += 1
      continue
    }
    const fileKey = getFileKey(file)
    if (existingKeys.has(fileKey)) {
      duplicateCount += 1
      continue
    }
    if (selectedFiles.value.length + acceptedFiles.length >= MAX_BATCH_FILES) {
      overflowCount += 1
      continue
    }
    existingKeys.add(fileKey)
    acceptedFiles.push(file)
  }

  selectedFiles.value = [...selectedFiles.value, ...acceptedFiles]
  const rejectedMessages = [
    invalidCount ? `${invalidCount} 个非 PDF` : '',
    oversizedCount ? `${oversizedCount} 个超过 200 MB` : '',
    duplicateCount ? `${duplicateCount} 个重复文件` : '',
    overflowCount ? `${overflowCount} 个超过批量上限` : '',
  ].filter(Boolean)
  if (acceptedFiles.length > 0) {
    const suffix = rejectedMessages.length ? `；已跳过${rejectedMessages.join('、')}` : ''
    showToast(`已加入 ${acceptedFiles.length} 个 PDF${suffix}`)
  } else {
    showToast(`未加入文件：${rejectedMessages.join('、') || '没有可用 PDF'}`)
  }
}

function handleFileChange(event: Event): void {
  const input = event.target as HTMLInputElement
  acceptFiles(Array.from(input.files ?? []))
  input.value = ''
}

function handleDrop(event: DragEvent): void {
  isDragging.value = false
  acceptFiles(Array.from(event.dataTransfer?.files ?? []))
}

function removeSelectedFile(file: File): void {
  selectedFiles.value = selectedFiles.value.filter((item) => item !== file)
}

function clearSelectedFiles(): void {
  selectedFiles.value = []
  if (fileInput.value) fileInput.value.value = ''
}

function createPolicy(): TranslationPolicy {
  return {
    sourceLanguage: sourceLanguage.value,
    targetLanguage: targetLanguage.value,
    translateAllTranslatableText: translateAllTranslatableText.value,
    preserveProperNouns: preserveProperNouns.value,
    preserveGlossaryTerms: preserveGlossaryTerms.value,
    preserveModelsAndStandards: preserveModelsAndStandards.value,
    glossaryLearningMode: glossaryLearningMode.value,
    protectedTerms: protectedTermsInput.value.split(/[,，\n]/).map((item) => item.trim()).filter(Boolean),
  }
}

async function loadJobs(showSuccess = false): Promise<void> {
  jobsLoading.value = true
  try {
    const response = await fetch('/api/v1/jobs', { headers: { Accept: 'application/json' } })
    if (!response.ok) throw new Error(`任务接口返回 ${response.status}`)
    const payload = await response.json() as ApiLocalizationJob[]
    jobs.value = payload.map(mapApiJob)
    if (showSuccess) showToast('任务列表已刷新')
  } catch (error) {
    console.error('加载任务列表失败', error)
    showToast('任务列表加载失败，请检查后端服务')
  } finally {
    jobsLoading.value = false
  }
}

async function loadJobDetails(jobId: string, showLoading = true): Promise<void> {
  if (showLoading) detailsLoading.value = true
  try {
    const response = await fetch(`/api/v1/jobs/${jobId}/details`, {
      headers: { Accept: 'application/json' },
    })
    if (!response.ok) throw new Error(`任务详情接口返回 ${response.status}`)
    const details = mapApiJobDetails(await response.json() as ApiJobDetails)
    activeJobDetails.value = details
    jobs.value = jobs.value.map((job) => job.id === details.job.id ? details.job : job)
  } catch (error) {
    console.error('加载任务详情失败', error)
    if (showLoading) showToast('任务详情加载失败')
  } finally {
    if (showLoading) detailsLoading.value = false
  }
}

function viewJobDetails(job: LocalizationJob): void {
  activeJobDetails.value = null
  void loadJobDetails(job.id)
}

function closeJobDetails(): void {
  activeJobDetails.value = null
  detailsLoading.value = false
}

async function submitFile(file: File, policy: TranslationPolicy): Promise<LocalizationJob> {
  const formData = new FormData()
  formData.append('file', file)
  formData.append('model', selectedModel.value.trim())
  formData.append('strategy', selectedStrategy.value)
  formData.append('translation_policy', JSON.stringify({
    source_language: policy.sourceLanguage,
    target_language: policy.targetLanguage,
    translate_all_translatable_text: policy.translateAllTranslatableText,
    preserve_proper_nouns: policy.preserveProperNouns,
    preserve_glossary_terms: policy.preserveGlossaryTerms,
    preserve_models_and_standards: policy.preserveModelsAndStandards,
    glossary_learning_mode: policy.glossaryLearningMode,
    protected_terms: policy.protectedTerms,
  }))

  const response = await fetch('/api/v1/jobs', {
    method: 'POST',
    body: formData,
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: string } | null
    throw new Error(payload?.detail ?? `创建任务失败（${response.status}）`)
  }
  return mapApiJob(await response.json() as ApiLocalizationJob)
}

async function startJobs(): Promise<void> {
  if (selectedFiles.value.length === 0) return showToast('请先选择需要本地化的 PDF 文件')
  const filesToSubmit = [...selectedFiles.value]
  const policy = createPolicy()
  const successfulFiles = new Set<File>()
  const failedFiles: string[] = []
  let nextFileIndex = 0

  Object.assign(batchSubmission, {
    total: filesToSubmit.length,
    completed: 0,
    failed: 0,
  })
  jobSubmitting.value = true

  async function submitNextFile(): Promise<void> {
    while (nextFileIndex < filesToSubmit.length) {
      const file = filesToSubmit[nextFileIndex]
      nextFileIndex += 1
      try {
        const newJob = await submitFile(file, policy)
        successfulFiles.add(file)
        jobs.value = [newJob, ...jobs.value.filter((job) => job.id !== newJob.id)]
      } catch (error) {
        batchSubmission.failed += 1
        failedFiles.push(file.name)
        console.error(`创建任务失败：${file.name}`, error)
      } finally {
        batchSubmission.completed += 1
      }
    }
  }

  try {
    const concurrency = Math.min(BATCH_UPLOAD_CONCURRENCY, filesToSubmit.length)
    await Promise.all(Array.from({ length: concurrency }, () => submitNextFile()))
    selectedFiles.value = selectedFiles.value.filter((file) => !successfulFiles.has(file))
    if (fileInput.value) fileInput.value.value = ''
    if (failedFiles.length === 0) {
      showToast(`已并发创建 ${successfulFiles.size} 个真实任务`)
    } else {
      showToast(`成功 ${successfulFiles.size} 个，失败 ${failedFiles.length} 个；失败文件已保留`)
    }
  } finally {
    jobSubmitting.value = false
  }
}

async function cancelJob(job: LocalizationJob): Promise<void> {
  try {
    const response = await fetch(`/api/v1/jobs/${job.id}`, {
      method: 'DELETE',
      headers: { Accept: 'application/json' },
    })
    if (!response.ok) throw new Error(`取消任务失败（${response.status}）`)
    const updatedJob = mapApiJob(await response.json() as ApiLocalizationJob)
    jobs.value = jobs.value.map((item) => item.id === updatedJob.id ? updatedJob : item)
    if (activeJobDetails.value?.job.id === updatedJob.id) {
      void loadJobDetails(updatedJob.id, false)
    }
    showToast('任务已取消')
  } catch (error) {
    console.error('取消任务失败', error)
    showToast(error instanceof Error ? error.message : '取消任务失败')
  }
}

async function reprocessJob(job: LocalizationJob): Promise<void> {
  try {
    const response = await fetch(`/api/v1/jobs/${job.id}/reprocess`, {
      method: 'POST',
      headers: { Accept: 'application/json' },
    })
    if (!response.ok) {
      const payload = await response.json().catch(() => null) as { detail?: string } | null
      throw new Error(payload?.detail ?? `重新处理失败（${response.status}）`)
    }
    const updatedJob = mapApiJob(await response.json() as ApiLocalizationJob)
    jobs.value = jobs.value.map((item) => item.id === updatedJob.id ? updatedJob : item)
    if (activeJobDetails.value?.job.id === updatedJob.id) {
      void loadJobDetails(updatedJob.id, false)
    }
    showToast('已复用服务器保留的原 PDF，按当前流水线重新排队')
  } catch (error) {
    console.error('重新处理任务失败', error)
    showToast(error instanceof Error ? error.message : '重新处理失败')
  }
}

async function downloadJob(job: LocalizationJob): Promise<void> {
  if (!job.resultDownloadable) return showToast('该任务没有可下载产物')
  try {
    const response = await fetch(`/api/v1/jobs/${job.id}/download`)
    if (!response.ok) {
      const payload = await response.json().catch(() => null) as { detail?: string } | null
      throw new Error(payload?.detail ?? `下载失败（${response.status}）`)
    }
    const blob = await response.blob()
    if (blob.type !== 'application/pdf' || blob.size === 0) {
      throw new Error('服务端未返回有效 PDF')
    }
    const objectUrl = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = objectUrl
    const defaultFileName = job.resultFileName ?? `${job.fileName.replace(/\.pdf$/i, '')}_本地化.pdf`
    anchor.download = defaultFileName
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
    showToast(`已下载真实结果：${anchor.download}`)
  } catch (error) {
    console.error('下载结果失败', error)
    showToast(error instanceof Error ? error.message : '下载结果失败')
  }
}

function mapApiGlossaryTerm(term: ApiGlossaryTerm): GlossaryTerm {
  return {
    id: term.id,
    source: term.source_text,
    target: term.target_text,
    category: term.category,
    sourceType: term.source,
    sourceJob: term.source_job_id ?? undefined,
  }
}

async function loadGlossaryTerms(): Promise<void> {
  try {
    const response = await fetch('/api/v1/glossary', {
      headers: { Accept: 'application/json' },
    })
    if (!response.ok) throw new Error(`术语接口返回 ${response.status}`)
    const payload = await response.json() as ApiGlossaryTerm[]
    glossaryTerms.value = payload.map(mapApiGlossaryTerm)
  } catch (error) {
    console.error('加载术语库失败', error)
    showToast('术语库加载失败，请检查后端服务')
  }
}

async function migrateLegacyGlossaryTerms(): Promise<void> {
  // 将旧版浏览器术语一次性迁移到服务端正式术语库。

  const storedGlossary = window.localStorage.getItem(GLOSSARY_STORAGE_KEY)
  if (!storedGlossary) return
  try {
    const legacyTerms = JSON.parse(storedGlossary) as GlossaryTerm[]
    if (Array.isArray(legacyTerms)) {
      for (const term of legacyTerms) {
        if (!term.source?.trim() || !term.target?.trim()) continue
        await fetch('/api/v1/glossary', {
          method: 'POST',
          headers: {
            Accept: 'application/json',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            source_text: term.source.trim(),
            target_text: term.target.trim(),
            category: term.category?.trim() || '专业术语',
          }),
        })
      }
    }
    window.localStorage.removeItem(GLOSSARY_STORAGE_KEY)
  } catch (error) {
    console.error('迁移旧版术语失败', error)
  }
}

async function addGlossaryTerm(): Promise<void> {
  const source = newGlossaryTerm.source.trim()
  const target = newGlossaryTerm.target.trim()
  if (!source || !target) return showToast('请填写术语原文和标准译文')
  if (glossaryTerms.value.some((term) => term.source.toLocaleLowerCase() === source.toLocaleLowerCase())) {
    return showToast('该术语已存在，请直接编辑现有条目')
  }
  try {
    const response = await fetch('/api/v1/glossary', {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        source_text: source,
        target_text: target,
        category: newGlossaryTerm.category.trim() || '专业术语',
      }),
    })
    if (!response.ok) throw new Error(`术语接口返回 ${response.status}`)
    const savedTerm = mapApiGlossaryTerm(await response.json() as ApiGlossaryTerm)
    glossaryTerms.value = [
      savedTerm,
      ...glossaryTerms.value.filter((term) => term.id !== savedTerm.id),
    ]
    newGlossaryTerm.source = ''
    newGlossaryTerm.target = ''
    newGlossaryTerm.category = '专业术语'
    showToast('术语已写入服务端，后续任务会冻结快照')
  } catch (error) {
    console.error('新增术语失败', error)
    showToast('术语写入失败')
  }
}

async function removeGlossaryTerm(term: GlossaryTerm): Promise<void> {
  try {
    const response = await fetch(`/api/v1/glossary/${term.id}`, {
      method: 'DELETE',
    })
    if (!response.ok) throw new Error(`删除术语失败（${response.status}）`)
    glossaryTerms.value = glossaryTerms.value.filter((item) => item.id !== term.id)
    showToast(`已删除术语：${term.source}`)
  } catch (error) {
    console.error('删除术语失败', error)
    showToast('术语删除失败')
  }
}

function loadPersistedData(): void {
  try {
    window.localStorage.removeItem('docweave.connection-settings')
    window.sessionStorage.removeItem('docweave.llm-api-key')
  } catch (error) {
    console.error('清理旧版浏览器连接配置失败', error)
  }
}

onMounted(() => {
  syncNavigationFromHash()
  if (!window.location.hash) window.history.replaceState(null, '', '#/workspace')
  loadPersistedData()
  void loadJobs()
  void migrateLegacyGlossaryTerms().then(loadGlossaryTerms)
  pollingTimer = window.setInterval(() => {
    if (jobs.value.some((job) => activeJobStatuses.includes(job.status))) {
      void loadJobs()
    }
    if (
      activeJobDetails.value
      && activeJobStatuses.includes(activeJobDetails.value.job.status)
    ) {
      void loadJobDetails(activeJobDetails.value.job.id, false)
    }
  }, 3000)
  window.addEventListener('hashchange', syncNavigationFromHash)
})

onBeforeUnmount(() => {
  if (pollingTimer) window.clearInterval(pollingTimer)
  if (toastTimer) window.clearTimeout(toastTimer)
  window.removeEventListener('hashchange', syncNavigationFromHash)
})
</script>

<template>
  <main class="app-shell">
    <aside class="sidebar">
      <div class="brand"><span class="brand-mark">⌁</span><span>DocWeave</span></div>
      <nav aria-label="主导航">
        <button
          v-for="item in navigationItems"
          :key="item.key"
          class="navigation-item"
          :class="{ active: activeNavigation === item.key }"
          :aria-current="activeNavigation === item.key ? 'page' : undefined"
          :data-testid="`nav-${item.key}`"
          type="button"
          @click="navigateTo(item.key)"
        >
          <span aria-hidden="true">{{ item.icon }}</span>{{ item.label }}
        </button>
      </nav>
      <div class="sidebar-footer">PDF 本地化工作流<br><span>v0.8.4 · 图表保护、策略记忆隔离与几何质检</span></div>
    </aside>

    <section class="workspace">
      <header class="topbar">
        <label class="search" :class="{ disabled: activeNavigation === 'settings' }">
          <span>⌕</span>
          <input v-model="searchQuery" :disabled="activeNavigation === 'settings'" :placeholder="searchPlaceholder" />
        </label>
      </header>

      <div class="content-grid" :class="{ 'single-column': activeNavigation !== 'workspace' }">
        <section class="main-content">
          <div class="page-heading">
            <div>
              <h1 data-testid="page-title">{{ activePageMeta.title }}</h1>
              <p>{{ activePageMeta.description }}</p>
            </div>
            <div v-if="activeNavigation === 'workspace' || activeNavigation === 'library'" class="completion">
              已完成 <strong>{{ completedJobs }}</strong> 份文档
            </div>
          </div>

          <template v-if="activeNavigation === 'workspace'">
            <section class="upload-panel" :class="{ dragging: isDragging, selected: selectedFiles.length > 0 }" @dragenter.prevent="isDragging = true" @dragover.prevent="isDragging = true" @dragleave.prevent="isDragging = false" @drop.prevent="handleDrop">
              <input ref="fileInput" class="file-input" type="file" accept="application/pdf,.pdf" multiple @change="handleFileChange" />
              <div class="pdf-icon">PDF</div>
              <template v-if="selectedFiles.length > 0">
                <h2 data-testid="selected-file-count">已选择 {{ selectedFiles.length }} 个 PDF</h2>
                <p>合计 {{ formatFileSize(selectedFilesSize) }}，将使用同一组任务规则并发处理。</p>
                <div class="batch-file-list" data-testid="batch-file-list">
                  <div v-for="file in selectedFiles" :key="getFileKey(file)" class="batch-file-item">
                    <span><strong>{{ file.name }}</strong><small>{{ formatFileSize(file.size) }}</small></span>
                    <button type="button" :disabled="jobSubmitting" @click="removeSelectedFile(file)">移除</button>
                  </div>
                </div>
                <div class="batch-file-actions">
                  <small>最多 {{ BATCH_UPLOAD_CONCURRENCY }} 个文件并发上传和执行，单批最多 {{ MAX_BATCH_FILES }} 个文件。</small>
                  <button class="text-button" type="button" :disabled="jobSubmitting" @click="clearSelectedFiles">清空所选</button>
                </div>
              </template>
              <template v-else><h2>拖拽一个或多个 PDF 到此处，或 <button type="button" class="text-button" @click="pickFile">批量选择文件</button></h2><p>支持原生文字 PDF 与扫描件，单个文件不超过 200 MB。</p></template>
            </section>

            <section class="translation-config" aria-labelledby="translation-config-title">
              <div class="config-heading"><div><h2 id="translation-config-title">语言、模型与翻译规则</h2><p>模型和术语学习策略随当前任务保存，不影响其他任务。</p></div><strong>{{ languageLabel }}</strong></div>
              <div class="language-fields">
                <label><span>输入语言</span><select v-model="sourceLanguage" aria-label="输入语言"><option v-for="item in languageOptions" :key="item.value" :value="item.value">{{ item.label }}</option></select></label>
                <span class="language-arrow" aria-hidden="true">→</span>
                <label><span>输出语言</span><select v-model="targetLanguage" aria-label="输出语言"><option v-for="item in targetLanguageOptions" :key="item.value" :value="item.value">{{ item.label }}</option></select></label>
                <label><span>处理策略</span><select v-model="selectedStrategy" aria-label="处理策略"><option>智能路由</option><option>原生文字优先</option><option>MinerU 结构解析</option></select></label>
              </div>
              <div class="execution-fields">
                <label><span>翻译模型</span><select v-model="selectedModel" data-testid="task-model" aria-label="翻译模型"><option v-for="model in modelOptions" :key="model" :value="model">{{ model }}</option></select><small>从当前服务支持的 GPT、Gemini 和 Claude 模型中选择，模型随本任务保存。</small></label>
                <label><span>术语学习</span><select v-model="glossaryLearningMode" data-testid="glossary-learning-mode" aria-label="术语学习"><option v-for="item in glossaryLearningOptions" :key="item.value" :value="item.value">{{ item.label }}</option></select><small>{{ glossaryLearningOptions.find((item) => item.value === glossaryLearningMode)?.description }}</small></label>
              </div>
              <div class="policy-options">
                <label><input v-model="translateAllTranslatableText" type="checkbox" /><span><strong>全文翻译</strong>正文、表格、图表标题、标签、页眉页脚与注释均翻译</span></label>
                <label><input v-model="preserveProperNouns" type="checkbox" /><span><strong>保留专有名词</strong>公司、品牌、人名、地名、组织等不强制翻译</span></label>
                <label><input v-model="preserveGlossaryTerms" type="checkbox" /><span><strong>保留术语库命中项</strong>术语以术语库或本任务清单为准</span></label>
                <label><input v-model="preserveModelsAndStandards" type="checkbox" /><span><strong>保留型号与标准</strong>产品型号、化学式、标准号、数值和单位不改写</span></label>
              </div>
              <label class="protected-terms"><span>本任务保留词（可选）</span><input v-model="protectedTermsInput" placeholder="例如：FCCL，Coverlay，NexFlex，IPC-4204" /><small>使用逗号或换行分隔。此处仅填写需要保持原文的专名或术语。</small></label>
              <div class="rule-summary"><strong>执行原则：</strong>除已确认的保留项外，不因原文是英文、日文或其他语言而跳过翻译；尽量消除正文和图表中的外语自然语言残留。</div>
              <div class="upload-actions"><button class="primary-button" data-testid="start-job" type="button" :disabled="jobSubmitting || selectedFiles.length === 0" @click="startJobs">{{ submitButtonLabel }}</button></div>
            </section>

            <section class="task-section" aria-labelledby="task-title">
              <div class="section-heading"><div><h2 id="task-title">任务列表</h2><p>仅显示后端已持久化的真实上传任务和处理状态。</p></div><button class="secondary-button" data-testid="refresh-jobs" type="button" :disabled="jobsLoading" @click="loadJobs(true)">↻ {{ jobsLoading ? '刷新中' : '刷新' }}</button></div>
              <div class="table-wrap"><table><thead><tr><th>文件名与语言</th><th>处理策略</th><th>状态</th><th>进度</th><th>创建时间</th><th>操作</th></tr></thead><tbody>
                <tr v-for="job in filteredJobs" :key="job.id">
                  <td><div class="file-name"><span class="file-badge">PDF</span><div><strong>{{ job.fileName }}</strong><small>{{ job.fileSize }} · {{ job.languageLabel }}</small></div></div></td>
                  <td><div class="runtime-cell"><strong>{{ job.strategy }}</strong><small>{{ job.model }} · {{ getGlossaryLearningLabel(job.policy.glossaryLearningMode) }}</small></div></td>
                  <td><span class="status" :class="getJobStatusClass(job)" :title="getJobStatusTitle(job)"><i></i>{{ getJobStatusLabel(job) }}</span></td>
                  <td><div class="progress-cell"><span>{{ job.progress }}%</span><div class="progress-track"><i :style="{ width: `${job.progress}%` }"></i></div></div></td>
                  <td>{{ job.createdAt }}</td>
                  <td class="actions">
                    <button data-testid="view-job-details" type="button" @click="viewJobDetails(job)">查看详情</button>
                    <button v-if="job.resultDownloadable" data-testid="download-job" type="button" @click="downloadJob(job)">下载结果</button>
                    <button v-if="canReprocessJob(job)" data-testid="reprocess-job" type="button" @click="reprocessJob(job)">{{ job.resultOutdated ? '按新版重做' : '重新处理' }}</button>
                    <button v-if="activeJobStatuses.includes(job.status)" type="button" class="danger" @click="cancelJob(job)">取消</button>
                  </td>
                </tr>
                <tr v-if="filteredJobs.length === 0"><td colspan="6"><div class="empty-state">没有匹配的任务</div></td></tr>
              </tbody></table></div>
            </section>
          </template>

          <template v-else-if="activeNavigation === 'library'">
            <section class="summary-grid" aria-label="文档统计">
              <article><span>全部任务</span><strong>{{ jobs.length }}</strong><small>包含处理中与已完成文档</small></article>
              <article><span>处理中</span><strong>{{ jobs.filter((job) => activeJobStatuses.includes(job.status)).length }}</strong><small>正在排队、解析、翻译、复原或自动质检</small></article>
              <article><span>已完成</span><strong>{{ completedJobs }}</strong><small>具有真实文件的任务可下载结果</small></article>
            </section>
            <section class="library-panel">
              <div class="section-heading"><div><h2>全部文档</h2><p>顶部搜索框可按文件名、语言和处理策略筛选。</p></div><button class="primary-button" type="button" @click="navigateTo('workspace')">+ 新建本地化任务</button></div>
              <div class="table-wrap library-table"><table><thead><tr><th>文件名与语言</th><th>处理策略</th><th>状态</th><th>进度</th><th>创建时间</th><th>操作</th></tr></thead><tbody>
                <tr v-for="job in filteredJobs" :key="job.id">
                  <td><div class="file-name"><span class="file-badge">PDF</span><div><strong>{{ job.fileName }}</strong><small>{{ job.fileSize }} · {{ job.languageLabel }}</small></div></div></td>
                  <td><div class="runtime-cell"><strong>{{ job.strategy }}</strong><small>{{ job.model }} · {{ getGlossaryLearningLabel(job.policy.glossaryLearningMode) }}</small></div></td>
                  <td><span class="status" :class="getJobStatusClass(job)" :title="getJobStatusTitle(job)"><i></i>{{ getJobStatusLabel(job) }}</span></td>
                  <td><div class="progress-cell"><span>{{ job.progress }}%</span><div class="progress-track"><i :style="{ width: `${job.progress}%` }"></i></div></div></td>
                  <td>{{ job.createdAt }}</td>
                  <td class="actions">
                    <button data-testid="view-job-details" type="button" @click="viewJobDetails(job)">查看详情</button>
                    <button v-if="job.resultDownloadable" data-testid="download-job" type="button" @click="downloadJob(job)">下载结果</button>
                    <button v-if="canReprocessJob(job)" data-testid="reprocess-job" type="button" @click="reprocessJob(job)">{{ job.resultOutdated ? '按新版重做' : '重新处理' }}</button>
                    <button v-if="activeJobStatuses.includes(job.status)" type="button" class="danger" @click="cancelJob(job)">取消</button>
                  </td>
                </tr>
                <tr v-if="filteredJobs.length === 0"><td colspan="6"><div class="empty-state">没有匹配的文档</div></td></tr>
              </tbody></table></div>
            </section>
          </template>

          <template v-else-if="activeNavigation === 'glossary'">
            <section class="learning-overview" aria-labelledby="learning-overview-title">
              <span class="learning-icon" aria-hidden="true">AI</span>
              <div><h2 id="learning-overview-title">任务自动学习</h2><p>解析和翻译完成后，从正文、表格与图表中提取专业术语；学习方式由每个任务单独选择。</p></div>
              <div class="learning-modes"><span><strong>审核录入</strong>先生成候选，人工确认后进入正式术语库</span><span><strong>自动录入</strong>高置信度候选去重后直接写入术语库</span></div>
            </section>
            <section class="glossary-editor" aria-labelledby="glossary-editor-title">
              <div class="section-heading"><div><h2 id="glossary-editor-title">手动添加术语</h2><p>标准译文可与原文相同，用于明确要求保留原词。</p></div></div>
              <form class="glossary-form" @submit.prevent="addGlossaryTerm">
                <label><span>术语原文</span><input v-model="newGlossaryTerm.source" placeholder="例如：FCCL" /></label>
                <label><span>标准译文</span><input v-model="newGlossaryTerm.target" placeholder="例如：FCCL" /></label>
                <label><span>分类</span><input v-model="newGlossaryTerm.category" placeholder="例如：材料" /></label>
                <button class="primary-button" type="submit">添加术语</button>
              </form>
            </section>
            <section class="glossary-list">
              <div class="section-heading"><div><h2>术语列表</h2><p>共 {{ glossaryTerms.length }} 条，顶部搜索框可快速筛选。</p></div></div>
              <div class="table-wrap glossary-table"><table><thead><tr><th>术语原文</th><th>标准译文</th><th>分类</th><th>来源</th><th>操作</th></tr></thead><tbody>
                <tr v-for="term in filteredGlossaryTerms" :key="term.id"><td><strong>{{ term.source }}</strong></td><td>{{ term.target }}</td><td><span class="category-tag">{{ term.category }}</span></td><td><span class="source-tag" :class="term.sourceType">{{ term.sourceType === 'task' ? `任务学习 · ${term.sourceJob ?? '未知任务'}` : '手动录入' }}</span></td><td class="actions"><button class="danger" type="button" @click="removeGlossaryTerm(term)">删除</button></td></tr>
                <tr v-if="filteredGlossaryTerms.length === 0"><td colspan="5"><div class="empty-state">没有匹配的术语</div></td></tr>
              </tbody></table></div>
            </section>
          </template>

          <template v-else>
            <div class="settings-form" data-testid="settings-form">
              <section class="settings-card" aria-labelledby="mineru-settings-title">
                <div class="settings-card-heading">
                  <span class="service-icon mineru-service">M</span>
                  <div><h2 id="mineru-settings-title">MinerU 解析服务</h2><p>通过后端环境变量 <code>MINERU_BASE_URL</code>，或 <code>MINERU_HOST</code> 与 <code>MINERU_PORT</code> 配置。</p></div>
                  <span class="connection-state">后端配置</span>
                </div>
                <p class="deployment-note">默认连接本机解析服务。部署到其他环境时，请在服务器的 <code>.env</code> 中设置地址；不要把内网地址或访问凭据提交到仓库。</p>
              </section>

              <section class="settings-card" aria-labelledby="llm-settings-title">
                <div class="settings-card-heading">
                  <span class="service-icon llm-service">AI</span>
                  <div><h2 id="llm-settings-title">通用大模型 API</h2><p>通过后端环境变量 <code>LLM_BASE_URL</code> 与 <code>LLM_API_KEY</code> 配置，具体模型仍由每个任务选择。</p></div>
                  <span class="connection-state">后端配置</span>
                </div>
                <p class="deployment-note">API Key 不会写入浏览器存储或任务数据。生产部署应使用 HTTPS，并通过密钥管理服务或仅服务器可读的环境配置注入凭据。</p>
              </section>
            </div>
          </template>
        </section>

        <aside v-if="activeNavigation === 'workspace'" class="strategy-rail" aria-label="处理规则说明">
          <div><h2>处理规则</h2><p>语言规则先于解析与渲染步骤执行，并随任务保存。</p></div>
          <article class="strategy-card full-translation"><span class="strategy-icon">中</span><h3>全文中文化</h3><p>默认面向全部源语言，以简体中文作为输出。</p><ul><li>正文、表格与注释</li><li>图表标题与标签</li><li>页眉、页脚与图注</li></ul></article>
          <article class="strategy-card native"><span class="strategy-icon">名</span><h3>受控保留</h3><p>只保留经规则确认的专名、术语、型号和标准。</p><ul><li>术语库命中项</li><li>自定义保留词</li><li>单位、数值与化学式</li></ul></article>
          <article class="strategy-card mineru"><span class="strategy-icon">▱</span><h3>结构解析</h3><p>复杂版式与扫描件使用 MinerU，提升表格和图文区域识别。</p><ul><li>版面检测与重建</li><li>表格与图表识别</li><li>按区域回写译文</li></ul></article>
          <article class="strategy-card visual"><span class="strategy-icon">✓</span><h3>外语残留质检</h3><p>渲染后扫描正文与图表的可翻译外语，作为复检重点。</p><ul><li>语言残留检查</li><li>文本溢出检查</li><li>术语与数字校验</li></ul></article>
        </aside>
      </div>
    </section>

    <div v-if="activeJobDetails || detailsLoading" class="modal-backdrop" data-testid="job-detail-backdrop" @click.self="closeJobDetails">
      <section class="job-detail-modal" role="dialog" aria-modal="true" aria-labelledby="job-detail-title" data-testid="job-detail-dialog">
        <button class="close-button" type="button" aria-label="关闭任务详情" @click="closeJobDetails">×</button>
        <div v-if="detailsLoading && !activeJobDetails" class="detail-loading">正在读取真实运行报告…</div>
        <template v-else-if="activeJobDetails">
          <header class="job-detail-header">
            <div>
              <span class="detail-kicker">任务运行报告</span>
              <h2 id="job-detail-title">{{ activeJobDetails.job.fileName }}</h2>
              <p>{{ activeJobDetails.job.model }} · {{ activeJobDetails.job.strategy }} · {{ activeJobDetails.job.languageLabel }}</p>
            </div>
            <span class="status detail-status" :class="getJobStatusClass(activeJobDetails.job)"><i></i>{{ getJobStatusLabel(activeJobDetails.job) }}</span>
          </header>

          <div class="job-detail-scroll">
            <section class="detail-outcome" :class="getJobStatusClass(activeJobDetails.job)">
              <strong>{{ activeJobDetails.job.resultOutdated ? '旧版产物已隔离' : activeJobDetails.job.status === 'completed' || activeJobDetails.job.status === 'needs_review' ? '交付完成' : activeJobDetails.job.status === 'failed' ? '执行失败' : getStatusLabel(activeJobDetails.job.status) }}</strong>
              <span>{{ getOutcomeMessage(activeJobDetails) }}</span>
            </section>

            <section class="metrics-grid" aria-label="资源消耗">
              <article><span>总耗时</span><strong>{{ formatDuration(activeJobDetails.metrics.duration_ms) }}</strong><small>从实际开始执行到结束</small></article>
              <article><span>MinerU 解析</span><strong>{{ formatDuration(activeJobDetails.metrics.mineru_duration_ms) }}</strong><small>文档结构提取耗时</small></article>
              <article><span>大模型调用</span><strong>{{ formatDuration(activeJobDetails.metrics.llm_duration_ms) }}</strong><small>正文与文件名翻译耗时</small></article>
              <article><span>PDF 生成</span><strong>{{ formatDuration(activeJobDetails.metrics.render_duration_ms) }}</strong><small>结果文档渲染耗时</small></article>
              <article><span>全页质检</span><strong>{{ formatDuration(activeJobDetails.metrics.validation_duration_ms) }}</strong><small>{{ activeJobDetails.metrics.quality_issue_count }} 条质量检查记录</small></article>
              <article><span>翻译批次</span><strong>{{ activeJobDetails.metrics.translation_batch_count }}</strong><small>断点恢复 {{ activeJobDetails.metrics.resumed_translation_batch_count }} 批</small></article>
              <article><span>翻译记忆</span><strong>{{ activeJobDetails.metrics.translation_memory_hit_count }}</strong><small>精确命中并跳过模型请求</small></article>
              <article class="token-metric">
                <span>Token 合计</span>
                <strong>{{ activeJobDetails.metrics.token_usage_available ? formatTokenCount(activeJobDetails.metrics.total_tokens) : '—' }}</strong>
                <small v-if="activeJobDetails.metrics.token_usage_available">输入 {{ formatTokenCount(activeJobDetails.metrics.input_tokens) }} · 输出 {{ formatTokenCount(activeJobDetails.metrics.output_tokens) }}</small>
                <small v-else>模型服务未返回 usage 字段</small>
              </article>
              <article><span>结果文件</span><strong>{{ activeJobDetails.metrics.result_file_size !== null ? formatFileSize(activeJobDetails.metrics.result_file_size) : '—' }}</strong><small>{{ activeJobDetails.job.resultFileName ?? '尚未生成' }}</small></article>
            </section>

            <section v-if="activeJobDetails.qualityIssues.length" class="runtime-log-section" aria-labelledby="quality-issue-title">
              <div class="detail-section-heading">
                <div><h3 id="quality-issue-title">质量检查与优化建议</h3><p>严格检测记录可用于人工抽检和持续优化；结果文件已经生成，可正常预览和下载。</p></div>
              </div>
              <ol class="runtime-log-list quality-issue-list">
                <li v-for="group in qualityIssueGroups" :key="group.key" class="warning">
                  <i></i>
                  <div>
                    <div class="log-meta">
                      <strong>{{ group.label }} × {{ group.issues.length }}</strong>
                      <span>{{ formatIssuePages(group.pages) }}</span>
                      <em>{{ group.severity === 'error' ? '建议重点复核' : '保守提示' }}</em>
                    </div>
                    <p>{{ getQualityIssueDescription() }}</p>
                    <small v-if="group.sampleBlockIds.length">示例坐标块：{{ group.sampleBlockIds.join('、') }}</small>
                    <details class="issue-details">
                      <summary>查看 {{ group.issues.length }} 条定位明细</summary>
                      <ul>
                        <li v-for="issue in group.issues" :key="issue.id">
                          <span v-if="issue.page_index !== null">第 {{ issue.page_index + 1 }} 页</span>
                          <code v-if="issue.block_id">{{ issue.block_id }}</code>
                        </li>
                      </ul>
                    </details>
                  </div>
                </li>
              </ol>
            </section>

            <section class="runtime-log-section" aria-labelledby="runtime-log-title">
              <div class="detail-section-heading">
                <div><h3 id="runtime-log-title">本次运行日志</h3><p>本次 {{ currentRunLogs.length }} 条；历史运行 {{ historicalRunLogs.length }} 条，运行中每 3 秒刷新。</p></div>
                <span v-if="detailsLoading">刷新中…</span>
              </div>
              <ol class="runtime-log-list" data-testid="runtime-log-list">
                <li v-for="entry in currentRunLogs" :key="entry.id" :class="entry.level">
                  <i></i>
                  <div>
                    <div class="log-meta"><strong>{{ getStageLabel(entry.stage) }}</strong><span>{{ entry.createdAt }}</span><em v-if="entry.progress !== null">{{ entry.progress }}%</em></div>
                    <p>{{ getCustomerLogMessage(entry) }}</p>
                  </div>
                </li>
              </ol>
              <details v-if="historicalRunLogs.length" class="historical-logs">
                <summary>展开历史运行日志（{{ historicalRunLogs.length }} 条）</summary>
                <ol class="runtime-log-list">
                  <li v-for="entry in historicalRunLogs" :key="entry.id" :class="entry.level">
                    <i></i>
                    <div>
                      <div class="log-meta"><strong>{{ getStageLabel(entry.stage) }}</strong><span>{{ entry.createdAt }}</span><em v-if="entry.progress !== null">{{ entry.progress }}%</em></div>
                      <p>{{ getCustomerLogMessage(entry) }}</p>
                    </div>
                  </li>
                </ol>
              </details>
            </section>
          </div>

          <footer class="job-detail-actions">
            <button class="secondary-button" type="button" @click="closeJobDetails">关闭</button>
            <button v-if="activeJobDetails.job.resultDownloadable" class="primary-button" data-testid="download-job-details" type="button" @click="downloadJob(activeJobDetails.job)">下载结果</button>
            <button v-if="canReprocessJob(activeJobDetails.job)" class="primary-button" data-testid="reprocess-job-details" type="button" @click="reprocessJob(activeJobDetails.job)">{{ activeJobDetails.job.resultOutdated ? '按新版重做' : '重新处理' }}</button>
          </footer>
        </template>
      </section>
    </div>

    <div v-if="toastMessage" class="toast" role="status">{{ toastMessage }}</div>
  </main>
</template>
