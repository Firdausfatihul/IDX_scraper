from __future__ import annotations
import threading, time
from collections import defaultdict
from idx_digest.llm_scheduler import GlobalLLMScheduler, ScheduledJob

def test_scheduler_enforces_global_and_per_group_limits():
    lock=threading.Lock(); active=0; max_total=0; by=defaultdict(int); max_by=defaultdict(int)
    def work(group):
        nonlocal active,max_total
        with lock:
            active+=1; by[group]+=1; max_total=max(max_total,active); max_by[group]=max(max_by[group],by[group])
        time.sleep(.04)
        with lock: active-=1; by[group]-=1
        return group
    s=GlobalLLMScheduler(max_workers=4,max_per_group=2)
    try:
        for g in 'ABC':
            for i in range(4): s.submit(ScheduledJob(job_id=f'{g}-{i}',group_key=g,stage='document',ticker=g,func=lambda g=g:work(g)))
        m=s.wait()
    finally: s.close()
    assert max_total==4; assert all(v<=2 for v in max_by.values()); assert m['completed']==12; assert m['failed']==0

def test_completion_hook_can_enqueue_dependency_without_deadlock():
    order=[]; lock=threading.Lock(); s=GlobalLLMScheduler(max_workers=2,max_per_group=2); remaining={'n':2}
    def rec(name):
        time.sleep(.02)
        with lock: order.append(name)
        return name
    def done(_v,e):
        assert e is None
        with lock: remaining['n']-=1; ready=remaining['n']==0
        if ready: s.submit(ScheduledJob(job_id='ann',group_key='ann',stage='announcement',ticker='T',func=lambda:rec('announcement')))
    try:
        for i in range(2): s.submit(ScheduledJob(job_id=f'd{i}',group_key='ann',stage='document',ticker='T',func=lambda i=i:rec(f'doc-{i}'),on_complete=done))
        s.wait()
    finally: s.close()
    assert order[-1]=='announcement'; assert set(order[:2])=={'doc-0','doc-1'}


def test_scheduler_fairness_is_by_ticker_not_number_of_groups():
    starts=[]; lock=threading.Lock(); gate=threading.Event()
    def work(t):
        with lock: starts.append(t)
        gate.wait(timeout=.15)
        return t
    s=GlobalLLMScheduler(max_workers=4,max_per_group=2)
    try:
        # AAAA has many disclosure groups. It must not consume every initial slot.
        for i in range(8):
            s.submit(ScheduledJob(job_id=f'A-{i}',group_key=f'A-ann-{i}',stage='document',ticker='AAAA',func=lambda:work('AAAA')))
        s.submit(ScheduledJob(job_id='B',group_key='B-ann',stage='document',ticker='BBBB',func=lambda:work('BBBB')))
        s.submit(ScheduledJob(job_id='C',group_key='C-ann',stage='document',ticker='CCCC',func=lambda:work('CCCC')))
        time.sleep(.04)
        gate.set(); s.wait()
    finally: s.close()
    assert 'BBBB' in starts[:6]
    assert 'CCCC' in starts[:6]


def test_scheduler_weighted_stage_priority_does_not_starve_reducers():
    order=[]; lock=threading.Lock()
    def work(name):
        with lock: order.append(name)
        time.sleep(.01)
        return name
    s=GlobalLLMScheduler(max_workers=1,max_per_group=1)
    try:
        for i in range(6):
            s.submit(ScheduledJob(job_id=f'd{i}',group_key=f'd{i}',stage='document',ticker=f'D{i}',func=lambda i=i:work(f'doc{i}')))
        s.submit(ScheduledJob(job_id='ann',group_key='ann',stage='announcement',ticker='ANN',func=lambda:work('announcement')))
        s.submit(ScheduledJob(job_id='company',group_key='company',stage='company',ticker='CO',func=lambda:work('company')))
        s.wait()
    finally: s.close()
    assert order.index('announcement') < 6
    assert order[-1] != 'announcement'
