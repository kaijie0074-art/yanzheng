import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ResearchExports } from './ResearchExports'

const { post } = vi.hoisted(() => ({ post: vi.fn() }))
vi.mock('@/lib/api/competition', () => ({
  competitionApi: { post },
  downloadResearchFile: vi.fn(),
  researchError: (error: unknown) => String(error),
}))

beforeEach(() => vi.clearAllMocks())

function generate() {
  render(<ResearchExports notebookId="notebook:isolated" locked={false} />)
  fireEvent.click(screen.getByRole('button', { name: '检查完成条件并生成过程文件' }))
}

describe('研究完成与文件生成是不同状态', () => {
  it('文件全部生成也不能把没有正式决定的研究显示为通过', async () => {
    post.mockResolvedValueOnce({
      status: 'incomplete', formal_decision_count: 0, blocked: [],
      files: [{ name: 'process.json', url: '/api/competition/notebook:isolated/exports/process.json' }],
    })
    generate()
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('单个PDF生成失败不应把有效正式决定误报为业务未完成', async () => {
    post.mockResolvedValueOnce({
      status: 'formal_decision_available', formal_decision_count: 1,
      blocked: ['PDF字体不可用'],
      files: [{ name: 'process.json', url: '/api/competition/notebook:isolated/exports/process.json' }],
    })
    generate()
    expect(await screen.findByRole('status')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toBeInTheDocument()
  })
})
