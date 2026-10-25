import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import ApiKeysPage from './page'
import { ProviderInfo } from '@/lib/api/providers'
import { Credential } from '@/lib/api/credentials'

// useTranslation is mocked globally in setup.ts (t returns the key string)

vi.mock('@/components/layout/AppShell', () => ({
  AppShell: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}))

vi.mock('@/components/settings', () => ({
  MigrationBanner: () => null,
  DefaultModelSelectors: () => null,
  ProviderSection: ({ provider }: { provider: ProviderInfo }) => (
    <div data-testid="provider-section">{provider.display_name}</div>
  ),
}))

const mockUseModels = vi.fn()
vi.mock('@/lib/hooks/use-models', () => ({
  useModels: () => mockUseModels(),
  useModelDefaults: vi.fn(() => ({ data: null, isLoading: false })),
}))

const mockUseCredentials = vi.fn()
vi.mock('@/lib/hooks/use-credentials', () => ({
  useCredentials: () => mockUseCredentials(),
  useCredentialStatus: vi.fn(() => ({
    data: { configured: {}, source: {}, encryption_configured: true },
  })),
  useEnvStatus: vi.fn(() => ({ data: {} })),
}))

const mockUseProviders = vi.fn()
vi.mock('@/lib/hooks/use-providers', () => ({
  useProviders: () => mockUseProviders(),
}))

const providers: ProviderInfo[] = [
  {
    name: 'openai',
    display_name: 'OpenAI',
    modalities: ['language'],
    configuration_type: 'credential',
    docs_url: null,
    env_configured: false,
  },
  {
    name: 'anthropic',
    display_name: 'Anthropic',
    modalities: ['language'],
    configuration_type: 'credential',
    docs_url: null,
    env_configured: false,
  },
]

const anthropicCredential = {
  id: 'credential:1',
  name: 'Anthropic Prod',
  provider: 'anthropic',
  modalities: ['language'],
  has_api_key: true,
  created: '2026-01-01T00:00:00Z',
  updated: '2026-01-01T00:00:00Z',
  model_count: 0,
} as Credential

describe('ApiKeysPage', () => {
  beforeEach(() => {
    mockUseModels.mockReturnValue({ data: [], isLoading: false })
    mockUseCredentials.mockReturnValue({ data: [], isLoading: false })
    mockUseProviders.mockReturnValue({
      data: providers,
      isLoading: false,
      isError: false,
    })
  })

  it('renders one section per provider from GET /api/providers', () => {
    render(<ApiKeysPage />)
    const sections = screen.getAllByTestId('provider-section')
    expect(sections.map(s => s.textContent)).toEqual(['OpenAI', 'Anthropic'])
  })

  it('sorts configured providers first, keeping backend order otherwise', () => {
    mockUseCredentials.mockReturnValue({
      data: [anthropicCredential],
      isLoading: false,
    })
    render(<ApiKeysPage />)
    const sections = screen.getAllByTestId('provider-section')
    expect(sections.map(s => s.textContent)).toEqual(['Anthropic', 'OpenAI'])
  })

  it('sorts registered local models alongside configured API providers', () => {
    mockUseProviders.mockReturnValue({
      data: [
        ...providers,
        { name: 'omp', display_name: 'OMP', configuration_type: 'local_account', modalities: ['language'], env_configured: false },
        { name: 'fastembed', display_name: 'FastEmbed', configuration_type: 'local_onnx', modalities: ['embedding'], env_configured: false },
      ],
      isLoading: false,
      isError: false,
    })
    mockUseCredentials.mockReturnValue({ data: [anthropicCredential], isLoading: false })
    mockUseModels.mockReturnValue({
      data: [
        { id: 'model:omp', name: 'account-model', provider: 'omp', type: 'language', credential: null, created: '', updated: '' },
        { id: 'model:fastembed', name: 'wrong-modality', provider: 'fastembed', type: 'language', credential: null, created: '', updated: '' },
      ],
      isLoading: false,
    })
    render(<ApiKeysPage />)
    expect(screen.getAllByTestId('provider-section').map(s => s.textContent)).toEqual([
      'Anthropic', 'OMP', 'OpenAI', 'FastEmbed',
    ])
  })

  it('shows a loading state while the provider list loads', () => {
    mockUseProviders.mockReturnValue({
      data: undefined,
      isLoading: true,
      isError: false,
    })
    render(<ApiKeysPage />)
    expect(screen.queryAllByTestId('provider-section')).toHaveLength(0)
  })

  it('shows an error state when the provider list fails to load', () => {
    mockUseProviders.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
    })
    render(<ApiKeysPage />)
    expect(screen.getByText('apiKeys.providersLoadFailed')).toBeInTheDocument()
    expect(
      screen.getByText('apiKeys.providersLoadFailedDescription')
    ).toBeInTheDocument()
    expect(screen.queryAllByTestId('provider-section')).toHaveLength(0)
  })
})
