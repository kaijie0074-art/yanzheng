'use client'

import { useCallback, useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { competitionApi, downloadResearchFile, researchPath, ResearchState, WritingDraft } from '@/lib/api/competition'
import { accessLabel, ActionFeedback, CoverageDetails, PaperSourcePicker, readableSource, ResearchActionProps, SourceLink, useResearchAction } from './PaperWorkShared'

type WritingBuffer = { draftId: string; title: string; markdown: string }
function bufferKey(notebookId: string) { return `research-writing-buffer-v1:${notebookId}` }
function readWritingBuffer(notebookId: string): WritingBuffer | null {
  if (typeof window === 'undefined') return null
  try {
    const raw = window.sessionStorage.getItem(bufferKey(notebookId))
    if (!raw) return null
    const value: unknown = JSON.parse(raw)
    if (value && typeof value === 'object' && 'draftId' in value && typeof value.draftId === 'string' && 'title' in value && typeof value.title === 'string' && 'markdown' in value && typeof value.markdown === 'string') return { draftId: value.draftId, title: value.title, markdown: value.markdown }
  } catch { /* 暂存失败由实际编辑时显式提示，不把无效缓存当成研究版本。 */ }
  return null
}
function writeWritingBuffer(notebookId: string, buffer: WritingBuffer | null): boolean {
  try {
    if (buffer) window.sessionStorage.setItem(bufferKey(notebookId), JSON.stringify(buffer))
    else window.sessionStorage.removeItem(bufferKey(notebookId))
    return true
  } catch { return false }
}

function DraftEditor({ notebookId, draft, current, onVersion, onDirty, ...actions }: ResearchActionProps & {
  notebookId: string; draft: WritingDraft; current: boolean; onVersion: (draft: WritingDraft) => void; onDirty: (dirty: boolean) => void
}) {
  const originalBody = draft.markdown.split('<!-- research-workbench:references -->')[0].trimEnd()
  const [buffer] = useState(() => {
    const stored = readWritingBuffer(notebookId)
    return stored?.draftId === draft.id ? stored : null
  })
  const [title, setTitle] = useState(buffer?.title ?? draft.title)
  const [markdown, setMarkdown] = useState(buffer?.markdown ?? originalBody)
  const [restored, setRestored] = useState(!!buffer)
  const [bufferError, setBufferError] = useState('')
  const action = useResearchAction(actions)
  const locked = actions.locked || !!action.busy
  const dirty = title !== draft.title || markdown !== originalBody
  useEffect(() => { onDirty(dirty) }, [dirty, onDirty])
  function updateBuffer(nextTitle: string, nextMarkdown: string) {
    const nextDirty = nextTitle !== draft.title || nextMarkdown !== originalBody
    const stored = writeWritingBuffer(notebookId, nextDirty ? { draftId: draft.id, title: nextTitle, markdown: nextMarkdown } : null)
    setBufferError(stored ? '' : '浏览器无法暂存未保存修改；请先保存新版本后再离开，否则历史导航可能丢失未保存内容。')
    setTitle(nextTitle)
    setMarkdown(nextMarkdown)
    onDirty(nextDirty)
  }
  return <div className="space-y-4">
    <p className="text-xs text-muted-foreground">{current ? '当前研究版本' : '历史草稿，不代表当前研究；只读和导出'} {draft.study_revision} · {draft.kind === 'manual' ? '人工编辑版本' : 'AI 辅助版本'} · {new Date(draft.created_at).toLocaleString()}{draft.parent_id && ' · 已保留上一版本'} · 待人工核验</p>
    <div className="space-y-2"><Label htmlFor="writing-title">草稿标题</Label><Input id="writing-title" value={title} disabled={locked || (!current && !dirty)} readOnly={!current} onChange={event => updateBuffer(event.target.value, markdown)} /></div>
    <div className="space-y-2"><Label htmlFor="writing-markdown">人工编辑草稿（Markdown，保留引用编号）</Label><Textarea id="writing-markdown" value={markdown} disabled={locked || (!current && !dirty)} readOnly={!current} rows={18} onChange={event => updateBuffer(title, event.target.value)} /><p className="text-xs text-muted-foreground">只编辑正文；参考文献与覆盖说明由系统根据已保存来源生成。需要修正书目时请前往<a href="#sources" className="underline">资料库</a>，不要在正文补造作者或 DOI。</p></div>
    {dirty && <p className="text-sm">有未保存修改；下载的是已保存版本。人工新增内容不会自动获得引文支持。</p>}
    {restored && dirty && <p className="text-sm">已恢复本标签页上次未保存的标题和正文。临时缓冲不等于已保存版本，请保存新版本或明确放弃。</p>}
    {bufferError && <p role="alert" className="text-sm text-destructive">{bufferError}</p>}
    {dirty && !current && <p role="alert" className="text-sm text-destructive">研究版本已变化：以上标题和正文是旧版本的未保存编辑，已保留供复制恢复，不能保存为当前研究成果。请先复制需要的内容，再放弃此缓冲以选择其他版本。</p>}
    {dirty && <Button variant="outline" disabled={!!action.busy} onClick={() => {
      if (!window.confirm('确定放弃当前未保存的标题和正文？需要保留的内容请先复制。')) return
      updateBuffer(draft.title, originalBody)
      setRestored(false)
    }}>放弃未保存修改，恢复已保存稿</Button>}
    <Button disabled={locked || !current || !dirty || !markdown.trim() || !title.trim()} onClick={() => void action.run('正在保存人工编辑版本（不调用模型）', async () => {
      const saved = await competitionApi.saveWriting(notebookId, draft.id, { markdown, title: title.trim() })
      const pending = readWritingBuffer(notebookId)
      if (pending?.draftId === draft.id && pending.title === title && pending.markdown === markdown) writeWritingBuffer(notebookId, null)
      onDirty(false)
      onVersion(saved)
      return '已保存为新版本，原版本及引用快照保留。人工编辑不等于核验通过。'
    })}>保存人工编辑为新版本（不调用 AI）</Button>
    <div className="flex flex-wrap gap-2">{([{ format: 'docx', label: 'Word' }, { format: 'md', label: 'Markdown' }, { format: 'bib', label: 'BibTeX' }] as const).map(({ format, label }) => <Button key={format} variant="outline" disabled={locked} onClick={() => void action.run(`正在下载 ${label}`, async () => { await downloadResearchFile(researchPath(notebookId, `/writing/${encodeURIComponent(draft.id)}/export?format=${format}`), `research-draft.${format}`); return `${label} 已准备下载，包含草稿与待核验标识。` })}>下载 {label}</Button>)}</div>
    <ActionFeedback {...action} />
    <CoverageDetails coverage={draft.coverage} />
    <div className="space-y-2 text-sm"><h4 className="font-medium">真实来源引用与访问层级</h4>{!draft.references.length && <p>没有参考文献；仅为提纲 / 结构建议，不可当作文献结论。</p>}{draft.references.map(reference => <div key={reference.number} className="rounded-lg border p-3"><p>[{reference.number}] <SourceLink url={reference.official_url}>{reference.title}</SourceLink></p><p className="mt-1 text-xs text-muted-foreground">{reference.authors.join('；') || '作者未知'} · {reference.year ?? '年份未知'} · {accessLabel(reference.access_level)} · {reference.bibliography_status === 'confirmed' ? '书目已确认，论述仍待核验' : '书目待人工确认'}{reference.pages.length > 0 && ` · 物理页 ${reference.pages.join('、')}`}</p>{reference.doi && <p className="text-xs break-all">DOI：{reference.doi}</p>}</div>)}</div>
  </div>
}

export function PaperWriting({ notebookId, state, ready, onDirtyChange, ...actions }: ResearchActionProps & {
  notebookId: string; state: ResearchState; ready: boolean; onDirtyChange?: (dirty: boolean) => void
}) {
  const [selected, setSelected] = useState<string[]>([])
  const [kind, setKind] = useState<'outline' | 'draft' | 'revision'>('outline')
  const [instructions, setInstructions] = useState('')
  const [rights, setRights] = useState(false)
  // 固定正在查看的快照；外部新增版本或研究换版不能替换本地编辑缓冲。
  const stored = state.paper_work?.writing_drafts ?? []
  const [active, setActive] = useState<WritingDraft | null>(() => {
    const buffer = readWritingBuffer(notebookId)
    return stored.find(draft => draft.id === buffer?.draftId) ?? [...stored].sort((a, b) => b.created_at.localeCompare(a.created_at))[0] ?? null
  })
  const [dirty, setDirty] = useState(false)
  const action = useResearchAction(actions)
  const locked = actions.locked || !!action.busy
  const drafts = (active && !stored.some(draft => draft.id === active.id) ? [active, ...stored] : [...stored]).sort((a, b) => b.created_at.localeCompare(a.created_at))
  const firstDraft = drafts[0]
  useEffect(() => {
    if (!active && firstDraft) setActive(firstDraft)
  }, [active, firstDraft])
  const current = active?.study_revision === state.brief?.revision
  const sourceIds = selected.filter(id => state.sources.some(source => source.id === id && readableSource(source)))
  const canGenerate = ready && state.model.configured && instructions.trim() && (kind === 'outline' || sourceIds.length > 0) && (!sourceIds.length || rights) && (kind !== 'revision' || (active && current)) && !dirty
  const reportDirty = useCallback((value: boolean) => {
    setDirty(value)
    onDirtyChange?.(value)
  }, [onDirtyChange])
  function selectVersion(draft: WritingDraft) {
    setActive(draft)
    reportDirty(false)
  }
  return <div className="space-y-5">
    <p className="text-sm text-muted-foreground">无需先通过证据卡人审即可写作草稿。模型基于所选真实摘要 / 正文辅助组织，不替你确认书目、研究发现或最终结论。提纲可无文献；实质段落与改写至少选择一份可读资料。</p>
    <div className="space-y-2"><Label htmlFor="writing-kind">写作任务</Label><select id="writing-kind" className="block rounded-md border bg-background p-2" value={kind} disabled={locked} onChange={event => setKind(event.target.value as typeof kind)}><option value="outline">生成论文提纲</option><option value="draft">基于文献生成段落草稿</option><option value="revision">根据指令改写当前选中版本</option></select></div>
    <PaperSourcePicker sources={state.sources} selected={sourceIds} disabled={locked} onChange={ids => { setSelected(ids); setRights(false) }} />
    <div className="space-y-2"><Label htmlFor="writing-instructions">写作 / 改写要求</Label><Textarea id="writing-instructions" value={instructions} disabled={locked} onChange={event => setInstructions(event.target.value)} rows={3} placeholder="例如：组织一段文献综述，比较研究方法与限制，保留来源引用，不补造结果。" /></div>
    <label className="flex items-start gap-2 text-sm"><input type="checkbox" className="mt-1" checked={rights} disabled={locked || !sourceIds.length} onChange={event => setRights(event.target.checked)} />我有权将所选摘要 / 正文片段发送给当前模型，并同意本次真实调用。</label>
    <Button disabled={locked || !canGenerate} onClick={() => void action.run('AI 正在生成论文辅助草稿', async () => { const draft = await competitionApi.write(notebookId, { kind, instructions: instructions.trim(), source_ids: sourceIds, rights_confirmed: rights, parent_id: kind === 'revision' ? active?.id : null }); selectVersion(draft); return '草稿已保存。请检查覆盖范围、来源引用并人工修订。' })}>{kind === 'outline' ? 'AI 生成提纲' : kind === 'draft' ? 'AI 生成有引用草稿' : 'AI 按指令改写选中版本'}（模型调用）</Button>
    {!state.model.configured && <p className="text-sm text-muted-foreground">先配置模型，才能生成或改写；已有草稿仍可人工编辑和下载。</p>}
    {!ready && <p className="text-sm text-muted-foreground">请先保存并确认研究问题。</p>}
    {kind !== 'outline' && !sourceIds.length && <p className="text-sm text-muted-foreground">请选择至少一份带摘要或正文的资料。只有书目时，请先获取开放全文或另选有摘要文献。</p>}
    {kind === 'revision' && (!active || !current) && <p className="text-sm text-muted-foreground">改写需先选择当前研究版本的草稿；历史版本只能阅读和导出。</p>}
    {dirty && <p className="text-sm">请先保存或明确放弃人工修改，再切换版本或调用 AI；未保存内容不会自动转为新研究版本。</p>}
    <ActionFeedback {...action} />
    <div className="space-y-3"><Label htmlFor="writing-version">已保存版本（{drafts.length}）</Label><select id="writing-version" className="block w-full rounded-md border bg-background p-2" value={active?.id ?? ''} disabled={locked || dirty || !drafts.length} onChange={event => { const draft = drafts.find(item => item.id === event.target.value); if (draft) selectVersion(draft) }}>{!drafts.length && <option value="">尚无草稿</option>}{drafts.map(draft => <option key={draft.id} value={draft.id}>{draft.title} · {draft.study_revision === state.brief?.revision ? '当前研究' : '历史研究'} v{draft.study_revision} · {draft.kind === 'manual' ? '人工编辑' : 'AI 辅助'} · {new Date(draft.created_at).toLocaleString()} · {draft.id.slice(0, 8)}</option>)}</select>
      {active && <DraftEditor key={active.id} notebookId={notebookId} draft={active} current={current} onVersion={selectVersion} onDirty={reportDirty} {...actions} />}
    </div>
  </div>
}
