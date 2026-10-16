#!/usr/bin/env python3
"""Local Codex usage ledger. Standard library only; no inference or remote calls."""
import argparse
import contextlib
import datetime as dt
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import py_compile
import secrets
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens', 'total_tokens')
LABEL = 'local.codex-usage-ledger.daily'
CURRENCY_DIGITS = {'CNY': 2, 'USD': 2, 'EUR': 2, 'HKD': 2, 'GBP': 2,
                   'JPY': 0, 'KRW': 0, 'SGD': 2, 'AUD': 2, 'CAD': 2}
EXPENSE_CATEGORIES = ('订阅', 'API 充值/调用', '图像/视频生成', '素材/插件', '云服务', '其他')


class DuplicateExpense(ValueError):
    def __init__(self, expense):
        self.expense = expense
        super().__init__('同日同商家同金额已有记录 ' + expense['id'] + '；若为另一笔付款，请明确允许同额记录。')


def storage_path():
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'CodexUsageLedger'
    return Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local' / 'share'))) / 'codex-usage-ledger'


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(encode(value), encoding='utf-8')
    temp.replace(path)


def settings(root=ROOT):
    values = json.loads((root / 'config.example.json').read_text())
    local = root / 'config.local.json'
    if local.exists():
        values.update(json.loads(local.read_text()))
    ZoneInfo(values['timezone'])
    return values


def database(data):
    data.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(data / 'usage.sqlite', timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript('''
    CREATE TABLE IF NOT EXISTS cursors (
      sid TEXT PRIMARY KEY, path TEXT, offset INTEGER, context TEXT, signature TEXT);
    CREATE TABLE IF NOT EXISTS sessions (
      sid TEXT PRIMARY KEY, project_key TEXT, project_name TEXT, attribution TEXT,
      provider TEXT, available INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS events (
      fingerprint TEXT PRIMARY KEY, sid TEXT, timestamp TEXT, model TEXT,
      input_tokens INTEGER, cached_input_tokens INTEGER, output_tokens INTEGER,
      reasoning_output_tokens INTEGER, total_tokens INTEGER);
    CREATE INDEX IF NOT EXISTS event_time ON events(timestamp);
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS expenses (
      id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, payment_date TEXT NOT NULL,
      amount_minor INTEGER NOT NULL, currency TEXT NOT NULL, payee TEXT NOT NULL,
      description TEXT NOT NULL, category TEXT NOT NULL, project TEXT NOT NULL,
      kind TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL,
      voided_at TEXT, void_reason TEXT);
    CREATE INDEX IF NOT EXISTS expense_date ON expenses(payment_date);
    ''')
    return db


def put_meta(db, key, value):
    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, encode(value)))


def get_meta(db, key, default=None):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def expense_view(row):
    value = dict(row)
    digits = CURRENCY_DIGITS[value['currency']]
    value['amount'] = format(Decimal(value['amount_minor']) / (10 ** digits), '.' + str(digits) + 'f')
    value['signed_minor'] = value['amount_minor'] * (-1 if value['kind'] == 'refund' else 1)
    return value


def expense_list(data, include_voided=False):
    """Read only: listing an empty installation must not create files or tables."""
    path = data / 'usage.sqlite'
    if not path.exists():
        return []
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='expenses'").fetchone():
            return []
        query = 'SELECT * FROM expenses' + ('' if include_voided else ' WHERE voided_at IS NULL')
        return [expense_view(row) for row in db.execute(query + ' ORDER BY payment_date DESC,created_at DESC,id')]


def normalized_expense(values, timezone='Asia/Shanghai'):
    if not isinstance(values, dict):
        raise ValueError('支出信息必须是对象')
    def field(name, default='', required=False, limit=300):
        raw = values.get(name, default)
        if not isinstance(raw, str):
            raise ValueError(name + ' 必须是文字')
        text = raw.strip()
        if (required and not text) or len(text) > limit or any(ord(c) < 32 for c in text):
            raise ValueError(name + ' 为空、过长或含控制字符')
        return text
    currency = field('currency', required=True).upper()
    currency = {'RMB': 'CNY', '人民币': 'CNY', '美元': 'USD'}.get(currency, currency)
    if currency not in CURRENCY_DIGITS:
        raise ValueError('暂不支持该币种，请使用明确的币种代码')
    try:
        amount = Decimal(str(values.get('amount', '')))
        minor = amount * (10 ** CURRENCY_DIGITS[currency])
        if not amount.is_finite() or not 0 < minor <= 10**12 or minor != minor.to_integral_value():
            raise ValueError('金额应大于零，且符合该币种小数位数；退款也请填正数并选择退款类型')
    except (InvalidOperation, TypeError):
        raise ValueError('金额无效')
    today = dt.datetime.now(ZoneInfo(timezone)).date().isoformat()
    day = field('payment_date', today, required=True)
    try:
        if dt.date.fromisoformat(day).isoformat() != day:
            raise ValueError()
    except ValueError:
        raise ValueError('付款日期应为 YYYY-MM-DD 格式的真实日期')
    kind = field('kind', 'expense')
    category = field('category', '其他')
    if kind not in ('expense', 'refund') or category not in EXPENSE_CATEGORIES:
        raise ValueError('支出类型或费用分类无效')
    return {'request_key': field('request_key', required=True, limit=200),
            'payment_date': day, 'amount_minor': int(minor), 'currency': currency,
            'payee': field('payee', required=True, limit=100),
            'description': field('description', required=True), 'category': category,
            'project': field('project', '共用/未分配', limit=100) or '共用/未分配',
            'kind': kind, 'note': field('note', limit=1000)}


def expense_add(data, values, timezone='Asia/Shanghai'):
    item = normalized_expense(values, timezone)
    with contextlib.closing(database(data)) as db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT * FROM expenses WHERE request_key=?', (item['request_key'],)).fetchone()
        if existing:
            if any(existing[k] != v for k, v in item.items()):
                raise ValueError('同一请求标识已有不同内容，未覆盖旧记录')
            if existing['voided_at']:
                raise ValueError('该请求对应记录已作废，不会重新启用；新付款请用新的请求标识')
            return {'created': False, 'expense': expense_view(existing)}
        candidate = db.execute('''SELECT * FROM expenses WHERE payment_date=? AND currency=?
            AND amount_minor=? AND lower(payee)=lower(?) AND kind=? AND voided_at IS NULL LIMIT 1''',
            tuple(item[k] for k in ('payment_date', 'currency', 'amount_minor', 'payee', 'kind'))).fetchone()
        if candidate and values.get('allow_duplicate') is not True:
            raise DuplicateExpense(expense_view(candidate))
        item.update(id=uuid.uuid4().hex[:16], created_at=dt.datetime.now(dt.timezone.utc).isoformat())
        columns = list(item)
        db.execute('INSERT INTO expenses (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')', tuple(item.values()))
        db.commit()
        return {'created': True, 'expense': expense_view(db.execute('SELECT * FROM expenses WHERE id=?', (item['id'],)).fetchone())}


def expense_void(data, expense_id, reason):
    if not isinstance(expense_id, str) or not expense_id:
        raise ValueError('请提供记录编号')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 300:
        raise ValueError('请提供有效作废原因')
    # A mistaken ID must not initialize a new ledger.
    if not (data / 'usage.sqlite').exists():
        raise ValueError('找不到该支出记录')
    with contextlib.closing(database(data)) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM expenses WHERE id=?', (expense_id,)).fetchone()
        if not row:
            raise ValueError('找不到该支出记录')
        if not row['voided_at']:
            db.execute('UPDATE expenses SET voided_at=?,void_reason=? WHERE id=?',
                       (dt.datetime.now(dt.timezone.utc).isoformat(), reason.strip(), expense_id))
        db.commit()
        return {'voided': True, 'expense': expense_view(db.execute('SELECT * FROM expenses WHERE id=?', (expense_id,)).fetchone())}


def read_catalog(home):
    paths = sorted(home.glob('state_*.sqlite'), key=lambda p: int(p.stem.split('_')[-1]), reverse=True)
    if not paths:
        return {}, {}, [], ['未找到 Codex 项目数据库；将按会话工作目录归集。']
    with contextlib.closing(sqlite3.connect(paths[0].as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        names = {r['id']: r['name'] for r in db.execute('SELECT id,name FROM projects')}
        roots = [(r['path'], r['project_id']) for r in db.execute('SELECT path,project_id FROM project_roots')]
        rows = {r['id']: dict(r) for r in db.execute(
            'SELECT id,rollout_path,cwd,project_id,model_provider FROM threads')}
    return rows, names, roots, []


def original_worktree(cwd):
    """Resolve an existing worktree to its main checkout without invoking Git."""
    path = Path(cwd)
    for parent in [path, *path.parents]:
        marker = parent / '.git'
        if marker.is_file():
            try:
                content = marker.read_text().strip()
                if not content.startswith('gitdir: '):
                    return cwd
                gd = (parent / content[8:]).resolve()
                common = gd / 'commondir'
                if common.is_file():
                    cd = (gd / common.read_text().strip()).resolve()
                    if cd.name == '.git':
                        return str(cd.parent)
            except (OSError, ValueError):
                return cwd
        if marker.is_dir():
            break
    return cwd


def attribute(row, context, names, roots, catalog):
    project = row.get('project_id')
    if project in names:
        return project, names[project], '项目 ID'
    parent = context.get('parent')
    seen = set()
    while parent and parent not in seen:
        seen.add(parent)
        pr = catalog.get(parent, {})
        if pr.get('project_id') in names:
            pid = pr['project_id']
            return pid, names[pid], '父会话项目'
        break
    cwd = row.get('cwd') or context.get('cwd')
    if not cwd:
        return 'unassigned', '未归属项目', '缺少目录'
    cwd = original_worktree(cwd)
    candidates = [(r, pid) for r, pid in roots if cwd == r or cwd.startswith(r.rstrip('/') + '/')]
    if candidates:
        r, pid = max(candidates, key=lambda item: len(item[0]))
        return pid, names.get(pid, Path(r).name), '项目根目录'
    if cwd == str(Path.home()) or '/.codex/worktrees/' in cwd or '/.chatgpt-projects/' in cwd:
        return 'unassigned', '未归属项目', '目录无法对应'
    path = Path(cwd)
    name = path.parent.name + ' / ' + path.name if path.parent.name else (path.name or cwd)
    return 'directory:' + hashlib.sha256(cwd.encode()).hexdigest()[:20], name, '工作目录推断'


def as_usage(value):
    return {k: max(0, int(value.get(k, 0) or 0)) for k in FIELDS}


def parse_file(db, path, sid, row, names, roots, catalog, warnings):
    previous = db.execute('SELECT * FROM cursors WHERE sid=?', (sid,)).fetchone()
    stat = path.stat()
    with path.open('rb') as stream:
        signature = hashlib.sha256(stream.read(min(256, stat.st_size))).hexdigest()
        context = {}
        start = 0
        if previous and previous['path'] == str(path) and previous['signature'] == signature and stat.st_size >= previous['offset']:
            context = json.loads(previous['context'])
            start = previous['offset']
        stream.seek(start)
        end = start
        added = 0
        while True:
            offset = stream.tell()
            raw = stream.readline()
            if not raw or not raw.endswith(b'\n'):
                break  # A live writer can have an unfinished final JSON line.
            end = stream.tell()
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeError):
                warnings.append('存在无法解析的完整记录行：' + sid + ':' + str(offset))
                continue
            payload = event.get('payload') or {}
            typ = event.get('type')
            if typ == 'session_meta':
                # Do not persist instructions, messages, titles, or tool results.
                context['cwd'] = payload.get('cwd', context.get('cwd'))
                context['provider'] = payload.get('model_provider', 'unknown')
                context['parent'] = payload.get('forked_from_id')
            elif typ == 'turn_context':
                context['model'] = payload.get('model', 'unknown')
            elif typ == 'event_msg' and payload.get('type') == 'token_count':
                timestamp = event.get('timestamp')
                try:
                    stamp = dt.datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    if stamp.tzinfo is None:
                        raise ValueError('timezone missing')
                    timestamp = stamp.astimezone(dt.timezone.utc).isoformat()
                except (ValueError, TypeError, AttributeError):
                    warnings.append('用量记录缺少有效时间：' + sid)
                    continue
                limits = payload.get('rate_limits')
                if limits:
                    old = get_meta(db, 'quota', {})
                    if timestamp >= old.get('timestamp', ''):
                        put_meta(db, 'quota', {'timestamp': timestamp, 'limits': limits})
                info = payload.get('info') or {}
                if not info.get('total_token_usage'):
                    continue
                total = as_usage(info['total_token_usage'])
                last = as_usage(info.get('last_token_usage') or {})
                before = context.get('total')
                if before == total:
                    continue
                if before and all(total[k] >= before[k] for k in FIELDS):
                    delta = {k: total[k] - before[k] for k in FIELDS}
                else:
                    # A fork may start with an inherited cumulative baseline.
                    delta = last if info.get('last_token_usage') else total
                    if not before and total['total_tokens'] > delta['total_tokens']:
                        context['inherited_baseline'] = True
                    if before:
                        context['counter_reset'] = True
                context['total'] = total
                if not delta['total_tokens']:
                    continue
                model = context.get('model', 'unknown')
                fingerprint = hashlib.sha256(encode([timestamp, total, last, model]).encode()).hexdigest()
                # Global fingerprint also removes replayed records copied into forks.
                cursor = db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?)',
                                    (fingerprint, sid, timestamp, model, *(delta[k] for k in FIELDS)))
                added += cursor.rowcount
        db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?,?,?,?)',
                   (sid, str(path), end, encode(context), signature))
        key, name, why = attribute(row, context, names, roots, catalog)
        db.execute('INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,1)',
                   (sid, key, name, why, row.get('model_provider') or context.get('provider', 'unknown')))
        return added, end - start


def collect(home, data):
    started = time.perf_counter()
    data.mkdir(parents=True, exist_ok=True)
    with (data / 'collect.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows, names, roots, warnings = read_catalog(home)
        files = {}
        for row in rows.values():
            p = Path(row['rollout_path'])
            if p.is_file():
                files[row['id']] = p
        for folder in ('sessions', 'archived_sessions'):
            for p in (home / folder).rglob('*.jsonl'):
                # Codex rollout filenames end in a UUID; metadata is read during parsing.
                sid = p.stem[-36:]
                if len(sid) == 36:
                    files.setdefault(sid, p)
        if not files:
            raise RuntimeError('没有找到可读的本地 Codex 会话记录；未改动已有统计。')
        with contextlib.closing(database(data)) as db:
            db.execute('UPDATE sessions SET available=0')
            added = read_bytes = 0
            for sid, path in files.items():
                try:
                    db.execute('SAVEPOINT file_parse')
                    n, size = parse_file(db, path, sid, rows.get(sid, {}), names, roots, rows, warnings)
                    db.execute('RELEASE file_parse')
                    added += n
                    read_bytes += size
                except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
                    db.execute('ROLLBACK TO file_parse')
                    db.execute('RELEASE file_parse')
                    warnings.append('无法采集会话 ' + sid + '：' + type(exc).__name__)
            missing = len(set(rows) - set(files))
            anomalies = [json.loads(r[0]) for r in db.execute('SELECT context FROM cursors')]
            result = {'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                      'seconds': round(time.perf_counter() - started, 3), 'files': len(files),
                      'new_events': added, 'bytes_read': read_bytes, 'missing_rollouts': missing,
                      'warnings': sorted(set(warnings)),
                      'inherited_baselines': sum(bool(v.get('inherited_baseline')) for v in anomalies),
                      'counter_resets': sum(bool(v.get('counter_reset')) for v in anomalies)}
            put_meta(db, 'collection', result)
            db.commit()
        atomic_json(data / 'last-run.json', result)
        return result


def report(data, config):
    tz = ZoneInfo(config['timezone'])
    daily = {}
    with contextlib.closing(database(data)) as db:
        for r in db.execute('''SELECT e.*,s.project_key,s.project_name,s.attribution,s.provider
                            FROM events e JOIN sessions s USING(sid) ORDER BY timestamp'''):
            day = dt.datetime.fromisoformat(r['timestamp']).astimezone(tz).date().isoformat()
            key = (day, r['project_key'], r['model'], r['provider'])
            item = daily.setdefault(key, {'day': day, 'project': r['project_key'], 'name': r['project_name'],
                                         'attribution': r['attribution'], 'model': r['model'], 'provider': r['provider'],
                                         'sessions': set(), **dict.fromkeys(FIELDS, 0)})
            item['sessions'].add(r['sid'])
            for f in FIELDS:
                item[f] += r[f]
        for item in daily.values():
            item['sessions'] = sorted(item['sessions'])
        return {'rows': list(daily.values()), 'expenses': expense_list(data, include_voided=True),
                'currency_digits': CURRENCY_DIGITS, 'expense_categories': EXPENSE_CATEGORIES,
                'collection': get_meta(db, 'collection', {}),
                'quota': get_meta(db, 'quota'), 'config': config,
                'schedule': schedule_status(),
                'session_count': db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],
                'now': dt.datetime.now(tz).isoformat()}


def scheduler_path():
    return Path.home() / 'Library' / 'LaunchAgents' / (LABEL + '.plist')


def schedule_status():
    path = scheduler_path()
    if not path.exists():
        return {'installed': False}
    try:
        value = plistlib.loads(path.read_bytes())
        if value.get('ProgramArguments', [None, None])[1] not in (str(ROOT / 'app.py'), str(storage_path() / 'collector.pyc')):
            return {'installed': False}
        return {'installed': True, **value.get('StartCalendarInterval', {}), 'timezone': time.tzname[0]}
    except (ValueError, OSError, IndexError):
        return {'installed': False}


def install_schedule(root, hour, minute):
    if sys.platform != 'darwin':
        raise RuntimeError('自动安装仅支持 macOS；其他系统请用系统计划任务执行 collect。')
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError('小时或分钟无效')
    path = scheduler_path()
    runtime = storage_path()
    runtime.mkdir(parents=True, exist_ok=True)
    artifact = runtime / 'collector.pyc'
    data = runtime / 'data'
    expected = [sys.executable, str(artifact), '--data-dir', str(data), 'collect']
    if path.exists():
        old = plistlib.loads(path.read_bytes())
        if old.get('ProgramArguments') not in (expected, [sys.executable, str(root / 'app.py'), 'collect']):
            raise RuntimeError('已有同名计划任务且来源不同，停止以免覆盖。')
    # Generated bytecode, not a second source checkout; avoids Documents TCC for launchd.
    py_compile.compile(str(root / 'app.py'), cfile=str(artifact), dfile='codex-usage-ledger/app.py', doraise=True)
    atomic_json(runtime / 'collector-manifest.json', {
        'source_sha256': hashlib.sha256((root / 'app.py').read_bytes()).hexdigest(),
        'artifact_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(),
        'python': sys.version.split()[0], 'source_root': str(root)})
    data.mkdir(exist_ok=True)
    value = {'Label': LABEL, 'ProgramArguments': expected,
             'StartCalendarInterval': {'Hour': hour, 'Minute': minute},
             'RunAtLoad': True, 'ProcessType': 'Background', 'LowPriorityIO': True, 'Nice': 10,
             'StandardOutPath': str(data / 'daily.log'), 'StandardErrorPath': str(data / 'daily-error.log'),
             'WorkingDirectory': str(runtime)}
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        subprocess.run(['launchctl', 'bootout', 'gui/' + str(os.getuid()), str(path)], capture_output=True)
    path.write_bytes(plistlib.dumps(value))
    subprocess.run(['launchctl', 'bootstrap', 'gui/' + str(os.getuid()), str(path)], check=True)
    return {'installed': str(path), 'hour': hour, 'minute': minute,
            'timezone': 'macOS 系统时区；休眠时不唤醒，唤醒后由系统补跑'}


def status(root, data):
    return {'collection': json.loads((data / 'last-run.json').read_text()) if (data / 'last-run.json').exists() else None,
            'schedule_installed': scheduler_path().exists(), 'runtime_bytes': sum(p.stat().st_size for p in data.glob('*') if p.is_file()) if data.exists() else 0,
            'dependencies': {'python': sys.version.split()[0], 'platform': sys.platform, 'third_party': []}}


def serve(root, home, data, idle_seconds=180):
    nonce = secrets.token_urlsafe(24)
    touched = [time.monotonic()]
    assets = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'), '/style.css': ('style.css', 'text/css')}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def valid_host(self):
            return self.headers.get('Host') == '127.0.0.1:' + str(self.server.server_port)

        def respond(self, code, value, mime='application/json'):
            body = encode(value).encode() if mime == 'application/json' else value
            self.send_response(code)
            self.send_header('Content-Type', mime + '; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.valid_host():
                return self.respond(403, {'error': '拒绝非本机请求'})
            touched[0] = time.monotonic()
            if self.path == '/api/ping':
                return self.respond(200, {'app': 'codex-usage-ledger'})
            if self.path == '/api/data':
                return self.respond(200, report(data, settings(root)))
            if self.path in assets:
                filename, mime = assets[self.path]
                body = (root / 'web' / filename).read_bytes().replace(b'__DASHBOARD_NONCE__', nonce.encode())
                return self.respond(200, body, mime)
            return self.respond(404, {'error': '未找到页面'})

        def do_POST(self):
            origin = 'http://127.0.0.1:' + str(self.server.server_port)
            if not self.valid_host() or self.headers.get('Origin') != origin or self.headers.get('X-Dashboard-Nonce') != nonce:
                return self.respond(403, {'error': '请求校验失败，请重新打开看板'})
            touched[0] = time.monotonic()
            try:
                size = int(self.headers.get('Content-Length', 0))
                if size > 4096:
                    return self.respond(413, {'error': '请求过大'})
                raw = self.rfile.read(size)
                if self.path == '/api/expenses/add':
                    return self.respond(200, expense_add(data, json.loads(raw), settings(root)['timezone']))
                if self.path == '/api/expenses/void':
                    values = json.loads(raw)
                    if not isinstance(values, dict):
                        raise ValueError('作废信息必须是对象')
                    return self.respond(200, expense_void(data, values.get('id'), values.get('reason')))
                if self.path == '/api/refresh':
                    result = collect(home, data)
                    return self.respond(200, result)
                if self.path == '/api/settings':
                    values = json.loads(raw)
                    amount = values.get('subscription_monthly')
                    currency = values.get('currency', 'USD')
                    if amount is not None and (isinstance(amount, bool) or not isinstance(amount, (float, int)) or not 0 <= amount <= 1000000):
                        raise ValueError('月费应为空或有效非负金额')
                    if currency not in ('USD', 'CNY', 'EUR', 'HKD'):
                        raise ValueError('币种无效')
                    config = settings(root)
                    config.update(subscription_monthly=amount, currency=currency)
                    atomic_json(root / 'config.local.json', config)
                    return self.respond(200, {'saved': True})
                return self.respond(404, {'error': '接口不存在'})
            except DuplicateExpense as exc:
                return self.respond(409, {'error': str(exc), 'duplicate': exc.expense})
            except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
                return self.respond(400, {'error': str(exc)})

    server = HTTPServer(('127.0.0.1', 0), Handler)
    server.timeout = 1
    address = 'http://127.0.0.1:' + str(server.server_port)
    runtime = data / 'server.json'
    atomic_json(runtime, {'url': address, 'pid': os.getpid()})
    print(address, flush=True)
    try:
        while time.monotonic() - touched[0] < idle_seconds:
            server.handle_request()
    finally:
        server.server_close()
        if runtime.exists() and json.loads(runtime.read_text()).get('pid') == os.getpid():
            runtime.unlink()


def open_dashboard(root, data):
    runtime = data / 'server.json'
    url = None
    if runtime.exists():
        try:
            candidate = json.loads(runtime.read_text())['url']
            with urllib.request.urlopen(candidate + '/api/ping', timeout=1) as res:
                if json.load(res).get('app') == 'codex-usage-ledger':
                    url = candidate
        except (OSError, ValueError, KeyError):
            pass
    if not url:
        data.mkdir(parents=True, exist_ok=True)
        with (data / 'server.log').open('a') as log:
            process = subprocess.Popen([sys.executable, str(root / 'app.py'), '--data-dir', str(data), 'serve'], cwd=root,
                                       stdout=log, stderr=log, start_new_session=True)
        for _ in range(60):
            if runtime.exists():
                value = json.loads(runtime.read_text())
                if value['pid'] == process.pid:
                    url = value['url']
                    break
            time.sleep(.1)
        if not url:
            raise RuntimeError('本地看板未启动，请检查 data/server.log。')
    webbrowser.open(url)
    return {'url': url, 'idle_shutdown_seconds': 180}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-home', type=Path, default=Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))))
    parser.add_argument('--data-dir', type=Path, default=storage_path() / 'data')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('collect', help='增量读取用量，更新本地数据库；无模型调用')
    sub.add_parser('status', help='只读检查运行报告和计划任务文件')
    sub.add_parser('open', help='按需启动本地网页并打开浏览器')
    srv = sub.add_parser('serve', help='本机网页服务，页面关闭后自动退出')
    srv.add_argument('--idle-seconds', type=int, default=180)
    install = sub.add_parser('install-schedule', help='macOS 安装每日采集计划，不唤醒电脑')
    install.add_argument('--hour', type=int, default=9)
    install.add_argument('--minute', type=int, default=0)
    sub.add_parser('remove-schedule', help='移除本工具的每日计划，保留统计数据')
    add = sub.add_parser('expense-add', help='记录已支付费用或已收到的退款；不执行付款')
    for field in ('amount', 'currency', 'payee', 'description', 'request-key'):
        add.add_argument('--' + field, required=True)
    add.add_argument('--date', dest='payment_date', help='默认按北京时间取今天，格式 YYYY-MM-DD')
    add.add_argument('--category', default='其他', choices=EXPENSE_CATEGORIES)
    add.add_argument('--project', default='共用/未分配')
    add.add_argument('--kind', default='expense', choices=('expense', 'refund'))
    add.add_argument('--note', default='')
    add.add_argument('--allow-duplicate', action='store_true', help='仅在确认是另一笔同额付款后使用')
    listing = sub.add_parser('expense-list', help='严格只读列出已记录支出')
    listing.add_argument('--include-voided', action='store_true')
    void = sub.add_parser('expense-void', help='按明确指示作废误记，保留原记录与原因')
    void.add_argument('--id', required=True)
    void.add_argument('--reason', required=True)
    args = parser.parse_args()
    if args.command == 'expense-add':
        values = vars(args).copy()
        if values.get('payment_date') is None:
            values.pop('payment_date')
        result = expense_add(args.data_dir, values)
    elif args.command == 'expense-list':
        result = {'expenses': expense_list(args.data_dir, args.include_voided)}
    elif args.command == 'expense-void':
        result = expense_void(args.data_dir, args.id, args.reason)
    elif args.command == 'collect':
        result = collect(args.codex_home.resolve(), args.data_dir.resolve())
    elif args.command == 'status':
        result = status(ROOT, args.data_dir)
    elif args.command == 'install-schedule':
        result = install_schedule(ROOT, args.hour, args.minute)
    elif args.command == 'remove-schedule':
        p = scheduler_path()
        if p.exists():
            old = plistlib.loads(p.read_bytes())
            if old.get('ProgramArguments', [None, None])[1] not in (str(ROOT / 'app.py'), str(storage_path() / 'collector.pyc')):
                raise RuntimeError('同名计划归属不一致，停止。')
            subprocess.run(['launchctl', 'bootout', 'gui/' + str(os.getuid()), str(p)], check=True)
            p.unlink()
        result = {'removed': True}
    elif args.command == 'open':
        result = open_dashboard(ROOT, args.data_dir)
    else:
        serve(ROOT, args.codex_home, args.data_dir, args.idle_seconds)
        return
    print(encode(result))


if __name__ == '__main__':
    try:
        main()
    except DuplicateExpense as error:
        print(encode({'error': str(error), 'duplicate': error.expense}), file=sys.stderr)
        sys.exit(2)
    except (RuntimeError, OSError, ValueError, sqlite3.Error, subprocess.CalledProcessError) as error:
        print(encode({'error': str(error)}), file=sys.stderr)
        sys.exit(1)
