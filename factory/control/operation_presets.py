"""Versioned operation briefs compiled at the authenticated run entry point."""
OPERATION_BRIEFS = {
    'bugfix': '复现用户描述的问题，记录触发条件与预期结果；定位根因并检查同类调用。只修复确认受影响的范围，补充能捕获原问题的回归检查，再执行相关既有检查。无法复现时明确证据缺口，不把猜测当成修复完成。',
    'startup': '检查项目启动说明、依赖锁文件、环境变量名称、端口和启动命令；在隔离工作区实际启动并检查可用性。修复确认的配置或代码问题，记录验证命令、结果和停止方法。不得输出密钥值，不得终止不属于本任务的进程。',
    'release': '检查构建、运行时配置、健康检查和现有发布流程；补齐可重复执行的部署说明或脚本，以及失败回滚步骤。在工作区实际验证构建与启动。只有已配置且已授权的目标才可发布；未连接服务器时交付准备结果并明确未部署，不得宣称线上健康。',
    'dependencies': '检查依赖声明与锁文件一致性、安装失败及运行时兼容性。只更新解决已确认问题所需的依赖，避免无关的大版本升级；保留可复现安装，运行相关回归检查，说明变化、兼容性影响与回退方式。',
}


def operation_request(kind: str, description: str) -> str:
    if kind == 'general':
        return description
    brief = OPERATION_BRIEFS[kind]
    return (f'{description}\n\n[项目工作方式 · {kind} · v1]\n{brief}\n'
            '沿用项目既有权限、能力挂载和验收规则；优先复用有效成果。交付包含实际改动、验证证据、尚未解决的事项；需要额外授权或缺少连接时说明具体阻碍。')
