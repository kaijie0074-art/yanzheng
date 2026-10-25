import { NextResponse } from 'next/server'
import type { NextRequest } from 'next/server'
import { checkLocalRequestBoundary } from '@/lib/local-request-boundary'

export function proxy(request: NextRequest) {
  const blocked = checkLocalRequestBoundary(request)
  if (blocked) return blocked

  const { pathname } = request.nextUrl

  // Redirect root to the research workbench.
  if (pathname === '/') {
    const destination = request.nextUrl.clone()
    destination.pathname = '/research'
    return NextResponse.redirect(destination)
  }

  return NextResponse.next()
}

export const config = {
  matcher: [
    '/((?!_next/static|_next/image|favicon.ico).*)',
  ],
}
