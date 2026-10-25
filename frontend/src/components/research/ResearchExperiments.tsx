'use client'

import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import { Aggregate, competitionApi, Experiment, researchError, ResearchState } from '@/lib/api/competition'

export const METHOD_LABELS: Record<string, string> = { word_nb: 'TF-IDF + 朴素贝叶斯', word_lr: 'TF-IDF + 逻辑回归' }
const STATUS_LABELS: Record<string, string> = { validation_complete: '验证集比较完成，等待人工冻结', confirmed: '保留集确认完成', running: '运行中', failed: '运行失败', unverified: '完整性未通过' }
const METRICS = [['spam_f1', '垃圾短信 F1'], ['spam_precision', '垃圾短信精确率'], ['spam_recall', '垃圾短信召回率'], ['macro_f1', '宏平均 F1'], ['ham_false_positive_rate', '正常短信误报率']] as const

function Metrics({ rows, caption }: { rows: Aggregate[]; caption: string }) {
  if (!rows.length) return <p className="text-sm text-muted-foreground">{caption}：尚未运行，不显示估计分数。</p>
  return <div className="overflow-x-auto"><table className="w-full text-sm text-left"><caption className="text-left mb-3 font-medium">{caption} · 五个抽样种子的均值 ± 样本标准差</caption><thead><tr className="border-b"><th className="p-2">方法 / 标签预算</th>{METRICS.map(([key, label]) => <th key={key} className="p-2 whitespace-nowrap">{label}</th>)}<th className="p-2 whitespace-nowrap">平均总耗时（秒）</th><th className="p-2 whitespace-nowrap">样本支持数</th></tr></thead><tbody>{rows.map(row => <tr key={`${row.candidate}-${row.budget}`} className="border-b"><th className="p-2 font-medium whitespace-nowrap">{METHOD_LABELS[row.candidate] ?? row.candidate}<br />{row.budget} 标签</th>{METRICS.map(([key]) => <td key={key} className="p-2 whitespace-nowrap">{row.metrics[key] ? `${row.metrics[key].mean.toFixed(4)} ± ${row.metrics[key].sample_std.toFixed(4)}` : '未记录'}</td>)}<td className="p-2">{row.timing.total_seconds?.mean.toFixed(3) ?? '未记录'}</td><td className="p-2 whitespace-nowrap">垃圾 {row.spam_support} / 正常 {row.ham_support}</td></tr>)}</tbody></table></div>
}

function ExperimentRun({ notebookId, experiment, revision, disabled, onSaved }: { notebookId: string; experiment: Experiment; revision: number; disabled: boolean; onSaved: () => Promise<void> }) {
  const [reason, setReason] = useState('')
  const [acknowledge, setAcknowledge] = useState(false)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  async function act(action: 'freeze' | 'confirm') {
    setBusy(action)
    setError('')
    try {
      await competitionApi.post(notebookId, `/experiments/${action}`, action === 'freeze' ? { run_id: experiment.run_id, reason: reason.trim(), acknowledge } : { run_id: experiment.run_id })
      await onSaved()
    } catch (error) { setError(researchError(error)) }
    finally { setBusy('') }
  }
  const frozen = experiment.frozen_selection
  const sameRevision = frozen?.study_revision === revision
  return <article className="rounded-xl border p-4 space-y-4">
    <h3 className="font-semibold">{experiment.created_at ? new Date(experiment.created_at).toLocaleString('zh-CN') : '实验记录'} · {experiment.status === 'validation_complete' && frozen ? '方案已冻结，等待保留集确认' : STATUS_LABELS[experiment.status] ?? '未完成'}</h3>
    {experiment.integrity_error && <p role="alert" className="text-sm text-destructive">{experiment.integrity_error}</p>}
    <Metrics rows={experiment.validation_aggregates} caption="验证集真实结果" />
    <p className="text-sm">固定规则推荐：{experiment.recommended_candidate ? METHOD_LABELS[experiment.recommended_candidate] ?? experiment.recommended_candidate : '尚未产生'}。按 1000 标签预算的平均垃圾短信 F1 选择，再比较正常短信误报率。</p>
    {!frozen && experiment.status === 'validation_complete' && <div className="space-y-3 rounded-lg bg-muted/30 p-4">
      <Label htmlFor={`freeze-${experiment.run_id}`}>人工冻结理由</Label><Textarea id={`freeze-${experiment.run_id}`} value={reason} onChange={event => setReason(event.target.value)} placeholder="说明为什么接受该验证集选择，并保留适用范围与风险。" />
      <label className="text-sm flex items-start gap-2"><input type="checkbox" className="mt-1" checked={acknowledge} onChange={event => setAcknowledge(event.target.checked)} />我已查看验证集结果，接受固定规则推荐；冻结后不得利用测试分数换赢家、调参或删除不理想的种子。</label>
      <Button disabled={disabled || !!busy || !reason.trim() || !acknowledge} onClick={() => void act('freeze')}>{busy === 'freeze' ? '正在冻结…' : '人工确认并冻结方案'}</Button>
    </div>}
    {frozen && <div className="rounded-lg bg-muted/30 p-4 space-y-2"><p className="text-sm font-medium">已冻结：{METHOD_LABELS[frozen.chosen_candidate] ?? frozen.chosen_candidate} · 研究版本 {frozen.study_revision}</p><p className="text-sm whitespace-pre-wrap">理由：{frozen.reason}</p>{!sameRevision && <p className="text-sm text-destructive">此实验冻结于历史问题版本，不能用于当前正式决定。</p>}{experiment.status === 'validation_complete' && <Button disabled={disabled || !!busy || !sameRevision} onClick={() => void act('confirm')}>{busy === 'confirm' ? '正在运行保留集确认…' : '运行已冻结方案的保留集确认'}</Button>}</div>}
    <Metrics rows={experiment.test_aggregates} caption="保留集真实结果" />
    <p className="text-xs text-muted-foreground">这些种子共享同一验证 / 测试集，样本标准差不是独立测试集置信区间。仅适用于历史英文短信与当前预设，不能外推至中文反诈或生产系统。</p>
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
  </article>
}

export function ResearchExperiments({ notebookId, state, locked, onSaved }: { notebookId: string; state: ResearchState; locked: boolean; onSaved: () => Promise<void> }) {
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  async function act(action: 'prepare' | 'audit' | 'compare') {
    setBusy(action)
    setError('')
    setMessage('')
    try {
      await competitionApi.post(notebookId, `/experiments/${action}`)
      setMessage(action === 'prepare' ? '官方材料已准备。下一步审计原始数据。' : action === 'audit' ? '数据审计完成。下一步运行固定验证集比较。' : '验证集比较完成。请查看真实结果，写下理由后人工冻结。')
      await onSaved()
    } catch (error) { setError(researchError(error)) }
    finally { setBusy('') }
  }
  const preparation = state.experiment_preparation
  const manifest = preparation?.manifest
  const summaryFields = [['official_sample_count', '官方公布数'], ['actual_parsed_count', '实际解析数'], ['retained_count', '去重隔离后保留数'], ['duplicate_removed_count', '同标签重复移除数'], ['conflict_row_count', '冲突标签隔离数']] as const
  return <div className="space-y-4">
    <p className="text-sm text-muted-foreground">仅运行预设的两条 SMS 管道，不执行模型生成代码。短信正文仅本地处理。固定划分为训练池 60%、验证 20%、测试 20%，训练预算 200 / 500 / 1000，各五个抽样种子。</p>
    {preparation.error && <p role="alert" className="rounded-lg border border-destructive/40 p-3 text-sm text-destructive">{preparation.error}</p>}
    <div className="flex flex-wrap gap-2">
      <Button variant="outline" disabled={locked || !!busy || preparation?.prepared} onClick={() => void act('prepare')}>{busy === 'prepare' ? '正在下载官方材料…' : preparation?.prepared ? '1. 官方材料已准备' : '1. 准备官方材料'}</Button>
      <Button variant="outline" disabled={locked || !!busy || !preparation?.prepared} onClick={() => void act('audit')}>{busy === 'audit' ? '正在审计…' : preparation?.audited ? '2. 重新审计当前数据' : '2. 审计原始数据'}</Button>
      <Button disabled={locked || !!busy || !preparation?.audited || !state.brief} onClick={() => void act('compare')}>{busy === 'compare' ? '正在运行固定比较…' : '3. 运行验证集比较'}</Button>
    </div>
    <p className="text-xs text-muted-foreground">按顺序准备 → 审计 → 验证集比较 → 人工冻结 → 保留集确认。运行可能需要等待，不自动重试；已有结果保留为历史。</p>
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    {message && <p role="status" className="text-sm">{message}</p>}
    {manifest && <dl className="grid gap-3 rounded-lg border p-4 sm:grid-cols-3">{summaryFields.map(([key, label]) => <div key={key}><dt className="text-xs text-muted-foreground">{label}</dt><dd className="text-lg font-medium">{typeof manifest[key] === 'number' ? manifest[key].toLocaleString() : '未记录'}</dd></div>)}</dl>}
    {state.experiments.length === 0 && <p className="text-sm text-muted-foreground">尚无实验结果。这里不会展示示例分数或模型预测的分数。</p>}
    {state.experiments.map(experiment => <ExperimentRun key={experiment.run_id} notebookId={notebookId} experiment={experiment} revision={state.brief?.revision ?? 0} disabled={locked || !!busy} onSaved={async () => { setMessage(''); await onSaved() }} />)}
  </div>
}
