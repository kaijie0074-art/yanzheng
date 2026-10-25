import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { Experiment, ResearchState } from '@/lib/api/competition'
import { ResearchExperiments } from './ResearchExperiments'

function stateFor(status: string, revision: number): ResearchState {
  const experiment: Experiment = {
    run_id: 'isolated-experiment', status, recommended_candidate: 'word_lr',
    validation_aggregates: [], test_aggregates: [],
    frozen_selection: { chosen_candidate: 'word_lr', reason: '隔离界面门禁测试，不是研究结论', study_revision: 1 },
  }
  return {
    notebook_id: 'notebook:isolated', sms_case: true,
    brief: {
      revision, confirmed_at: '2026-01-01T00:00:00Z',
      content: { question: '隔离门禁测试', available_resources: '公开材料', constraints: '不得作为研究结果', population_or_object: '测试记录', validation_goal: '验证冻结与当前修订门禁' },
    },
    sources: [], cards: [], candidate_runs: [], decisions: [], experiments: [experiment],
    model: { backend: 'local_account', model: 'test-only', configured: true },
    experiment_preparation: { prepared: true, audited: true, manifest: null, error: null },
    discovery: { plans: [], runs: [] }, paper_work: { reading_reports: [], writing_drafts: [] }, model_runs: [],
  }
}

describe('保留集只能由当前有效冻结方案进入', () => {
  it.each([
    { status: 'validation_complete', revision: 1, visible: true, enabled: true },
    { status: 'validation_complete', revision: 2, visible: true, enabled: false },
    { status: 'unverified', revision: 1, visible: false, enabled: false },
    { status: 'failed', revision: 1, visible: false, enabled: false },
    { status: 'confirmed', revision: 1, visible: false, enabled: false },
  ])('$status / 当前修订 $revision', ({ status, revision, visible, enabled }) => {
    render(<ResearchExperiments notebookId="notebook:isolated" state={stateFor(status, revision)} locked={false} onSaved={vi.fn()} />)
    const action = screen.queryByRole('button', { name: '运行已冻结方案的保留集确认' })
    if (!visible) {
      expect(action).not.toBeInTheDocument()
    } else if (enabled) {
      expect(action).toBeEnabled()
    } else {
      expect(action).toBeDisabled()
    }
  })
})
