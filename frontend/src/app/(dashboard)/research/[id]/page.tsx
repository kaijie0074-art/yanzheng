'use client'

import { FormEvent, ReactNode, useCallback, useEffect, useRef, useState } from 'react'
import Link from 'next/link'
import { useParams } from 'next/navigation'
import { AppShell } from '@/components/layout/AppShell'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import { ModelSetup } from '@/components/research/ModelSetup'
import { ResearchSources, PageSelection } from '@/components/research/ResearchSources'
import { LiteratureDiscovery } from '@/components/research/LiteratureDiscovery'
import { PaperReading } from '@/components/research/PaperReading'
import { PaperWriting } from '@/components/research/PaperWriting'
import { WebResearch } from '@/components/research/WebResearch'
import { EvidenceReview } from '@/components/research/EvidenceReview'
import { ResearchDecisions } from '@/components/research/ResearchDecisions'
import { ResearchExperiments } from '@/components/research/ResearchExperiments'
import { ResearchExports } from '@/components/research/ResearchExports'
import { competitionApi, researchError, ResearchState } from '@/lib/api/competition'

function Step({ id, number, title, subtitle, children }: { id: string; number: number; title: string; subtitle: string; children: ReactNode }) {
  return <section id={id} className="scroll-mt-6 rounded-xl border bg-card p-5 md:p-6 space-y-5">
    <header className="flex items-start gap-3"><span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground text-sm font-semibold">{number}</span><div><h2 className="text-xl font-semibold">{title}</h2><p className="text-sm text-muted-foreground mt-1">{subtitle}</p></div></header>{children}
  </section>
}

export default function ResearchDetailPage() {
  const params = useParams<{ id: string }>()
  const notebookId = decodeURIComponent(params.id)
  return <ResearchDetail key={notebookId} notebookId={notebookId} />
}

function ResearchDetail({ notebookId }: { notebookId: string }) {
  const [state, setState] = useState<ResearchState | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [actionError, setActionError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState('')
  const [question, setQuestion] = useState('')
  const [resources, setResources] = useState('')
  const [selected, setSelected] = useState<PageSelection[]>([])
  const [rightsConfirmed, setRightsConfirmed] = useState(false)
  const [writingDirty, setWritingDirty] = useState(false)
  const pending = useRef(false)
  const initialized = useRef(false)
  const load = useCallback(async () => {
    setRefreshing(true)
    setLoadError('')
    try {
      const result = await competitionApi.state(notebookId)
      setState(result)
      if (!initialized.current) {
        setQuestion(result.brief?.content.question ?? '')
        setResources(result.brief?.content.available_resources ?? '')
        initialized.current = true
      }
    } catch (error) { setLoadError(researchError(error)); throw error }
    finally { setRefreshing(false); setLoading(false) }
  }, [notebookId])
  useEffect(() => {
    let active = true
    competitionApi.state(notebookId).then(result => {
      if (!active) return
      setState(result)
      setQuestion(result.brief?.content.question ?? '')
      setResources(result.brief?.content.available_resources ?? '')
      initialized.current = true
    }).catch(error => { if (active) setLoadError(researchError(error)) }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [notebookId])
  useEffect(() => {
    if (!writingDirty) return
    const beforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    // Next Link 的客户端导航不会触发 beforeunload，需在路由点击处理前拦截。
    const beforeNavigate = (event: MouseEvent) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
      const link = event.target instanceof Element ? event.target.closest('a[href]') : null
      if (!(link instanceof HTMLAnchorElement) || link.hasAttribute('download') || (link.target && link.target !== '_self')) return
      const destination = new URL(link.href, window.location.href)
      if (destination.origin === window.location.origin && destination.pathname === window.location.pathname && destination.search === window.location.search) return
      if (!window.confirm('草稿标题和正文尚未保存到研究记录，临时缓冲不能代替保存。确定离开？')) {
        event.preventDefault()
        event.stopPropagation()
      }
    }
    window.addEventListener('beforeunload', beforeUnload)
    document.addEventListener('click', beforeNavigate, true)
    return () => {
      window.removeEventListener('beforeunload', beforeUnload)
      document.removeEventListener('click', beforeNavigate, true)
    }
  }, [writingDirty])
  async function act(action: 'brief' | 'analyze' | 'candidates') {
    if (pending.current || busy || refreshing) return
    if (action === 'brief' && writingDirty) {
      setActionError('草稿有未保存的人工修改。请先保存或明确放弃修改，再确认新的研究问题；当前标题和正文已保留。')
      return
    }
    pending.current = true
    setBusy(action === 'brief' ? '正在保存研究问题' : action === 'analyze' ? 'AI 正在提取证据' : 'AI 正在生成候选方案')
    setActionError('')
    setMessage('')
    try {
      const body = action === 'brief' ? { question: question.trim(), resources: resources.trim() } : action === 'analyze' ? { pages: selected, rights_confirmed: rightsConfirmed } : {}
      await competitionApi.post(notebookId, `/${action}`, body)
      if (action === 'brief') { setSelected([]); setRightsConfirmed(false) }
      setMessage(action === 'brief' ? '研究问题已确认，可以直接让 AI 找文献。历史报告与草稿保留，但不作为当前版本成果。' : action === 'analyze' ? '证据提取完成，所有新卡仍待人工复核。请逐卡打开原 PDF。' : '候选已生成，请自行比较证据、局限并记录决定。')
    } catch (error) { setActionError(researchError(error)) }
    finally {
      try { await load() } catch { /* load 已显示回读失败，禁止自动重发模型请求 */ }
      pending.current = false
      setBusy('')
    }
  }
  function confirmBrief(event: FormEvent) { event.preventDefault(); if (question.trim()) void act('brief') }
  const currentCards = state?.cards.filter(card => card.study_revision === state.brief?.revision) ?? []
  const historyCards = state?.cards.filter(card => card.study_revision !== state.brief?.revision) ?? []
  const eligibleCount = currentCards.filter(card => card.eligible).length
  const selectedCharacters = selected.reduce((count, item) => count + (state?.sources.find(source => source.id === item.source_id)?.pages.find(page => page.page_number === item.page_number)?.text.length ?? 0), 0)
  const locked = !!busy || refreshing
  const confirmed = !!state?.brief?.confirmed_at
  const briefChanged = question.trim() !== (state?.brief?.content.question ?? '') || resources.trim() !== (state?.brief?.content.available_resources ?? '')
  const ready = confirmed && !briefChanged
  const analysisReady = ready && !!state?.model.configured && selected.length > 0 && selectedCharacters > 0 && selectedCharacters <= 24000 && rightsConfirmed
  const reports = state?.paper_work?.reading_reports.filter(report => report.study_revision === state.brief?.revision) ?? []
  const drafts = state?.paper_work?.writing_drafts.filter(draft => draft.study_revision === state.brief?.revision) ?? []
  const next = !ready ? ['brief', '保存并确认研究问题'] : !state?.sources.length ? ['discovery', '让 AI 制定策略并找真实文献'] : !reports.length ? ['reading', '选择资料，让 AI 辅助阅读'] : !drafts.length ? ['writing', '生成提纲或基于文献写作草稿'] : ['writing', '检查引用，人工编辑并下载草稿']
  const actions = { locked, onSaved: load, onBusy: setBusy }

  return <AppShell><div className="flex-1 overflow-y-auto"><div className="mx-auto max-w-6xl space-y-6 p-5 md:p-8">
    <header className="space-y-4"><div className="flex flex-wrap items-center justify-between gap-3"><Link href="/research" className="text-sm text-muted-foreground hover:underline">← 返回研究工作台</Link><div className="flex flex-wrap gap-2"><Button variant="outline" asChild><Link href={`/notebooks/${encodeURIComponent(notebookId)}`}>原始资料 · 聊天 · 笔记</Link></Button><Button variant="ghost" disabled={loading || locked} onClick={() => { void load().catch(() => undefined) }}>刷新状态（不调用 AI）</Button></div></div>
      <h1 className="text-2xl md:text-3xl font-bold tracking-tight">{state?.brief?.content.question ?? '开始你的研究'}</h1>
      <p className="text-sm text-muted-foreground">AI 找文献、整理阅读、辅助写作；真实来源与待核验状态始终可见。模型和联网请求由明确按钮触发，不自动重复付费调用。</p>
    </header>
    {loading && <p role="status" className="rounded-xl border p-6">正在读取研究记录…</p>}
    {loadError && <div role="alert" className="rounded-lg border border-destructive/40 p-4 text-sm"><p className="text-destructive">{loadError}</p><Button className="mt-3" variant="outline" disabled={locked} onClick={() => { void load().catch(() => undefined) }}>重新读取状态（不调用模型）</Button></div>}
    {state && <>
      <div className="rounded-xl bg-muted/40 p-5 space-y-3"><a href={`#${next[0]}`} className="font-medium underline underline-offset-4">下一步：{next[1]}</a><p className="text-sm text-muted-foreground">{state.sources.length} 份已纳入资料 · 当前研究版本 {state.brief?.revision ?? '未确认'} · {reports.length} 份阅读报告 · {drafts.length} 个写作版本</p><nav aria-label="研究步骤" className="flex flex-wrap gap-x-5 gap-y-2 text-sm">{[['brief', '1 研究问题'], ['web-research', '联网查阅'], ['discovery', '2 文献发现'], ['sources', '3 资料库'], ['reading', '4 阅读'], ['writing', '5 写作'], ['records', '6 研究记录']].map(([id, label]) => <a key={id} href={`#${id}`} className="underline underline-offset-4">{label}</a>)}</nav></div>
      {busy && <p role="status" aria-live="polite" className="sticky top-0 z-10 rounded-lg border bg-background p-3 text-sm">{busy}…长任务可能需要数分钟。切换页面不能保证取消服务端操作，请勿重复提交。</p>}
      {actionError && <p role="alert" className="rounded-lg border border-destructive/40 p-4 text-sm text-destructive">{actionError}</p>}
      {message && <p role="status" className="rounded-lg border p-4 text-sm">{message}</p>}
      <details open={!state.model.configured}><summary className="cursor-pointer text-sm font-medium">模型连接 · {state.model.configured ? `已配置 ${state.model.model}（不是本次调用证明）` : '需要配置后才可调用 AI'}</summary><fieldset disabled={locked} className="mt-3"><ModelSetup onConfigured={() => { void load().catch(() => undefined) }} /></fieldset></details>
      <Step id="brief" number={1} title="研究问题" subtitle="从任意研究兴趣开始。修改问题会建立新版本，保留历史资料、阅读与草稿。">
        <form onSubmit={confirmBrief} className="space-y-3"><div className="space-y-2"><Label htmlFor="detail-question">研究问题</Label><Textarea id="detail-question" value={question} disabled={locked} onChange={event => setQuestion(event.target.value)} required rows={3} /></div><div className="space-y-2"><Label htmlFor="detail-resources">可用资源与限制</Label><Textarea id="detail-resources" value={resources} disabled={locked} onChange={event => setResources(event.target.value)} rows={2} /></div><Button disabled={locked || !question.trim() || (confirmed && !briefChanged)} type="submit">{confirmed ? briefChanged ? '确认修改，建立新研究版本' : '当前问题已确认' : '我确认这个问题与资源条件'}</Button>{confirmed && <p className="text-xs text-muted-foreground">当前研究版本 {state.brief?.revision} · 确认时间 {new Date(state.brief!.confirmed_at).toLocaleString()}。修改前请先保存写作区的人工编辑。</p>}</form>
      </Step>
      <section id="web-research" aria-labelledby="web-research-heading" className="scroll-mt-6 rounded-xl border bg-card p-5 md:p-6 space-y-5">
        <header><h2 id="web-research-heading" className="text-xl font-semibold">联网查阅</h2><p className="mt-1 text-sm text-muted-foreground">直接提出问题，让 AI 规划公开搜索、实际读取网页片段并回答；不要求先上传 PDF。</p></header>
        <WebResearch key={state.brief?.revision} notebookId={notebookId} state={state} ready={ready} {...actions} />
      </section>
      <Step id="discovery" number={2} title="文献发现" subtitle="不需要先有 PDF。AI 规划检索、真实数据库返回书目，你决定纳入哪些资料。"><LiteratureDiscovery notebookId={notebookId} state={state} ready={ready} {...actions} /></Step>
      <Step id="sources" number={3} title="资料库" subtitle="已纳入的书目与摘要可直接用于辅助读写；开放全文自动尝试获取，手动上传是补充。"><ResearchSources notebookId={notebookId} sources={state.sources} selected={selected} onSelected={pages => { setSelected(pages); setRightsConfirmed(false) }} {...actions} /></Step>
      <Step id="reading" number={4} title="辅助阅读" subtitle="按问题自动选取可用片段，展示实际覆盖与可回溯原句，不冒充读过全文。"><PaperReading key={state.brief?.revision} notebookId={notebookId} state={state} ready={ready} {...actions} /></Step>
      <Step id="writing" number={5} title="论文辅助写作" subtitle="提纲、有来源引用的草稿、按指令改写与人工版本保存。草稿不等于完成的论文。"><PaperWriting notebookId={notebookId} state={state} ready={ready} onDirtyChange={setWritingDirty} {...actions} /></Step>
      <Step id="records" number={6} title="研究记录与按需核验" subtitle="保留真实检索、阅读覆盖与写作版本。导出过程不表示论文或参赛材料已完成，未形成正式决定不阻塞辅助写作。">
        <ResearchExports notebookId={notebookId} locked={locked || briefChanged} />
        <details className="rounded-lg border p-4"><summary className="cursor-pointer font-medium">真实模型调用记录（{state.model_runs?.length ?? 0} 次）</summary><div className="mt-4 space-y-3">
          {!state.model_runs?.length && <p className="text-sm text-muted-foreground">尚无模型调用记录；配置就绪不是推理已成功的证明。</p>}
          {state.model_runs?.map(run => <article key={run.id} className="rounded-lg bg-muted/30 p-3 text-sm space-y-1"><p>{run.task} · {new Date(run.called_at).toLocaleString()} · {run.status}{run.error_type && ` · ${run.error_type}`}</p><p>实际模型：{run.actual_model || '未取得实际模型信息'} · 后端：{run.backend}</p><p className="text-xs text-muted-foreground">请求模型：{run.requested_model} · 研究版本 {run.study_revision ?? '未绑定'}{run.study_revision !== state.brief?.revision && '（非当前版本成果）'} · 调用 ID：{run.id}</p></article>)}
        </div></details>
        {state.experiments.length > 0 && <details className="rounded-lg border p-4"><summary className="cursor-pointer font-medium">已有固定案例实验记录（非当前通用研究步骤）</summary><div className="mt-4 space-y-3"><p className="text-sm text-muted-foreground">仅保留原有案例的历史记录访问；通用研究不需要运行固定实验。</p><ResearchExperiments notebookId={notebookId} state={state} locked onSaved={load} /></div></details>}
        <details className="rounded-lg border p-4"><summary className="cursor-pointer font-medium">深入操作：逐页提取证据与人工核验（不是写作前置条件）</summary><div className="mt-4 space-y-4">
          <p className="text-sm">在资料库展开“选择物理页”，选取需要深入核验的正文。已选 {selected.length} 页 · {selectedCharacters.toLocaleString()} / 24,000 字符。</p>
          {selectedCharacters > 24000 && <p className="text-sm text-destructive">超出单次上限，请减少选页。不会静默截断。</p>}
          <label className="flex items-start gap-2 text-sm"><input className="mt-1" type="checkbox" checked={rightsConfirmed} onChange={event => setRightsConfirmed(event.target.checked)} disabled={locked || selected.length === 0} />我有权将选中的论文片段发送给当前模型，并同意本次真实调用。</label>
          <Button disabled={locked || !analysisReady} onClick={() => void act('analyze')}>AI 提取逐页证据（模型调用）</Button>
          {!analysisReady && <p className="text-xs text-muted-foreground">需要已确认的当前问题、已配置模型、可读选页及本次授权。资料库中的书目确认不等于论据成立。</p>}
          {currentCards.length === 0 && <p className="text-sm text-muted-foreground">尚无当前版本证据卡，不影响阅读与草稿写作。</p>}
          {currentCards.map((card, index) => <EvidenceReview key={`${card.id}-${card.review_status}-${card.review_note}`} notebookId={notebookId} card={card} index={index} currentRevision={state.brief?.revision} locked={locked || briefChanged} onSaved={load} />)}
          {historyCards.length > 0 && <details><summary className="cursor-pointer text-sm">历史版本证据（{historyCards.length} 张）</summary><div className="mt-4 space-y-4">{historyCards.map((card, index) => <EvidenceReview key={card.id} notebookId={notebookId} card={card} index={index} currentRevision={state.brief?.revision} locked={locked} onSaved={load} />)}</div></details>}
        </div></details>
        <details className="rounded-lg border p-4"><summary className="cursor-pointer font-medium">深入操作：证据支持下的方案与决定</summary><div className="mt-4 space-y-4"><p className="text-sm text-muted-foreground">这不是每项研究的必经阶段。正式决定仍需真实引文、书目确认和人工支持；历史案例及数据保留，不作为当前通用工作流的固定任务。</p><Button disabled={locked || !ready || !state.model.configured || eligibleCount === 0} onClick={() => void act('candidates')}>AI 生成候选方案（模型调用）</Button>{eligibleCount === 0 && <p className="text-sm text-muted-foreground">正式方案需要至少一张经人工支持的合格证据；草稿写作没有此门禁。</p>}<ResearchDecisions notebookId={notebookId} state={state} locked={locked || briefChanged} onSaved={load} /></div></details>
      </Step>
      <footer className="text-xs text-muted-foreground">基于 Open Notebook（MIT）。原始资料、PDF、聊天与笔记始终保留；引用访问层级、AI 辅助与人工判断分别记录。</footer>
    </>}
  </div></div></AppShell>
}
