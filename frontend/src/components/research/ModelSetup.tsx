'use client'

import { useCallback, useEffect, useState } from 'react'
import Link from 'next/link'
import { Button } from '@/components/ui/button'
import { competitionApi, LocalModelStatus, researchError } from '@/lib/api/competition'

export function ModelSetup({ onConfigured }: { onConfigured?: () => void }) {
  const [status, setStatus] = useState<LocalModelStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try { setStatus(await competitionApi.localStatus()) }
    catch (error) { setError(researchError(error)) }
    finally { setLoading(false) }
  }, [])
  useEffect(() => { void load() }, [load])
  async function setup() {
    setBusy(true)
    setError('')
    setMessage('')
    try {
      await competitionApi.setup()
      setStatus(await competitionApi.localStatus())
      setMessage('配置已保存，已有的明确默认模型会保留。启用不等于推理连通验证；分析时才会真实调用账号后端。')
      onConfigured?.()
    } catch (error) { setError(researchError(error)) }
    finally { setBusy(false) }
  }
  return <section className="rounded-xl border bg-muted/30 p-5 space-y-3" aria-label="模型连接">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <h2 className="font-semibold">连接 AI · 使用本人本机账号</h2>
      {status?.configured && <span className="text-sm text-muted-foreground">语言模型已配置 · {status.embedding_ready ? '本地检索向量已就绪' : '本地检索向量未就绪'}</span>}
    </div>
    <p className="text-sm text-muted-foreground">复用本机 OMP 已登录账号，不读取或复制登录令牌。启用配置语言模型与真实本地 embedding，不推理、不下载模型，也不覆盖已有的明确默认模型。本机账号模型仍会联网，分析可能消耗额度；回答完成后一次返回，不是实时逐字流。</p>
    {status?.model && <p className="text-sm">当前模型：<span className="break-all">{status.model}</span></p>}
    {loading && <p role="status" className="text-sm">正在检查本机配置…</p>}
    {status && !status.available && <p className="text-sm">未找到 OMP。请先在本机安装并登录，或使用下方入口配置自己的 API；仍可创建研究和导入材料。</p>}
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    {message && <p role="status" className="text-sm">{message}</p>}
    <div className="flex flex-wrap gap-2">
      <Button type="button" onClick={() => void setup()} disabled={loading || busy || !status?.available}>{busy ? '正在配置…' : status?.configured ? '重新配置本机账号与本地检索' : '启用本人本机账号与本地检索'}</Button>
      <Button type="button" variant="outline" disabled={loading || busy} onClick={() => void load()}>刷新连接状态</Button>
      <Button variant="ghost" asChild><Link href="/settings/models">使用自己的 API / 高级模型管理</Link></Button>
    </div>
  </section>
}
