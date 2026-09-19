from tests.test_admin_config_conversation import env, _login, _member

def test_member_can_start_daily_chat(env):
    client,store,service=env
    h=_login(client)
    aid=client.post('/api/v4/agents',headers=h,json={'name':'Team helper','purpose':'Daily chat'}).json()['id']
    member=_member(client,'daily-member')
    r=client.post(f'/api/v4/agents/{aid}/conversations',headers=member,json={'mode':'do'})
    assert r.status_code==201, (r.status_code,r.text)
