import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ResearchState, WebResearchRun } from '@/lib/api/competition'
import { WebResearch } from './WebResearch'

const api = vi.hoisted(() => ({ webResearch: vi.fn() }))
vi.mock('@/lib/api/competition', () => ({
  competitionApi: api,
  researchError: (error: unknown) => error instanceof Error ? error.message : String(error),
}))

const quote = 'Readers should verify primary sources.'
function report(): WebResearchRun {
  return {
    id: 'web-1', study_revision: 1, question: '怎样核对网页信息？', status: 'partial', created_at: '2026-01-01T00:00:00Z', completed_at: '2026-01-01T00:01:00Z', provider: 'bing',
    queries: [{ query: 'primary sources', status: 'success', message: '搜索响应已保存', searched_at: '2026-01-01T00:00:20Z', results: [{ url: 'https://example.org/guide', title: 'Public guidance', snippet: 'Search snippet, not page text' }] }],
    pages: [{ id: 'page-1', url: 'https://example.org/guide', final_url: 'https://example.org/guide', title: 'Public guidance', status: 'read', message: '仅使用实际取得的片段', retrieved_at: '2026-01-01T00:00:30Z', excerpt: quote, prepared_characters: quote.length, extracted_characters: 9000, truncated: true, content_sha256: 'a'.repeat(64), excerpt_sha256: 'b'.repeat(64) }],
    findings: [{ text: '该指南强调核对原始来源。', page_id: 'page-1', quote }], limitations: ['尚未经人工核验'], message: '已依据真实片段回答，覆盖有限。', model_run_ids: ['call-1'],
  }
}
function state(): ResearchState {
  return {
    notebook_id: 'notebook:web', brief: { revision: 1, confirmed_at: '2026-01-01T00:00:00Z', content: { question: '公开资料可信度', available_resources: '', constraints: '', population_or_object: '', validation_goal: '' } },
    sources: [], cards: [], candidate_runs: [], decisions: [], experiments: [], sms_case: false,
    model: { configured: true, backend: 'local_account', model: 'configured/not-proof' },
    experiment_preparation: { prepared: false, audited: false, manifest: null, error: null },
    discovery: { plans: [], runs: [] }, paper_work: { reading_reports: [], writing_drafts: [] }, web_research: { runs: [] },
    model_runs: [{ id: 'call-1', study_revision: 1, task: 'web_research_answer', backend: 'controlled', requested_model: 'controlled/model', actual_model: 'actual/model', called_at: '2026-01-01T00:00:40Z', prompt_version: 'test', status: 'success', error_type: null, input_refs: { web_research_id: 'web-1' } }],
  }
}
function callbacks() { return { locked: false, onSaved: vi.fn().mockResolvedValue(undefined), onBusy: vi.fn() } }

beforeEach(() => vi.clearAllMocks())

describe('可操作的AI联网查阅', () => {
  it('无PDF也能提问，但只有明确本次授权后才可启动；编辑问题撤销授权', async () => {
    api.webResearch.mockResolvedValue(report())
    const props = callbacks()
    render(<WebResearch notebookId="notebook:web" state={state()} ready {...props} />)
    const question = screen.getByLabelText('让 AI 联网查什么？')
    expect(question).toHaveValue('公开资料可信度')
    const button = screen.getByRole('button', { name: /AI 联网查阅/ })
    const consent = screen.getByRole('checkbox')
    expect(button).toBeDisabled()
    fireEvent.click(consent)
    expect(button).toBeEnabled()
    fireEvent.change(question, { target: { value: '  怎样核对网页信息？  ' } })
    expect(consent).not.toBeChecked()
    expect(button).toBeDisabled()
    fireEvent.click(consent)
    fireEvent.click(button)
    await waitFor(() => expect(props.onSaved).toHaveBeenCalledTimes(1))
    expect(api.webResearch).toHaveBeenCalledWith('notebook:web', { question: '怎样核对网页信息？', consent: true })
    expect(consent).not.toBeChecked()
  })

  it('处理中不重复付费，失败后回读已有记录并保留问题，不自动重试', async () => {
    const { promise, reject } = Promise.withResolvers<WebResearchRun>()
    api.webResearch.mockReturnValueOnce(promise)
    const props = callbacks()
    const data = state()
    const view = render(<WebResearch notebookId="notebook:web" state={data} ready {...props} />)
    fireEvent.change(screen.getByLabelText('让 AI 联网查什么？'), { target: { value: '需要保留的问题' } })
    fireEvent.click(screen.getByRole('checkbox'))
    const button = screen.getByRole('button', { name: /AI 联网查阅/ })
    fireEvent.click(button)
    fireEvent.click(button)
    expect(button).toBeDisabled()
    expect(api.webResearch).toHaveBeenCalledTimes(1)
    await act(async () => reject(new Error('模型回答服务超时')))
    expect(await screen.findByRole('alert')).toHaveTextContent('模型回答服务超时')
    expect(props.onSaved).toHaveBeenCalledTimes(1)
    expect(screen.getByLabelText('让 AI 联网查什么？')).toHaveValue('需要保留的问题')
    const saved = report()
    saved.status = 'error'
    saved.findings = []
    saved.message = '真实网页已取得，未生成回答'
    data.web_research!.runs = [saved]
    view.rerender(<WebResearch notebookId="notebook:web" state={data} ready {...props} />)
    expect(screen.getByText(saved.message)).toBeInTheDocument()
    expect(api.webResearch).toHaveBeenCalledTimes(1)
    expect(props.onBusy).toHaveBeenLastCalledWith('')
  })

  it('已有回答重新打开不调用模型，显示真正的原句、实际模型与片段范围', () => {
    const data = state()
    data.web_research!.runs = [report()]
    render(<WebResearch notebookId="notebook:web" state={data} ready {...callbacks()} />)
    expect(screen.getByText(quote, { selector: 'blockquote' })).toBeInTheDocument()
    expect(screen.getByText(/实际模型：actual\/model/)).toBeInTheDocument()
    fireEvent.click(screen.getByText(/实际读取范围与未读原因/))
    expect(screen.getByText(/9000|9,000/)).toHaveTextContent('已截断，只使用片段')
    expect(api.webResearch).not.toHaveBeenCalled()
  })

  it.each(['http://example.org/a', 'javascript:alert(1)', 'https://user:password@example.org/a', 'https://example.org:8443/a'])('不把不安全的持久来源地址变成可执行链接：%s', url => {
    const data = state()
    const saved = report()
    saved.pages[0].url = saved.pages[0].final_url = url
    saved.queries[0].results[0].url = url
    data.web_research!.runs = [saved]
    render(<WebResearch notebookId="notebook:web" state={data} ready {...callbacks()} />)
    expect(screen.queryByRole('link', { name: 'Public guidance' })).not.toBeInTheDocument()
    expect(screen.getByText(quote, { selector: 'blockquote' })).toBeInTheDocument()
  })

  it('网页中的HTML仅为文字，不创建图片、脚本或可执行DOM', () => {
    const data = state()
    const saved = report()
    saved.pages[0].excerpt = '<img src=x onerror=alert(1)><script>steal()</script>'
    saved.findings[0].quote = saved.pages[0].excerpt
    data.web_research!.runs = [saved]
    const view = render(<WebResearch notebookId="notebook:web" state={data} ready {...callbacks()} />)
    expect(screen.getByText(saved.findings[0].quote, { selector: 'blockquote' })).toBeInTheDocument()
    expect(view.container.querySelector('img,script')).toBeNull()
    expect(screen.getAllByRole('link', { name: 'Public guidance' })[0]).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('研究修订后旧查阅放进历史，展开或刷新状态不重发模型请求', () => {
    const data = state()
    data.brief!.revision = 2
    data.web_research!.runs = [report()]
    render(<WebResearch notebookId="notebook:web" state={data} ready {...callbacks()} />)
    expect(screen.getByText('该指南强调核对原始来源。')).not.toBeVisible()
    fireEvent.click(screen.getByText(/历史版本联网查阅/))
    expect(screen.getByText('该指南强调核对原始来源。')).toBeVisible()
    expect(screen.getByText(/历史记录，不代表当前研究/)).toBeInTheDocument()
    expect(api.webResearch).not.toHaveBeenCalled()
  })

  it('只有搜索候选而正文读取失败时，不把摘要展示成已生成的回答', () => {
    const data = state()
    const saved = report()
    saved.pages = []
    saved.findings = []
    saved.status = 'error'
    saved.message = '没有取得可用正文，未生成回答'
    data.web_research!.runs = [saved]
    render(<WebResearch notebookId="notebook:web" state={data} ready {...callbacks()} />)
    expect(screen.getByRole('alert')).toHaveTextContent('未生成回答')
    expect(screen.getByText(/已读网页 0/)).toBeInTheDocument()
    expect(screen.queryByText(quote, { selector: 'blockquote' })).not.toBeInTheDocument()
    expect(api.webResearch).not.toHaveBeenCalled()
  })
})

it('网页读取后研究换版，待用片段不能冒充已提供模型的输入', () => {
  const data = state()
  const saved = report()
  saved.status = 'stale'
  saved.findings = []
  saved.model_run_ids = []
  data.model_runs = []
  data.web_research!.runs = [saved]
  render(<WebResearch notebookId="notebook:web" state={data} ready {...callbacks()} />)
  fireEvent.click(screen.getByText(/实际读取范围与未读原因/))
  expect(screen.getByText(/尚无回答调用记录/)).toBeVisible()
  expect(screen.queryByText(/本次发送模型/)).not.toBeInTheDocument()
  expect(api.webResearch).not.toHaveBeenCalled()
})

it('含补充平面数学字符的片段使用服务端码点计数，不误用UTF-16长度', () => {
  const data = state()
  const saved = report()
  saved.pages[0].excerpt = 'A𝑥B'
  saved.pages[0].prepared_characters = 3
  saved.findings[0].quote = 'A𝑥B'
  data.web_research!.runs = [saved]
  render(<WebResearch notebookId="notebook:web" state={data} ready {...callbacks()} />)
  fireEvent.click(screen.getByText(/实际读取范围与未读原因/))
  expect(screen.getByText(/已取得待用片段/)).toHaveTextContent('3 /')
  expect(screen.getByText(/已有回答调用记录/)).toBeVisible()
})
