'use client'

import { FormEvent, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { competitionApi, ResearchSource } from '@/lib/api/competition'
import { PaperViewer } from './PaperViewer'
import { ActionFeedback, ResearchActionProps, SourceLink, useResearchAction } from './PaperWorkShared'

export type PageSelection = { source_id: string; page_number: number }

function Bibliography({ notebookId, source, ...actions }: ResearchActionProps & { notebookId: string; source: ResearchSource }) {
  const [title, setTitle] = useState(source.title)
  const [authors, setAuthors] = useState(source.authors.join('; '))
  const [year, setYear] = useState(source.year?.toString() ?? '')
  const [url, setUrl] = useState(source.official_url)
  const [doi, setDoi] = useState(source.doi ?? '')
  const [confirmed, setConfirmed] = useState(false)
  const action = useResearchAction(actions)
  const locked = actions.locked || !!action.busy
  async function save(event: FormEvent) {
    event.preventDefault()
    if (!title.trim()) return
    await action.run('正在保存书目', async () => {
      await competitionApi.source(notebookId, source.id, { title: title.trim(), authors: authors.split(/[;；\n]/).map(author => author.trim()).filter(Boolean), year: year ? Number(year) : null, official_url: url.trim(), doi: doi.trim() || null, confirmed })
      return confirmed ? '书目已人工确认；论述仍需逐条核验。' : '书目草稿已保存，尚未人工确认。'
    })
  }
  const prefix = `source-${source.id}`
  return <form onSubmit={save} className="space-y-3 rounded-lg bg-muted/30 p-4">
    <p className="text-sm">对照论文首页或出版方正式页面填写。保存改动或撤销确认会使关联人工复核与正式决定失效。</p>
    <div className="space-y-1"><Label htmlFor={`${prefix}-title`}>论文标题</Label><Input id={`${prefix}-title`} value={title} onChange={event => setTitle(event.target.value)} required /></div>
    <div className="space-y-1"><Label htmlFor={`${prefix}-authors`}>作者（多人以分号分隔）</Label><Input id={`${prefix}-authors`} value={authors} onChange={event => setAuthors(event.target.value)} required={confirmed} /></div>
    <div className="grid gap-3 sm:grid-cols-2">
      <div className="space-y-1"><Label htmlFor={`${prefix}-year`}>出版年份</Label><Input id={`${prefix}-year`} type="number" min="1000" max="2100" value={year} onChange={event => setYear(event.target.value)} required={confirmed} /></div>
      <div className="space-y-1"><Label htmlFor={`${prefix}-doi`}>DOI（没有则留空）</Label><Input id={`${prefix}-doi`} value={doi} onChange={event => setDoi(event.target.value)} /></div>
    </div>
    <div className="space-y-1"><Label htmlFor={`${prefix}-url`}>出版方或正式来源页面</Label><Input id={`${prefix}-url`} type="url" value={url} onChange={event => setUrl(event.target.value)} placeholder="https://…" required={confirmed} /></div>
    <label className="flex items-start gap-2 text-sm"><input type="checkbox" className="mt-1" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />我已对照正式来源核实以上标题、作者和年份，确认这份书目。</label>
    <Button disabled={locked || !title.trim()} type="submit">{action.busy ? '正在保存…' : confirmed ? '保存并确认书目' : '保存为未确认书目'}</Button>
    <ActionFeedback {...action} />
  </form>
}

export function ResearchSources({ notebookId, sources, selected, onSelected, ...actions }: ResearchActionProps & { notebookId: string; sources: ResearchSource[]; selected: PageSelection[]; onSelected: (pages: PageSelection[]) => void }) {
  const [file, setFile] = useState<File | null>(null)
  const action = useResearchAction(actions)
  const locked = actions.locked || !!action.busy
  const [fileError, setFileError] = useState('')
  const input = useRef<HTMLInputElement>(null)
  async function upload(event: FormEvent) {
    event.preventDefault()
    if (!file || locked) return
    if (file.size > 20 * 1024 * 1024) { setFileError('每份 PDF 最大 20 MiB，请选择较小文件。'); return }
    setFileError('')
    await action.run('正在解析并保存 PDF', async () => {
      const body = new FormData()
      body.append('file', file)
      await competitionApi.post(notebookId, '/sources', body)
      setFile(null)
      if (input.current) input.current.value = ''
      return '资料已导入；可直接辅助阅读写作，正式论据仍需人工核验。'
    })
  }
  function toggle(sourceId: string, page: number, checked: boolean) {
    onSelected(checked ? [...selected, { source_id: sourceId, page_number: page }] : selected.filter(item => item.source_id !== sourceId || item.page_number !== page))
  }
  return <div className="space-y-5">
    <ActionFeedback {...action} />
    <details className="rounded-lg border border-dashed p-4"><summary className="cursor-pointer text-sm font-medium">补充导入：上传合法取得的 PDF</summary>
      <form onSubmit={upload} className="mt-4 space-y-3">
        <Label htmlFor="research-pdf">选择 PDF</Label>
        <Input ref={input} id="research-pdf" type="file" accept="application/pdf,.pdf" onChange={event => { setFile(event.target.files?.[0] ?? null); setFileError('') }} disabled={locked} />
        <p className="text-xs text-muted-foreground">每份最多 20 MiB、100 页，最多 20 份；按文件哈希去重。扫描件无文本时不会假装已读。导入后也可在原生笔记本使用。</p>
        <Button type="submit" disabled={!file || locked}>{action.busy ? '正在处理…' : '上传 PDF'}</Button>
        {fileError && <p role="alert" className="text-sm text-destructive">{fileError}</p>}
      </form>
    </details>
    {sources.length === 0 && <p className="text-muted-foreground">资料库为空。请先在文献发现中加入结果；可用摘要即可辅助阅读和写作，不必先准备 PDF。</p>}
    {sources.map((source, index) => <article key={source.id} className="rounded-xl border p-4 space-y-4">
      <div><h3 className="font-semibold">材料 {index + 1} · {source.title}</h3><p className="mt-1 text-sm text-muted-foreground">{source.authors.join('；') || '作者信息缺失'} · {source.year ?? '年份未知'} · {source.page_count} 页 · {source.access_level === 'fulltext' ? '有全文材料' : source.access_level === 'abstract' ? '仅摘要' : '仅书目信息'} · {source.bibliography_status === 'confirmed' ? '书目已人工确认，论述待核验' : '书目待确认，不阻塞辅助读写'}</p></div>
      {source.official_url && <p className="text-sm"><SourceLink url={source.official_url}>打开正式来源页面</SourceLink></p>}
      {source.abstract && <details><summary className="cursor-pointer text-sm">查看已保存摘要</summary><p className="mt-2 whitespace-pre-wrap text-sm">{source.abstract}</p></details>}
      {source.integrity_error && <p role="alert" className="text-sm text-destructive">{source.integrity_error}</p>}
      {!source.pdf_url && <div className="space-y-2"><Button variant="outline" disabled={locked || !source.open_access_pdf_url} onClick={() => void action.run('正在尝试获取开放全文', async () => { const result = await competitionApi.fetchFulltext(notebookId, source.id); return result.message })}>尝试获取开放全文</Button><p className="text-xs text-muted-foreground">{source.open_access_pdf_url ? '只从已保存的公开获取位置尝试；失败仍保留摘要与书目，不绕过登录或付费墙。' : '没有可自动获取的开放全文位置，可使用已有摘要或补充导入合法 PDF。'}</p></div>}
      <details><summary className="cursor-pointer font-medium text-sm">深入操作：修改 / 人工确认书目</summary><div className="mt-3"><Bibliography notebookId={notebookId} source={source} {...actions} /></div></details>
      {source.pdf_url && <PaperViewer url={source.pdf_url} title={source.title} />}
      <details><summary className="cursor-pointer font-medium text-sm">选择物理页并查看提取文本（已选 {selected.filter(page => page.source_id === source.id).length} 页）</summary>
        <p className="my-3 text-sm text-muted-foreground">这是按需进行的逐页证据核验；阅读与草稿写作会自动选择片段，无需在此选页。无文本的页不能分析。</p>
        <div className="max-h-[32rem] space-y-3 overflow-y-auto">{source.pages.map(page => <div key={page.page_number} className="rounded-lg border p-3">
          <label className="flex items-center gap-2 text-sm font-medium"><input type="checkbox" checked={selected.some(item => item.source_id === source.id && item.page_number === page.page_number)} disabled={locked || !page.text.trim()} onChange={event => toggle(source.id, page.page_number, event.target.checked)} />物理第 {page.page_number} 页 · {page.text.length.toLocaleString()} 字符{!page.text.trim() && ' · 无可提取文本'}</label>
          <details className="mt-2"><summary className="text-sm cursor-pointer">查看本页原文文本</summary><p className="whitespace-pre-wrap break-words text-sm mt-2 max-h-72 overflow-y-auto">{page.text || '本页无文本层。请对照原 PDF，不把提取失败当作没有证据。'}</p></details>
        </div>)}</div>
      </details>
    </article>)}
  </div>
}
