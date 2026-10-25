import { render, screen, fireEvent } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { describe, it, expect, vi } from 'vitest'
import { ProviderSection } from './ProviderSection'
import { ProviderInfo } from '@/lib/api/providers'
import type { Model } from '@/lib/types/models'
import type { Credential } from '@/lib/api/credentials'

// useTranslation is mocked globally in setup.ts (t returns the key string)

vi.mock('./CredentialFormDialog', () => ({
  CredentialFormDialog: () => <div data-testid="credential-form-dialog" />,
}))

vi.mock('./CredentialItem', () => ({
  CredentialItem: () => <div data-testid="credential-item" />,
}))

function makeProvider(overrides: Partial<ProviderInfo> = {}): ProviderInfo {
  return {
    name: 'openai',
    display_name: 'OpenAI',
    modalities: ['language', 'embedding'],
    configuration_type: 'credential',
    docs_url: 'https://platform.openai.com/api-keys',
    env_configured: false,
    ...overrides,
  }
}

const sectionDefaults = {
  credentials: [],
  models: [],
  defaults: null,
  allCredentials: [],
  encryptionReady: true,
} satisfies Omit<ComponentProps<typeof ProviderSection>, 'provider'>

function renderSection(
  provider: ProviderInfo,
  overrides: Partial<ComponentProps<typeof ProviderSection>> = {},
) {
  return render(<ProviderSection {...sectionDefaults} provider={provider} {...overrides} />)
}

function makeModel(provider: string, type: Model['type']): Model {
  return {
    id: `model:${provider}`,
    name: `${provider}-registered-model`,
    provider,
    type,
    credential: null,
    created: '2026-01-01T00:00:00Z',
    updated: '2026-01-01T00:00:00Z',
  }
}

describe('ProviderSection', () => {
  it('renders the display name from the backend registry', () => {
    renderSection(makeProvider())
    expect(screen.getByText('OpenAI')).toBeInTheDocument()
  })

  it('renders one modality badge per registry modality', () => {
    renderSection(makeProvider())
    expect(screen.getByText('common.language')).toBeInTheDocument()
    expect(screen.getByText('models.embedding')).toBeInTheDocument()
  })

  it('renders providers the frontend has never seen, with a fallback icon', () => {
    // "Zero frontend edits to add a provider": a brand-new provider with a
    // brand-new modality must render (raw label + generic icon), not break.
    const { container } = renderSection(
      makeProvider({
        name: 'newprovider',
        display_name: 'Brand New Provider',
        modalities: ['language', 'holograms'],
      })
    )
    expect(screen.getByText('Brand New Provider')).toBeInTheDocument()
    expect(screen.getByText('holograms')).toBeInTheDocument()
    // The unknown modality badge still carries an icon (the fallback one)
    const badges = container.querySelectorAll('svg')
    expect(badges.length).toBeGreaterThanOrEqual(2)
  })

  it.each([
    ['omp', 'local_account', 'language', 'apiKeys.localAccount'],
    ['fastembed', 'local_onnx', 'embedding', 'apiKeys.localOnnx'],
  ] as const)('uses registered %s models without collecting credentials', (name, configuration_type, type, title) => {
    const provider = makeProvider({ name, configuration_type, modalities: [type] })
    const model = makeModel(name, type)
    const view = renderSection(provider, { encryptionReady: false })

    expect(screen.getByText(title)).toBeInTheDocument()
    expect(screen.getByText('apiKeys.notConfigured')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'apiKeys.addConfig' })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'apiKeys.localSetup' })).toHaveAttribute('href', '/research')
    expect(screen.queryByTestId('credential-form-dialog')).not.toBeInTheDocument()

    view.rerender(<ProviderSection {...sectionDefaults} provider={provider} models={[model]} encryptionReady={false} />)

    expect(screen.getByText('apiKeys.configured')).toBeInTheDocument()
    expect(screen.queryByText('apiKeys.notConfigured')).not.toBeInTheDocument()
    expect(screen.getByText(model.name)).toBeInTheDocument()
    expect(screen.queryByTestId('credential-form-dialog')).not.toBeInTheDocument()

    view.rerender(<ProviderSection {...sectionDefaults} provider={provider} />)
    expect(screen.getByText('apiKeys.notConfigured')).toBeInTheDocument()
    expect(screen.queryByText(model.name)).not.toBeInTheDocument()
  })

  it('does not count another provider or an unsupported model modality as local setup', () => {
    renderSection(
      makeProvider({ name: 'omp', configuration_type: 'local_account', modalities: ['language'] }),
      { models: [makeModel('openai', 'language'), makeModel('omp', 'embedding')] },
    )
    expect(screen.getByText('apiKeys.notConfigured')).toBeInTheDocument()
    expect(screen.queryByRole('list')).not.toBeInTheDocument()
  })

  it('still opens the API credential form for an unconfigured remote provider', () => {
    renderSection(makeProvider(), { models: [makeModel('openai', 'language')] })
    expect(screen.getByText('apiKeys.notConfigured')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'apiKeys.addConfig' }))
    expect(screen.getByTestId('credential-form-dialog')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'apiKeys.localSetup' })).not.toBeInTheDocument()
  })

  it('keeps existing API credentials configured and editable', () => {
    const credential: Credential = {
      id: 'credential:openai',
      name: 'OpenAI',
      provider: 'openai',
      modalities: ['language'],
      has_api_key: true,
      created: '2026-01-01T00:00:00Z',
      updated: '2026-01-01T00:00:00Z',
      model_count: 0,
    }
    renderSection(makeProvider(), { credentials: [credential], allCredentials: [credential] })
    expect(screen.getByText('apiKeys.configured')).toBeInTheDocument()
    expect(screen.getByTestId('credential-item')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'apiKeys.addConfig' }))
    expect(screen.getByTestId('credential-form-dialog')).toBeInTheDocument()
  })

  it('still disables API credential creation when encryption is unavailable', () => {
    renderSection(makeProvider(), { encryptionReady: false })
    const addButton = screen.getByRole('button', { name: 'apiKeys.addConfig' })
    expect(addButton).toBeDisabled()
    fireEvent.click(addButton)
    expect(screen.queryByTestId('credential-form-dialog')).not.toBeInTheDocument()
  })
})
