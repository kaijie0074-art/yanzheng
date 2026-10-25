import type { NextRequest } from 'next/server'
import { checkLocalRequestBoundary } from '@/lib/local-request-boundary'

const INTERNAL_API_URL = process.env.INTERNAL_API_URL || 'http://localhost:5055'

export async function sseProxy(req: NextRequest, upstreamPath: string) {
  const blocked = checkLocalRequestBoundary(req)
  if (blocked) return blocked

  const body = await req.text()
  const auth = req.headers.get('authorization')
  const origin = req.headers.get('origin')

  const upstream = await fetch(`${INTERNAL_API_URL}${upstreamPath}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(auth ? { Authorization: auth } : {}),
      ...(origin !== null ? { Origin: origin } : {}),
      Accept: 'text/event-stream',
    },
    body,
    cache: 'no-store',
  })

  if (!upstream.ok || !upstream.body) {
    return new Response(upstream.body, {
      status: upstream.status,
      headers: { 'Content-Type': upstream.headers.get('content-type') ?? 'application/json' },
    })
  }

  return new Response(upstream.body, {
    status: 200,
    headers: {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache, no-transform',
      Connection: 'keep-alive',
      'X-Accel-Buffering': 'no',
    },
  })
}
