"""管理员配置工具的暴露面闸门。

红线：普通职能体聊天（哪怕聊天的人是管理员）的工具清单里不得出现配置工具。
否则一个报价助手会话就能被引导去改部署目标。两个条件缺一不可：
role 必须是 admin，且 binding 必须显式要了配置面。
"""
from factory.control import conversation_tools as ct


# 普通会话本就有的工具：算账、导出，以及「本角色已挂靠的能力包」两件。
# 这条测试关心的是**配置工具**不得出现，不是清单一成不变。
BASELINE = {'calc', 'export', 'attached_tools', 'attached_tool_doc', 'run_attached_tool',
            'session_artifacts', 'read_session_artifact'}


def _tools(tmp_path, *, actor_role, admin_config):
    from factory.control.store import Store
    db = str(tmp_path / 'f.db')
    Store(db)
    binding = {'db_path': db, 'conversation_id': 'c1', 'actor_id': 'u1',
               'actor_role': actor_role, 'admin_config': admin_config}
    return ct.ConversationTools.from_binding(binding)


def _names(tools):
    """真实 MCP 清单：和模型看到的是同一份，不是内部字典的近似。"""
    import anyio
    from mcp import ClientSession

    server = ct.create_server(tools, lambda *e: None)
    instance = server['instance']
    seen = {}

    async def roundtrip():
        csend, sread = anyio.create_memory_object_stream(10)
        ssend, cread = anyio.create_memory_object_stream(10)
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(instance.run, sread, ssend, instance.create_initialization_options())
            async with ClientSession(cread, csend) as session:
                await session.initialize()
                inventory = await session.list_tools()
                seen['names'] = {t.name for t in inventory.tools}
            tasks.cancel_scope.cancel()

    anyio.run(roundtrip)
    return seen['names']


def test_member_ordinary_chat_has_no_config_tools(tmp_path):
    assert _names(_tools(tmp_path, actor_role='member', admin_config=False)) == BASELINE


def test_admin_ordinary_chat_has_no_config_tools(tmp_path):
    """这条是关键：管理员在普通聊天里也不该看到配置工具。"""
    assert _names(_tools(tmp_path, actor_role='admin', admin_config=False)) == BASELINE


def test_member_cannot_get_config_tools_by_asking_for_the_surface(tmp_path):
    """binding 就算声称要配置面，role 不是 admin 也不给。"""
    assert _names(_tools(tmp_path, actor_role='member', admin_config=True)) == BASELINE


def test_admin_config_surface_exposes_config_tools(tmp_path):
    from factory.control.admin_config_tools import ADMIN_TOOL_NAMES
    names = _names(_tools(tmp_path, actor_role='admin', admin_config=True))
    assert BASELINE <= names
    # ADMIN_TOOL_NAMES 是模型侧的全名（mcp__session__ 前缀），清单里是裸名。
    bare = {n.rsplit('__', 1)[-1] for n in ADMIN_TOOL_NAMES}
    assert bare <= names, sorted(bare - names)


def test_binding_for_defaults_to_no_config_surface(tmp_path):
    from factory.control.store import Store
    db = str(tmp_path / 'g.db')
    b = ct.binding_for(Store(db), 'c1', 'u1', actor_role='admin')
    assert b['admin_config'] is False
