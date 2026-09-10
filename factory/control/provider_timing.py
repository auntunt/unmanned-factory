"""SDK-boundary timing, explicitly not HTTP TTFT or upstream queue timing."""
import time


class ProviderTiming:
    def __init__(self, emit, clock=time.monotonic):
        self.emit, self.clock = emit, clock
        self.started = self.last = clock()
        self.first_sdk = self.first_assistant = None
        self.last_tool_result = None
        self.max_gap = 0
        self.commands = 0
        self.command_wait = self.command_execution = 0
        self.compactions = 0

    def message(self, kind, *, tool_result=False, compact=False):
        now = self.clock()
        gap = now - self.last
        self.last = now
        self.max_gap = max(self.max_gap, gap)
        elapsed = now - self.started
        if self.first_sdk is None:
            self.first_sdk = elapsed
            self.emit('provider.timing', {'phase': 'first_sdk_event', 'elapsed_s': round(elapsed, 3), 'scope': 'sdk'})
        if 'assistant' in kind.lower():
            if self.first_assistant is None:
                self.first_assistant = elapsed
                self.emit('provider.timing', {'phase': 'first_assistant_message', 'elapsed_s': round(elapsed, 3), 'scope': 'sdk'})
            if self.last_tool_result is not None:
                self.emit('provider.timing', {'phase': 'after_tool_result',
                    'elapsed_s': round(now - self.last_tool_result, 3), 'scope': 'sdk',
                    'interpretation': 'SDK response interval; generation, network and internal retries are not separated'})
                self.last_tool_result = None
        if gap >= 15 or compact:
            self.emit('provider.timing', {'phase': 'sdk_event_gap', 'elapsed_s': round(gap, 3),
                'scope': 'sdk', 'next_event': kind, 'compaction_observed': compact})
        if compact:
            self.compactions += 1
        if tool_result:
            self.last_tool_result = now

    def command(self, payload):
        self.commands += 1
        self.command_wait += payload.get('wait_s', 0) or 0
        self.command_execution += payload.get('execution_s', payload.get('duration_s', 0)) or 0

    def finish(self, outcome):
        self.emit('provider.timing', {'phase': 'execution_summary', 'scope': 'sdk', 'outcome': outcome,
            'elapsed_s': round(self.clock() - self.started, 3),
            'first_sdk_event_s': self.first_sdk, 'first_assistant_message_s': self.first_assistant,
            'max_sdk_event_gap_s': round(self.max_gap, 3), 'compactions_observed': self.compactions,
            'commands': self.commands, 'command_capacity_wait_s': round(self.command_wait, 3),
            'command_execution_s': round(self.command_execution, 3),
            'upstream_queue_s': None, 'http_ttft_s': None})
