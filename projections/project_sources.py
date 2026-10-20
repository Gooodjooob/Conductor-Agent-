"""Bounded read-only engineering observations, separate from frozen catalog evidence.

No dispatch, ledger writes, credential reads, markdown PASS inference or registry changes.
Only explicitly registered metadata files and exact workflow-bound task identities.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from pathlib import Path

MAX_FILE = 512 * 1024
MAX_CARDS = 512
MAX_TOTAL = 24 * 1024 * 1024
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
AGENT = re.compile(r"(?:dev|qa)-(?:ckb|wkb)?[0-9]+|cox")


def _regular(path, root):
    p, r = Path(path), Path(root)
    if not p.is_absolute() or not r.is_absolute() or '..' in p.parts:
        raise ValueError('source_path_invalid')
    p.relative_to(r)
    for part in (p, *p.parents):
        if part.is_symlink():
            raise ValueError('source_symlink')
    if not stat.S_ISREG(p.stat().st_mode):
        raise ValueError('source_not_regular')
    return p


class Reader:
    def __init__(self):
        self.total = 0

    def read(self, path, root):
        p = _regular(path, root)
        if p.stat().st_size > MAX_FILE:
            raise ValueError('source_too_large')
        with p.open('rb') as f:
            raw = f.read(MAX_FILE + 1)
        self.total += len(raw)
        if len(raw) > MAX_FILE or self.total > MAX_TOTAL:
            raise ValueError('source_budget_exceeded')
        return raw


def card_roles(raw):
    """Structured header only; filename/model are never execution identity evidence."""
    text = raw.decode('utf-8', errors='replace')[:12000]
    out = {}
    if not text.startswith('---\n'):
        return out
    header = text.split('\n---', 1)[0]
    for role in ['executor', 'reviewer', 'qa', 'issuer']:
        match = re.search(r'^' + role + r':\s*\n((?:[ \t]+[^\n]*\n?)*)', header, re.M)
        if match:
            who = re.search(r'^\s+(?:agentId|alias):\s*["\x27]?([A-Za-z0-9_.-]+)', match.group(1), re.M)
            if who and ID.fullmatch(who.group(1)):
                out[role] = who.group(1)
                if role == 'executor' and re.search(r'role:\s*["\x27]?qa-reporter', match.group(1)):
                    out['qa'] = who.group(1)
    return out


def _scalar(value):
    if not isinstance(value, str) or len(value) > 160:
        return None
    return value if not any(ord(c) < 32 for c in value) else None


def execution_observation(binding, reader):
    """Follow exact controller identity; pending next round cannot reuse old review."""
    result = {'sourcePath': binding['path'], 'kind': 'workflow', 'available': False, 'scopeNote':binding.get('scopeNote', ''), 'historical':binding.get('historical', False)}
    try:
        raw = reader.read(binding['path'], binding['root'])
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('source_schema_invalid')
        result.update(available=True, sha256=hashlib.sha256(raw).hexdigest(),
                      phase=_scalar(data.get('currentPhase') or data.get('phase')),
                      declaredState=_scalar(data.get('workerStatus') or data.get('status')),
                      assignedAgent=_scalar(data.get('agentId')),
                      observedAgent=_scalar(data.get('observedAgent')),
                      sourceId=_scalar(data.get('sourceId')),
                      taskId=_scalar(data.get('taskId')), attemptId=_scalar(data.get('attemptId')),
                      cardPath=_scalar_path(data.get('card') or data.get('currentCard')),
                      runtimeIdentity='UNVERIFIED')
        task_id, attempt_id = result['taskId'], result['attemptId']
        db = data.get('currentDatabase') or data.get('db')
        if not db or not task_id or not attempt_id or not ID.fullmatch(task_id) or not ID.fullmatch(attempt_id):
            result['ledgerNote'] = '当前 task/attempt 尚未齐备；不复用上一轮身份或审核'
            return result
        p = _regular(db, binding['root'])
        conn = sqlite3.connect(p.as_uri() + '?mode=ro', uri=True, timeout=.25)
        conn.row_factory = sqlite3.Row
        steps = [0]
        def budget():
            steps[0] += 1000
            return 1 if steps[0] > 100000 else 0
        conn.set_progress_handler(budget, 1000)
        try:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            task = conn.execute('SELECT task_id,agent_id,source_id,status,current_attempt_id,workspace FROM tasks WHERE task_id=? LIMIT 1', (task_id,)).fetchone()
            attempt = conn.execute('SELECT attempt_id,state FROM attempts WHERE task_id=? AND attempt_id=? LIMIT 1', (task_id, attempt_id)).fetchone()
            if not task or not attempt or task['current_attempt_id'] != attempt_id or (result['assignedAgent'] is not None and task['agent_id'] != result['assignedAgent']) or (result['sourceId'] is not None and task['source_id'] != result['sourceId']):
                raise ValueError('execution_identity_mismatch')
            deliveries = conn.execute('SELECT delivery_id,report_path FROM deliveries WHERE task_id=? AND attempt_id=? ORDER BY created_at DESC LIMIT 4', (task_id, attempt_id)).fetchall()
            reviews = []
            for delivery in deliveries:
                reviews += [dict(x) for x in conn.execute('SELECT reviewer_id,decision,review_id FROM reviews WHERE task_id=? AND delivery_id=? ORDER BY created_at DESC LIMIT 4', (task_id, delivery['delivery_id']))]
            result.update(ledgerMatched=True, ledgerState=task['status'], attemptState=attempt['state'],
                          assignedAgent=result['assignedAgent'] or task['agent_id'], sourceId=result['sourceId'] or task['source_id'],
                          developer=task['agent_id'], workspace=task['workspace'], databasePath=str(p),
                          deliveries=[dict(x, reportPath=str(Path(task['workspace']) / x['report_path']) if not Path(x['report_path']).is_absolute() else x['report_path']) for x in deliveries], reviews=reviews,
                          qa='未找到独立 QA 身份；reviewer 与 QA 分开记录')
        finally:
            conn.close()
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        result['available'] = False
        result['error'] = '来源缺失、超限、不可读或身份不匹配；保留原资料，不补造状态'
    return result


def _scalar_path(value):
    return value if isinstance(value, str) and len(value) < 4096 and not any(ord(c) < 32 for c in value) else None


def discover_cards(project):
    known = {c['path']: dict(c) for c in project.get('cardSources', [])[:MAX_CARDS]}
    scanned = 0
    for root in project.get('cardRoots', [])[:4]:
        base = Path(root)
        if not base.is_absolute() or not base.is_dir() or any(p.is_symlink() for p in (base, *base.parents)):
            continue
        for directory, dirs, names in os.walk(base, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not (Path(directory)/d).is_symlink())[:64]
            for name in sorted(names):
                scanned += 1
                if scanned > 4096 or len(known) >= MAX_CARDS:
                    return list(known.values()), '任务卡扫描达到上限；可能还有未显示条目'
                if not name.endswith('.md'):
                    continue
                path = Path(directory)/name
                if path.is_symlink():
                    continue
                if base.name == 'task-cards' and 'conductor/docs/development' in str(base):
                    rel = str(path.relative_to(base)).lower()
                    if not rel.startswith(('2026-09-', '2026-10-')):
                        continue
                    owner = 'd4-product' if any(x in rel for x in ['d4', 'dispatch', 'workbuddy', 'codebuddy']) else 'dsh-sales' if any(x in rel for x in ['dsh', 'sales-assistant', 'pwa']) else 'conductor-ecosystem'
                    if owner != project['projectId']:
                        continue
                if str(path) not in known:
                    known[str(path)] = {'path':str(path), 'root':str(base), 'title':path.stem, 'newlyDiscovered':True}
    return list(known.values()), None


def observe_projects(rows):
    """Rows are per-request catalog copies; timestamps omitted to keep unchanged UI stable."""
    reader = Reader()
    for project in rows:
        for task in project['tasks']:
            observations = []
            for ref in task.get('watchedSources', [])[:8]:
                record = {'sourcePath': ref['path'], 'available': False}
                try:
                    raw = reader.read(ref['path'], ref['root'])
                    record.update(available=True, sha256=hashlib.sha256(raw).hexdigest(),
                                  changed=hashlib.sha256(raw).hexdigest() != ref.get('sha256'))
                except (OSError, ValueError, KeyError):
                    record['note'] = '来源不可读或超过范围/预算，状态未自动推断'
                observations.append(record)
            task['sourceObservations'] = observations
            if any(x.get('changed') for x in observations):
                task['sourceUpdateNote'] = '原工程资料已变化；下面摘要是登记快照，当前完成状态需核新证据。'
            binding = task.get('executionSource')
            if binding:
                task['executionFacts'] = execution_observation(binding, reader)
                facts = task['executionFacts']
                if facts.get('phase') and not facts.get('historical'):
                    task['stateLabel'] = '来源当前状态：' + (facts.get('ledgerState') or facts.get('declaredState') or facts['phase'])
                if facts.get('developer'):
                    task['member'] = facts['developer'] + '（账本匹配；runtime Agent 身份未核）'
                    task['workerLabel'] = '开发者 ' + facts['developer']
                elif facts.get('assignedAgent'):
                    task['member'] = facts['assignedAgent'] + '（workflow指定；执行身份未齐）'
                    task['workerLabel'] = '指定开发者 ' + facts['assignedAgent']
            elif task.get('member', '').startswith('项目原会话'):
                task['member'] = '实际开发者未接入；来源 Codex 会话见材料'
        project['sourceUpdatePolicy'] = '每30秒只读核已登记来源；结构化执行身份/状态自动读回；仅文档变化标记待复核，不自动判验收通过。'
        bound = [t['executionFacts'] for t in project['tasks'] if t.get('executionFacts', {}).get('phase') and not t['executionFacts'].get('historical')]
        if bound:
            project['currentExecutionStages'] = [{'phase': x['phase'], 'taskId': x.get('taskId'), 'state': x.get('ledgerState') or x.get('declaredState')} for x in bound]
        cards = []
        discovered, scan_note = discover_cards(project)
        if scan_note:
            project['cardScanNote'] = scan_note
        for ref in discovered:
            card = {'path': ref['path'], 'title': ref['title'], 'roles': {}, 'evidence': '仅卡内声明，未证明实际执行/QA'}
            try:
                raw = reader.read(ref['path'], ref['root'])
                card.update(roles=card_roles(raw), changed=hashlib.sha256(raw).hexdigest() != ref.get('sha256'))
                if ref.get('newlyDiscovered'):
                    card['newlyDiscovered'] = True
                    card['title'] = next((line[2:].strip()[:240] for line in raw.decode('utf-8',errors='replace').splitlines() if line.startswith('# ')), ref['title'])
            except (OSError, ValueError, KeyError):
                card['error'] = '卡片不可读或超过采集预算'
            cards.append(card)
        for card in cards:
            for task in project['tasks']:
                facts = task.get('executionFacts', {})
                if facts.get('cardPath') == card['path'] and facts.get('ledgerMatched'):
                    card['actualIdentity'] = '开发账本：' + facts['developer'] + '；review：' + ('；'.join(r['reviewer_id'] + ':' + r['decision'] for r in facts.get('reviews', [])) or '未找到') + '；QA：未找到独立登记'
        project['sourceCards'] = cards
    return rows
