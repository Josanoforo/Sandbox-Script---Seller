"""Proofie sandbox seed — one file, standard library only (Python 3.10+).
Copyright 2026 Jonatan J. Guadarrama G. Licensed under the Apache License, Version 2.0.

Populates YOUR PayPal sandbox with the data Proofie needs: two sales by the same sandbox buyer and one
buyer-opened "Item not received" dispute. Sale 1 is captured once and gets one DEMO tracker; the dispute is
opened on it. Sale 2 is captured once with NO tracker (for the tracking page). --one-sale skips sale 2.
Use your sandbox Business app (PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET in the environment or in an env file)
and a separate sandbox Personal buyer account; the buyer approves both payments and opens the dispute in the
browser. This script never opens, answers, escalates or resolves a dispute and never sends evidence. It talks
only to the PayPal sandbox. Run --dry-run first (no credentials, no network). On resume use the SAME --state.
"""
from __future__ import annotations
import argparse
import base64
import json
import math
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit
import uuid

BASE = 'https://api-m.sandbox.paypal.com'
ORDER_PATH = '/v2/checkout/orders'
TRACKER_PATH = '/v1/shipping/trackers'
DISPUTE_PATH = '/v1/customer/disputes/'
TOKEN_PATH = '/v1/oauth2/token'
EVIDENCE_NAME = 'sandbox_seed.jsonl'
CREDENTIAL_KEYS = ('PAYPAL_CLIENT_ID', 'PAYPAL_CLIENT_SECRET')

# DEMO DATA - fictitious address and name; set on the order itself (SET_PROVIDED_ADDRESS).
CURRENCY = 'USD'
SHIPPING_NAME = 'Ana Demo Ficticia'
SHIPPING_ADDRESS = {'address_line_1': 'Calle Falsa 123, Col. Demo', 'admin_area_2': 'Ciudad de Mexico',
                    'admin_area_1': 'CDMX', 'postal_code': '06600', 'country_code': 'MX'}


class Stop(RuntimeError):
    """A safe, judge-facing stop; messages never contain response bodies."""


class SeedError(RuntimeError):
    """HTTP >= 400 from PayPal. Carries status, issue name and debug id; never the response body."""
    def __init__(self, status, issue=None, debug_id=None):
        super().__init__('PayPal HTTP %s issue=%s debug_id=%s' % (status, issue, debug_id))
        self.status, self.issue, self.debug_id = status, issue, debug_id


def identifier(value: str) -> str:
    if not re.fullmatch(r'[A-Za-z0-9-]{3,80}', value):
        raise ValueError('Expected an identifier, not a URL')
    return value


def sandbox_url(value: str, *, approval: bool = False) -> str:
    parsed = urlsplit(value)
    hosts = {'www.sandbox.paypal.com', 'sandbox.paypal.com'} if approval else {'api-m.sandbox.paypal.com'}
    if (parsed.scheme != 'https' or parsed.hostname not in hosts or parsed.username
            or parsed.password or parsed.port not in (None,443) or parsed.fragment):
        raise Stop('Only the PayPal sandbox origin is allowed')
    return value


def assert_client(client) -> None:
    value=str(client.base_url)
    sandbox_url(value)
    parsed=urlsplit(value)
    if parsed.path not in ('','/') or parsed.query:
        raise Stop('Sandbox client must use the API origin without a path or query')


def cents_to_str(cents: int) -> str:
    if isinstance(cents, bool) or not isinstance(cents, int) or cents <= 0:
        raise ValueError('amount must be a positive integer number of cents')
    return '%d.%02d' % (cents // 100, cents % 100)


def build_order_body(amount_cents: int, label: str) -> dict:
    return {'intent': 'CAPTURE',
            'purchase_units': [{'reference_id': label, 'description': 'DEMO DATA - ' + label,
                                'amount': {'currency_code': CURRENCY, 'value': cents_to_str(amount_cents)},
                                'shipping': {'name': {'full_name': SHIPPING_NAME}, 'address': dict(SHIPPING_ADDRESS)}}],
            'payment_source': {'paypal': {'experience_context': {
                'user_action': 'PAY_NOW',
                'shipping_preference': 'SET_PROVIDED_ADDRESS',
                'return_url': 'https://example.com/paypal/return',
                'cancel_url': 'https://example.com/paypal/cancel',
                'brand_name': 'Proofie (DEMO)',
                'landing_page': 'LOGIN'}}}}


def read_env_file(path: Path) -> dict:
    """Plain KEY=VALUE lines; returns ONLY the two credential keys, nothing else is kept."""
    found = {}
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        if key.startswith('export '):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        if key in CREDENTIAL_KEYS and value:
            found[key] = value
    return found


def load_credentials(env_file=None, *, environ=None, cwd=None) -> tuple:
    environ = os.environ if environ is None else environ
    values = {k: environ.get(k, '') for k in CREDENTIAL_KEYS}
    if not all(values.values()):
        path = Path(env_file) if env_file else Path(cwd or Path.cwd()) / '.env'
        if path.is_file():
            for key, value in read_env_file(path).items():
                values[key] = values[key] or value
        elif env_file:
            raise Stop('Env file not found: ' + str(path))
    missing = [k for k in CREDENTIAL_KEYS if not values[k]]
    if missing:
        raise Stop('Sandbox credentials missing (' + ', '.join(missing) + '). Set PAYPAL_CLIENT_ID and '
                   'PAYPAL_CLIENT_SECRET in the environment, or in an env file (--env-file PATH, default: the '
                   'file named .env in the current folder). Nothing was sent.')
    return values['PAYPAL_CLIENT_ID'], values['PAYPAL_CLIENT_SECRET']


def _facts(path: str, body) -> dict:
    """The ONLY response fields that reach the evidence log: ids, statuses, link rels. Never a body."""
    if not isinstance(body, dict):
        return {}
    def text(value):
        return value if isinstance(value, (str, int)) and not isinstance(value, bool) and len(str(value)) <= 120 else None
    facts = {}
    if path.startswith(ORDER_PATH):
        captures = [c for p in body.get('purchase_units', []) if isinstance(p, dict)
                    for c in p.get('payments', {}).get('captures', []) if isinstance(c, dict)]
        capture = next((c for c in captures if c.get('status') == 'COMPLETED'), captures[0] if captures else {})
        facts = {'order_id': text(body.get('id')), 'order_status': text(body.get('status')),
                 'capture_id': text(capture.get('id')), 'capture_status': text(capture.get('status'))}
    elif path.startswith(TRACKER_PATH):
        listed = body.get('trackers') or body.get('tracker_identifiers')
        tracker = listed[0] if isinstance(listed, list) and listed and isinstance(listed[0], dict) else body
        facts = {'tracker_transaction_id': text(tracker.get('transaction_id')),
                 'tracking_number': text(tracker.get('tracking_number')), 'tracker_status': text(tracker.get('status'))}
    elif path.startswith(DISPUTE_PATH):
        facts = {'dispute_id': text(body.get('dispute_id')), 'dispute_reason': text(body.get('reason')),
                 'dispute_status': text(body.get('status'))}
    facts = {k: v for k, v in facts.items() if v is not None}
    links = body.get('links')
    if isinstance(links, list) and any(isinstance(l, dict) and l.get('rel') for l in links):
        facts['link_rels'] = [str(l['rel']) for l in links if isinstance(l, dict) and l.get('rel')]
    return facts


class SeedClient:
    """Minimal sandbox client on urllib. Logs one sanitized JSON line per call; never bodies or secrets."""
    base_url = BASE

    def __init__(self, client_id: str, client_secret: str, evidence_dir, timeout: float = 30.0):
        self._basic = base64.b64encode((client_id + ':' + client_secret).encode('utf-8')).decode('ascii')
        self._timeout = timeout
        self._token = None
        self._token_expiry = 0.0
        evidence_dir = Path(evidence_dir)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = evidence_dir / EVIDENCE_NAME

    def _log(self, record: dict) -> None:
        record = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'), **record}
        with self.log_path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')

    def _send(self, method: str, path: str, headers: dict, data):
        """Returns (status, lower-cased headers, parsed JSON or {}); raises only transport errors."""
        request = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=self._timeout)
        except urllib.error.HTTPError as error:
            status, head, raw = error.code, error.headers, error.read()
        else:
            try:
                status, head, raw = getattr(response, 'status', None) or response.getcode(), response.headers, response.read()
            finally:
                close = getattr(response, 'close', None)
                if close:
                    close()
        head = {str(k).lower(): v for k, v in (head.items() if head else [])}
        try:
            body = json.loads(raw.decode('utf-8')) if raw else {}
        except ValueError:
            body = {'non_json_length': len(raw)}
        return status, head, body

    def token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        started = time.perf_counter()
        try:
            status, head, body = self._send('POST', TOKEN_PATH, {
                'Authorization': 'Basic ' + self._basic, 'Accept': 'application/json',
                'Content-Type': 'application/x-www-form-urlencoded'},
                urlencode({'grant_type': 'client_credentials'}).encode('ascii'))
        except Exception as exc:
            self._log({'op': 'oauth_token', 'method': 'POST', 'path': TOKEN_PATH, 'status': None,
                       'ms': round((time.perf_counter() - started) * 1000),
                       'transport_error': type(exc).__name__, 'outcome': 'unknown'})
            raise
        ms = round((time.perf_counter() - started) * 1000)
        data = body if isinstance(body, dict) else {}
        if status != 200 or not data.get('access_token'):
            self._log({'op': 'oauth_token', 'method': 'POST', 'path': TOKEN_PATH, 'status': status, 'ms': ms})
            raise SeedError(status, str(data.get('error'))[:80] if data.get('error') else None, head.get('paypal-debug-id'))
        self._token = data['access_token']
        self._token_expiry = time.time() + int(data.get('expires_in') or 0)
        self._log({'op': 'oauth_token', 'status': 200, 'ms': ms, 'expires_in': data.get('expires_in')})
        return self._token

    def call(self, method: str, path: str, op: str, *, json_body=None, request_id=None) -> dict:
        if not path.startswith('/') or '://' in path:
            raise Stop('Only PayPal sandbox API paths are allowed')
        method = method.upper()
        headers = {'Authorization': 'Bearer ' + self.token(), 'Content-Type': 'application/json',
                   'Accept': 'application/json', 'Prefer': 'return=representation'}
        if method == 'POST':
            request_id = request_id or str(uuid.uuid4())
            headers['PayPal-Request-Id'] = request_id
        else:
            request_id = None
        data = json.dumps(json_body).encode('utf-8') if json_body is not None else None
        started = time.perf_counter()
        try:
            status, head, body = self._send(method, path, headers, data)
        except Exception as exc:  # class name only: the text may echo request details
            self._log({'op': op, 'method': method, 'path': path, 'status': None,
                       'ms': round((time.perf_counter() - started) * 1000), 'request_id': request_id,
                       'transport_error': type(exc).__name__, 'outcome': 'unknown'})
            raise
        ms = round((time.perf_counter() - started) * 1000)
        debug_id = head.get('paypal-debug-id') or (body.get('debug_id') if isinstance(body, dict) else None)
        issue = None
        if status >= 400 and isinstance(body, dict):
            details = body.get('details')
            issue = details[0].get('issue') if isinstance(details, list) and details and isinstance(details[0], dict) else body.get('name')
            issue = str(issue)[:80] if issue else None
        self._log({'op': op, 'method': method, 'path': path, 'status': status, 'ms': ms, 'request_id': request_id,
                   'debug_id': debug_id, 'issue': issue, **(_facts(path, body) if status < 400 else {})})
        if status >= 400:
            raise SeedError(status, issue, debug_id)
        return body


def completed_capture(order: dict) -> str | None:
    captures = [c for p in order.get('purchase_units', [])
                for c in p.get('payments', {}).get('captures', []) if c.get('status') == 'COMPLETED']
    if len(captures) > 1:
        raise Stop('More than one completed capture; inspect the order manually')
    if not captures:
        return None
    capture=identifier(captures[0]['id'])
    if not re.fullmatch(r'[A-Za-z0-9]{17}',capture):
        raise Stop('Capture id does not match the existing tracker contract')
    return capture


def assess(capture: str, dispute: dict) -> bool:
    matches = any(t.get('seller_transaction_id') == capture for t in dispute.get('disputed_transactions', []))
    action=False
    for link in dispute.get('links',[]):
        if link.get('rel','').replace('_','-')=='provide-evidence' and link.get('method','').upper()=='POST':
            try:
                sandbox_url(link.get('href',''))
            except (Stop,ValueError):
                continue
            action=True
    return (matches and dispute.get('reason') == 'MERCHANDISE_OR_SERVICE_NOT_RECEIVED'
            and dispute.get('status') == 'WAITING_FOR_SELLER_RESPONSE' and action)


class Checkpoint:
    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'schema':1}
        if self.data.get('schema') != 1:
            raise Stop('Unsupported state file; retain it for inspection')

    def save(self, **updates):
        self.data.update(updates)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + '.pending')
        with temporary.open('x',encoding='utf-8') as stream:
            json.dump(self.data,stream,sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary,self.path)


def _sale(client, state, *, second, order_id, amount_cents, attempts, interval, emit, sleep) -> tuple:
    """Create (once) -> buyer approves -> capture (once) for sale 1 or, with second=True, sale 2.
    State keys: sale 1 order/create/capture, sale 2 order2/create2/capture2. Returns (order id, capture id)."""
    sfx, tag = ('2', ' (second sale)') if second else ('', '')
    order_key, create_key, capture_key = 'order'+sfx, 'create'+sfx, 'capture'+sfx
    stored = state.data.get(order_key)
    if order_id:
        order_id = identifier(order_id)
        if stored and stored != order_id:
            raise Stop('Order differs from the checkpoint; use its original state file'+tag)
        if second and order_id == state.data.get('order'):
            raise Stop('The second sale must be a different order than the first')
        state.save(**{order_key:order_id})
    else:
        order_id = stored
    if not order_id:
        if state.data.get(create_key+'_attempted'):
            raise Stop('Create outcome unknown'+tag+'. Find the order id in private evidence and resume '
                       +('--order2' if second else '--order')+'; no retry')
        body = build_order_body(amount_cents,'proofie-seed-'+('2-' if second else '')+uuid.uuid4().hex[:12])
        request = str(uuid.uuid4())
        state.save(**{create_key+'_attempted':True, create_key+'_request':request})
        try:
            created = client.call('POST',ORDER_PATH,'seed_create',json_body=body,request_id=request)
        except Exception:
            raise Stop('Create outcome unknown'+tag+'. Inspect private evidence; do not repeat create') from None
        order_id = identifier(created['id'])
        state.save(**{order_key:order_id})
    path = ORDER_PATH + '/' + identifier(order_id)
    def read_order(op):
        found=client.call('GET',path,op)
        if found.get('id') != order_id:
            raise Stop('Order readback does not match the requested order; inspect manually'+tag)
        return found
    order = read_order('seed_order_readback')
    emit(('Second sale order ID: ' if second else 'Order ID: ') + order_id)
    for attempt in range(attempts):
        if completed_capture(order) or order.get('status') == 'APPROVED':
            break
        if attempt == 0:
            approval = next((l.get('href') for l in order.get('links',[]) if l.get('rel') in ('approve','payer-action')), None)
            if not approval:
                raise Stop('No buyer approval link; inspect this order manually'+tag)
            emit('Open as the SANDBOX PERSONAL BUYER and approve'+(' the second sale' if second else '')+': '
                 + sandbox_url(approval,approval=True))
        if attempt+1 == attempts:
            raise Stop('Buyer approval timed out'+tag+'. Approve in the browser and resume --order with the SAME --state')
        sleep(interval)
        order = read_order('seed_approval_read')
    capture = completed_capture(order)
    if not capture:
        if state.data.get(capture_key+'_attempted'):
            raise Stop('Previous capture is still unconfirmed'+tag+'. Inspect private evidence; no second capture POST')
        request = str(uuid.uuid4())
        state.save(**{capture_key+'_attempted':True, capture_key+'_request':request})
        try:
            client.call('POST',path+'/capture','seed_capture',json_body={},request_id=request)
        except Exception:
            pass  # Only GET reconciles an uncertain response; never repeat POST.
        order = read_order('seed_capture_readback')
        capture = completed_capture(order)
        if not capture:
            raise Stop('Capture unconfirmed'+tag+'. Resume the SAME state for readback only; no blind retry')
    if state.data.get(capture_key) and state.data[capture_key] != capture:
        raise Stop('Capture differs from the checkpoint; inspect manually'+tag)
    state.save(**{capture_key:capture})
    return order_id, capture


def seed(client, state: Checkpoint, *, order_id=None, order2_id=None, dispute_id=None, amount_cents=2500,
         second_amount_cents=1500, one_sale=False, attempts=60, interval=10, emit=print, ask=input,
         sleep=time.sleep) -> str:
    assert_client(client)
    if attempts < 1 or interval < 0 or not math.isfinite(interval):
        raise ValueError('Polling bounds must be positive attempts and nonnegative interval')
    state.save(ready=False)
    common = dict(attempts=attempts, interval=interval, emit=emit, sleep=sleep)
    order_id, capture = _sale(client,state,second=False,order_id=order_id,amount_cents=amount_cents,**common)
    tracking = state.data.get('tracking') or ('DEMO' + uuid.uuid4().hex[:20].upper())
    state.save(tracking=tracking)
    tracker_get = TRACKER_PATH+'?'+urlencode({'transaction_id':capture})
    def read_tracker():
        body=client.call('GET',tracker_get,'seed_tracker_readback')
        return any(t.get('transaction_id') == capture and t.get('tracking_number') == tracking
                   for t in body.get('trackers',[]) if isinstance(t,dict))
    confirmed = read_tracker()
    if not confirmed:
        if state.data.get('tracker_attempted'):
            raise Stop('Previous tracker is unconfirmed. Inspect private evidence; no second tracker POST')
        # Exact existing Add Tracking shape; no optional feature import or activation.
        body={'trackers':[{'transaction_id':capture,'tracking_number':tracking,
                           'status':'SHIPPED','carrier':'OTHER','carrier_name_other':'DEMO synthetic carrier'}]}
        request=str(uuid.uuid4())
        state.save(tracker_attempted=True,tracker_request=request)
        try:
            posted=client.call('POST',TRACKER_PATH,'seed_tracker_add',json_body=body,request_id=request)
        except Exception:
            posted=None
        confirmed=read_tracker()
        if not confirmed or (isinstance(posted,dict) and posted.get('errors')):
            raise Stop('Tracker unconfirmed or rejected. Resume SAME state for GET only; no blind retry')
    state.save(tracker_confirmed=True)
    emit('Capture ID: '+capture)
    if not one_sale:
        # Second sale: same buyer, captured once, deliberately NO tracker (the tracking page needs one without).
        order2, capture2 = _sale(client,state,second=True,order_id=order2_id,amount_cents=second_amount_cents,**common)
        if capture2 == capture or order2 == order_id:
            raise Stop('The second sale must be a different order and capture than the first')
        emit('Second sale capture ID: '+capture2+' (no tracker added, on purpose)')
    emit('Buyer: sign in to www.sandbox.paypal.com with the separate Personal account. '
         'Resolution Center > Report a Problem > select the FIRST payment (USD '
         +cents_to_str(amount_cents)+', order '+order_id+') > Continue > '
         'Issues with your purchase > Item not received > Continue. Complete the shown form and submit. '
         'Do not escalate, answer or send evidence. Copy its dispute ID.')
    # Recorded list lacks a supported transaction filter. Do not enumerate unrelated details.
    dispute_id=identifier(dispute_id or state.data.get('dispute') or ask('Paste this payment\'s dispute ID: ').strip())
    state.save(dispute=dispute_id)
    for attempt in range(attempts):
        try:
            dispute=client.call('GET',DISPUTE_PATH+dispute_id,'seed_dispute_read')
        except Exception as exc:
            if getattr(exc,'status',None) != 404:
                raise Stop('Dispute read failed. Inspect the private evidence and resume') from None
            dispute=None
        if dispute is not None:
            if dispute.get('dispute_id') != dispute_id:
                raise Stop('Dispute readback does not match the requested id; not ready')
            if not any(t.get('seller_transaction_id') == capture for t in dispute.get('disputed_transactions',[])):
                raise Stop('Dispute does not match this capture; no dispute was changed')
            if dispute.get('reason') != 'MERCHANDISE_OR_SERVICE_NOT_RECEIVED':
                raise Stop('Dispute reason is not Item Not Received; not ready')
            if assess(capture,dispute):
                state.save(ready=True)
                emit('Dispute ID: '+dispute_id+'; ready for seller response. Open Proofie on this dispute. No evidence sent.')
                return dispute_id
        if attempt+1<attempts:
            sleep(interval)
    raise Stop('Dispute did not become ready within the polling bound. Check buyer submission/status and resume; no writes to dispute')


def checkout_root() -> Path:
    """The folder state and evidence must stay out of: the repository root when run as tools/sandbox_seed.py
    inside a checkout, otherwise the folder the script was copied to."""
    here = Path(__file__).resolve().parent
    return here.parent if here.name == 'tools' and (here.parent / '.git').exists() else here


DESCRIPTION = """Proofie sandbox seed - one file, standard library only (Python 3.10+).
Prepares, in YOUR PayPal sandbox, two sales by the same buyer plus one buyer-opened dispute:
sale 1 (captured once, one DEMO tracker, the Item not received dispute is opened on it) and
sale 2 (captured once, NO tracker). Use --one-sale to skip sale 2.
Credentials: PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET from the environment, else from --env-file PATH
(default: the file named .env in the current folder); only those two keys are read, never printed.
It never opens, answers, escalates or resolves a dispute and never sends evidence. Sandbox only.
Run --dry-run first. On resume use the SAME --state. --state and --evidence-dir must be private and
outside this script's folder."""


def main(argv=None, *, client_factory=None, ask=input) -> int:
    parser=argparse.ArgumentParser(description=DESCRIPTION,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dry-run',action='store_true',help='Plan only; no client, credentials, state or network')
    parser.add_argument('--order',type=identifier,help='Resume sale 1 with this order ID and the SAME private --state')
    parser.add_argument('--order2',type=identifier,help='Resume sale 2 with this order ID (only if its create outcome was unknown)')
    parser.add_argument('--dispute',type=identifier,help='Buyer-provided dispute ID; otherwise prompted after both captures')
    parser.add_argument('--state',type=Path,help='Private checkpoint outside this folder; preserve across resume')
    parser.add_argument('--evidence-dir',type=Path,help='Private sanitized evidence outside this folder')
    parser.add_argument('--env-file',type=Path,help='KEY=VALUE file with PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET (default: .env in the current folder)')
    parser.add_argument('--amount-cents',type=int,default=2500,help='Sale 1 amount, positive integer USD cents (default 2500)')
    parser.add_argument('--second-amount-cents',type=int,default=1500,help='Sale 2 amount, positive integer USD cents (default 1500)')
    parser.add_argument('--one-sale',action='store_true',help='Skip sale 2 (only the tracked sale and the dispute)')
    parser.add_argument('--attempts',type=int,default=60,help='GET polling cap per human phase (default 60)')
    parser.add_argument('--interval',type=float,default=10,help='Seconds between GET polls (default 10)')
    args=parser.parse_args(argv)
    if (args.amount_cents<1 or args.second_amount_cents<1 or args.attempts<1 or args.interval<0
            or not math.isfinite(args.interval)):
        parser.error('Positive cents/attempts and nonnegative interval required')
    print('SANDBOX ONLY. Plan:')
    print('  sale 1 (USD '+cents_to_str(args.amount_cents)+'): create order; buyer approves; capture once; add one DEMO tracker')
    print('  second sale: ' + ('skipped (--one-sale)' if args.one_sale else
          'USD '+cents_to_str(args.second_amount_cents)+', same buyer; create; buyer approves; capture once; NO tracker'))
    print('  the buyer opens Item not received on sale 1; GET readiness. Never writes to a dispute.')
    if args.dry_run:
        print('Dry run: no client, credentials, checkpoint or network. Resume keeps the SAME --state.')
        return 0
    root=checkout_root()
    if not args.state or not args.evidence_dir:
        parser.error('--state and --evidence-dir outside the checkout are required')
    for path in (args.state.resolve(),args.evidence_dir.resolve()):
        if path==root or root in path.parents:
            parser.error('State and evidence must be outside the checkout')
    if ask('Type SANDBOX to authorize these sandbox test orders, captures and tracker: ') != 'SANDBOX':
        print('Cancelled before client initialization')
        return 2
    if client_factory is None:
        try:
            credentials=load_credentials(args.env_file)
        except Stop as exc:
            print(exc)
            return 3
        client_factory=lambda:SeedClient(*credentials,args.evidence_dir.resolve())
    state_path=args.state.resolve()
    lock=state_path.with_name(state_path.name+'.lock')
    lock.parent.mkdir(parents=True,exist_ok=True)
    try:
        with lock.open('x',encoding='utf-8'):
            pass
        try:
            state=Checkpoint(state_path)
            seed(client_factory(),state,order_id=args.order,order2_id=args.order2,dispute_id=args.dispute,
                 amount_cents=args.amount_cents,second_amount_cents=args.second_amount_cents,
                 one_sale=args.one_sale,attempts=args.attempts,interval=args.interval,ask=ask)
        finally:
            lock.unlink()  # Only the exclusive lock created by this invocation.
        return 0
    except FileExistsError:
        print('State is locked. Confirm the prior process ended; never start two preparations.')
        return 3
    except Exception as exc:
        if isinstance(exc,Stop):
            print(exc)
        elif isinstance(exc,SeedError):
            print('Stopped: '+str(exc)+'. Inspect private evidence; no blind retry.')
        else:
            print('Stopped: '+type(exc).__name__+'. Inspect private evidence; no blind retry.')
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
