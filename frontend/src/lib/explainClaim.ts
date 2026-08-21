// claim → 人话。「谁不满意、为什么、你该干什么」三段。
//
// 数据来源是审计库 supervisor_verdict.claims，实测只有 10 种 check 名
// （见 audit.db）。每种都有明确的人话解释和下一步动作。
// 兜底分支必须保留：新 check 上线时不能显示空白。

import type { Claim } from '../types'

export interface ClaimExplanation {
  /** 一句话说清哪里不对，操作者视角 */
  title: string
  /** 为什么会这样 / 背景 */
  why?: string
  /** 操作者能做的下一步；没有就不显示 */
  action?: string
  /** 是环境/配置问题（不是 AI 干得不好）→ 值得单独标出来 */
  kind: 'env' | 'quality' | 'policy' | 'unknown'
}

/** got 字段里常见的环境错误特征 → 人话。命中即优先，比 check 名更准。 */
const GOT_PATTERNS: Array<{
  test: RegExp
  build: (got: string) => ClaimExplanation
}> = [
  {
    test: /execvp .*claude: No such file or directory/,
    build: () => ({
      title: '干活的 AI 程序没找到，这轮根本没跑起来',
      why: '沙箱里找不到 claude 命令，不是 AI 干得不好，是机器上没装或路径没挂进沙箱。',
      action: '让维护者检查 claude CLI 安装路径和沙箱挂载，修好后重投一次。',
      kind: 'env',
    }),
  },
  {
    test: /\.venv\/bin\/python: not found|exit 127/,
    build: () => ({
      title: '测试跑不起来：Python 环境没准备好',
      why: '要用的 .venv 不存在，测试命令直接报 127（命令找不到），所以拿不到测试结果。',
      action: '这是环境问题，不是代码问题。让维护者先把虚拟环境建好。',
      kind: 'env',
    }),
  },
  {
    test: /VIRTUAL_ENV=.*does not match the project environment/,
    build: () => ({
      title: '测试报错退出：虚拟环境路径对不上',
      why: '环境变量指的 .venv 和项目里的不是同一个，测试命令被环境问题带崩了。',
      action: '让维护者统一虚拟环境路径后重跑，这轮的失败说明不了代码好坏。',
      kind: 'env',
    }),
  },
  {
    test: /no changes produced/,
    build: () => ({
      title: 'AI 一个文件都没改',
      why: '这轮跑完之后代码和跑之前一模一样，等于什么也没做。',
      action: '通常说明任务描述太模糊，AI 不知道该动哪。把需求写具体点再投。',
      kind: 'quality',
    }),
  },
]

/** check 名 → 人话。GOT_PATTERNS 没命中时用。 */
const CHECK_TEXT: Record<string, ClaimExplanation> = {
  'pre-dispatch-grading': {
    title: '开工前被拦：这类活不许机器单独做',
    why: '系统按改动性质分级，只有 A、B 两级允许无人值守。这个任务被判成更高一级。',
    action: '你自己动手做，或者把任务拆小到只碰允许的范围。',
    kind: 'policy',
  },
  'post-diff-grading': {
    title: '干完发现越界了：实际改动比申报的严重',
    why: '任务申报的是最低风险级别，但 AI 真正改的文件属于更敏感的一类（比如数据模型）。',
    action: '人工看一眼这个 diff 再决定要不要收。或者投任务时就申报正确的级别。',
    kind: 'policy',
  },
  'pre-dispatch-spec-ref': {
    title: '开工前被拦：引用了规格编号，却没给规格文档',
    why: '任务里写了编号，但没有能查到正文的文档，AI 无从下手。',
    action: '补上规格文档路径，或者把编号删掉、直接把要求写在任务描述里。',
    kind: 'policy',
  },
  'no-checks-defined': {
    title: '没有验收标准，所以不能算通过',
    why: '任务没定义任何一条能自动跑的检查，监工没有客观依据判它成功。空手判过关等于放水。',
    action: '投任务时至少给一条能跑的验证命令（比如某个测试要过、某个接口要返回 200）。',
    kind: 'policy',
  },
  harness: {
    title: '执行程序本身出错，没拿到结果',
    why: '跑 AI 的那层出问题了，这轮的成败和代码质量无关。',
    action: '看下面的原始报错，一般是环境或配置问题，交给维护者。',
    kind: 'env',
  },
  diff: {
    title: '没有产生任何代码改动',
    action: '把需求写具体一点再投一次。',
    kind: 'quality',
  },
  no_regression: {
    title: '回归测试没过：可能弄坏了原来能用的功能',
    action: '看下面的原始输出定位是哪条测试红了。',
    kind: 'quality',
  },
  regression_tests: {
    title: '回归测试没过',
    action: '看下面的原始输出定位失败的测试。',
    kind: 'quality',
  },
  pytest_console: {
    title: '控制台相关的测试没过',
    action: '看下面的原始输出定位失败的测试。',
    kind: 'quality',
  },
  triple_return: {
    title: '指定的那条检查没通过',
    action: '看下面的原始输出。',
    kind: 'quality',
  },
}

export function explainClaim(claim: Claim): ClaimExplanation {
  const got = claim.actual ?? claim.got ?? ''

  for (const p of GOT_PATTERNS) {
    if (p.test.test(got)) return p.build(got)
  }

  const byCheck = CHECK_TEXT[claim.check]
  if (byCheck) return byCheck

  // 兜底：不认识的 check。原样把 check 名摊出来，别装作没这回事。
  return {
    title: `检查「${claim.check}」没通过`,
    why: claim.expected ? `要求是：${claim.expected}` : undefined,
    action: '这是一条还没配人话说明的检查，看下面的原始记录。',
    kind: 'unknown',
  }
}

/** 一轮里所有 fail claim 汇总成一句话，给折叠状态下的标题用。 */
export function summarizeClaims(claims: Claim[]): string {
  if (claims.length === 0) return ''
  const first = explainClaim(claims[0])
  if (claims.length === 1) return first.title
  return `${first.title}（另有 ${claims.length - 1} 条）`
}

/** 这轮的失败是不是全都是环境问题——用来提示「不是 AI 的锅」。 */
export function allEnvIssues(claims: Claim[]): boolean {
  return claims.length > 0 && claims.every((c) => explainClaim(c).kind === 'env')
}
