import axios from 'axios'
import apiClient from './client'

export type ReviewStatus = 'unreviewed' | 'supported' | 'partially_supported' | 'unsupported' | 'insufficient'
export interface ResearchBrief {
  revision: number
  confirmed_at: string
  content: { question: string; available_resources: string; constraints: string; population_or_object: string; validation_goal: string }
}
export interface ResearchSource {
  id: string; title: string; authors: string[]; year: number | null; official_url: string; doi: string | null
  bibliography_status: string; access_level: string; page_count: number; pdf_url: string | null; integrity_error: string | null
  pages: { page_number: number; text: string; text_status: string }[]
  abstract: string; discovery_provider?: string; open_access_pdf_url?: string | null; license?: string | null
}
export interface EvidenceCard {
  id: string; study_revision: number; quote_match: boolean; validation_note: string
  review_status: ReviewStatus; review_note: string; eligible: boolean; page_text: string; source_title: string; pdf_url: string | null; integrity_error: string | null
  content: { claim: string; source_id: string; page_number: number; quote: string; conditions: string; limitations: string; ai_relation: string }
}
export interface Candidate {
  candidate_id: string; method: string; evidence_ids: string[]; conditions: string; feasibility: string; minimal_validation: string; objections: string
}
export interface CandidateRun {
  id: string; study_revision: number; candidates: Candidate[]; created_at: string; sms_case: boolean
}
export interface Decision {
  id: string; study_revision: number; chosen_candidate: string; reason: string; evidence_ids: string[]
  experiment_run_id: string | null; status: string; invalidated_at: string | null; created_at: string; candidate_run_id: string
  effective_status: 'final' | 'draft'; gate_reasons: string[]
}
export interface Aggregate {
  candidate: string; budget: number; metrics: Record<string, { mean: number; sample_std: number; n: number }>
  timing: Record<string, { mean: number; sample_std: number; n: number }>; spam_support: number; ham_support: number
}
export interface Experiment {
  run_id: string; status: string; created_at?: string; recommended_candidate: string | null
  validation_aggregates: Aggregate[]; test_aggregates: Aggregate[]; limitations?: string[]; integrity_error?: string
  frozen_selection?: { chosen_candidate: string; reason: string; study_revision: number }
}
export interface ResearchState {
  notebook_id: string; brief: ResearchBrief | null; sources: ResearchSource[]; cards: EvidenceCard[]
  candidate_runs: CandidateRun[]; decisions: Decision[]; experiments: Experiment[]; sms_case: boolean
  model: { backend: string; model: string; configured: boolean }
  experiment_preparation: { prepared: boolean; audited: boolean; manifest: Record<string, unknown> | null; error: string | null }
  discovery: { plans: DiscoveryPlan[]; runs: DiscoveryRun[] }
  paper_work: { reading_reports: ReadingReport[]; writing_drafts: WritingDraft[] }
  web_research?: { runs: WebResearchRun[] }
  model_runs: ResearchModelRun[]
}
export interface LocalModelStatus { available: boolean; configured: boolean; model: string; embedding_ready: boolean }
export interface ResearchExport { status: 'formal_decision_available' | 'incomplete'; formal_decision_count: number; files: { name: string; url: string }[]; blocked: string[] }

export type DiscoveryProvider = 'openalex' | 'crossref'
export interface DiscoveryPlan {
  id: string; study_revision: number; queries: string[]; scope: string; inclusion_criteria: string[]; created_at: string
}
export interface DiscoveryResult {
  index: number; title: string; authors: string[]; year: number | null; doi: string | null; official_url: string
  abstract: string; pdf_url: string | null; license: string | null; remote_id: string
  access_level: 'metadata' | 'abstract'; source_id: string | null; recommendation: string
}
export interface DiscoveryRun {
  id: string; study_revision: number; provider: DiscoveryProvider; query: string
  status: 'success' | 'error' | 'pending'; message: string; results: DiscoveryResult[]; created_at: string
}
export interface PaperCoverage {
  source_id: string; title: string; level: 'fulltext' | 'abstract'; pages: number[]; total_pages: number; characters: number
}
export interface PaperReference {
  number: number; source_id: string; title: string; authors: string[]; year: number | null; doi: string | null
  official_url: string; access_level: 'fulltext' | 'abstract' | 'metadata'; pages: number[]; bibliography_status: string
}
export interface ReadingReport {
  id: string; study_revision: number; question: string; answer: string
  findings: { text: string; source_id: string; page_number: number | null; quote: string }[]
  limitations: string[]; coverage: PaperCoverage[]; model_run_id: string; created_at: string
}
export interface WritingDraft {
  id: string; study_revision: number; kind: 'outline' | 'draft' | 'revision' | 'manual'; title: string
  markdown: string; references: PaperReference[]; coverage: PaperCoverage[]
  model_run_id: string | null; parent_id: string | null; created_at: string
}
export interface SourceImportResult { source_id: string; status: string; message: string }
export interface ResearchModelRun {
  id: string; study_revision: number | null; task: string; backend: string; requested_model: string
  actual_model: string | null; called_at: string; prompt_version: string; status: string
  error_type: string | null; input_refs: Record<string, unknown>
}

export interface WebResearchRun {
  id: string; study_revision: number; question: string; status: 'running' | 'success' | 'partial' | 'error' | 'stale'
  created_at: string; completed_at: string | null; provider: string
  queries: { query: string; status: 'success' | 'error'; message: string; searched_at: string; results: { url: string; title: string; snippet: string }[] }[]
  pages: {
    id: string; url: string; final_url: string; title: string; status: 'read' | 'error'; message: string; retrieved_at: string
    excerpt: string; prepared_characters: number; extracted_characters: number; truncated: boolean; content_sha256: string; excerpt_sha256: string
  }[]
  findings: { text: string; page_id: string; quote: string }[]
  limitations: string[]; message: string; model_run_ids: string[]
}

export function researchPath(id: string, suffix = '') {
  return `/competition/${encodeURIComponent(id)}${suffix}`
}
export const competitionApi = {
  create: async (question: string, resources: string) => (await apiClient.post<{ notebook_id: string }>('/competition/notebooks', { question, resources })).data,
  state: async (id: string) => (await apiClient.get<ResearchState>(researchPath(id))).data,
  post: async <T = unknown>(id: string, suffix: string, body: unknown = {}) => (await apiClient.post<T>(researchPath(id, suffix), body)).data,
  source: async (id: string, sourceId: string, body: unknown) => (await apiClient.put(researchPath(id, `/sources/${encodeURIComponent(sourceId)}`), body)).data,
  localStatus: async () => (await apiClient.get<LocalModelStatus>('/local-model/status')).data,
  setup: async () => (await apiClient.post('/local-model/setup', {})).data,
  discover: async (id: string, provider: DiscoveryProvider) => (await apiClient.post<ResearchState['discovery']>(researchPath(id, '/discovery/start'), { provider }, { timeout: 600000 })).data,
  search: async (id: string, query: string, provider: DiscoveryProvider) => (await apiClient.post<DiscoveryRun>(researchPath(id, '/discovery/search'), { query, provider })).data,
  rank: async (id: string, runId: string) => (await apiClient.post<DiscoveryRun>(researchPath(id, `/discovery/runs/${encodeURIComponent(runId)}/rank`), {}, { timeout: 600000 })).data,
  include: async (id: string, runId: string, index: number) => (await apiClient.post<SourceImportResult>(researchPath(id, `/discovery/runs/${encodeURIComponent(runId)}/include`), { index, fetch_fulltext: true }, { timeout: 600000 })).data,
  fetchFulltext: async (id: string, sourceId: string) => (await apiClient.post<SourceImportResult>(researchPath(id, `/sources/${encodeURIComponent(sourceId)}/fetch-fulltext`), {}, { timeout: 600000 })).data,
  read: async (id: string, body: { source_ids: string[]; question: string; rights_confirmed: boolean }) => (await apiClient.post<ReadingReport>(researchPath(id, '/reading'), body, { timeout: 600000 })).data,
  webResearch: async (id: string, body: { question: string; consent: boolean }) => (await apiClient.post<WebResearchRun>(researchPath(id, '/web-research'), body, { timeout: 600000 })).data,
  write: async (id: string, body: { kind: 'outline' | 'draft' | 'revision'; instructions: string; source_ids: string[]; rights_confirmed: boolean; parent_id?: string | null }) => (await apiClient.post<WritingDraft>(researchPath(id, '/writing'), body, { timeout: 600000 })).data,
  saveWriting: async (id: string, draftId: string, body: { markdown: string; title?: string }) => (await apiClient.put<WritingDraft>(researchPath(id, `/writing/${encodeURIComponent(draftId)}`), body)).data,
}

export function researchError(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail
    if (typeof detail === 'string') return detail
    if (error.code === 'ECONNABORTED') return '请求等待超时，服务端可能仍在处理。请先刷新状态，不要立即重复模型或联网请求。'
    if (error.response?.status === 422) return '填写内容未通过校验，请检查必填项、年份及网址。输入已保留。'
    if (error.response?.status === 409) return '研究版本或当前状态已变化。本次操作未应用，请刷新后检查当前版本再继续。'
    return '请求未完成，服务端可能仍在处理。先刷新研究状态，确认没有结果后再手动重试。输入已保留，不会自动重复调用模型。'
  }
  return error instanceof Error ? error.message : '操作未完成，输入已保留。'
}

// 文件也经过原有认证客户端，不将 token 放进链接或浏览器地址栏。
export async function researchBlob(url: string): Promise<Blob> {
  const path = url.startsWith('/api/') ? url.slice(4) : url
  if (!path.startsWith('/competition/')) throw new Error('文件地址不属于研究工作台。')
  const response = await apiClient.get<Blob>(path, { responseType: 'blob' })
  return response.data
}
export async function downloadResearchFile(url: string, name: string) {
  const blob = await researchBlob(url)
  const objectUrl = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = objectUrl
  link.download = name
  link.click()
  setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000)
}
