import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'
import type { DiscoveryRun, ReadingReport, ResearchSource, ResearchState, WritingDraft } from '@/lib/api/competition'
import { LiteratureDiscovery } from './LiteratureDiscovery'
import { PaperReading } from './PaperReading'
import { PaperWriting } from './PaperWriting'
import ResearchDetailPage from '@/app/(dashboard)/research/[id]/page'

const api = vi.hoisted(() => ({ state: vi.fn(), post: vi.fn(), discover: vi.fn(), search: vi.fn(), rank: vi.fn(), include: vi.fn(), read: vi.fn(), write: vi.fn(), saveWriting: vi.fn(), download: vi.fn() }))
vi.mock('next/navigation', () => ({ useParams: () => ({ id: 'notebook%3Atest' }) }))
vi.mock('@/components/layout/AppShell', () => ({ AppShell: ({ children }: { children: ReactNode }) => <>{children}</> }))
vi.mock('./ModelSetup', () => ({ ModelSetup: () => null }))
vi.mock('./ResearchSources', () => ({ ResearchSources: () => null }))
vi.mock('./EvidenceReview', () => ({ EvidenceReview: () => null }))
vi.mock('./ResearchDecisions', () => ({ ResearchDecisions: () => null }))
vi.mock('./ResearchExperiments', () => ({ ResearchExperiments: () => null }))
vi.mock('./ResearchExports', () => ({ ResearchExports: () => null }))
vi.mock('@/lib/api/competition', () => ({
  competitionApi: api,
  downloadResearchFile: api.download,
  researchPath: (id: string, suffix = '') => `/competition/${encodeURIComponent(id)}${suffix}`,
  researchError: (error: unknown) => error instanceof Error ? error.message : String(error),
}))

function source(id = 'source-1'): ResearchSource {
  return { id, title: `真实来源 ${id}`, authors: ['作者甲'], year: 2024, official_url: 'https://example.org/paper', doi: null, bibliography_status: 'unverified', access_level: 'abstract', page_count: 0, pdf_url: null, integrity_error: null, pages: [], abstract: '绿地与居民心理健康之间存在关联。' }
}
function state(): ResearchState {
  return {
    notebook_id: 'notebook:test', brief: { revision: 1, confirmed_at: '2026-01-01T00:00:00Z', content: { question: '城市绿地与健康', available_resources: '', constraints: '', population_or_object: '', validation_goal: '' } },
    sources: [], cards: [], candidate_runs: [], decisions: [], experiments: [], sms_case: false,
    model: { backend: 'local_account', model: 'test-only', configured: true },
    experiment_preparation: { prepared: false, audited: false, manifest: null, error: null },
    discovery: { plans: [], runs: [] }, paper_work: { reading_reports: [], writing_drafts: [] }, model_runs: [],
  }
}
function draft(): WritingDraft {
  return { id: 'draft-1', study_revision: 1, kind: 'draft', title: '绿地健康综述', markdown: '研究报告了关联 [1]。', references: [{ number: 1, source_id: 'source-1', title: '真实来源 source-1', authors: ['作者甲'], year: 2024, doi: null, official_url: 'https://example.org/paper', access_level: 'abstract', pages: [], bibliography_status: 'unverified' }], coverage: [{ source_id: 'source-1', title: '真实来源 source-1', level: 'abstract', pages: [], total_pages: 0, characters: 20 }], model_run_id: 'model-run-1', parent_id: null, created_at: '2026-01-01T01:00:00Z' }
}
function run(): DiscoveryRun {
  return { id: 'run-1', study_revision: 1, provider: 'openalex', query: 'urban green health', status: 'success', message: '', created_at: '2026-01-01T00:00:00Z', results: [{ index: 0, title: '真实检索结果', authors: ['作者甲'], year: 2024, doi: null, official_url: 'https://example.org/paper', abstract: '真实摘要', pdf_url: null, license: null, remote_id: 'remote-1', access_level: 'abstract', source_id: null, recommendation: '相关方法可供参考' }] }
}
function callbacks() { return { locked: false, onSaved: vi.fn().mockResolvedValue(undefined), onBusy: vi.fn() } }

beforeEach(() => {
  vi.clearAllMocks()
  window.sessionStorage.clear()
})
afterEach(() => vi.restoreAllMocks())

describe('真实文献发现交互', () => {
  it('没有PDF也可启动AI检索，处理中不重复提交，失败后回读并保留已成功查询', async () => {
    const { promise, reject } = Promise.withResolvers<unknown>()
    api.discover.mockReturnValueOnce(promise)
    const props = callbacks()
    const data = state()
    data.discovery.runs = [run()]
    render(<LiteratureDiscovery notebookId="notebook:test" state={data} ready {...props} />)
    const button = screen.getByRole('button', { name: /AI 制定策略并查找资料/ })
    fireEvent.click(button)
    fireEvent.click(button)
    expect(button).toBeDisabled()
    expect(api.discover).toHaveBeenCalledTimes(1)
    await act(async () => reject(new Error('模型相关性服务失败')))
    expect(await screen.findByRole('alert')).toHaveTextContent('模型相关性服务失败')
    expect(props.onSaved).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('link', { name: '真实检索结果' })).toBeInTheDocument()
    expect(button).toBeEnabled()
  })

  it('未配置模型仍可手动检索，切换数据库与编辑查询实际发送用户选择', async () => {
    api.search.mockResolvedValueOnce(run())
    const data = state()
    data.model.configured = false
    render(<LiteratureDiscovery notebookId="notebook:test" state={data} ready {...callbacks()} />)
    expect(screen.getByRole('button', { name: /AI 制定策略并查找资料/ })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('检索数据库'), { target: { value: 'crossref' } })
    fireEvent.change(screen.getByLabelText('编辑检索式'), { target: { value: '  mental health  ' } })
    fireEvent.click(screen.getByRole('button', { name: '仅联网检索（不调用 AI）' }))
    await waitFor(() => expect(api.search).toHaveBeenCalledWith('notebook:test', 'mental health', 'crossref'))
    expect(api.discover).not.toHaveBeenCalled()
  })

  it('真实结果一键纳入，不要求重抄书目，全文失败说明保留', async () => {
    api.include.mockResolvedValueOnce({ source_id: 'source-1', status: 'abstract', message: '已纳入摘要；没有开放全文位置' })
    const props = callbacks()
    const data = state()
    data.discovery.runs = [run()]
    const view = render(<LiteratureDiscovery notebookId="notebook:test" state={data} ready {...props} />)
    fireEvent.click(screen.getByRole('button', { name: '加入资料库并尝试开放全文' }))
    expect(await screen.findByText('已纳入摘要；没有开放全文位置')).toBeInTheDocument()
    expect(api.include).toHaveBeenCalledWith('notebook:test', 'run-1', 0)
    expect(props.onSaved).toHaveBeenCalledTimes(1)
    data.discovery.runs[0].results[0].source_id = 'source-1'
    view.rerender(<LiteratureDiscovery notebookId="notebook:test" state={data} ready {...props} />)
    expect(screen.getByRole('button', { name: '已加入资料库' })).toBeDisabled()
  })
  it('无命中与联网失败分别呈现，不把失败当作文献不存在', () => {
    const data = state()
    data.discovery.runs = [{ ...run(), results: [] }]
    const props = callbacks()
    const view = render(<LiteratureDiscovery notebookId="notebook:test" state={data} ready {...props} />)
    expect(screen.getByText(/本次查询没有命中/)).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    data.discovery.runs = [{ ...run(), status: 'error', message: 'OpenAlex 配额已用尽', results: [] }]
    view.rerender(<LiteratureDiscovery notebookId="notebook:test" state={data} ready {...props} />)
    expect(screen.getByRole('alert')).toHaveTextContent('OpenAlex 配额已用尽')
    expect(screen.queryByText(/本次查询没有命中/)).not.toBeInTheDocument()
  })
})

describe('无需书目人审的辅助阅读', () => {
  it('选择摘要并明确授权才能阅读，报告引用回到摘要而非伪造PDF页码', async () => {
    const report: ReadingReport = { id: 'read-1', study_revision: 1, question: '哪些条件影响结论？', answer: '需要关注适用条件。', findings: [{ text: '发现关联', source_id: 'source-1', page_number: null, quote: '绿地与居民心理健康之间存在关联。' }], limitations: ['摘要不含完整方法'], coverage: draft().coverage, model_run_id: 'model-run-1', created_at: '2026-01-01T01:00:00Z' }
    api.read.mockResolvedValueOnce(report)
    const data = state()
    data.sources = [source()]
    const props = callbacks()
    const view = render(<PaperReading notebookId="notebook:test" state={data} ready {...props} />)
    fireEvent.click(screen.getByRole('checkbox', { name: /真实来源 source-1/ }))
    fireEvent.change(screen.getByLabelText('你想从这些资料中了解什么？'), { target: { value: report.question } })
    const button = screen.getByRole('button', { name: /AI 阅读所选资料/ })
    expect(button).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox', { name: /我有权将所选资料/ }))
    expect(button).toBeEnabled()
    fireEvent.click(button)
    await waitFor(() => expect(props.onSaved).toHaveBeenCalledTimes(1))
    expect(api.read).toHaveBeenCalledWith('notebook:test', { source_ids: ['source-1'], question: report.question, rights_confirmed: true })
    data.paper_work.reading_reports = [report]
    view.rerender(<PaperReading notebookId="notebook:test" state={data} ready {...props} />)
    expect(screen.getByText(/摘要原句（无 PDF 页码）/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /打开原 PDF/ })).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('查看来源摘要'))
    expect(screen.getAllByText(source().abstract).length).toBeGreaterThan(0)
    data.brief!.revision = 2
    view.rerender(<PaperReading notebookId="notebook:test" state={data} ready {...props} />)
    expect(screen.getByText(/历史报告，不代表当前研究/)).toBeInTheDocument()
  })

  it('仅元数据不可阅读，最多八份资料且变更选择撤销授权', () => {
    const data = state()
    data.sources = Array.from({ length: 9 }, (_, index) => source(`source-${index}`))
    data.sources.push({ ...source('metadata'), abstract: '', access_level: 'metadata' })
    render(<PaperReading notebookId="notebook:test" state={data} ready {...callbacks()} />)
    expect(screen.getByRole('checkbox', { name: /真实来源 metadata/ })).toBeDisabled()
    for (let index = 0; index < 8; index++) fireEvent.click(screen.getByRole('checkbox', { name: new RegExp(`真实来源 source-${index} ·`) }))
    expect(screen.getByRole('checkbox', { name: /真实来源 source-8/ })).toBeDisabled()
    const rights = screen.getByRole('checkbox', { name: /我有权将所选资料/ })
    fireEvent.click(rights)
    expect(rights).toBeChecked()
    fireEvent.click(screen.getByRole('checkbox', { name: /真实来源 source-0/ }))
    expect(rights).not.toBeChecked()
    expect(screen.getByRole('checkbox', { name: /真实来源 source-8/ })).toBeEnabled()
  })
})

describe('论文辅助写作与真实版本', () => {
  it('无来源可生成结构提纲但不可生成实质草稿，历史草稿不能直接改写', async () => {
    api.write.mockResolvedValueOnce({ ...draft(), kind: 'outline', references: [], coverage: [] })
    render(<PaperWriting notebookId="notebook:test" state={state()} ready {...callbacks()} />)
    fireEvent.change(screen.getByLabelText('写作 / 改写要求'), { target: { value: '组织一份研究提纲' } })
    fireEvent.change(screen.getByLabelText('写作任务'), { target: { value: 'draft' } })
    expect(screen.getByRole('button', { name: /AI 生成有引用草稿/ })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('写作任务'), { target: { value: 'outline' } })
    fireEvent.click(screen.getByRole('button', { name: /AI 生成提纲/ }))
    await waitFor(() => expect(api.write).toHaveBeenCalledWith('notebook:test', { kind: 'outline', instructions: '组织一份研究提纲', source_ids: [], rights_confirmed: false, parent_id: null }))
    expect(await screen.findByLabelText('人工编辑草稿（Markdown，保留引用编号）')).toHaveValue(draft().markdown)
  })

  it('未人审摘要可生成有引用草稿，改写明确绑定当前选中父版本', async () => {
    const data = state()
    data.sources = [source()]
    api.write.mockResolvedValueOnce(draft()).mockResolvedValueOnce({ ...draft(), id: 'draft-2', kind: 'revision', parent_id: 'draft-1' })
    const props = callbacks()
    render(<PaperWriting notebookId="notebook:test" state={data} ready {...props} />)
    fireEvent.change(screen.getByLabelText('写作任务'), { target: { value: 'draft' } })
    fireEvent.change(screen.getByLabelText('写作 / 改写要求'), { target: { value: '比较方法局限' } })
    fireEvent.click(screen.getByRole('checkbox', { name: /真实来源 source-1/ }))
    expect(screen.getByRole('button', { name: /AI 生成有引用草稿/ })).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox', { name: /我有权将所选摘要/ }))
    fireEvent.click(screen.getByRole('button', { name: /AI 生成有引用草稿/ }))
    await waitFor(() => expect(props.onSaved).toHaveBeenCalledTimes(1))
    expect(api.write).toHaveBeenLastCalledWith('notebook:test', { kind: 'draft', instructions: '比较方法局限', source_ids: ['source-1'], rights_confirmed: true, parent_id: null })
    expect(screen.getByRole('link', { name: '真实来源 source-1' })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('写作任务'), { target: { value: 'revision' } })
    fireEvent.click(screen.getByRole('button', { name: /AI 按指令改写选中版本/ }))
    await waitFor(() => expect(api.write).toHaveBeenLastCalledWith('notebook:test', { kind: 'revision', instructions: '比较方法局限', source_ids: ['source-1'], rights_confirmed: true, parent_id: 'draft-1' }))
  })

  it('人工编辑保存新版本，不调用模型；下载使用新版本且刷新仍可读取', async () => {
    const data = state()
    data.paper_work.writing_drafts = [{ ...draft(), markdown: `${draft().markdown}\n\n<!-- research-workbench:references -->\n不可人工改写的系统参考文献尾部` }]
    data.model.configured = false
    const manual = { ...draft(), id: 'manual-2', kind: 'manual' as const, markdown: '人工收窄适用范围 [1]。', model_run_id: null, parent_id: 'draft-1', created_at: '2026-01-01T02:00:00Z' }
    api.saveWriting.mockResolvedValueOnce(manual)
    api.download.mockResolvedValue(undefined)
    const props = callbacks()
    const view = render(<PaperWriting notebookId="notebook:test" state={data} ready {...props} />)
    expect(screen.getByLabelText('人工编辑草稿（Markdown，保留引用编号）')).toHaveValue(draft().markdown)
    fireEvent.change(screen.getByLabelText('人工编辑草稿（Markdown，保留引用编号）'), { target: { value: manual.markdown } })
    expect(screen.getByLabelText(/已保存版本/)).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: /保存人工编辑为新版本/ }))
    await waitFor(() => expect(props.onSaved).toHaveBeenCalledTimes(1))
    expect(api.saveWriting).toHaveBeenCalledWith('notebook:test', 'draft-1', { markdown: manual.markdown, title: manual.title })
    expect(api.write).not.toHaveBeenCalled()
    for (const [label, format] of [['Word', 'docx'], ['Markdown', 'md'], ['BibTeX', 'bib']]) {
      fireEvent.click(screen.getByRole('button', { name: `下载 ${label}` }))
      await waitFor(() => expect(api.download).toHaveBeenLastCalledWith(`/competition/notebook%3Atest/writing/manual-2/export?format=${format}`, `research-draft.${format}`))
      await waitFor(() => expect(screen.getByRole('button', { name: `下载 ${label}` })).toBeEnabled())
    }
    view.unmount()
    data.paper_work.writing_drafts = [draft(), manual]
    render(<PaperWriting notebookId="notebook:test" state={data} ready {...callbacks()} />)
    expect(screen.getByLabelText('人工编辑草稿（Markdown，保留引用编号）')).toHaveValue(manual.markdown)
    expect(screen.getByLabelText(/已保存版本/)).toHaveValue('manual-2')
  })

  it('历史版本只读但仍可导出，保存失败保留人工输入且回读状态', async () => {
    const data = state()
    data.sources = [source()]
    data.paper_work.writing_drafts = [draft()]
    data.brief!.revision = 2
    const props = callbacks()
    const view = render(<PaperWriting notebookId="notebook:test" state={data} ready {...props} />)
    expect(screen.getByLabelText('人工编辑草稿（Markdown，保留引用编号）')).toBeDisabled()
    expect(screen.getByRole('button', { name: '下载 Word' })).toBeEnabled()
    fireEvent.change(screen.getByLabelText('写作任务'), { target: { value: 'revision' } })
    fireEvent.change(screen.getByLabelText('写作 / 改写要求'), { target: { value: '改写这份历史草稿' } })
    fireEvent.click(screen.getByRole('checkbox', { name: /真实来源 source-1/ }))
    fireEvent.click(screen.getByRole('checkbox', { name: /我有权将所选摘要/ }))
    expect(screen.getByRole('button', { name: /AI 按指令改写选中版本/ })).toBeDisabled()
    data.brief!.revision = 1
    view.rerender(<PaperWriting notebookId="notebook:test" state={data} ready {...props} />)
    api.saveWriting.mockRejectedValueOnce(new Error('版本冲突，请刷新'))
    fireEvent.change(screen.getByLabelText('人工编辑草稿（Markdown，保留引用编号）'), { target: { value: '未保存的人工修订 [1]。' } })
    fireEvent.click(screen.getByRole('button', { name: /保存人工编辑为新版本/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent('版本冲突')
    expect(screen.getByLabelText('人工编辑草稿（Markdown，保留引用编号）')).toHaveValue('未保存的人工修订 [1]。')
    expect(props.onSaved).toHaveBeenCalledTimes(1)
  })

  it('外部新增同研究版本后刷新及下载回读，保留首次编辑的版本和缓冲，并可保存后主动切换', async () => {
    const data = state()
    const external = { ...draft(), id: 'external-2', title: '外部新增草稿', created_at: '2026-01-01T02:00:00Z' }
    const manual = { ...draft(), id: 'manual-3', title: '我的未保存标题', markdown: '我的未保存正文 [1]。', kind: 'manual' as const, created_at: '2026-01-01T03:00:00Z' }
    data.paper_work.writing_drafts = [draft()]
    api.state.mockResolvedValue(data)
    api.download.mockResolvedValue(undefined)
    api.saveWriting.mockResolvedValueOnce(manual)
    render(<ResearchDetailPage />)
    await screen.findByLabelText('草稿标题')
    fireEvent.change(screen.getByLabelText('草稿标题'), { target: { value: manual.title } })
    fireEvent.change(screen.getByLabelText('人工编辑草稿（Markdown，保留引用编号）'), { target: { value: manual.markdown } })
    const refreshed = { ...data, paper_work: { ...data.paper_work, writing_drafts: [external, draft()] } }
    api.state.mockResolvedValue(refreshed)
    fireEvent.click(screen.getByRole('button', { name: '刷新状态（不调用 AI）' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '刷新状态（不调用 AI）' })).toBeEnabled())
    expect(screen.getByLabelText('草稿标题')).toHaveValue(manual.title)
    expect(screen.getByLabelText('人工编辑草稿（Markdown，保留引用编号）')).toHaveValue(manual.markdown)
    expect(screen.getByLabelText(/已保存版本/)).toHaveValue('draft-1')
    expect(screen.getByLabelText(/已保存版本/)).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '下载 Markdown' }))
    await waitFor(() => expect(api.state).toHaveBeenCalledTimes(3))
    await waitFor(() => expect(screen.getByRole('button', { name: '下载 Markdown' })).toBeEnabled())
    expect(api.download).toHaveBeenCalledWith('/competition/notebook%3Atest/writing/draft-1/export?format=md', 'research-draft.md')
    expect(screen.getByLabelText('草稿标题')).toHaveValue(manual.title)
    expect(screen.getByLabelText('人工编辑草稿（Markdown，保留引用编号）')).toHaveValue(manual.markdown)
    expect(screen.getByText(/有未保存修改；下载的是已保存版本/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /保存人工编辑为新版本/ }))
    await waitFor(() => expect(screen.getByLabelText(/已保存版本/)).toBeEnabled())
    expect(api.saveWriting).toHaveBeenCalledWith('notebook:test', 'draft-1', { title: manual.title, markdown: manual.markdown })
    expect(screen.getByLabelText(/已保存版本/)).toHaveValue('manual-3')
    expect(screen.queryByText(/有未保存修改；下载的是已保存版本/)).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText(/已保存版本/), { target: { value: external.id } })
    expect(screen.getByLabelText('草稿标题')).toHaveValue(external.title)
    expect(api.write).not.toHaveBeenCalled()
  })

  it('保存遭遇409并回读新研究版本时保留标题正文、冲突和dirty，明确处置后可选择新稿', async () => {
    const data = state()
    data.paper_work.writing_drafts = [draft()]
    api.state.mockResolvedValue(data)
    const saving = Promise.withResolvers<WritingDraft>()
    api.saveWriting.mockReturnValueOnce(saving.promise)
    render(<ResearchDetailPage />)
    await screen.findByLabelText('草稿标题')
    fireEvent.change(screen.getByLabelText('草稿标题'), { target: { value: '冲突时仍需保留的标题' } })
    fireEvent.change(screen.getByLabelText('人工编辑草稿（Markdown，保留引用编号）'), { target: { value: '冲突时仍需保留的正文 [1]。' } })
    fireEvent.click(screen.getByRole('button', { name: /保存人工编辑为新版本/ }))
    expect(screen.getByRole('button', { name: /保存人工编辑为新版本/ })).toBeDisabled()
    const latest = { ...draft(), id: 'new-research-draft', study_revision: 2, title: '新研究草稿', created_at: '2026-01-01T02:00:00Z' }
    api.state.mockResolvedValue({ ...data, brief: { ...data.brief!, revision: 2, content: { ...data.brief!.content, question: '另一标签页的新问题' } }, paper_work: { ...data.paper_work, writing_drafts: [latest, draft()] } })
    await act(async () => saving.reject(Object.assign(new Error('409：研究版本冲突，请刷新'), { response: { status: 409 } })))
    await waitFor(() => expect(screen.getByRole('button', { name: '刷新状态（不调用 AI）' })).toBeEnabled())
    expect(api.state).toHaveBeenCalledTimes(2)
    expect(screen.getByLabelText('草稿标题')).toHaveValue('冲突时仍需保留的标题')
    expect(screen.getByLabelText('人工编辑草稿（Markdown，保留引用编号）')).toHaveValue('冲突时仍需保留的正文 [1]。')
    expect(screen.getByLabelText('人工编辑草稿（Markdown，保留引用编号）')).toBeEnabled()
    expect(screen.getByLabelText('人工编辑草稿（Markdown，保留引用编号）')).toHaveAttribute('readonly')
    expect(screen.getByText('409：研究版本冲突，请刷新')).toBeInTheDocument()
    expect(screen.getByText(/以上标题和正文是旧版本的未保存编辑/)).toBeInTheDocument()
    expect(screen.getByText(/有未保存修改；下载的是已保存版本/)).toBeInTheDocument()
    expect(screen.getByLabelText(/已保存版本/)).toHaveValue('draft-1')
    expect(screen.getByLabelText(/已保存版本/)).toBeDisabled()
    expect(screen.getByRole('button', { name: /保存人工编辑为新版本/ })).toBeDisabled()
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true)
    fireEvent.click(screen.getByRole('button', { name: /放弃未保存修改/ }))
    expect(screen.getByLabelText('草稿标题')).toHaveValue('冲突时仍需保留的标题')
    fireEvent.click(screen.getByRole('button', { name: /放弃未保存修改/ }))
    expect(confirm).toHaveBeenCalledTimes(2)
    expect(screen.queryByText(/有未保存修改；下载的是已保存版本/)).not.toBeInTheDocument()
    expect(screen.getByLabelText(/已保存版本/)).toBeEnabled()
    fireEvent.change(screen.getByLabelText(/已保存版本/), { target: { value: latest.id } })
    expect(screen.getByLabelText('草稿标题')).toHaveValue(latest.title)
    expect(screen.getByLabelText('草稿标题')).toBeEnabled()
    expect(api.saveWriting).toHaveBeenCalledTimes(1)
    expect(api.write).not.toHaveBeenCalled()
  })

  it('同页修改研究条件被dirty拦截，ready变为false仍可保存原研究草稿后明确提交问题', async () => {
    const data = state()
    data.paper_work.writing_drafts = [draft()]
    api.state.mockResolvedValue(data)
    const manual = { ...draft(), id: 'manual-new', kind: 'manual' as const, title: '人工标题', markdown: '人工正文 [1]。' }
    api.saveWriting.mockResolvedValueOnce(manual)
    api.post.mockResolvedValue(undefined)
    render(<ResearchDetailPage />)
    await screen.findByLabelText('草稿标题')
    fireEvent.change(screen.getByLabelText('草稿标题'), { target: { value: manual.title } })
    fireEvent.change(screen.getByLabelText('人工编辑草稿（Markdown，保留引用编号）'), { target: { value: manual.markdown } })
    fireEvent.change(screen.getByLabelText('研究问题'), { target: { value: '待确认的新研究问题' } })
    fireEvent.click(screen.getByRole('button', { name: '确认修改，建立新研究版本' }))
    expect(api.post).not.toHaveBeenCalled()
    expect(screen.getByRole('alert')).toHaveTextContent('草稿有未保存的人工修改')
    expect(screen.getByLabelText('草稿标题')).toHaveValue(manual.title)
    expect(screen.getByLabelText('人工编辑草稿（Markdown，保留引用编号）')).toHaveValue(manual.markdown)
    expect(screen.getByLabelText(/已保存版本/)).toBeDisabled()
    expect(screen.getByRole('button', { name: /保存人工编辑为新版本/ })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: /保存人工编辑为新版本/ }))
    await waitFor(() => expect(screen.getByRole('button', { name: '确认修改，建立新研究版本' })).toBeEnabled())
    expect(api.saveWriting).toHaveBeenCalledWith('notebook:test', 'draft-1', { title: manual.title, markdown: manual.markdown })
    expect(screen.getByLabelText(/已保存版本/)).toBeEnabled()
    expect(screen.queryByText(/有未保存修改；下载的是已保存版本/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '确认修改，建立新研究版本' }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('notebook:test', '/brief', { question: '待确认的新研究问题', resources: '' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '刷新状态（不调用 AI）' })).toBeEnabled())
    expect(api.write).not.toHaveBeenCalled()
  })

  it('未保存时拦截Next Link和刷新离页，页内锚点不受阻，处置缓冲后解除保护', async () => {
    const data = state()
    data.paper_work.writing_drafts = [draft()]
    api.state.mockResolvedValue(data)
    render(<ResearchDetailPage />)
    await screen.findByLabelText('草稿标题')
    fireEvent.change(screen.getByLabelText('草稿标题'), { target: { value: '尚未保存' } })
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    expect(fireEvent.click(screen.getByRole('link', { name: '← 返回研究工作台' }))).toBe(false)
    expect(confirm).toHaveBeenCalledTimes(1)
    expect(screen.getByLabelText('草稿标题')).toHaveValue('尚未保存')
    fireEvent.click(screen.getByRole('link', { name: '5 写作' }))
    expect(confirm).toHaveBeenCalledTimes(1)
    const unload = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(unload)
    expect(unload.defaultPrevented).toBe(true)
    confirm.mockReturnValue(true)
    fireEvent.click(screen.getByRole('button', { name: /放弃未保存修改/ }))
    const cleanUnload = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(cleanUnload)
    expect(cleanUnload.defaultPrevented).toBe(false)
    expect(screen.getByLabelText(/已保存版本/)).toBeEnabled()
  })
  it('历史导航卸载后恢复原版本的未保存标题和正文，不串入其他研究或自动切到新版本', () => {
    const data = state()
    data.paper_work.writing_drafts = [draft()]
    const props = callbacks()
    const view = render(<PaperWriting notebookId="notebook:test" state={data} ready {...props} />)
    fireEvent.change(screen.getByLabelText('草稿标题'), { target: { value: '待保存标题' } })
    fireEvent.change(screen.getByLabelText('人工编辑草稿（Markdown，保留引用编号）'), { target: { value: '后退前尚未保存的正文 [1]。' } })
    view.unmount()
    const other = render(<PaperWriting notebookId="notebook:other" state={data} ready {...props} />)
    expect(screen.getByLabelText('草稿标题')).toHaveValue(draft().title)
    other.unmount()
    data.paper_work.writing_drafts.push({ ...draft(), id: 'external-new', title: '后来生成的版本', created_at: '2026-01-02T00:00:00Z' })
    const recovered = render(<PaperWriting notebookId="notebook:test" state={data} ready {...props} />)
    expect(screen.getByLabelText('草稿标题')).toHaveValue('待保存标题')
    expect(screen.getByLabelText('人工编辑草稿（Markdown，保留引用编号）')).toHaveValue('后退前尚未保存的正文 [1]。')
    expect(screen.getByLabelText(/已保存版本/)).toHaveValue('draft-1')
    expect(screen.getByLabelText(/已保存版本/)).toBeDisabled()
    expect(screen.getByRole('button', { name: /保存人工编辑为新版本/ })).toBeEnabled()
    recovered.unmount()
    data.brief!.revision = 2
    render(<PaperWriting notebookId="notebook:test" state={data} ready {...props} />)
    expect(screen.getByLabelText('人工编辑草稿（Markdown，保留引用编号）')).toHaveValue('后退前尚未保存的正文 [1]。')
    expect(screen.getByLabelText('人工编辑草稿（Markdown，保留引用编号）')).toHaveAttribute('readonly')
    expect(screen.getByRole('button', { name: /保存人工编辑为新版本/ })).toBeDisabled()
    expect(api.saveWriting).not.toHaveBeenCalled()
    expect(api.write).not.toHaveBeenCalled()
  })

  it('明确放弃后不会在再次进入研究时恢复已丢弃的缓冲', () => {
    const data = state()
    data.paper_work.writing_drafts = [draft()]
    const props = callbacks()
    const view = render(<PaperWriting notebookId="notebook:test" state={data} ready {...props} />)
    fireEvent.change(screen.getByLabelText('草稿标题'), { target: { value: '明确放弃的标题' } })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    fireEvent.click(screen.getByRole('button', { name: /放弃未保存修改/ }))
    view.unmount()
    render(<PaperWriting notebookId="notebook:test" state={data} ready {...props} />)
    expect(screen.getByLabelText('草稿标题')).toHaveValue(draft().title)
    expect(screen.getByLabelText(/已保存版本/)).toBeEnabled()
    expect(screen.getByRole('button', { name: /保存人工编辑为新版本/ })).toBeDisabled()
  })

  it('浏览器拒绝临时存储时保留当前输入并显式提示，仍可正常保存版本', async () => {
    const data = state()
    data.paper_work.writing_drafts = [draft()]
    const props = callbacks()
    api.saveWriting.mockResolvedValueOnce({ ...draft(), id: 'manual-after-storage-error', kind: 'manual', title: '存储失败但仍可保存' })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('quota', 'QuotaExceededError') })
    render(<PaperWriting notebookId="notebook:test" state={data} ready {...props} />)
    fireEvent.change(screen.getByLabelText('草稿标题'), { target: { value: '存储失败但仍可保存' } })
    expect(screen.getByLabelText('草稿标题')).toHaveValue('存储失败但仍可保存')
    expect(screen.getByRole('alert')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /保存人工编辑为新版本/ }))
    await waitFor(() => expect(screen.getByLabelText(/已保存版本/)).toHaveValue('manual-after-storage-error'))
    expect(screen.getByRole('button', { name: /保存人工编辑为新版本/ })).toBeDisabled()
    expect(api.write).not.toHaveBeenCalled()
  })
  it('旧页面迟到的保存响应不能清除重新进入后产生的新编辑', async () => {
    const data = state()
    data.paper_work.writing_drafts = [draft()]
    const props = callbacks()
    const pending = Promise.withResolvers<WritingDraft>()
    const saved = { ...draft(), id: 'saved-late', kind: 'manual' as const, title: '提交中的标题', markdown: '提交中的正文 [1]。', created_at: '2026-01-02T00:00:00Z' }
    api.saveWriting.mockReturnValueOnce(pending.promise)
    const first = render(<PaperWriting notebookId="notebook:test" state={data} ready {...props} />)
    fireEvent.change(screen.getByLabelText('草稿标题'), { target: { value: saved.title } })
    fireEvent.change(screen.getByLabelText('人工编辑草稿（Markdown，保留引用编号）'), { target: { value: saved.markdown } })
    fireEvent.click(screen.getByRole('button', { name: /保存人工编辑为新版本/ }))
    first.unmount()
    const reopened = render(<PaperWriting notebookId="notebook:test" state={data} ready {...props} />)
    fireEvent.change(screen.getByLabelText('草稿标题'), { target: { value: '返回页面后继续编辑的标题' } })
    fireEvent.change(screen.getByLabelText('人工编辑草稿（Markdown，保留引用编号）'), { target: { value: '返回页面后继续编辑的正文 [1]。' } })
    await act(async () => pending.resolve(saved))
    reopened.unmount()
    data.paper_work.writing_drafts.push(saved)
    render(<PaperWriting notebookId="notebook:test" state={data} ready {...props} />)
    expect(screen.getByLabelText('草稿标题')).toHaveValue('返回页面后继续编辑的标题')
    expect(screen.getByLabelText('人工编辑草稿（Markdown，保留引用编号）')).toHaveValue('返回页面后继续编辑的正文 [1]。')
    expect(screen.getByLabelText(/已保存版本/)).toHaveValue('draft-1')
    expect(screen.getByLabelText(/已保存版本/)).toBeDisabled()
  })
})
