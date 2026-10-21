"""Validate signed request charges with isolated attempts and explicit estimates.

The TEE supplies quantities and the frozen quote; management checks all arithmetic.
Legacy v1 status restrictions remain unchanged for existing records.
"""


COMMON = ('calculator', 'provider', 'basis', 'currency', 'official_price_revision',
          'official_version', 'multiplier_millionths')
USAGE = {'input', 'output', 'total', 'cache_read', 'cache_write', 'cache_write_hour',
         'reasoning', 'reported_cost_in_usd_ticks', 'tools'}


def integer(value, maximum=2**64-1):
    return type(value) is int and 0 <= value <= maximum


def usage(value):
    return (isinstance(value, dict) and set(value) <= USAGE
            and all(v is None or integer(v) for k, v in value.items() if k != 'tools')
            and ('tools' not in value or (isinstance(value['tools'], dict)
                 and set(value['tools']) <= {'web_search', 'x_search', 'code_execution'}
                 and all(integer(v) for v in value['tools'].values()))))


def evidence(b, attempt_status):
    source, coverage = b.get('usage_source'), b.get('coverage')
    if source not in ('official', 'estimated', 'mixed', 'none') or coverage not in ('complete', 'partial', 'unknown'):
        return False
    status = b.get('upstream_status')
    if status is not None and not (integer(status, 599) and status >= 100):
        return False
    if 'upstream_failed' in b and type(b['upstream_failed']) is not bool:
        return False
    if not usage(b.get('usage')):
        return False
    if source in ('estimated', 'mixed'):
        # Missing usage on a successful attempt does not authorize estimation.
        # A provider may report a model failure inside HTTP 200 (e.g. SSE).
        if not (attempt_status >= 400 or (status is not None and status >= 400)
                or b.get('upstream_failed') is True):
            return False
        e, reported = b.get('estimate'), b.get('reported_usage')
        if not isinstance(e, dict) or not usage(reported) or coverage != 'partial':
            return False
        if (set(e) != {'method', 'input_tokens', 'visible_output_tokens', 'input_source', 'output_source',
                       'cache_policy', 'context_incomplete', 'hidden_output_included'}
                or e['method'] != 'o200k_json_text_v1'
                or e['input_source'] not in ('local', 'upstream') or e['output_source'] not in ('local', 'upstream')
                or e['cache_policy'] not in ('ordinary_input', 'upstream')
                or type(e['context_incomplete']) is not bool or e['hidden_output_included'] is not False
                or any(v is not None and not integer(v) for v in (e['input_tokens'], e['visible_output_tokens']))):
            return False
        if not ((e['visible_output_tokens'] or 0) > 0
                or any((reported.get(k) or 0) > 0 for k in ('input', 'output', 'cache_read', 'cache_write'))):
            return False
        for field, estimate_field in (('input', 'input_tokens'), ('output', 'visible_output_tokens')):
            expected = e[estimate_field] if e[field+'_source'] == 'local' else reported.get(field)
            if b['usage'].get(field) != expected:
                return False
        if any(b['usage'].get(k) != reported.get(k) for k in ('cache_read', 'cache_write', 'cache_write_hour')):
            return False
        ordinary = reported.get('cache_read') is None and reported.get('cache_write') is None
        if e['cache_policy'] != ('ordinary_input' if ordinary else 'upstream'):
            return False
    elif 'estimate' in b or 'reported_usage' in b:
        return False
    if b.get('state') == 'calculated':
        if source in ('official', 'none') and coverage != 'complete':
            return False
        if source == 'none' and (b.get('charge_microusd') != 0 or b.get('official_cost_picousd') != '0'
                                 or b['usage'].get('input') != 0 or b['usage'].get('output') != 0):
            return False
    return True


def validate(event, validate_amount):
    result = event['billing']
    records = result.get('attempt_records')
    count = event.get('attempts')
    if (result.get('state') != 'calculated' or not integer(count, 16) or count == 0
            or not isinstance(records, list) or len(records) != count
            or not integer(event.get('status'), 599) or event['status'] < 100):
        return None
    total, priced, unknown = 0, 0, 0
    estimated, official = False, False
    for number, record in enumerate(records, 1):
        if (not isinstance(record, dict) or set(record) != {'number', 'status', 'billing'}
                or type(record['number']) is not int or record['number'] != number
                or not integer(record['status'], 599) or record['status'] < 100):
            return None
        b = record['billing']
        if (not isinstance(b, dict) or type(b.get('version')) is not int or b['version'] != 1
                or any(b.get(k) != result.get(k) for k in COMMON) or not evidence(b, record['status'])):
            return None
        if b.get('state') == 'pending':
            if 'charge_microusd' in b or 'official_cost_picousd' in b:
                return None
            unknown += 1
            continue
        if b.get('state') != 'calculated':
            return None
        # Reuse v1's exact monetary checks. The v2 evidence rules above govern
        # error/cancellation eligibility; HTTP status is not a price dimension.
        check = {**event, 'status': 200, 'attempts': 1, 'billing': b}
        if validate_amount(check) is None:
            return None
        total += int(b['official_cost_picousd'])
        if total >= 2**128:
            return None
        priced += 1
        estimated |= b['usage_source'] in ('estimated', 'mixed')
        official |= b['usage_source'] in ('official', 'mixed')
    source = 'mixed' if estimated and official else 'estimated' if estimated else 'official' if official else 'none'
    coverage = 'partial' if unknown or estimated else 'complete'
    if (not priced or result.get('usage_source') != source or result.get('coverage') != coverage
            or type(result.get('unknown_attempts')) is not int or result['unknown_attempts'] != unknown):
        return None
    multiplier = result.get('multiplier_millionths')
    if not integer(multiplier):
        return None
    numerator = total * multiplier
    if numerator >= 2**128:
        return None
    computed = ((numerator + 10**12-1)//10**12, (total + 10**6-1)//10**6)
    if (result.get('official_cost_picousd') != str(total) or result.get('charge_numerator') != str(numerator)
            or result.get('charge_denominator') != '1000000000000'
            or result.get('rounding') != 'ceil_once_to_micro_usd'
            or any(not integer(result.get(k)) or result[k] != v
                   for k, v in zip(('charge_microusd', 'official_cost_microusd'), computed))):
        return None
    return computed
