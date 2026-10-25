'use client'

import { useState } from 'react'
import Link from 'next/link'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Plus, Check, X } from 'lucide-react'
import { useTranslation } from '@/lib/hooks/use-translation'
import { Credential } from '@/lib/api/credentials'
import { ProviderInfo } from '@/lib/api/providers'
import { Model, ModelDefaults } from '@/lib/types/models'
import {
  getTypeIcon,
  getTypeColor,
  TYPE_COLOR_INACTIVE,
} from '@/lib/providers'
import { CredentialFormDialog } from './CredentialFormDialog'
import { CredentialItem } from './CredentialItem'

interface ProviderSectionProps {
  provider: ProviderInfo
  credentials: Credential[]
  models: Model[]
  defaults: ModelDefaults | null
  allCredentials: Credential[]
  encryptionReady: boolean
}

export function ProviderSection({
  provider,
  credentials,
  models,
  defaults,
  allCredentials,
  encryptionReady,
}: ProviderSectionProps) {
  const { t } = useTranslation()
  const [addOpen, setAddOpen] = useState(false)

  const isLocal = provider.configuration_type !== 'credential'
  const displayName = provider.configuration_type === 'local_account'
    ? t('apiKeys.localAccount')
    : provider.configuration_type === 'local_onnx'
      ? t('apiKeys.localOnnx')
      : provider.display_name || provider.name
  const modalities = provider.modalities.length > 0 ? provider.modalities : ['language']
  const providerModels = models.filter(m => isLocal
    ? m.provider === provider.name && provider.modalities.includes(m.type)
    : credentials.some(c => c.id === m.credential)
  )
  const isConfigured = isLocal ? providerModels.length > 0 : credentials.length > 0
  const modalityLabels: Record<string, string> = {
    language: t('common.language'),
    embedding: t('models.embedding'),
    text_to_speech: t('models.tts'),
    speech_to_text: t('models.stt'),
  }
  const activeTypes = new Set<string>(providerModels.map(m => m.type))

  return (
    <Card className={isConfigured ? 'border-l-2 border-l-fern' : undefined}>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3 flex-wrap">
            <CardTitle className={`text-lg capitalize ${isConfigured ? '' : 'text-muted-foreground'}`}>{displayName}</CardTitle>
            <div className="flex items-center gap-1">
              {modalities.map((type) => (
                <Badge
                  key={type}
                  variant="secondary"
                  className={`text-xs gap-1 ${activeTypes.has(type) ? getTypeColor(type) : TYPE_COLOR_INACTIVE}`}
                >
                  {getTypeIcon(type)}
                  <span className="hidden sm:inline">{modalityLabels[type] ?? type}</span>
                </Badge>
              ))}
            </div>
          </div>
          <div className="flex items-center gap-2">
            {isConfigured ? (
              <span className="inline-flex items-center gap-1.5 text-xs font-medium text-fern">
                <Check className="h-3 w-3" />
                {t('apiKeys.configured')}
              </span>
            ) : (
              <span className="inline-flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
                <X className="h-3 w-3" />
                {t('apiKeys.notConfigured')}
              </span>
            )}
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        {isLocal ? (
          <>
            <p className="text-sm text-muted-foreground">{t('apiKeys.localSetupHint')}</p>
            {providerModels.length > 0 && (
              <ul className="space-y-1 text-sm">
                {providerModels.map(model => <li key={model.id} className="break-all">{model.name}</li>)}
              </ul>
            )}
            <Button variant="outline" size="sm" className="w-full" asChild>
              <Link href="/research">{t('apiKeys.localSetup')}</Link>
            </Button>
          </>
        ) : (
          <>
            {credentials.map(cred => (
              <CredentialItem
                key={cred.id}
                credential={cred}
                models={models}
                defaults={defaults}
                allCredentials={allCredentials}
              />
            ))}

            <Button
              variant="outline"
              size="sm"
              onClick={() => setAddOpen(true)}
              className="w-full gap-2"
              disabled={!encryptionReady}
            >
              <Plus className="h-4 w-4" />
              {t('apiKeys.addConfig')}
            </Button>
          </>
        )}
      </CardContent>

      {!isLocal && addOpen && (
        <CredentialFormDialog
          open={addOpen}
          onOpenChange={setAddOpen}
          provider={provider.name}
        />
      )}
    </Card>
  )
}
