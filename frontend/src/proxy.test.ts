import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { NextRequest } from 'next/server'
import { proxy } from './proxy'
import { POST as ask } from './app/api/search/ask/route'
import { POST as sourceChat } from './app/api/sources/[sourceId]/chat/sessions/[sessionId]/messages/route'

const sseRoutes = [
  { path: '/api/search/ask', handle: ask },
  {
    path: '/api/sources/source:example/chat/sessions/session:example/messages',
    handle: (request: NextRequest) => sourceChat(request, {
      params: Promise.resolve({ sourceId: 'source:example', sessionId: 'session:example' }),
    }),
  },
]

function request(path: string, method = 'GET', headers: Record<string, string> = {}) {
  // Keep the URL local even with an adversarial Host, as happens after
  // normalization. Authorization must inspect the actual Host header.
  return new NextRequest(`http://127.0.0.1:3000${path}`, {
    method,
    headers: { host: '127.0.0.1:3000', ...headers },
    ...(method === 'POST' ? { body: '{"question":"定位原文证据"}' } : {}),
  })
}

beforeEach(() => {
  vi.stubEnv('RESEARCH_LOCAL_ONLY', '1')
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('rewrite 前的本机请求边界', () => {

  it.each(['evil.example:3000', 'localhost:3001', 'localhost', '127.0.0.2:3000', '[::1]:5055', '__proto__', 'localhost:3000,evil.example'])(
    '拒绝不受信 Host 的读取：%s', host => {
      const response = proxy(request('/api/notebooks', 'GET', {
        host,
        'x-forwarded-host': '127.0.0.1:3000',
        'x-forwarded-proto': 'http',
      }))
      expect(response.status).toBe(403)
      expect(response.headers.get('x-middleware-next')).toBeNull()
    },
  )

  it('缺少原始 Host 时不使用 URL 或转发头授权', () => {
    const incoming = request('/api/notebooks')
    incoming.headers.delete('host')
    incoming.headers.set('x-forwarded-host', 'localhost:3000')
    expect(proxy(incoming).status).toBe(403)
  })

  it.each(['http://evil.example', 'null', '', 'https://localhost:3000', 'http://localhost:3001', 'http://[::1]:3000'])(
    '拒绝不受信 Origin 的写入：%s', origin => {
      expect(proxy(request('/api/notebooks', 'POST', { origin, 'content-type': 'text/plain' })).status).toBe(403)
    },
  )

  it.each(['127.0.0.1:3000', 'localhost:3000', '[::1]:3000'])(
    '允许无 Origin 的本机 CLI 读写：%s', host => {
      for (const method of ['GET', 'POST']) {
        const response = proxy(request('/api/notebooks', method, { host, 'x-forwarded-host': 'evil.example' }))
        expect(response.status).toBe(200)
        expect(response.headers.get('x-middleware-next')).toBe('1')
      }
    },
  )

  it.each(['http://127.0.0.1:3000', 'http://localhost:3000'])(
    '允许本机浏览器写入：%s', origin => {
      expect(proxy(request('/api/notebooks', 'POST', { origin })).headers.get('x-middleware-next')).toBe('1')
    },
  )

  it('保留首页进入研究工作台的行为', () => {
    const response = proxy(request('/'))
    expect(response.status).toBe(307)
    expect(new URL(response.headers.get('location')!).pathname).toBe('/research')
  })

  it.each([undefined, '0'] as const)('关闭本机标志 %s 时保留通用部署行为', flag => {
    vi.stubEnv('RESEARCH_LOCAL_ONLY', flag)
    const response = proxy(request('/api/notebooks', 'POST', { host: 'notebook.example:443', origin: 'https://notebook.example' }))
    expect(response.headers.get('x-middleware-next')).toBe('1')
  })
})

describe.each(sseRoutes)('流式入口 $path 的独立边界', ({ path, handle }) => {
  it.each([
    { host: 'evil.example:3000', origin: 'http://localhost:3000' },
    { host: 'localhost:3000', origin: 'https://evil.example' },
    { host: 'localhost:3000', origin: 'null' },
  ])('在读取 text/plain 请求体和访问后端之前拒绝 $host / $origin', async headers => {
    const upstream = vi.fn<typeof fetch>()
    vi.stubGlobal('fetch', upstream)
    const incoming = request(path, 'POST', {
      ...headers,
      'content-type': 'text/plain',
      'x-forwarded-host': 'localhost:3000',
      'x-forwarded-proto': 'http',
    })
    const readBody = vi.spyOn(incoming, 'text')

    const response = await handle(incoming)

    expect(response.status).toBe(403)
    expect(readBody).not.toHaveBeenCalled()
    expect(incoming.bodyUsed).toBe(false)
    expect(upstream).not.toHaveBeenCalled()
  })

  it.each([undefined, 'http://127.0.0.1:3000', 'http://localhost:3000'])(
    '接受本机请求并保留真实 Origin：%s', async origin => {
      // Transport fixture only: no simulated model answer or inference.
      const upstream = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 204 }))
      vi.stubGlobal('fetch', upstream)
      const incoming = request(path, 'POST', {
        ...(origin !== undefined ? { origin } : {}),
        authorization: 'Bearer test-only',
        'x-forwarded-host': 'evil.example',
        'x-forwarded-proto': 'https',
        'x-forwarded-for': '198.51.100.1',
      })

      const response = await handle(incoming)

      expect(response.status).toBe(204)
      expect(upstream).toHaveBeenCalledOnce()
      const sentHeaders = new Headers(upstream.mock.calls[0][1]?.headers)
      expect(sentHeaders.get('origin')).toBe(origin ?? null)
      expect(sentHeaders.get('authorization')).toBe('Bearer test-only')
      expect(sentHeaders.get('x-forwarded-host')).toBeNull()
      expect(sentHeaders.get('x-forwarded-proto')).toBeNull()
      expect(sentHeaders.get('x-forwarded-for')).toBeNull()
    },
  )

  it('关闭本机标志后允许自托管域名，但仍将 Origin 留给后端判断', async () => {
    vi.stubEnv('RESEARCH_LOCAL_ONLY', '0')
    const upstream = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', upstream)

    const response = await handle(request(path, 'POST', {
      host: 'notebook.example:443',
      origin: 'https://notebook.example',
    }))

    expect(response.status).toBe(204)
    expect(upstream).toHaveBeenCalledOnce()
    expect(new Headers(upstream.mock.calls[0][1]?.headers).get('origin')).toBe('https://notebook.example')
  })
})
