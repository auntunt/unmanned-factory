import { describe, expect, it } from 'vitest'
import { explainClaim, allEnvIssues, summarizeClaims } from './explainClaim'
import { resolutionText, modelText, roleText } from './humanize'
import type { Claim } from '../types'

/**
 * 真实数据里出现过的全部 claim（取自 factory-data/audit.db 的
 * supervisor_verdict.claims，共 10 种 check 名）。
 * 每一条都必须有人话，不许掉进 unknown 兜底。
 */
const REAL_CLAIMS: Array<[string, Claim]> = [
  ['risk/pre-dispatch-grading', {
    check: 'pre-dispatch-grading',
    expected: 'class A/B (unmanned allowed)',
    got: 'C: new UX surface [new_ux]',
  }],
  ['risk/post-diff-grading', {
    check: 'post-diff-grading',
    expected: 'class A (declared)',
    got: 'B: data model [factory/intelligence/models.py]',
  }],
  ['risk/pre-dispatch-spec-ref', {
    check: 'pre-dispatch-spec-ref',
    expected: 'spec_ref 的每个编号都能在 spec_doc 里查到正文',
    got: 'spec_ref 有编号但没有 spec_doc，编号无正文可查',
  }],
  ['regression/no-checks-defined', {
    check: 'no-checks-defined',
    expected: '至少一条廉价客观裁判',
    got: '任务没有定义任何 check，不能判为通过',
  }],
  ['regression/harness', {
    check: 'harness',
    command: 'claude_code',
    expected: 'exit_status ok',
    got: 'error: bwrap: execvp /home/ubuntu/.npm-global/bin/claude: No such file or directory\n',
  }],
  ['regression/pytest_console', {
    check: 'pytest_console',
    expected: 'exit_zero',
    got: 'exit 127: /bin/sh: 1: .venv/bin/python: not found\n',
  }],
  ['regression/triple_return', {
    check: 'triple_return',
    expected: 'exit_zero',
    got: 'exit 127: /bin/sh: 1: .venv/bin/python: not found\n',
  }],
  ['regression/no_regression', {
    check: 'no_regression',
    expected: 'exit_zero',
    got: 'exit 1: warning: `VIRTUAL_ENV=/home/ubuntu/workspace/unmanned-factory/.venv` does not match the project environment path `.venv` and will be ignored\n',
  }],
  ['regression/regression_tests', {
    check: 'regression_tests',
    expected: 'exit_zero',
    got: 'exit 1: warning: `VIRTUAL_ENV=/x/.venv` does not match the project environment path `.venv`\n',
  }],
  ['regression/diff', {
    check: 'diff',
    expected: '至少一个文件改动',
    got: 'no changes produced',
  }],
]

describe('explainClaim: 真实数据全覆盖', () => {
  it.each(REAL_CLAIMS)('%s 有人话结论且不落兜底', (_name, claim) => {
    const ex = explainClaim(claim)
    expect(ex.title.length).toBeGreaterThan(4)
    expect(ex.kind).not.toBe('unknown')
    // 标题里不许出现内部 check 名/枚举
    expect(ex.title).not.toContain(claim.check)
  })

  it.each(REAL_CLAIMS)('%s 给出了下一步动作', (_name, claim) => {
    expect(explainClaim(claim).action?.length ?? 0).toBeGreaterThan(4)
  })
})

describe('explainClaim: 环境问题要和质量问题区分开', () => {
  it('claude 二进制缺失 → env（不是 AI 干得不好）', () => {
    const ex = explainClaim({
      check: 'harness',
      got: 'error: bwrap: execvp /home/ubuntu/.npm-global/bin/claude: No such file or directory\n',
    })
    expect(ex.kind).toBe('env')
  })

  it('.venv 缺失 → env，即使 check 名是质量类的', () => {
    // got 特征优先于 check 名：pytest_console 本身算 quality，
    // 但 exit 127 说明测试根本没跑起来，是环境问题。
    expect(
      explainClaim({
        check: 'pytest_console',
        got: 'exit 127: /bin/sh: 1: .venv/bin/python: not found\n',
      }).kind,
    ).toBe('env')
  })

  it('没产生改动 → quality（AI 真的没干活）', () => {
    expect(explainClaim({ check: 'diff', got: 'no changes produced' }).kind).toBe(
      'quality',
    )
  })

  it('分级拦截 → policy（规则不让做，不是做得不好）', () => {
    expect(explainClaim({ check: 'pre-dispatch-grading' }).kind).toBe('policy')
  })
})

describe('未登记的 check 不许静默丢掉', () => {
  const ex = explainClaim({ check: 'brand_new_check_2027', expected: 'foo' })

  it('原样保留 check 名，操作者还能搜', () => {
    expect(ex.title).toContain('brand_new_check_2027')
  })

  it('标成 unknown 而不是伪装成已知类型', () => {
    expect(ex.kind).toBe('unknown')
  })
})

describe('allEnvIssues', () => {
  it('全是环境问题 → true', () => {
    expect(
      allEnvIssues([
        { check: 'harness', got: 'error: bwrap: execvp /x/claude: No such file or directory' },
        { check: 'pytest_console', got: 'exit 127: /bin/sh: 1: .venv/bin/python: not found' },
      ]),
    ).toBe(true)
  })

  it('混了质量问题 → false（不能对操作者说「不是 AI 的锅」）', () => {
    expect(
      allEnvIssues([
        { check: 'harness', got: 'error: bwrap: execvp /x/claude: No such file or directory' },
        { check: 'diff', got: 'no changes produced' },
      ]),
    ).toBe(false)
  })

  it('空数组 → false，不是 true（没有 claim ≠ 全是环境问题）', () => {
    expect(allEnvIssues([])).toBe(false)
  })
})

describe('summarizeClaims', () => {
  it('多条时说明还有几条，不隐藏', () => {
    const s = summarizeClaims([
      { check: 'diff', got: 'no changes produced' },
      { check: 'no-checks-defined' },
    ])
    expect(s).toContain('另有 1 条')
  })

  it('空数组返回空串，让调用方自己兜底', () => {
    expect(summarizeClaims([])).toBe('')
  })
})

describe('resolutionText: 每个结局都说清「系统做了什么」', () => {
  it.each(['merged', 'reworked', 'escalated', 'blocked', 'not_dispatched', 'pending'])(
    '%s 有 label/what/tone',
    (r) => {
      const t = resolutionText(r)
      expect(t.label).not.toBe(r) // 不能直接吐英文枚举
      expect(t.what.length).toBeGreaterThan(6)
      expect(['ok', 'wait', 'stop']).toContain(t.tone)
    },
  )

  it('未登记状态原样显示并标 stop（不许猜成成功）', () => {
    const t = resolutionText('some_new_state')
    expect(t.label).toBe('some_new_state')
    expect(t.tone).toBe('stop')
  })

  it('reworked 是 wait 不是 stop：系统还在自己重试，不用操作者动手', () => {
    expect(resolutionText('reworked').tone).toBe('wait')
  })

  it('escalated 和 blocked 都是 stop，但话术不同（下一步动作不一样）', () => {
    expect(resolutionText('escalated').tone).toBe('stop')
    expect(resolutionText('blocked').tone).toBe('stop')
    expect(resolutionText('escalated').what).not.toBe(resolutionText('blocked').what)
  })
})

describe('modelText / roleText', () => {
  it.each(['haiku', 'sonnet', 'opus'])('%s 翻成角色而不是型号', (m) => {
    expect(modelText(m)).not.toBe(m)
    expect(modelText(m)).toMatch(/AI/)
  })

  it('未知模型名兜底也可读', () => {
    expect(modelText('gpt-9')).toContain('gpt-9')
  })

  it.each(['risk', 'regression', 'scope', 'beacon'])('%s 有中文名和职责', (r) => {
    const t = roleText(r)
    expect(t.name).toMatch(/监工/)
    expect(t.duty.length).toBeGreaterThan(5)
  })

  it('未知 role 原样显示', () => {
    expect(roleText('mystery').name).toBe('mystery')
  })
})
