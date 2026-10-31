"""One bounded, timed burst per account and daily flash round."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Event, Lock
import time

from checkin_client import CheckinError

BURST_DEFAULTS = {'burst_count': 40, 'burst_interval': 0.05, 'burst_workers': 8}
PREPARE_SECONDS = 30
FOLLOWUP_SECONDS = 10


def run_flash_burst(prepared, settings, before_submit, cancelled, *, followup_seconds=FOLLOWUP_SECONDS,
                    result_key='flash', stop_on_result=True, planned_offsets=None):
    """Queue by planned time, independently of earlier request completion.

    Each worker leases one prepared HTTP session. Queued calls are cancelled
    after a terminal reply; calls already sent are drained before closing it.
    """
    if not prepared.ready:
        return {**prepared.result, 'state': 'skipped', 'submitted': False,
                'message': prepared.result[result_key+'_message'], 'attempt_count': 0, 'attempts': []}
    count, interval, workers = (settings[k] for k in ('burst_count', 'burst_interval', 'burst_workers'))
    start, end = prepared.start_monotonic, prepared.end_monotonic
    times = [start + offset for offset in planned_offsets] if planned_offsets is not None else [start + i * interval for i in range(count)]
    if followup_seconds is not None:
        times.append(times[-1] + followup_seconds)
    halt, guard = Event(), Lock()
    records, pending = [], set()
    sent = 0

    def send(index):
        nonlocal sent
        if halt.is_set() or cancelled() or time.monotonic() >= end:
            return
        record = {'index': index + 1, 'followup': index == count, 'sent_at': None,
                  'planned_offset_ms':round((times[index]-start)*1000)}
        def entering():
            nonlocal sent
            if halt.is_set() or cancelled() or time.monotonic() >= end:
                raise CheckinError('已停止后续秒杀请求', 'BURST_STOPPED', 409)
            if before_submit is not None:
                before_submit()
            with guard:
                sent += 1
                record['sent_at'] = prepared.local_time()
        try:
            outcome = prepared.send(before_submit=entering)
        except CheckinError as error:
            outcome = {'state': 'uncertain' if error.uncertain else 'failed', 'message': str(error), 'code': error.code}
        record.update(outcome)
        with guard:
            if record['sent_at'] is not None or outcome.get('code') != 'BURST_STOPPED':
                records.append(record)
            if stop_on_result and outcome['state'] != 'retryable':
                halt.set()
        return outcome

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='checkin-flash-request')
    cursor = 0
    try:
        while cursor < len(times) or pending:
            if cancelled() or time.monotonic() >= end:
                halt.set()
            done = {future for future in pending if future.done()}
            for future in done:
                if not future.cancelled():
                    future.result()
            pending.difference_update(done)
            if halt.is_set():
                for future in pending:
                    future.cancel()
                cursor = len(times)
            now = time.monotonic()
            if cursor < len(times) and now >= times[cursor] and not halt.is_set():
                pending.add(pool.submit(send, cursor))
                cursor += 1
                continue
            if not pending and cursor >= len(times):
                break
            delay = max(0, times[cursor] - now) if cursor < len(times) else 0.1
            # Poll cancellation during prewarming and the delayed follow-up.
            delay = min(delay, 0.1)
            if pending:
                wait(pending, timeout=delay, return_when=FIRST_COMPLETED)
            else:
                halt.wait(delay)
    finally:
        halt.set()
        pool.shutdown(wait=True, cancel_futures=True)
    records.sort(key=lambda record: record['index'])
    winner = next((r for r in records if r['state'] == 'success'), None)
    uncertain = any(r['state'] == 'uncertain' for r in records)
    # A timeout or a limit reply can be reconciled through the official state;
    # neither authorizes another request outside this planned round.
    needs_check = uncertain or any(str(r.get('code', '')).startswith('PRIZE_LIMIT') for r in records)
    result = prepared.result
    if winner is None and needs_check:
        try:
            result = prepared.reconcile()
            if result[result_key+'_state'] == 'already_claimed':
                winner = {'message': '官方状态确认本月已领取'}
        except CheckinError:
            pass
    if winner:
        state, message = 'success', winner['message']
        result = {**result, result_key+'_state': 'already_claimed'}
    elif uncertain:
        state, message = 'failed', '领取结果待核实，已停止剩余请求，本轮不会重发'
    elif cancelled():
        state, message = 'stopped', '已停止后续请求，已发送的结果已保留'
    elif records:
        last = next((r for r in records if r['state'] in ('failed', 'terminal')), records[-1])
        state = 'failed' if last['state'] == 'failed' else 'skipped'
        message = last['message']
    else:
        state, message = 'skipped', '场次已结束，本轮没有发送请求'
    message = f'已发起 {sent} 次 · '+message
    return {**result, 'state': state, 'message': message, result_key+'_message': message,
            'submitted': bool(sent), 'uncertain': uncertain and winner is None,
            'attempt_count': sent, 'attempts': records}
