'use client'

import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { researchBlob, researchError } from '@/lib/api/competition'

export function PaperViewer({ url, page = 1, title, onOpened }: { url: string | null; page?: number; title: string; onOpened?: () => void }) {
  const [objectUrl, setObjectUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const currentUrl = useRef('')
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; if (currentUrl.current) URL.revokeObjectURL(currentUrl.current) }
  }, [])
  async function open() {
    if (!url) return
    setBusy(true)
    setError('')
    try {
      const blob = await researchBlob(url)
      if (!mounted.current) return
      if (currentUrl.current) URL.revokeObjectURL(currentUrl.current)
      const next = URL.createObjectURL(blob)
      currentUrl.current = next
      setObjectUrl(next)
    } catch (error) { if (mounted.current) setError(researchError(error)) }
    finally { if (mounted.current) setBusy(false) }
  }
  return <div className="space-y-3">
    <Button type="button" variant="outline" disabled={busy || !url} onClick={() => void open()}>{busy ? '正在读取原 PDF…' : `打开原 PDF · 物理第 ${page} 页`}</Button>
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    {objectUrl && <div className="space-y-2">
      <p className="text-xs text-muted-foreground">定位使用 PDF 物理页码，可能不同于论文印刷页码。若浏览器未自动定位，请在阅读器手动跳到第 {page} 页。</p>
      <iframe title={`${title}，物理第${page}页`} src={`${objectUrl}#page=${page}`} className="h-[65vh] min-h-80 w-full rounded-lg border bg-white" onLoad={() => onOpened?.()} />
      <a className="text-sm underline" href={`${objectUrl}#page=${page}`} target="_blank" rel="noopener noreferrer" onClick={() => onOpened?.()}>在新标签页打开原 PDF（浏览器不支持内嵌时使用）</a>
    </div>}
  </div>
}
