'use client'

import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { competitionApi, downloadResearchFile, researchError, ResearchExport } from '@/lib/api/competition'

const FILE_LABELS: Record<string, string> = { 'process.json': '下载完整过程 JSON', 'evidence.csv': '下载证据矩阵 CSV', 'process.pdf': '下载可读过程 PDF' }

export function ResearchExports({ notebookId, locked }: { notebookId: string; locked: boolean }) {
  const [result, setResult] = useState<ResearchExport | null>(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  async function generate() {
    setBusy('generate')
    setError('')
    setResult(null)
    try { setResult(await competitionApi.post<ResearchExport>(notebookId, '/exports')) }
    catch (error) { setError(researchError(error)) }
    finally { setBusy('') }
  }
  async function download(file: { name: string; url: string }) {
    setBusy(file.name)
    setError('')
    try { await downloadResearchFile(file.url, file.name) }
    catch (error) { setError(researchError(error)) }
    finally { setBusy('') }
  }
  return <div className="space-y-4">
    <p className="text-sm text-muted-foreground">导出检索、阅读覆盖、写作版本与证据核验的过程记录，不包含登录令牌或论文全文。自由输入仍可能含身份信息，请下载后人工检查。过程 PDF 不是论文定稿，也不是比赛正式提交说明。</p>
    <Button onClick={() => void generate()} disabled={locked || !!busy}>{busy === 'generate' ? '正在检查并生成…' : '检查完成条件并生成过程文件'}</Button>
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    {result && <div className="space-y-3">
      {result.status !== 'formal_decision_available' || result.formal_decision_count < 1 ? <div role="alert" className="rounded-lg border border-amber-500/50 bg-amber-500/5 p-4"><h3 className="font-semibold">过程已保留 · 尚无正式证据决定</h3><p className="mt-2 text-sm">导出仅作为研究过程记录，不代表论文或参赛材料已完成。这不阻塞文献发现、辅助阅读或草稿写作；如需形成正式论据，可按需进行原文人审核验。</p></div> : <p role="status" className="text-sm">包含 {result.formal_decision_count} 项通过当前证据门禁的正式决定，但不表示论文完成。请继续人工核对内容、匿名性与引用边界；程序不保证研究结论正确。</p>}
      {result.blocked.length > 0 && <div role="alert" className="rounded-lg border p-4"><h3 className="font-semibold">部分导出文件未能生成</h3><ul className="mt-2 list-disc pl-5 text-sm">{result.blocked.map((item, index) => <li key={index}>{item}</li>)}</ul></div>}
      <div className="flex flex-wrap gap-2">{result.files.map(file => <Button key={file.name} variant="outline" disabled={!!busy || locked} onClick={() => void download(file)}>{busy === file.name ? '正在下载…' : FILE_LABELS[file.name] ?? '下载过程文件'}</Button>)}</div>
      <p className="text-xs text-muted-foreground">每次下载都会按当前记录重新核验并生成独立快照，可能与上次预览不同。已下载的旧文件不会自动更新。</p>
    </div>}
  </div>
}
