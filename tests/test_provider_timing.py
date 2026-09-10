from factory.control.provider_timing import ProviderTiming


def test_sdk_timings_do_not_claim_http_or_model_thinking_time():
    clock = [0.0]
    events = []
    timing = ProviderTiming(lambda k, p: events.append((k, p)), lambda: clock[0])
    clock[0] = 2
    timing.message('SystemMessage')
    clock[0] = 8
    timing.message('AssistantMessage')
    timing.command({'wait_s': 2, 'execution_s': 3, 'duration_s': 5})
    clock[0] = 13
    timing.message('UserMessage', tool_result=True)
    clock[0] = 43
    timing.message('AssistantMessage')
    clock[0] = 80
    timing.message('UserMessage', compact=True)
    clock[0] = 81
    timing.finish('failed')
    summary = events[-1][1]
    assert summary['first_sdk_event_s'] == 2
    assert summary['first_assistant_message_s'] == 8
    assert summary['command_capacity_wait_s'] == 2
    assert summary['command_execution_s'] == 3
    assert summary['compactions_observed'] == 1
    assert summary['max_sdk_event_gap_s'] == 37
    assert summary['http_ttft_s'] is None and summary['upstream_queue_s'] is None
    assert any(p.get('phase') == 'after_tool_result' and p['elapsed_s'] == 30 for _, p in events)
    assert summary['outcome'] == 'failed'


def test_timing_event_survives_worker_transport():
    import json
    from factory.control.providers import SDKRunner
    events = []
    payload = {'phase': 'execution_summary', 'elapsed_s': 12, 'http_ttft_s': None}
    SDKRunner._consume_line(json.dumps({'type': 'provider.timing', 'payload': payload}).encode(),
        'stdout', lambda *args: events.append(args))
    assert events == [('provider.timing', payload)]
