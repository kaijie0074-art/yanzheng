'use client'

import { useRef, useState } from 'react'
import { PaperCoverage, ResearchSource, researchError } from '@/lib/api/competition'

export interface ResearchActionProps {
  locked: boolean
  onSaved: () => Promise<void>
  onBusy: (label: string) => void
}

// 即使服务端部分完成后报错也回读持久状态，不自动重发付费请求。
export function useResearchAction({ locked, onSaved, onBusy }: ResearchActionProps) {
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const pending = useRef(false)
  async function run(label: string, action: () => Promise<string | void>) {
    if (locked || pending.current) return
    pending.current = true
    setBusy(label)
    onBusy(label)
    setError('')
    setMessage('')
    try { setMessage((await action()) || '') }
    catch (error) { setError(researchError(error)) }
    finally {
      try { await onSaved() }
      catch (error) { setError(previous => [previous, `状态回读失败：${researchError(error)}`].filter(Boolean).join(' ')) }
      pending.current = false
      setBusy('')
      onBusy('')
    }
  }
  return { busy, error, message, run }
}

export function ActionFeedback({ busy, error, message }: { busy: string; error: string; message: string }) {
  return <>
    {busy && <p role="status" className="text-sm">{busy}…请等待，不要重复提交。</p>}
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    {message && <p role="status" className="text-sm">{message}</p>}
  </>
}

export function accessLabel(level: string) {
  return level === 'fulltext' ? '有全文材料' : level === 'abstract' ? '仅摘要' : '仅书目信息'
}

export function readableSource(source: ResearchSource) {
  return !source.integrity_error && (source.pages.some(page => page.text.trim()) || !!source.abstract?.trim())
}

export function PaperSourcePicker({ sources, selected, onChange, disabled }: {
  sources: ResearchSource[]; selected: string[]; onChange: (ids: string[]) => void; disabled: boolean
}) {
  return <fieldset disabled={disabled} className="rounded-lg border p-4 space-y-2">
    <legend className="px-1 text-sm font-medium">选择阅读 / 写作资料（{selected.length} / 8）</legend>
    {sources.length === 0 && <p className="text-sm text-muted-foreground">先在文献发现中加入资料，无需预先上传 PDF。</p>}
    {sources.map(source => <label key={source.id} className="flex items-start gap-2 text-sm">
      <input type="checkbox" className="mt-1" checked={selected.includes(source.id)} disabled={!readableSource(source) || (!selected.includes(source.id) && selected.length >= 8)} onChange={event => onChange(event.target.checked ? [...selected, source.id] : selected.filter(id => id !== source.id))} />
      <span>{source.title} · {accessLabel(source.access_level)}{!readableSource(source) && ' · 暂无可读文本，需取得摘要或合法全文'}</span>
    </label>)}
  </fieldset>
}

export function CoverageDetails({ coverage }: { coverage: PaperCoverage[] }) {
  return <div className="rounded-lg bg-muted/30 p-3 space-y-2 text-sm">
    <h4 className="font-medium">本次实际阅读覆盖</h4>
    {coverage.length === 0 && <p>未使用文献内容，仅为结构建议，无文献支持。</p>}
    {coverage.map(item => <p key={item.source_id}>{item.title}：{item.level === 'abstract' ? '仅摘要（不是全文）' : `正文物理页 ${item.pages.join('、')} / 全文 ${item.total_pages} 页`} · {item.characters.toLocaleString()} 字符</p>)}
    <p className="text-muted-foreground">自动选取相关片段，总输入最多 24,000 字符；不代表读过全部论文。原句匹配不是人工核验，也不保证推论成立。</p>
  </div>
}

export function SourceLink({ url, children }: { url: string; children: React.ReactNode }) {
  let safe = false
  try { safe = ['https:', 'http:'].includes(new URL(url).protocol) } catch { /* 无有效来源地址时不创建链接 */ }
  return safe ? <a href={url} target="_blank" rel="noopener noreferrer" className="underline underline-offset-4">{children}</a> : <span>{children}</span>
}
