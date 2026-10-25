'use client'

import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import { CandidateRun, competitionApi, researchError, ResearchState } from '@/lib/api/competition'
import { METHOD_LABELS } from './ResearchExperiments'
import { REVIEW_LABELS } from './EvidenceReview'

function CandidateDecision({ notebookId, run, state, locked, onSaved }: { notebookId: string; run: CandidateRun; state: ResearchState; locked: boolean; onSaved: () => Promise<void> }) {
  const [chosen, setChosen] = useState('')
  const [reason, setReason] = useState('')
  const [evidenceIds, setEvidenceIds] = useState<string[]>([])
  const [experimentId, setExperimentId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const current = run.study_revision === state.brief?.revision
  const candidate = run.candidates.find(item => item.candidate_id === chosen)
  const experiments = state.experiments.filter(item => item.status === 'confirmed' && item.frozen_selection?.study_revision === state.brief?.revision && item.frozen_selection?.chosen_candidate === chosen)
  const validExperiment = experiments.some(item => item.run_id === experimentId)
  const allEligible = evidenceIds.length > 0 && evidenceIds.every(id => state.cards.some(card => card.id === id && card.eligible && card.study_revision === state.brief?.revision))
  async function save(final: boolean) {
    if (!candidate) return
    setBusy(true)
    setError('')
    setMessage('')
    try {
      await competitionApi.post(notebookId, '/decisions', { candidate_run_id: run.id, chosen_candidate: chosen, reason: reason.trim(), evidence_ids: evidenceIds, experiment_run_id: validExperiment ? experimentId : null, final })
      setMessage(final ? '正式决定已保存；修改上游问题或证据后会失效。' : '草稿已保存，尚不是正式结论。')
      await onSaved()
    } catch (error) { setError(researchError(error)) }
    finally { setBusy(false) }
  }
  return <div className="space-y-4">
    <p className="text-sm text-muted-foreground">生成于 {new Date(run.created_at).toLocaleString('zh-CN')} · 研究版本 {run.study_revision}{!current && ' · 历史候选，不可用于当前正式决定'}</p>
    <div className="grid gap-4 lg:grid-cols-2">{run.candidates.map(item => <article key={item.candidate_id} className={`rounded-xl border p-4 space-y-3 ${chosen === item.candidate_id ? 'border-primary ring-1 ring-primary' : ''}`}>
      <h3 className="font-semibold">{item.method}</h3>
      <dl className="text-sm space-y-3"><div><dt className="font-medium">适用条件</dt><dd>{item.conditions}</dd></div><div><dt className="font-medium">可行性</dt><dd>{item.feasibility}</dd></div><div><dt className="font-medium">最小验证步骤</dt><dd>{item.minimal_validation}</dd></div><div><dt className="font-medium">反对理由与局限</dt><dd>{item.objections}</dd></div></dl>
      <details><summary className="text-sm cursor-pointer font-medium">查看本方案引用的 {item.evidence_ids.length} 条证据</summary><ul className="mt-2 space-y-3">{item.evidence_ids.map(id => { const card = state.cards.find(value => value.id === id); return <li key={id} className="text-sm rounded-md bg-muted/30 p-3">{card ? <><a className="underline font-medium" href={`#evidence-${id}`}>{card.content.claim}</a><p className="mt-1">{card.source_title} · 第 {card.content.page_number} 页</p><p className="mt-1">{card.eligible ? '当前合格证据' : `当前不可用于正式论据：${REVIEW_LABELS[card.review_status]}`}</p><blockquote className="mt-2 border-l-2 pl-2">{card.content.quote}</blockquote></> : '引用记录当前不可用，请刷新状态后核对。'}</li> })}</ul></details>
      <Button type="button" variant={chosen === item.candidate_id ? 'default' : 'outline'} disabled={locked || busy || !current} onClick={() => { setChosen(item.candidate_id); setEvidenceIds(item.evidence_ids.filter(id => state.cards.some(card => card.id === id && card.eligible))); setExperimentId(''); setMessage('') }}>{chosen === item.candidate_id ? '已选择此方案' : '选择此方案，填写理由'}</Button>
    </article>)}</div>
    {candidate && <div className="rounded-xl border bg-muted/20 p-4 space-y-4">
      <h3 className="font-semibold">我的选择：{candidate.method}</h3>
      <fieldset className="space-y-2"><legend className="text-sm font-medium mb-2">保留哪些合格证据作为决定依据</legend>{candidate.evidence_ids.map(id => { const card = state.cards.find(item => item.id === id); return <label key={id} className="text-sm flex items-start gap-2"><input className="mt-1" type="checkbox" checked={evidenceIds.includes(id)} disabled={!card?.eligible || locked || busy} onChange={event => setEvidenceIds(event.target.checked ? [...evidenceIds, id] : evidenceIds.filter(value => value !== id))} />{card?.content.claim ?? '证据当前不可用'}{!card?.eligible && '（当前不合格）'}</label> })}</fieldset>
      <div className="space-y-2"><Label htmlFor={`decision-${run.id}`}>你的选择理由</Label><Textarea id={`decision-${run.id}`} value={reason} onChange={event => setReason(event.target.value)} rows={3} placeholder="结合证据、资源与反对意见，说明你为什么选择这一方案；不要直接照抄 AI 建议。" /></div>
      {state.sms_case && <div className="space-y-2"><Label htmlFor={`experiment-${run.id}`}>绑定同方案、同研究版本的已确认实验（正式决定必需）</Label><select id={`experiment-${run.id}`} className="w-full rounded-md border bg-background p-2 text-sm" value={experimentId} onChange={event => setExperimentId(event.target.value)}><option value="">请选择已完成保留集确认的实验</option>{experiments.map((item, index) => <option key={item.run_id} value={item.run_id}>实验 {index + 1} · {item.created_at ? new Date(item.created_at).toLocaleString('zh-CN') : '已确认'} · {METHOD_LABELS[item.recommended_candidate ?? ''] ?? '已冻结方案'}</option>)}</select>{experiments.length === 0 && <p className="text-sm text-muted-foreground">暂无可绑定实验。请在固定 SMS 实验区完成比较、人工冻结及保留集确认；当前可先保存草稿。</p>}</div>}
      <div className="flex flex-wrap gap-2"><Button variant="outline" disabled={locked || busy || !reason.trim()} onClick={() => void save(false)}>保存草稿</Button><Button disabled={locked || busy || !current || !reason.trim() || !allEligible || (state.sms_case && !validExperiment)} onClick={() => void save(true)}>{busy ? '正在保存…' : '确认并保存正式决定'}</Button></div>
      {!allEligible && <p className="text-xs text-muted-foreground">正式决定至少需要一张当前版本、书目和原文校验及人工复核全部合格的证据。</p>}
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      {message && <p role="status" className="text-sm">{message}</p>}
    </div>}
  </div>
}

export function ResearchDecisions({ notebookId, state, locked, onSaved }: { notebookId: string; state: ResearchState; locked: boolean; onSaved: () => Promise<void> }) {
  const currentRuns = state.candidate_runs.filter(run => run.study_revision === state.brief?.revision)
  const latest = currentRuns[currentRuns.length - 1]
  const history = state.candidate_runs.filter(run => run.id !== latest?.id)
  const objections = state.cards.filter(card => card.study_revision === state.brief?.revision && ['partially_supported', 'unsupported', 'insufficient'].includes(card.review_status))
  return <div className="space-y-5">
    {latest ? <CandidateDecision key={latest.id} notebookId={notebookId} run={latest} state={state} locked={locked} onSaved={onSaved} /> : <p className="text-sm text-muted-foreground">当前问题还没有候选方案。先复核至少一张合格证据，再显式调用 AI 生成候选。</p>}
    {objections.length > 0 && <section className="rounded-lg border p-4 space-y-2"><h3 className="font-semibold">仍需保留的异议与限制</h3>{objections.map(card => <p key={card.id} className="text-sm"><a className="underline" href={`#evidence-${card.id}`}>{card.content.claim}</a> · {REVIEW_LABELS[card.review_status]}：{card.review_note}</p>)}</section>}
    {history.length > 0 && <details><summary className="cursor-pointer text-sm font-medium">查看其他生成记录（{history.length} 批）</summary><div className="space-y-5 mt-4">{history.map(run => <CandidateDecision key={run.id} notebookId={notebookId} run={run} state={state} locked={locked} onSaved={onSaved} />)}</div></details>}
    <section className="space-y-3"><h3 className="font-semibold">人工决定记录</h3>{state.decisions.length === 0 ? <p className="text-sm text-muted-foreground">尚未保存决定。</p> : state.decisions.map(decision => {
      const method = state.candidate_runs.find(run => run.id === decision.candidate_run_id)?.candidates.find(item => item.candidate_id === decision.chosen_candidate)?.method ?? METHOD_LABELS[decision.chosen_candidate] ?? '历史方案'
      return <article key={decision.id} className="rounded-lg border p-4 text-sm space-y-2"><p className="font-medium">{method} · {decision.effective_status === 'final' ? '正式决定' : decision.status === 'final' ? '当前不能作为正式决定' : '草稿'}</p><p className="whitespace-pre-wrap">{decision.reason}</p><p className="text-muted-foreground">{new Date(decision.created_at).toLocaleString('zh-CN')} · {decision.evidence_ids.length} 条证据 · {decision.experiment_run_id ? '已绑定实验' : '未绑定实验'}</p>{decision.gate_reasons.length > 0 && <ul className="list-disc pl-5 text-muted-foreground">{decision.gate_reasons.map(reason => <li key={reason}>{reason}</li>)}</ul>}</article>
    })}</section>
  </div>
}
