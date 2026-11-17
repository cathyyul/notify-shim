import datetime as dt
import json
import sys
import uuid
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'notifiers'))
import claude_scheduler_watchdog as mod


def test_confirm_does_not_clear_quota_failure():
    state = {}
    now = dt.datetime(2026, 11, 17, 10)
    mod.evaluate(state, [mod.LogEvent('usage_limit', now, 'daily-memory-sync')], now)
    result = mod.evaluate(state, [mod.LogEvent('confirm', now + dt.timedelta(minutes=1),
                                              'daily-memory-sync')], now + dt.timedelta(minutes=2))
    assert result.incident_active
    assert 'daily-memory-sync' in state['active_incident']['usage_limit_open']


def test_undelivered_alert_survives_check_only_recovery_and_restart():
    state = {}
    now = dt.datetime(2026, 11, 17, 10)
    mod.evaluate(state, [mod.LogEvent('cleared', now, 'task-a')], now)
    state = json.loads(json.dumps(state))
    mod.evaluate(state, [mod.LogEvent('confirm', now + dt.timedelta(minutes=1), 'task-a')],
                 now + dt.timedelta(minutes=2))
    assert [item['kind'] for item in state['notification_outbox']] == ['alert', 'recovery']
    original = list(state['notification_outbox'])
    mod.evaluate(state, [], now + dt.timedelta(minutes=3))
    assert state['notification_outbox'] == original


def heartbeat(task, started, finished):
    return dict(schema_version=1, task_id=task, run_id=str(uuid.uuid4()),
                started_at=started.astimezone(dt.timezone.utc).isoformat(),
                finished_at=finished.astimezone(dt.timezone.utc).isoformat(), status='ok', summary='done')


@pytest.mark.parametrize('invalid', ['task', 'old', 'running', 'failed', 'missing', 'future', 'uuid', 'naive'])
def test_invalid_completion_never_clears_latest_failure(invalid):
    now = dt.datetime(2026, 11, 17, 10)
    state = {}
    mod.evaluate(state, [mod.LogEvent('usage_limit', now, 'task-a')], now)
    h = heartbeat('task-a', now + dt.timedelta(minutes=1), now + dt.timedelta(minutes=2))
    if invalid == 'task': h['task_id'] = 'other'
    if invalid == 'old': h = heartbeat('task-a', now - dt.timedelta(minutes=1), now + dt.timedelta(minutes=2))
    if invalid in ('running', 'failed'): h['status'] = invalid
    if invalid == 'missing': h.pop('finished_at')
    if invalid == 'future': h['finished_at'] = (now + dt.timedelta(days=1)).astimezone(dt.timezone.utc).isoformat()
    if invalid == 'uuid': h['run_id'] = 'bad'
    if invalid == 'naive': h['started_at'] = '2026-11-17T10:01:00'
    result = mod.evaluate(state, [], now + dt.timedelta(minutes=3), completion_evidence={'task-a': h})
    assert result.incident_active


def test_each_task_needs_completion_after_its_latest_quota_failure():
    now = dt.datetime(2026, 11, 17, 10)
    state = {}
    mod.evaluate(state, [mod.LogEvent('usage_limit', now, t) for t in ('task-a', 'task-b')], now)
    h = heartbeat('task-a', now + dt.timedelta(minutes=1), now + dt.timedelta(minutes=2))
    result = mod.evaluate(state, [], now + dt.timedelta(minutes=3), completion_evidence={'task-a': h})
    assert result.incident_active
    assert list(state['active_incident']['usage_limit_open']) == ['task-b']
    h = heartbeat('task-b', now + dt.timedelta(minutes=1), now + dt.timedelta(minutes=2))
    later = now + dt.timedelta(minutes=4)
    result = mod.evaluate(state, [mod.LogEvent('usage_limit', later, 'task-b')], later,
                          completion_evidence={'task-b': h})
    assert result.incident_active
    h = heartbeat('task-b', later + dt.timedelta(minutes=1), later + dt.timedelta(minutes=2))
    result = mod.evaluate(state, [], later + dt.timedelta(minutes=3), completion_evidence={'task-b': h})
    assert not result.incident_active
    assert '成功完成' in result.recovery_message
    again = mod.evaluate(state, [], later + dt.timedelta(minutes=4))
    assert not again.incident_active
    assert again.recovery_message == result.recovery_message


def test_legacy_quota_missing_timestamp_stays_unresolved():
    state = {'active_incident': {'causes': [mod.CAUSE_USAGE_LIMIT], 'affected_tasks': ['task-a']}}
    result = mod.evaluate(state, [mod.LogEvent('confirm', dt.datetime(2026, 11, 17, 10), 'task-a')],
                          dt.datetime(2026, 11, 17, 11))
    assert result.incident_active


def test_failed_delivery_then_check_only_recovery_delivers_old_alert_first(tmp_path, monkeypatch):
    now = dt.datetime.now()
    log, state = tmp_path / 'main.log', tmp_path / 'state.json'
    stamp = lambda t: t.strftime(mod.TS_FORMAT)
    log.write_text(stamp(now - dt.timedelta(minutes=2)) +
                   ' [CCDScheduledTasks] Cleared stale pending dispatch for: task-a\n')
    base = ['watchdog', '--log-file', str(log), '--state-file', str(state),
            '--heartbeat-dir', str(tmp_path / 'heartbeats')]
    monkeypatch.setattr(sys, 'argv', base + ['--notify'])
    monkeypatch.setattr(mod, 'send_notification', lambda *a: False)
    assert mod.main() == 1
    assert json.loads(state.read_text())['notification_outbox']
    with log.open('a') as f:
        f.write(stamp(now - dt.timedelta(minutes=1)) +
                ' [CCDScheduledTasks] Confirmed task run for: task-a\n')
    monkeypatch.setattr(sys, 'argv', base)
    assert mod.main() == 0
    sent = []
    monkeypatch.setattr(mod, 'send_notification', lambda message, _: (sent.append(message), True)[1])
    monkeypatch.setattr(sys, 'argv', base + ['--notify'])
    assert mod.main() == 0
    assert len(sent) == 2
    assert '延後送達' in sent[0] and '已恢復' in sent[1]
    assert json.loads(state.read_text())['notification_outbox'] == []
    assert mod.main() == 0
    assert len(sent) == 2
