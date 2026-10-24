#!/usr/bin/env python3
"""Collect historical OpenRouter catalogs. Python 3.9+, standard library only."""
import argparse
import csv
import gzip
import hashlib
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

VERSION = 'json-catalog-1.1'
SOURCES = {
    'catalog': 'https://openrouter.ai/api/frontend/v1/catalog/models',
    'cards': 'https://openrouter.ai/api/frontend/v1/models/find?active=true&fmt=cards',
}
ROOT = Path(__file__).resolve().parent if '__file__' in globals() else Path.cwd()
DEFAULT_OUTPUT = Path('/Users/bachka/Library/CloudStorage/ProtonDrive-mnbatsaikhan@proton.me-folder/Work Docs/East West Center/AMA articles/OWOM/data/openrouter/model_catalog')


class AccessLimited(Exception):
    pass


class SourceError(Exception):
    pass


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def timestamp_day(timestamp):
    return datetime.strptime(timestamp, '%Y%m%d%H%M%S').date().isoformat()


def variant_identity(model):
    endpoint = model.get('endpoint') or {}
    variant = endpoint.get('variant') or ''
    variant_slug = endpoint.get('model_variant_slug') or ''
    variant_permaslug = endpoint.get('model_variant_permaslug') or ''
    if not variant:
        variant = variant_slug.split(':', 1)[1] if ':' in variant_slug else 'standard'
    permaslug = model.get('permaslug') or ''
    if not variant_permaslug and permaslug:
        variant_permaslug = permaslug if variant == 'standard' else permaslug + ':' + variant
    return variant, variant_slug or model['slug'], variant_permaslug


def validate(payload, source):
    if not isinstance(payload, dict) or 'data' not in payload:
        raise SourceError('Expected a JSON object containing data.')
    data = payload['data']
    if source == 'catalog':
        models = data
    else:
        if not isinstance(data, dict) or not isinstance(data.get('analytics'), dict):
            raise SourceError('Expected data.models and data.analytics objects in card response.')
        models = data.get('models')
    if not isinstance(models, list) or not models:
        raise SourceError('Expected a nonempty model list.')
    for model in models:
        if not isinstance(model, dict) or not isinstance(model.get('slug'), str) or not model['slug']:
            raise SourceError('A model is missing its slug; schema needs inspection.')
        endpoint = model.get('endpoint')
        if endpoint is not None and not isinstance(endpoint, dict):
            raise SourceError('Unexpected endpoint type.')
    return models


def valid_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number >= 0 else None
    except InvalidOperation:
        return None


def text_number(value):
    number = valid_number(value)
    return format(number, 'f') if number is not None else ''


def token_price(endpoint, label):
    """Only normalize prices explicitly identified as token prices by metadata."""
    pricing = endpoint.get('pricing') or {}
    displays = endpoint.get('display_pricing') or pricing.get('display_pricing') or []
    matches = [d for d in displays if isinstance(d, dict)
               and d.get('kind') == 'token' and d.get('sku_label') == label]
    if len(matches) != 1 or matches[0].get('tiers'):
        return ''
    number = valid_number(matches[0].get('price'))
    return format(number * 1000000, 'f') if number is not None else ''


def match_usage(model, analytics):
    variant, _, version = variant_identity(model)
    keys = [version]
    if variant == 'standard':
        keys += [model.get('permaslug'), model.get('slug')]
    else:
        keys += [model['slug'] + ':' + variant]
    matches = []
    for key in dict.fromkeys(k for k in keys if k):
        candidate = analytics.get(key)
        if not isinstance(candidate, dict):
            continue
        if candidate.get('variant') and candidate['variant'] != variant:
            continue
        matches.append((key, candidate))
    if not matches:
        return '', {}, 'not_available'
    fingerprints = {json.dumps(value, sort_keys=True) for _, value in matches}
    if len(fingerprints) > 1:
        return '', {}, 'ambiguous'
    key, value = matches[0]
    return key, value, 'matched'


def build_rows(catalog, cards, day, provenance):
    models = validate(catalog, 'catalog')
    if cards is not None:
        active_models = validate(cards, 'cards')
        analytics = cards['data']['analytics']
    else:
        active_models, analytics = [], {}
    active_keys = {(m['slug'], variant_identity(m)[0]) for m in active_models}
    rows = []
    for index, model in enumerate(models):
        endpoint = model.get('endpoint') or {}
        pricing = endpoint.get('pricing') or {}
        if not isinstance(pricing, dict):
            raise SourceError('Unexpected pricing shape.')
        variant, variant_slug, variant_permaslug = variant_identity(model)
        analytics_key, usage, usage_status = match_usage(model, analytics)
        if cards is None:
            usage_status = 'source_unavailable'
        prompt = valid_number(usage.get('total_prompt_tokens'))
        completion = valid_number(usage.get('total_completion_tokens'))
        total = format(prompt + completion, 'f') if prompt is not None and completion is not None else ''
        if usage_status == 'matched' and not total:
            usage_status = 'incomplete_fields'
        row = {
            'snapshot_date': day,
            'source_record_index': index,
            'model_id': model['slug'],
            'model_name': model.get('name') or '',
            'short_name': model.get('short_name') or '',
            'creator_id': model.get('author') or '',
            'creator_name': model.get('author_display_name') or model.get('author') or '',
            'model_permaslug': model.get('permaslug') or '',
            'variant': variant,
            'model_variant_slug': variant_slug,
            'model_variant_permaslug': variant_permaslug,
            'source_created_at': model.get('created_at') or '',
            'release_date': '',
            'release_date_status': 'not_verified_against_public_release',
            'source_updated_at': model.get('updated_at') or '',
            'is_active': str((model['slug'], variant) in active_keys).lower() if cards is not None else '',
            'active_status_basis': 'membership_in_active_true_response' if cards is not None else 'active_source_unavailable',
            'associated_provider': endpoint.get('provider_display_name') or endpoint.get('provider_name') or '',
            'associated_provider_id': endpoint.get('provider_slug') or '',
            'associated_endpoint_id': endpoint.get('id') or '',
            'input_price_source_raw': pricing.get('prompt') if pricing.get('prompt') is not None else '',
            'output_price_source_raw': pricing.get('completion') if pricing.get('completion') is not None else '',
            'input_price_usd_per_million_tokens': token_price(endpoint, 'Input Price'),
            'output_price_usd_per_million_tokens': token_price(endpoint, 'Output Price'),
            'price_status': 'present' if pricing else 'not_available',
            'display_pricing_json': json.dumps(endpoint.get('display_pricing') or pricing.get('display_pricing') or [], separators=(',', ':')),
            'usage_status': usage_status,
            'analytics_key': analytics_key,
            'tokens_last_7_days': total,
            'prompt_tokens_last_7_days': text_number(usage.get('total_prompt_tokens')),
            'completion_tokens_last_7_days': text_number(usage.get('total_completion_tokens')),
            'reasoning_tokens_last_7_days': text_number(usage.get('total_native_tokens_reasoning')),
            'requests_last_7_days': text_number(usage.get('count')),
            'analytics_source_date': usage.get('date') or '',
            'usage_window_label': 'last 7 days' if usage else '',
            'source_url': 'https://openrouter.ai/' + variant_slug,
            'catalog_archive_url': provenance['catalog'].get('archive_url', ''),
            'catalog_capture_at_utc': provenance['catalog'].get('capture_at_utc', ''),
            'usage_archive_url': provenance.get('cards', {}).get('archive_url', ''),
            'usage_capture_at_utc': provenance.get('cards', {}).get('capture_at_utc', ''),
            'parser_version': VERSION,
        }
        rows.append(row)
    return rows


class ArchiveClient:
    def __init__(self, output, delay):
        self.output = output
        self.delay = delay
        self.last_request = 0
        # certifi is optional: useful on macOS Python installs lacking CA roots.
        try:
            import certifi
            self.context = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            self.context = ssl.create_default_context()

    def request(self, url):
        wait = self.delay - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait)
        self.last_request = time.monotonic()
        request = urllib.request.Request(url, headers={'User-Agent': 'OpenRouterHistoricalResearch/1.0', 'Accept': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=60, context=self.context) as response:
                final_url = response.url
                if urllib.parse.urlsplit(final_url).hostname != 'web.archive.org':
                    raise SourceError('Refusing a redirect outside web.archive.org.')
                raw = response.read(50 * 1024 * 1024 + 1)
                if len(raw) > 50 * 1024 * 1024:
                    raise SourceError('Response exceeded 50 MB limit.')
                if raw.startswith(b'\x1f\x8b'):
                    raw = gzip.decompress(raw)
                try:
                    payload = json.loads(raw)
                except (ValueError, UnicodeError) as error:
                    raise SourceError('Expected JSON; archive returned HTML or another response.') from error
                return payload, raw, final_url
        except urllib.error.HTTPError as error:
            if error.code in (403, 429):
                raise AccessLimited('HTTP %s: stopped globally. Retry-After: %s' % (error.code, error.headers.get('Retry-After', 'not provided'))) from error
            raise SourceError('HTTP %s for %s' % (error.code, url)) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise SourceError(str(error)) from error

    def discover(self, day, source):
        # Query archive metadata only; the model data still comes from the archived source.
        compact = day.replace('-', '')
        params = urllib.parse.urlencode({
            'url': SOURCES[source], 'matchType': 'exact', 'output': 'json',
            'from': compact, 'to': compact, 'filter': 'statuscode:200',
            'fl': 'timestamp,original',
        })
        url = 'https://web.archive.org/cdx/search/cdx?' + params
        cached = self.output / 'manifests' / (day + '-' + source + '.json')
        if cached.exists():
            index = json.loads(cached.read_text(encoding='utf-8'))
        else:
            index, _, _ = self.request(url)
            dump(cached, index)
        if not isinstance(index, list):
            raise SourceError('Unexpected CDX index shape.')
        candidates = []
        for entry in index[1:]:
            if len(entry) != 2 or not re.fullmatch(r'\d{14}', entry[0]):
                continue
            stamp, original = entry
            if timestamp_day(stamp) != day:
                continue
            if urllib.parse.urlsplit(original).hostname != 'openrouter.ai':
                continue
            seconds = int(stamp[8:10]) * 3600 + int(stamp[10:12]) * 60 + int(stamp[12:14])
            candidates.append((abs(seconds - 43200), stamp, original))
        return sorted(set(candidates))

    def collect_source(self, day, source, attempts):
        errors = []
        candidates = self.discover(day, source)
        for _, stamp, original in candidates[:attempts]:
            raw_url = 'https://web.archive.org/web/' + stamp + 'id_/' + original
            cached = self.output / 'raw' / day / (source + '-' + stamp + '.json')
            try:
                if cached.exists():
                    raw = cached.read_bytes()
                    payload, actual = json.loads(raw), raw_url
                else:
                    payload, raw, actual = self.request(raw_url)
                match = re.search(r'/web/(\d{14})(?:id_)?/', actual)
                if not match or timestamp_day(match[1]) != day:
                    raise SourceError('Wrong-day replay redirect; excluded from daily data.')
                validate(payload, source)
                if not cached.exists() and match[1] == stamp:
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    cached.write_bytes(raw)
                metadata = {
                    'archive_url': actual,
                    'capture_at_utc': datetime.strptime(match[1], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc).isoformat(),
                    'sha256': hashlib.sha256(raw).hexdigest(),
                    'candidate_count': len(candidates), 'failed_candidates': errors,
                    'selection_policy': 'first valid same-day response nearest noon UTC; not exhaustive best-coverage selection',
                }
                return payload, metadata
            except (SourceError, ValueError) as error:
                errors.append({'capture': stamp, 'error': str(error)})
        raise SourceError(json.dumps({'reason': 'No usable same-day capture among inspected candidates', 'candidate_count': len(candidates), 'attempts': errors}))


def save_result(output, day, catalog, cards, provenance):
    rows = build_rows(catalog, cards, day, provenance)
    report = {
        'snapshot_date': day, 'parser_version': VERSION,
        'result': 'catalog_and_usage_saved' if cards is not None else 'catalog_saved_usage_source_unavailable',
        'coverage_status': 'unverified', 'sources': provenance,
        'record_count': len(rows), 'unique_model_slugs': len({r['model_id'] for r in rows}),
        'unique_model_variants': len({(r['model_id'], r['variant']) for r in rows}),
        'active_records': sum(r['is_active'] == 'true' for r in rows),
        'not_in_active_response_records': sum(r['is_active'] == 'false' for r in rows),
        'unknown_active_status_records': sum(r['is_active'] == '' for r in rows),
        'priced_records': sum(r['price_status'] == 'present' for r in rows),
        'usage_matched_records': sum(r['usage_status'] == 'matched' for r in rows),
        'usage_ambiguous_records': sum(r['usage_status'] == 'ambiguous' for r in rows),
        'notes': ['NA is not zero.', 'Creation timestamp is not verified public release date.',
                  'Rolling seven-day totals are not daily usage.', 'Associated endpoint is not the full provider list.'],
        'collected_at_utc': datetime.now(timezone.utc).isoformat(),
    }
    destination = output / (day + '.csv')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.csv.tmp')
    with temporary.open('w', newline='', encoding='utf-8') as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows({key: 'NA' if value == '' or value is None else value for key, value in row.items()} for row in rows)
    temporary.replace(destination)
    dump(output / 'coverage' / (day + '-catalog.json'), report)
    print('%s: saved %s records / %s unique slugs; %s usage matches' % (
        day, len(rows), report['unique_model_slugs'], report['usage_matched_records']), flush=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', default='2026-07-01', help='First UTC date, YYYY-MM-DD')
    parser.add_argument('--end', help='Last date inclusive; defaults to start')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--delay', type=float, default=15, help='Minimum seconds between requests; at least 5')
    parser.add_argument('--candidates', type=int, default=3, help='Maximum same-day candidates per source')
    parser.add_argument('--catalog-file', type=Path, help='Offline fixture; requires cards file and exact date')
    parser.add_argument('--cards-file', type=Path)
    args = parser.parse_args(argv)
    try:
        start, end = date.fromisoformat(args.start), date.fromisoformat(args.end or args.start)
    except ValueError:
        parser.error('Dates must be valid YYYY-MM-DD values.')
    if end < start or args.delay < 5 or args.candidates < 1:
        parser.error('End must be on/after start; delay >= 5; candidates >= 1.')
    if bool(args.catalog_file) != bool(args.cards_file):
        parser.error('Use both --catalog-file and --cards-file for offline mode.')
    if args.catalog_file:
        if start != end:
            parser.error('Offline mode accepts one day only.')
        catalog = json.loads(args.catalog_file.read_text(encoding='utf-8'))
        cards = json.loads(args.cards_file.read_text(encoding='utf-8'))
        # Fixture mode never invents archive capture timestamps.
        provenance = {'catalog': {'fixture': str(args.catalog_file)}, 'cards': {'fixture': str(args.cards_file)}}
        save_result(args.output, start.isoformat(), catalog, cards, provenance)
        return 0
    client = ArchiveClient(args.output, args.delay)
    failures = 0
    day = start
    while day <= end:
        label = day.isoformat()
        report_path = args.output / 'coverage' / (label + '-catalog.json')
        csv_path = args.output / (label + '.csv')
        if report_path.exists() and csv_path.exists():
            prior = json.loads(report_path.read_text(encoding='utf-8'))
            if prior.get('parser_version') == VERSION and prior.get('result') == 'catalog_and_usage_saved' and not prior.get('sources', {}).get('catalog', {}).get('fixture'):
                print(label + ': already saved; skipping', flush=True)
                day += timedelta(days=1)
                continue
        print(label + ': locating archived catalog and card responses', flush=True)
        provenance = {}
        try:
            catalog, provenance['catalog'] = client.collect_source(label, 'catalog', args.candidates)
            cards = None
            try:
                cards, provenance['cards'] = client.collect_source(label, 'cards', args.candidates)
            except SourceError as error:
                provenance['cards'] = {'error': str(error)}
            if cards is None and csv_path.exists():
                raise SourceError('Incomplete rerun: preserving existing CSV.')
            save_result(args.output, label, catalog, cards, provenance)
            if cards is None:
                failures += 1
        except AccessLimited as error:
            dump(args.output / 'attempts' / (label + '-blocked.json'), {'date': label, 'error': str(error), 'sources': provenance})
            print('STOPPED: ' + str(error), file=sys.stderr)
            return 2
        except (SourceError, ValueError) as error:
            failures += 1
            dump(args.output / 'attempts' / (label + '-failed.json'), {'date': label, 'error': str(error), 'sources': provenance})
            print(label + ': unresolved; ' + str(error), file=sys.stderr, flush=True)
        day += timedelta(days=1)
    return 1 if failures else 0


if __name__ == '__main__':
    if '__file__' in globals():
        raise SystemExit(main())
    else:
        print('Collector loaded. Run main(["--start", "2026-07-01"]) in another cell.')
