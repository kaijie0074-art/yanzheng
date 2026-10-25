'use client'

import { FormEvent, useEffect, useState } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { AppShell } from '@/components/layout/AppShell'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import { ModelSetup } from '@/components/research/ModelSetup'
import { competitionApi, researchError } from '@/lib/api/competition'
import { notebooksApi } from '@/lib/api/notebooks'
import type { NotebookResponse } from '@/lib/types/api'

export default function ResearchPage() {
  const router = useRouter()
  const [question, setQuestion] = useState('')
  const [resources, setResources] = useState('')
  const [notebooks, setNotebooks] = useState<NotebookResponse[]>([])
  const [showArchived, setShowArchived] = useState(false)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [listError, setListError] = useState('')
  async function load() {
    setLoading(true)
    setListError('')
    try { setNotebooks(await notebooksApi.list()) }
    catch (error) { setListError(researchError(error)) }
    finally { setLoading(false) }
  }
  useEffect(() => { void load() }, [])
  async function create(event: FormEvent) {
    event.preventDefault()
    if (!question.trim() || busy) return
    setBusy(true)
    setError('')
    try {
      const result = await competitionApi.create(question.trim(), resources.trim())
      router.push(`/research/${encodeURIComponent(result.notebook_id)}`)
    } catch (error) { setError(researchError(error)); setBusy(false) }
  }
  const visibleNotebooks = notebooks.filter(notebook => !!notebook.archived === showArchived)
  const archivedCount = notebooks.filter(notebook => notebook.archived).length
  return <AppShell><div className="flex-1 overflow-y-auto"><div className="mx-auto max-w-5xl p-5 md:p-8 space-y-7">
    <header className="space-y-3">
      <p className="text-sm font-medium text-muted-foreground">研证 · AI 科研与论文辅助工作台</p>
      <h1 className="text-3xl font-bold tracking-tight">从研究兴趣，到可追溯的论文草稿</h1>
      <p className="text-muted-foreground">让 AI 帮你制定检索策略、查找真实文献、阅读资料和组织写作。不必先准备 PDF；引用来源、阅读范围和待核验状态始终可见。</p>
    </header>
    <form onSubmit={create} className="rounded-xl border bg-card p-6 space-y-4">
      <h2 className="text-lg font-semibold">开始一项研究</h2>
      <div className="space-y-2"><Label htmlFor="research-question">用一句话描述你想解决的问题</Label><Textarea id="research-question" value={question} onChange={event => setQuestion(event.target.value)} placeholder="例如：城市绿地如何影响居民的心理健康？" required rows={3} /></div>
      <div className="space-y-2"><Label htmlFor="research-resources">可用资源与限制（选填）</Label><Textarea id="research-resources" value={resources} onChange={event => setResources(event.target.value)} placeholder="例如：本机 CPU、公开论文、两周时间、不能使用敏感数据。" rows={2} /></div>
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      <Button type="submit" disabled={busy || !question.trim()}>{busy ? '正在创建…' : '确认问题并创建研究'}</Button>
      <p className="text-xs text-muted-foreground">点击创建即确认当前问题，不调用 AI。下一步可直接让 AI 找文献；问题和资源条件随时可以修改。</p>
    </form>
    <ModelSetup />
    <section className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-xl font-semibold">{showArchived ? '历史与归档研究' : '继续我的研究'}</h2><div className="flex gap-2"><Button variant="outline" onClick={() => setShowArchived(value => !value)}>{showArchived ? '返回当前研究' : `历史与归档（${archivedCount}）`}</Button><Button variant="ghost" disabled={loading} onClick={() => void load()}>刷新列表</Button></div></div>
      {loading && <p role="status">正在读取笔记本…</p>}
      {listError && <p role="alert" className="text-destructive">{listError}</p>}
      {!loading && !listError && visibleNotebooks.length === 0 && <p className="rounded-lg border border-dashed p-6 text-muted-foreground">{showArchived ? '没有已归档研究。归档只改变列表位置，不会删除材料或研究记录。' : '先在上方写下自己的研究问题，AI 会帮助查找资料；不需要先准备 PDF 或完整方案。'}</p>}
      <div className="grid gap-4 md:grid-cols-2">{visibleNotebooks.map(notebook => <article key={notebook.id} className="rounded-xl border p-5 space-y-3">
        <h3 className="font-semibold break-words">{notebook.name}</h3>
        {notebook.description && <p className="text-sm text-muted-foreground line-clamp-3">{notebook.description}</p>}
        <div className="flex flex-wrap gap-2"><Button asChild><Link href={`/research/${encodeURIComponent(notebook.id)}`}>进入研究</Link></Button><Button asChild variant="outline"><Link href={`/notebooks/${encodeURIComponent(notebook.id)}`}>资料 · 聊天 · 笔记</Link></Button></div>
      </article>)}</div>
    </section>
    <footer className="border-t pt-4 text-xs text-muted-foreground">基于 Open Notebook（MIT）构建。模型建议不等于事实，原句匹配不等于推论成立；未经人工核验的过程不会被包装成已完成的科研结论。</footer>
  </div></div></AppShell>
}
