// Build-time deployment prefix. Next Link/router already add this; only native
// browser requests, assets and full-page navigation should prepend it.
export const APP_BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH || ''

// Saved browser locations include basePath; Next router destinations must not.
// Never restore a location outside this application (including the main site).
export function loginDestination(saved: string | null, origin: string): string {
  if (!saved) return '/research'
  try {
    const target = new URL(saved, origin)
    if (target.origin !== origin) return '/research'
    if (APP_BASE_PATH && target.pathname !== APP_BASE_PATH && !target.pathname.startsWith(`${APP_BASE_PATH}/`)) {
      return '/research'
    }
    const pathname = target.pathname.slice(APP_BASE_PATH.length) || '/'
    if (pathname.startsWith('//') || pathname === '/login') return '/research'
    return pathname + target.search + target.hash
  } catch {
    return '/research'
  }
}
