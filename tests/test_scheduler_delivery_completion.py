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


def test_quota_failure_and_later_success_in_same_poll_are_both_queued():
    now = dt.datetime(2026, 11, 17, 10)
    later = now + dt.timedelta(minutes=3)
    state = {}
    event = mod.LogEvent('usage_limit', now, 'task-a')
    success = heartbeat('task-a', now + dt.timedelta(minutes=1), later)

    result = mod.evaluate(state, [event], later, completion_evidence={'task-a': success})

    assert not result.incident_active
    assert result.alert_message and result.recovery_message
    outbox = state['notification_outbox']
    assert [entry['kind'] for entry in outbox] == ['alert', 'recovery']
    assert outbox[0]['incident'] == outbox[1]['incident']
    assert outbox[0]['causes'] == [mod.CAUSE_USAGE_LIMIT]


def test_quota_completion_keeps_newer_dispatch_failure():
    now = dt.datetime(2026, 11, 17, 10)
    failure = now + dt.timedelta(minutes=4)
    state = {}
    mod.evaluate(state, [mod.LogEvent('usage_limit', now, 'task-a')], now)
    mod.evaluate(state, [mod.LogEvent('cleared', failure, 'task-a')], failure)
    state = json.loads(json.dumps(state))
    success = heartbeat('task-a', now + dt.timedelta(minutes=1), now + dt.timedelta(minutes=3))

    result = mod.evaluate(state, [], failure, completion_evidence={'task-a': success})

    assert result.incident_active
    incident = state['active_incident']
    assert incident['usage_limit_open'] == {}
    assert incident['open_failures'] == {'task-a': mod.fmt_ts(failure)}
    assert incident['causes'] == [mod.CAUSE_UNCONFIRMED]
    assert result.recovery_message is None


def test_legacy_queued_confirm_recovery_is_replaced_only_after_completion():
    now = dt.datetime(2026, 11, 17, 10)
    confirm = now + dt.timedelta(minutes=1)
    state = {'active_incident': {
        'causes': [mod.CAUSE_USAGE_LIMIT],
        'affected_tasks': ['task-a'],
        'first_seen_at': mod.fmt_ts(now),
        'last_failure_at': mod.fmt_ts(now),
        'last_alert_at': mod.fmt_ts(now),
        'alerted_tasks': ['task-a'],
        'alerted_causes': [mod.CAUSE_USAGE_LIMIT],
        'open_failures': {},
        'usage_limit_open': {},
        'resolved_by': {'kind': 'confirm', 'task': 'task-a', 'ts': mod.fmt_ts(confirm)},
    }}
    incident = state['active_incident']
    state['notification_outbox'] = [{
        'kind': 'recovery', 'incident': incident['first_seen_at'],
        'message': mod.format_recovery(incident, incident['resolved_by']),
        'tasks': ['task-a'], 'causes': [mod.CAUSE_USAGE_LIMIT],
    }]

    result = mod.evaluate(state, [], confirm)
    assert result.incident_active
    assert not result.recovery_message
    assert state['notification_outbox'] == []

    finish = confirm + dt.timedelta(minutes=2)
    success = heartbeat('task-a', confirm + dt.timedelta(minutes=1), finish)
    result = mod.evaluate(state, [], finish, completion_evidence={'task-a': success})
    assert not result.incident_active and '成功完成' in result.recovery_message
    assert [entry['kind'] for entry in state['notification_outbox']] == ['recovery']


def test_fresh_quota_alert_queues_after_a_failed_recovery_send(tmp_path, monkeypatch):
    start = dt.datetime.now().replace(microsecond=0) - dt.timedelta(minutes=10)
    log, state = tmp_path / 'main.log', tmp_path / 'state.json'
    heartbeats = tmp_path / 'heartbeats'
    heartbeats.mkdir()

    def quota(when, session):
        stamp = mod.fmt_ts(when)
        with log.open('a') as handle:
            handle.write('\n'.join([
                f'{stamp} [CCDScheduledTasks] Spawning new session for scheduled task task-a {{}}',
                f'{stamp} [CCDScheduledTasks] Confirmed task run for: task-a',
                f'{stamp} Starting local session {session}',
                f'{stamp} [CCD CycleHealth] {session} api_error (success): You hit your session limit',
            ]) + '\n')

    monkeypatch.setattr(sys, 'argv', [
        'watchdog', '--log-file', str(log), '--state-file', str(state),
        '--heartbeat-dir', str(heartbeats), '--notify',
    ])
    sent = []
    accepted = iter([True, False, False, True, True])

    def notify(message, _):
        outbox = json.loads(state.read_text())['notification_outbox']
        assert outbox, 'the notification must be persisted before a send is attempted'
        sent.append(message)
        return next(accepted)

    monkeypatch.setattr(mod, 'send_notification', notify)
    quota(start, 'local_first')
    assert mod.main() == 1
    success = heartbeat('task-a', start + dt.timedelta(minutes=1), start + dt.timedelta(minutes=2))
    (heartbeats / 'task-a.json').write_text(json.dumps(success))
    assert mod.main() == 0

    quota(start + dt.timedelta(minutes=5), 'local_second')
    assert mod.main() == 1
    outbox = json.loads(state.read_text())['notification_outbox']
    assert [entry['kind'] for entry in outbox] == ['recovery', 'alert']
    assert outbox[0]['incident'] != outbox[1]['incident']
    assert '先前恢復紀錄' in sent[2]

    assert mod.main() == 1
    assert len(sent) == 5
    assert '額度用盡' in sent[-1] and '延後送達' not in sent[-1]
    assert json.loads(state.read_text())['notification_outbox'] == []


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
