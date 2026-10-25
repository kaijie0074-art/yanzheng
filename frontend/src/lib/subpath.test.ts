import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AxiosError } from 'axios'
import { NextRequest } from 'next/server'

// Dynamic imports intentionally reload build-time APP_BASE_PATH after each
// environment change; static imports cannot exercise both deployment builds.

const deployments = [
  { prefix: '', runtimeUrl: '', apiBase: '' },
  { prefix: '/yanzheng', runtimeUrl: '', apiBase: '/yanzheng' },
  { prefix: '/yanzheng', runtimeUrl: 'https://kaijie.store/yanzheng/', apiBase: 'https://kaijie.store/yanzheng' },
]

afterEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
})

describe.each(deployments)('部署前缀 $prefix，公开 API $runtimeUrl', ({ prefix, runtimeUrl, apiBase }) => {
  const transport = vi.fn<typeof fetch>()

  beforeEach(() => {
    vi.resetModules()
    vi.stubEnv('NEXT_PUBLIC_BASE_PATH', prefix)
    vi.stubEnv('NEXT_PUBLIC_API_URL', '')
    vi.stubEnv('API_URL', '')
    vi.stubEnv('RESEARCH_LOCAL_ONLY', '0')
    localStorage.clear()
    transport.mockReset()
    transport.mockImplementation(async input => {
      if (input === `${prefix}/config`) return Response.json({ apiUrl: runtimeUrl })
      if (input === `${apiBase}/api/config`) return Response.json({ version: 'test', dbStatus: 'online' })
      throw new Error(`请求落在非预期地址：${String(input)}`)
    })
    vi.stubGlobal('fetch', transport)
  })

  it('配置发现请求与缓存均留在应用路径，公开地址不重复添加前缀', async () => {
    const { getConfig, getApiUrl } = await import('./config')
    expect(await getApiUrl()).toBe(apiBase)
    expect((await getConfig()).dbStatus).toBe('online')
    expect(transport.mock.calls.map(([url]) => url)).toEqual([`${prefix}/config`, `${apiBase}/api/config`])
  })

  it('服务端运行配置优先使用公开 URL，子站缺省不暴露内部端口', async () => {
    const { GET } = await import('../app/config/route')
    const request = new NextRequest(`https://kaijie.store${prefix}/config`, {
      headers: { host: 'kaijie.store', 'x-forwarded-proto': 'https' },
    })
    const fallback = await GET(request)
    expect((await fallback.json()).apiUrl).toBe(prefix || 'https://kaijie.store:5055')
    vi.stubEnv('API_URL', 'https://kaijie.store/yanzheng')
    expect((await (await GET(request)).json()).apiUrl).toBe('https://kaijie.store/yanzheng')
  })

  it('密码验证通过公开 API，失败不会把用户导航到主站', async () => {
    const { useAuthStore } = await import('./stores/auth-store')
    await import('./config').then(module => module.getConfig())
    transport.mockResolvedValueOnce(new Response(null, { status: 401 }))
    expect(await useAuthStore.getState().login('wrong-test-password')).toBe(false)
    expect(useAuthStore.getState().isAuthenticated).toBe(false)
    transport.mockResolvedValueOnce(new Response(null, { status: 200 }))
    expect(await useAuthStore.getState().login('test-password')).toBe(true)
    expect(useAuthStore.getState().token).toBe('test-password')
    expect(transport).toHaveBeenLastCalledWith(`${apiBase}/api/notebooks`, expect.objectContaining({
      headers: expect.objectContaining({ Authorization: 'Bearer test-password' }),
    }))
    useAuthStore.setState({ lastAuthCheck: null })
    transport.mockResolvedValueOnce(new Response(null, { status: 401 }))
    expect(await useAuthStore.getState().checkAuth()).toBe(false)
    expect(useAuthStore.getState().token).toBeNull()
  })


  it('普通请求过期后清除认证并导航到子站登录页', async () => {
    const storage = window.localStorage
    storage.setItem('auth-storage', JSON.stringify({ state: { token: 'expired-test-only' } }))
    const location = { href: `${prefix}/research` }
    vi.stubGlobal('window', { localStorage: storage, location })
    const { apiClient } = await import('./api/client')
    apiClient.defaults.adapter = async config => {
      throw new AxiosError('Unauthorized', 'ERR_BAD_REQUEST', config, undefined, {
        data: {}, status: 401, statusText: 'Unauthorized', headers: {}, config,
      })
    }
    await expect(apiClient.get('/notebooks')).rejects.toThrow('Unauthorized')
    expect(storage.getItem('auth-storage')).toBeNull()
    expect(location.href).toBe(`${prefix}/login`)
  })


  it('首页跳转保留部署前缀，不跳到主站研究页', async () => {
    const { proxy } = await import('../proxy')
    const request = new NextRequest(`https://kaijie.store${prefix}/`, { nextConfig: { basePath: prefix } })
    const target = new URL(proxy(request).headers.get('location')!)
    expect(target.origin).toBe('https://kaijie.store')
    expect(target.pathname.replace(/\/$/, '')).toBe(`${prefix}/research`)
  })

  it('登录回跳仅接受当前应用，并向 Next router 传递不含前缀的路径', async () => {
    const { loginDestination } = await import('./app-paths')
    const origin = 'https://kaijie.store'
    expect(loginDestination(`${prefix}/research/notebook%3Atest?tab=paper#sources`, origin)).toBe('/research/notebook%3Atest?tab=paper#sources')
    expect(loginDestination(`${origin}${prefix}/settings/models`, origin)).toBe('/settings/models')
    expect(loginDestination('https://other.example/research', origin)).toBe('/research')
    expect(loginDestination('//other.example/research', origin)).toBe('/research')
    expect(loginDestination(`${prefix}/login`, origin)).toBe('/research')
    if (prefix) {
      expect(loginDestination('/settings/models', origin)).toBe('/research')
      expect(loginDestination('/yanzheng-other/research', origin)).toBe('/research')
    }
  })
})
