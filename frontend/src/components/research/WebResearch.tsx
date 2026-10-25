'use client'

import { useState } from 'react'
import type { ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { competitionApi, ResearchState, WebResearchRun } from '@/lib/api/competition'
import { ActionFeedback, ResearchActionProps, useResearchAction } from './PaperWorkShared'

const STATUS_LABEL: Record<WebResearchRun['status'], string> = {
  running: '处理中', success: '查阅完成', partial: '有限覆盖',
  error: '未完成回答', stale: '旧版本查阅',
}

function WebLink({ url, children }: { url: string; children: ReactNode }) {
  let safe = false
  try {
    const parsed = new URL(url)
    safe = parsed.protocol === 'https:' && !parsed.username && !parsed.password && (!parsed.port || parsed.port === '443') && !/[\s\\\u0000-\u001f\u007f]/.test(url)
  } catch { /* 不把无效或含凭据地址渲染成可执行链接 */ }
  return safe ? <a href={url} target="_blank" rel="noopener noreferrer" className="break-words underline underline-offset-4">{children}</a> : <span className="break-words">{children}</span>
}

function LookupReport({ run, state }: { run: WebResearchRun; state: ResearchState }) {
  const current = run.study_revision === state.brief?.revision
  const models = [...new Set(state.model_runs.filter(call => run.model_run_ids.includes(call.id) && call.actual_model).map(call => call.actual_model))]
  const status = STATUS_LABEL[run.status]
  const answerCall = state.model_runs.find(call => call.task === 'web_research_answer' && call.input_refs.web_research_id === run.id)
  return <article className="rounded-xl border p-4 space-y-4">
    <header className="space-y-1"><h3 className="font-semibold">{run.question}</h3><p className="text-xs text-muted-foreground">{current ? '当前研究' : '历史记录，不代表当前研究'} · 版本 {run.study_revision} · {new Date(run.created_at).toLocaleString()} · {status}</p></header>
    <p role={run.status === 'error' || run.status === 'stale' ? 'alert' : 'status'} className="text-sm">{run.message}</p>
    <p className="text-xs text-muted-foreground">{run.provider === 'bing' ? '公开网页搜索：Bing' : `搜索服务：${run.provider || '尚未取得响应'}`} · 实际模型：{models.length ? models.join('、') : '尚未取得调用证明'} · 已读网页 {run.pages.filter(page => page.status === 'read').length} / 最多尝试 4 页</p>
    {run.findings.map((finding, index) => {
      const page = run.pages.find(item => item.id === finding.page_id && item.status === 'read')
      if (!page) return null
      return <div key={index} className="rounded-lg bg-muted/30 p-4 space-y-2">
        <p className="whitespace-pre-wrap text-sm font-medium">{finding.text}</p>
        <blockquote className="border-l-2 pl-3 text-sm whitespace-pre-wrap break-words">{finding.quote}</blockquote>
        <p className="text-xs text-muted-foreground"><WebLink url={page.final_url}>{page.title || page.final_url}</WebLink> · {new Date(page.retrieved_at).toLocaleString()} · 原句匹配，待人工判断</p>
      </div>
    })}
    <details className="rounded-lg border p-3"><summary className="cursor-pointer text-sm font-medium">实际读取范围与未读原因（{run.pages.length} 页尝试）</summary><div className="mt-3 space-y-4">
      {!run.pages.length && <p className="text-sm text-muted-foreground">尚无已读取网页；搜索摘要不是网页正文。</p>}
      {run.pages.map(page => <div key={page.id} className="space-y-2 text-sm">
        <p><WebLink url={page.final_url || page.url}>{page.title || page.url}</WebLink> · {page.status === 'read' ? '已取得正文片段' : '未作为已读资料'}</p>
        <p className="text-xs text-muted-foreground">{page.message} · {new Date(page.retrieved_at).toLocaleString()}</p>
        {page.status === 'read' && <><p className="text-xs">已取得待用片段 {page.prepared_characters.toLocaleString()} / 已提取 {page.extracted_characters.toLocaleString()} 字符{page.truncated && ' · 已截断，只使用片段'}</p><p className="text-xs text-muted-foreground">{answerCall ? '已有回答调用记录；失败时不保证模型服务已接收片段。' : '尚无回答调用记录，取得片段不代表已发给模型。'}</p><details><summary className="cursor-pointer text-xs">查看已取得的网页片段</summary><p className="mt-2 max-h-72 overflow-y-auto whitespace-pre-wrap break-words rounded bg-muted/40 p-3 text-xs">{page.excerpt}</p></details></>}
      </div>)}
    </div></details>
    <details className="rounded-lg border p-3"><summary className="cursor-pointer text-sm font-medium">检索词与候选网页（不等于已读）</summary><div className="mt-3 space-y-4">
      {!run.queries.length && <p className="text-sm text-muted-foreground">尚未取得搜索响应，不能声称已经联网找到资料。</p>}
      {run.queries.map((query, index) => <div key={index} className="space-y-2 text-sm"><p className="font-medium">{query.query}</p><p className="text-xs text-muted-foreground">{query.message} · {new Date(query.searched_at).toLocaleString()}</p>{query.status === 'success' && !query.results.length && <p>本次检索没有返回候选网页，不代表相关资料不存在。</p>}<ul className="list-disc pl-5 space-y-2">{query.results.map((result, resultIndex) => <li key={resultIndex}><WebLink url={result.url}>{result.title || result.url}</WebLink>{result.snippet && <p className="text-xs text-muted-foreground">搜索摘要（未作为回答依据）：{result.snippet}</p>}</li>)}</ul></div>)}
    </div></details>
    <div className="text-sm space-y-1"><h4 className="font-medium">限制与待核验事项</h4><ul className="list-disc pl-5 space-y-1">{run.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul></div>
  </article>
}

export function WebResearch({ notebookId, state, ready, ...actions }: ResearchActionProps & { notebookId: string; state: ResearchState; ready: boolean }) {
  const [question, setQuestion] = useState(state.brief?.content.question ?? '')
  const [consent, setConsent] = useState(false)
  const action = useResearchAction(actions)
  const locked = actions.locked || !!action.busy
  const runs = state.web_research?.runs ?? []
  const current = runs.filter(run => run.study_revision === state.brief?.revision)
  const history = runs.filter(run => run.study_revision !== state.brief?.revision)
  return <div className="space-y-4">
    <p className="text-sm text-muted-foreground">不需要先上传材料。模型拟定检索词，程序搜索并读取最多 4 个公开网页，再依据实际取得的片段回答。每次通常调用模型两次；有些网站无法读取或结果无关，会如实显示，不凭记忆补充。</p>
    <div className="space-y-2"><Label htmlFor="web-research-question">让 AI 联网查什么？</Label><Textarea id="web-research-question" value={question} maxLength={4000} rows={3} disabled={locked} onChange={event => { setQuestion(event.target.value); setConsent(false) }} placeholder="例如：RAGAS 如何评估检索增强生成？请找原始文档并说明局限。" /></div>
    <label className="flex items-start gap-2 text-sm"><input type="checkbox" className="mt-1" checked={consent} disabled={locked || !ready || !state.model.configured} onChange={event => setConsent(event.target.checked)} />我同意本次将问题与检索词发送给公开搜索服务，并将取得的网页片段发送给当前模型；不包含浏览器登录态。</label>
    <Button disabled={locked || !ready || !state.model.configured || !question.trim() || !consent} onClick={() => void action.run('AI 正在搜索、读取公开网页并回答', async () => { await competitionApi.webResearch(notebookId, { question: question.trim(), consent }); setConsent(false) })}>AI 联网查阅（搜索、阅读并回答）</Button>
    {!state.model.configured && <p className="text-sm text-muted-foreground">先在模型连接中完成配置；仅配置就绪不代表已调用模型。</p>}
    {!ready && <p className="text-sm text-muted-foreground">先确认当前研究问题及资源条件。未确认的修改不会悄悄覆盖已有研究版本。</p>}
    <ActionFeedback {...action} />
    {!runs.length && <p className="text-sm text-muted-foreground">尚无联网查阅记录。点击上面的按钮才会实际联网、调用模型；打开页面或刷新不会重发请求。</p>}
    {current.map(run => <LookupReport key={run.id} run={run} state={state} />)}
    {history.length > 0 && <details className="rounded-lg border p-4"><summary className="cursor-pointer text-sm font-medium">历史版本联网查阅（{history.length} 次）</summary><div className="mt-4 space-y-4">{history.map(run => <LookupReport key={run.id} run={run} state={state} />)}</div></details>}
  </div>
}
