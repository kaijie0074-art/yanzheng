'use client'

import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { competitionApi, ResearchState } from '@/lib/api/competition'
import { PaperViewer } from './PaperViewer'
import { ActionFeedback, CoverageDetails, PaperSourcePicker, readableSource, ResearchActionProps, useResearchAction } from './PaperWorkShared'

export function PaperReading({ notebookId, state, ready, ...actions }: ResearchActionProps & {
  notebookId: string; state: ResearchState; ready: boolean
}) {
  const [selected, setSelected] = useState<string[]>([])
  const [question, setQuestion] = useState('')
  const [rights, setRights] = useState(false)
  const action = useResearchAction(actions)
  const locked = actions.locked || !!action.busy
  const sourceIds = selected.filter(id => state.sources.some(source => source.id === id && readableSource(source)))
  const reports = state.paper_work?.reading_reports ?? []
  return <div className="space-y-4">
    <PaperSourcePicker sources={state.sources} selected={sourceIds} disabled={locked} onChange={ids => { setSelected(ids); setRights(false) }} />
    <div className="space-y-2"><Label htmlFor="reading-question">你想从这些资料中了解什么？</Label><Textarea id="reading-question" value={question} disabled={locked} onChange={event => setQuestion(event.target.value)} placeholder="例如：这些研究采用了什么方法，结论在哪些条件下成立？" rows={3} /></div>
    <label className="flex items-start gap-2 text-sm"><input type="checkbox" className="mt-1" checked={rights} disabled={locked || !sourceIds.length} onChange={event => setRights(event.target.checked)} />我有权将所选资料的摘要 / 正文片段发送给当前模型，并同意本次真实调用。</label>
    <Button disabled={locked || !ready || !state.model.configured || !sourceIds.length || !question.trim() || !rights} onClick={() => void action.run('AI 正在自动选取片段并阅读', async () => { await competitionApi.read(notebookId, { source_ids: sourceIds, question: question.trim(), rights_confirmed: rights }); return '阅读报告已保存，所有发现仍需人工核验。' })}>AI 阅读所选资料（自动选择片段）</Button>
    {!state.model.configured && <p className="text-sm text-muted-foreground">请先在模型连接中完成配置。</p>}
    {!state.sources.some(readableSource) && <p className="text-sm text-muted-foreground">暂无可读摘要或正文。请加入带摘要的检索结果、尝试获取开放全文，或补充导入合法 PDF。</p>}
    {!ready && <p className="text-sm text-muted-foreground">先保存并确认研究问题，避免将报告生成到旧版本。</p>}
    <ActionFeedback {...action} />
    {!reports.length && <p className="text-sm text-muted-foreground">尚无阅读报告。无需先确认书目或逐页人审即可辅助阅读，但不能将报告冒充已经核验的结论。</p>}
    {reports.map(report => <article key={report.id} className="rounded-xl border p-4 space-y-4">
      <header><h3 className="font-semibold">{report.question}</h3><p className="mt-1 text-xs text-muted-foreground">{report.study_revision === state.brief?.revision ? '当前研究' : '历史报告，不代表当前研究'} · 版本 {report.study_revision} · {new Date(report.created_at).toLocaleString()} · AI 辅助，待人工核验</p></header>
      <p className="whitespace-pre-wrap text-sm">{report.answer}</p>
      <CoverageDetails coverage={report.coverage} />
      {report.findings.map((finding, index) => {
        const source = state.sources.find(item => item.id === finding.source_id)
        return <div key={index} className="rounded-lg border p-3 space-y-2">
          <p className="text-sm font-medium">{finding.text}</p>
          <blockquote className="border-l-2 pl-3 text-sm whitespace-pre-wrap">{finding.quote}</blockquote>
          <p className="text-xs text-muted-foreground">{source?.title ?? finding.source_id} · {finding.page_number === null ? '摘要原句（无 PDF 页码）' : `正文物理第 ${finding.page_number} 页`} · 引用待人工判断</p>
          {finding.page_number !== null && source ? <PaperViewer url={source.pdf_url} page={finding.page_number} title={source.title} /> : <details><summary className="cursor-pointer text-sm">查看来源摘要</summary><p className="mt-2 whitespace-pre-wrap text-sm">{source?.abstract || '当前没有可展示的摘要，请检查资料库。'}</p></details>}
        </div>
      })}
      {report.limitations.length > 0 && <div className="text-sm"><h4 className="font-medium">限制与不确定性</h4><ul className="list-disc pl-5">{report.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul></div>}
    </article>)}
  </div>
}
