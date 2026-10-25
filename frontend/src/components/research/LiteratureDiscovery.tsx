'use client'

import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { competitionApi, DiscoveryProvider, ResearchState } from '@/lib/api/competition'
import { ActionFeedback, ResearchActionProps, SourceLink, useResearchAction } from './PaperWorkShared'

export function LiteratureDiscovery({ notebookId, state, ready, ...actions }: ResearchActionProps & {
  notebookId: string; state: ResearchState; ready: boolean
}) {
  const [provider, setProvider] = useState<DiscoveryProvider>('openalex')
  const [query, setQuery] = useState('')
  const action = useResearchAction(actions)
  const locked = actions.locked || !!action.busy
  const plans = state.discovery?.plans ?? []
  const runs = state.discovery?.runs ?? []
  return <div className="space-y-5">
    <p className="text-sm text-muted-foreground">AI 只制定检索式与相关性建议，书目来自 OpenAlex / Crossref 真实接口。这不是检索全网或知网；覆盖、摘要和开放全文受数据库及出版许可限制，未命中不等于文献不存在。</p>
    <div className="space-y-2"><Label htmlFor="discovery-provider">检索数据库</Label><select id="discovery-provider" className="block rounded-md border bg-background p-2" value={provider} disabled={locked} onChange={event => setProvider(event.target.value as DiscoveryProvider)}><option value="openalex">OpenAlex（含可用摘要与开放获取位置）</option><option value="crossref">Crossref（DOI 与出版元数据）</option></select></div>
    <Button disabled={locked || !ready || !state.model.configured} onClick={() => void action.run('AI 正在制定策略并联网查找文献', async () => { await competitionApi.discover(notebookId, provider); return '检索过程已保存。请查看各次查询、结果与推荐理由，再决定纳入哪些资料。' })}>AI 制定策略并查找资料（联网与模型调用）</Button>
    {!state.model.configured && <p className="text-sm text-muted-foreground">先展开模型连接并完成配置，才能使用 AI；也可以输入检索式直接联网查询。</p>}
    {!ready && <p className="text-sm text-muted-foreground">请先保存并确认上方研究问题的修改。</p>}
    <form className="flex flex-wrap items-end gap-3" onSubmit={event => { event.preventDefault(); if (query.trim()) void action.run('正在联网检索', async () => { await competitionApi.search(notebookId, query.trim(), provider); return '检索已保存，请查看真实结果。' }) }}>
      <div className="min-w-60 flex-1 space-y-2"><Label htmlFor="literature-query">编辑检索式</Label><Input id="literature-query" value={query} maxLength={500} disabled={locked} onChange={event => setQuery(event.target.value)} placeholder="输入关键词，或从 AI 策略中选取后修改" /></div>
      <Button variant="outline" type="submit" disabled={locked || !ready || !query.trim()}>仅联网检索（不调用 AI）</Button>
    </form>
    <ActionFeedback {...action} />
    {plans.map(plan => <details key={plan.id} open={plan.study_revision === state.brief?.revision} className="rounded-lg border p-3">
      <summary className="cursor-pointer text-sm font-medium">检索策略 · 研究版本 {plan.study_revision}{plan.study_revision !== state.brief?.revision && '（历史，不代表当前研究）'} · {new Date(plan.created_at).toLocaleString()}</summary>
      <p className="mt-3 text-sm">{plan.scope}</p><ul className="list-disc pl-5 text-sm">{plan.inclusion_criteria.map((criterion, index) => <li key={index}>{criterion}</li>)}</ul>
      <div className="mt-3 flex flex-wrap gap-2">{plan.queries.map(item => <Button key={item} variant="outline" disabled={locked} onClick={() => setQuery(item)}>{item}</Button>)}</div>
    </details>)}
    {!runs.length && <p className="rounded-lg border border-dashed p-5 text-sm">还没有检索记录。直接点击上方 AI 查找资料，不需要先上传 PDF。</p>}
    {runs.map(run => <details key={run.id} open={run.study_revision === state.brief?.revision} className="rounded-xl border p-4 space-y-3">
      <summary className="cursor-pointer font-medium">{run.provider === 'openalex' ? 'OpenAlex' : 'Crossref'} · {run.query} · {run.results.length} 条结果{run.study_revision !== state.brief?.revision && ` · 历史版本 ${run.study_revision}`}</summary>
      <p className="text-xs text-muted-foreground">{new Date(run.created_at).toLocaleString()} · 研究版本 {run.study_revision}</p>
      {run.status === 'error' ? <p role="alert" className="text-sm text-destructive">检索失败：{run.message}</p> : run.status === 'pending' ? <p role="status" className="text-sm">检索尚未完成，请刷新状态；不要立即重复请求。</p> : <>
        {run.message && <p className="text-sm">{run.message}</p>}
        {!run.results.length && <p className="text-sm">本次查询没有命中，请修改关键词或更换数据库。这不证明相关文献不存在。</p>}
        {run.results.length > 0 && <Button variant="outline" disabled={locked || !ready || !state.model.configured || run.study_revision !== state.brief?.revision} onClick={() => void action.run('AI 正在补充相关性建议', async () => { await competitionApi.rank(notebookId, run.id); return '相关性建议已保存，不等于文献质量认证。' })}>AI 补充相关性建议（模型调用）</Button>}
      </>}
      {run.results.map(result => <article key={result.index} className="rounded-lg bg-muted/20 p-4 space-y-3">
        <h3 className="font-semibold"><SourceLink url={result.official_url}>{result.title}</SourceLink></h3>
        <p className="text-sm text-muted-foreground">{result.authors.join('；') || '作者信息缺失'} · {result.year ?? '年份未知'} · {result.access_level === 'abstract' ? '有摘要' : '仅书目'} · {result.pdf_url ? '有开放全文位置，加入时尝试获取' : '未提供可自动获取的全文'}</p>
        {result.doi && <p className="text-xs break-all">DOI：{result.doi}</p>}
        {result.license && <p className="text-xs">许可：{result.license}</p>}
        {result.abstract ? <details><summary className="cursor-pointer text-sm">查看摘要</summary><p className="mt-2 whitespace-pre-wrap text-sm">{result.abstract}</p></details> : <p className="text-xs text-muted-foreground">数据库未提供摘要，不能仅凭标题当作读过论文。</p>}
        <p className="text-sm">{result.recommendation ? `AI 相关性建议：${result.recommendation}` : '尚无 AI 相关性建议，可按需要主动生成。'}</p>
        <Button variant="outline" disabled={locked || !ready || !!result.source_id || run.study_revision !== state.brief?.revision} onClick={() => void action.run('正在纳入资料并尝试开放全文', async () => { const included = await competitionApi.include(notebookId, run.id, result.index); return included.message })}>{result.source_id ? '已加入资料库' : '加入资料库并尝试开放全文'}</Button>
      </article>)}
    </details>)}
  </div>
}
