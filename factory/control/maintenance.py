"""Bounded retries for read-only maintenance when a provider is overloaded."""
import re
import time
from dataclasses import replace

from factory.control.providers import ProviderError, ProviderCancelled, ProviderTimeout


def overloaded(exc):
    return isinstance(exc, ProviderError) and bool(re.search(r'\b(?:429|503|529)\b|overloaded|over capacity', str(exc), re.I))


def failure_message(exc):
    if overloaded(exc):
        return '模型服务暂时繁忙，自动重试后仍未恢复。你的对话和 Skill 已保存，可点击“重新整理”继续。'
    if isinstance(exc, ProviderCancelled):
        return '整理已取消，对话和 Skill 已保存。'
    return '本次整理未完成，对话和 Skill 已保存。可重新整理，或在模型与工具中检查维护模型配置。'


def run_maintenance(runner, request, emit, cancel, on_retry):
    deadline = time.monotonic() + request.timeout_s
    for attempt in range(3):
        if cancel.is_set():
            raise ProviderCancelled('维护整理已取消')
        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            raise ProviderTimeout('维护整理已达到总时限')
        try:
            return runner.run(replace(request, timeout_s=remaining), emit, cancel)
        except ProviderError as exc:
            if not overloaded(exc) or attempt == 2:
                raise
            delay = 2 ** attempt
            if time.monotonic() + delay >= deadline:
                raise
            on_retry(attempt + 1)
            if cancel.wait(delay):
                raise ProviderCancelled('维护整理已取消') from None
