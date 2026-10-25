'use client'

import { FormEvent, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import { competitionApi, EvidenceCard, researchError, ReviewStatus } from '@/lib/api/competition'
import { PaperViewer } from './PaperViewer'

export const REVIEW_LABELS: Record<ReviewStatus, string> = {
  unreviewed: '尚未复核', supported: '支持当前主张', partially_supported: '仅部分支持', unsupported: '不支持当前主张', insufficient: '证据不足',
}
const RELATION_LABELS: Record<string, string> = { supports: '支持', contradicts: '反对', context_only: '仅提供背景', uncertain: '不确定' }

export function EvidenceReview({ notebookId, card, index, currentRevision, locked, onSaved }: { notebookId: string; card: EvidenceCard; index: number; currentRevision: number | undefined; locked: boolean; onSaved: () => Promise<void> }) {
  const [opened, setOpened] = useState(false)
  const [compared, setCompared] = useState(false)
  const [status, setStatus] = useState<ReviewStatus>(card.review_status)
  const [note, setNote] = useState(card.review_note)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)
  const current = card.study_revision === currentRevision
  async function review(event: FormEvent) {
    event.preventDefault()
    if (!opened || !compared || !note.trim()) return
    setBusy(true)
    setError('')
    setSaved(false)
    try {
      await competitionApi.post(notebookId, `/cards/${encodeURIComponent(card.id)}/review`, { status, note: note.trim(), pdf_opened: true })
      setSaved(true)
      await onSaved()
    } catch (error) { setError(researchError(error)) }
    finally { setBusy(false) }
  }
  return <article id={`evidence-${card.id}`} className="rounded-xl border p-5 space-y-4 scroll-mt-6">
    <div className="flex flex-wrap items-start justify-between gap-2"><h3 className="font-semibold">证据 {index + 1} · {card.content.claim}</h3><span className="text-xs rounded-full border px-3 py-1">{!current ? '历史版本' : card.eligible ? '可用于正式论据' : REVIEW_LABELS[card.review_status]}</span></div>
    <p className="text-sm text-muted-foreground">来源：{card.source_title} · 物理第 {card.content.page_number} 页 · 研究版本 {card.study_revision}</p>
    <blockquote className="border-l-4 border-primary bg-muted/40 p-4 whitespace-pre-wrap break-words">{card.content.quote}</blockquote>
    <div className="grid gap-3 text-sm md:grid-cols-2"><p><strong>适用条件：</strong>{card.content.conditions}</p><p><strong>限制：</strong>{card.content.limitations}</p></div>
    <p className="text-sm">程序引文校验：{card.quote_match ? '原句已匹配' : '未通过'}。{card.validation_note}</p>
    <p className="text-xs text-muted-foreground">AI 建议关系：{RELATION_LABELS[card.content.ai_relation] ?? '未知'}。原句存在不代表推论成立，AI 建议不会自动变成人工结论。</p>
    {card.integrity_error && <p role="alert" className="text-sm text-destructive">{card.integrity_error}</p>}
    <details><summary className="text-sm font-medium cursor-pointer">对照本页提取文本</summary><p className="mt-3 max-h-80 overflow-y-auto whitespace-pre-wrap break-words text-sm rounded-lg bg-muted/30 p-3">{card.page_text || '本页没有可提取文本，请打开原 PDF 检查。'}</p></details>
    <PaperViewer url={card.pdf_url} page={card.content.page_number} title={card.source_title} onOpened={() => setOpened(true)} />
    <form onSubmit={review} className="space-y-3 border-t pt-4">
      <label className="flex items-start gap-2 text-sm"><input className="mt-1" type="checkbox" checked={compared} disabled={!opened || !current || locked} onChange={event => setCompared(event.target.checked)} />我已打开上方原 PDF，对照实际页面、上下文与适用范围，准备作出独立判断。</label>
      {!opened && <p className="text-xs text-muted-foreground">请先点击“打开原 PDF”，原文显示后才能进行人工复核。</p>}
      {!current && <p className="text-sm text-muted-foreground">这是历史研究版本的证据，仅供追溯；请为当前问题重新提取。</p>}
      <div className="space-y-1"><Label htmlFor={`status-${card.id}`}>你的判断</Label><select id={`status-${card.id}`} className="w-full rounded-md border bg-background px-3 py-2 text-sm" value={status} onChange={event => setStatus(event.target.value as ReviewStatus)} disabled={!opened || !compared || !current || locked || busy}>{Object.entries(REVIEW_LABELS).map(([value, label]) => <option key={value} value={value} disabled={value === 'supported' && !card.quote_match}>{label}</option>)}</select></div>
      <div className="space-y-1"><Label htmlFor={`note-${card.id}`}>判断理由（必须说明原文是否支持，以及迁移边界）</Label><Textarea id={`note-${card.id}`} rows={3} value={note} onChange={event => setNote(event.target.value)} disabled={!opened || !compared || !current || locked || busy} required /></div>
      <Button type="submit" disabled={!opened || !compared || !current || !note.trim() || locked || busy}>{busy ? '正在保存复核…' : '保存人工判断'}</Button>
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      {saved && <p role="status" className="text-sm">人工判断已保存，正式可用性以最新状态为准。</p>}
    </form>
  </article>
}
