// Server-side only: check the original request before rewrites replace Host.
const LOCAL_HOSTS: Record<string, true> = {
  '127.0.0.1:3000': true,
  'localhost:3000': true,
  '[::1]:3000': true,
}
const LOCAL_ORIGINS: Record<string, true> = {
  'http://127.0.0.1:3000': true,
  'http://localhost:3000': true,
}
const SAFE_METHODS: Record<string, true> = { GET: true, HEAD: true, OPTIONS: true }

export function checkLocalRequestBoundary(request: Pick<Request, 'headers' | 'method'>): Response | null {
  if (process.env.RESEARCH_LOCAL_ONLY !== '1') return null

  // Never authorize using request.url or X-Forwarded-*; either may have
  // already been normalized by a proxy. The fixed port is part of the boundary.
  const host = request.headers.get('host')
  if (host === null || LOCAL_HOSTS[host.toLowerCase()] !== true) {
    return Response.json({ detail: '本机入口不接受外部主机地址' }, { status: 403 })
  }

  const origin = request.headers.get('origin')
  if (SAFE_METHODS[request.method.toUpperCase()] !== true && origin !== null && LOCAL_ORIGINS[origin] !== true) {
    return Response.json({ detail: '已阻止其他网站向本机研究服务提交操作' }, { status: 403 })
  }

  return null
}
