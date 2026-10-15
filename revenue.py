"""Read-only receivables from archived request-time billing; never use current prices."""
from collections import defaultdict
from datetime import date, datetime, timedelta
import hashlib
import json
from urllib.parse import parse_qs


def integer(value, maximum=10**15):
    return type(value) is int and 0 <= value <= maximum


def router_charge(event):
    """Same integer receipt checks as the platform; this does not confirm payment."""
    result, quote = event.get('billing'), event.get('merchant_pricing')
    if not isinstance(result, dict) or not isinstance(quote, dict):
        return None
    calculators = {'openai': 'chatgpt_v1', 'anthropic': 'claude_v1', 'xai': 'grok_v1', 'workbuddy': 'workbuddy_v1'}
    if (type(result.get('version')) is not int or result['version'] != 1 or result.get('state') != 'calculated'
            or result.get('provider') != event.get('provider')
            or result.get('calculator') != calculators.get(event.get('provider'))
            or result.get('basis') != 'official_list_price' or result.get('currency') != 'USD'
            or quote.get('basis') != 'official_pool_multiplier'
            or not event.get('merchant_id') or not integer(event.get('merchant_price_version')) or not event['merchant_price_version']
            or type(event.get('attempts')) is not int or event['attempts'] != 1
            or not integer(event.get('status')) or not 200 <= event['status'] < 400):
        return None
    for field in ('official_price_revision', 'official_version', 'multiplier_millionths'):
        if not integer(result.get(field)) or type(quote.get(field)) is not int or result[field] != quote[field]:
            return None
    if not result['official_price_revision'] or not result['official_version']:
        return None

    def decimal(value):
        if not isinstance(value, str) or not 1 <= len(value) <= 39 or not value.isascii() or not value.isdigit():
            return None
        return int(value)

    base, numerator = (decimal(result.get(k)) for k in ('official_cost_picousd', 'charge_numerator'))
    if base is None or numerator is None or numerator != base * result['multiplier_millionths']:
        return None
    if result.get('charge_denominator') != '1000000000000' or result.get('rounding') != 'ceil_once_to_micro_usd':
        return None
    items = result.get('items')
    if not isinstance(items, list) or len(items) > 16:
        return None
    summed = 0
    for item in items:
        if (not isinstance(item, dict) or not integer(item.get('quantity'), 2**64-1)
                or not integer(item.get('unit_price_microusd'), 10**12) or item.get('unit') not in ('call', 'million_tokens')):
            return None
        amount = item['quantity'] * item['unit_price_microusd'] * (10**6 if item['unit'] == 'call' else 1)
        if decimal(item.get('amount_picousd')) != amount:
            return None
        summed += amount
    if summed != base:
        return None
    computed = ((numerator + 10**12-1)//10**12, (base + 10**6-1)//10**6)
    if any(not integer(result.get(k), 2**64-1) or result[k] != v
           for k, v in zip(('charge_microusd', 'official_cost_microusd'), computed)):
        return None
    return computed


def legacy_charge(event):
    """Old receipts use their frozen quote, matching the platform's legacy calculation."""
    quote, usage, cache = event.get('merchant_pricing'), event.get('usage'), event.get('cache_usage') or {}
    if (not isinstance(quote, dict) or not isinstance(usage, dict) or not isinstance(cache, dict)
            or not integer(event.get('status')) or not 200 <= event['status'] < 400
            or not integer(event.get('attempts')) or event['attempts'] != 1
            or not integer(event.get('merchant_price_version')) or not event['merchant_price_version']
            or not integer(usage.get('unknown_usage_requests')) or usage['unknown_usage_requests']
            or any(not integer(usage.get(k), 2**64-1) for k in ('input_tokens', 'output_tokens'))):
        return None
    pooled = quote.get('basis') == 'official_pool_multiplier'
    multiplier = quote.get('multiplier_millionths') if pooled else 1_000_000
    if quote.get('basis') not in (None, 'official_pool_multiplier') or not integer(multiplier):
        return None
    if pooled and (not quote.get('official_source') or any(not integer(quote.get(k)) or not quote[k]
                    for k in ('official_price_revision', 'official_version', 'official_checked_at'))):
        return None
    valid_until, created = quote.get('valid_until'), event.get('created_at')
    if valid_until is not None and (not integer(valid_until) or not integer(created) or valid_until <= created):
        return None
    rates = quote
    tier = quote.get('long_context')
    if tier is not None:
        if not isinstance(tier, dict) or not integer(tier.get('above_input_tokens')) or not tier['above_input_tokens']:
            return None
        if usage['input_tokens'] > tier['above_input_tokens']:
            rates = tier.get('rates')
    if not isinstance(rates, dict) or any(not integer(rates.get(k)) for k in ('input', 'output')):
        return None
    splits = [cache.get(k, 0) for k in ('read', 'write', 'write_hour')]
    if any(not integer(n, 2**64-1) for n in splits):
        return None
    plain = usage['input_tokens'] - sum(splits)
    if plain < 0:
        return None
    raw = plain * rates['input'] + usage['output_tokens'] * rates['output']
    for tokens, key in zip(splits, ('cache_read', 'cache_write', 'cache_write_hour')):
        if tokens:
            if not integer(rates.get(key)):
                return None
            raw += tokens * rates[key]
    return ((raw * multiplier + 10**12-1)//10**12, (raw + 10**6-1)//10**6 if pooled else None)


def entry(instance, seq, event):
    result = event.get('billing')
    amount = None
    reason, basis = 'missing_historic_price_or_usage', 'legacy_quote'
    if type(event.get('attempts')) is int and event['attempts'] == 0:
        amount, basis, reason = (0, 0), 'not_forwarded', ''
    elif 'billing' in event:
        amount, basis = router_charge(event), 'router_calculation'
        reason = result.get('reason', 'invalid_router_calculation') if isinstance(result, dict) else 'invalid_router_calculation'
    else:
        amount = legacy_charge(event)
    text = lambda key: event.get(key) if isinstance(event.get(key), str) else ''
    quote = event.get('merchant_pricing') or {}
    quote = quote if isinstance(quote, dict) else {}
    return {'instance': instance, 'seq': str(seq), 'request_id': text('request_id'),
            'created_at': event.get('created_at') if integer(event.get('created_at'), 253402214400) else 0,
            'user_id': text('user_id') or text('key'), 'pool': text('pool'), 'model': text('model'),
            'provider': text('provider'), 'status': event.get('status') if integer(event.get('status'), 999) else None,
            'state': 'calculated' if amount is not None else 'pending', 'reason': '' if amount is not None else reason,
            'basis': basis, 'charge_microusd': str(amount[0]) if amount is not None else None,
            'official_cost_microusd': str(amount[1]) if amount is not None and amount[1] is not None else None,
            'multiplier_millionths': quote.get('multiplier_millionths') if integer(quote.get('multiplier_millionths')) else None,
            'price_version': event.get('merchant_price_version'), 'official_price_revision': quote.get('official_price_revision')}


def filters(query):
    values = parse_qs(query, keep_blank_values=True, max_num_fields=10)
    if set(values) - {'start', 'end', 'pool', 'offset'} or any(len(v) != 1 for v in values.values()):
        raise ValueError('收入筛选条件无效')
    values = {k: v[0] for k, v in values.items()}
    try:
        start = date.fromisoformat(values['start']) if values.get('start') else None
        end = date.fromisoformat(values['end']) if values.get('end') else None
        offset = int(values.get('offset') or '0')
        if (start and end and start > end) or not 0 <= offset <= 10**9 or len(values.get('pool', '')) > 128:
            raise ValueError()
        after = datetime.combine(start, datetime.min.time()).timestamp() if start else None
        before = datetime.combine(end + timedelta(days=1), datetime.min.time()).timestamp() if end else None
    except (ValueError, OverflowError, OSError):
        raise ValueError('请检查日期范围和分页参数') from None
    return values, after, before, offset


def summarize(rows):
    amount = sum(int(r['charge_microusd']) for r in rows if r['state'] == 'calculated')
    pending = sum(r['state'] == 'pending' for r in rows)
    return {'requests': len(rows), 'calculated': len(rows)-pending, 'pending': pending, 'charge_microusd': str(amount)}


class RevenueIndex:
    """Derived memory-only index; encrypted archive remains the authoritative source."""
    def __init__(self):
        self.cursor = 0
        self.entries, self.hashes = {}, {}

    def add(self, rowid, instance, seq, event):
        request = event.get('request_id')
        key = ('request', request) if isinstance(request, str) and request else ('archive', instance, seq)
        fingerprint = hashlib.sha256(json.dumps(event, sort_keys=True, separators=(',', ':')).encode()).digest()
        if key not in self.entries:
            self.entries[key], self.hashes[key] = entry(instance, seq, event), fingerprint
        elif self.hashes[key] != fingerprint:
            self.entries[key].update(state='pending', reason='conflicting_request_records', charge_microusd=None, official_cost_microusd=None)
        self.cursor = rowid

    def report(self, query='', now=None):
        values, after, before, offset = filters(query)
        now = now or datetime.now().astimezone()
        today = datetime.combine(now.date(), datetime.min.time()).timestamp()
        tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time()).timestamp()
        month = datetime.combine(now.date().replace(day=1), datetime.min.time()).timestamp()
        rows = list(self.entries.values())
        selected = sorted((r for r in rows if (after is None or r['created_at'] >= after)
                           and (before is None or r['created_at'] < before)
                           and (not values.get('pool') or r['pool'] == values['pool'])),
                          key=lambda r: (r['created_at'], r['instance'], int(r['seq'])), reverse=True)
        grouped = defaultdict(list)
        for row in selected:
            grouped[row['pool']].append(row)
        return {'currency': 'USD', 'unit': 'micro_usd', 'generated_at': int(now.timestamp()),
                'timezone': str(now.tzinfo or datetime.now().astimezone().tzinfo),
                'summary': {**summarize(rows),
                            'today_microusd': summarize([r for r in rows if today <= r['created_at'] < tomorrow])['charge_microusd'],
                            'month_microusd': summarize([r for r in rows if month <= r['created_at'] < tomorrow])['charge_microusd']},
                'filtered': summarize(selected), 'pools': sorted({r['pool'] for r in rows}),
                'by_pool': [{'pool': pool, **summarize(items)} for pool, items in sorted(grouped.items())],
                'data': [dict(row) for row in selected[offset:offset+50]],
                'offset': offset, 'limit': 50, 'has_more': offset+50 < len(selected)}
