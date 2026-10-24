"""July 1 Wayback activity pilot; Python 3.9+. No live OpenRouter requests."""
import argparse
import csv
import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import catalog_support as support

DEFAULT_OUTPUT = Path('/Users/bachka/Library/CloudStorage/ProtonDrive-mnbatsaikhan@proton.me-folder/Work Docs/East West Center/AMA articles/OWOM/data/openrouter/model_details')


def escape(value):
    # Percent encoding makes delimiters and literal NA reversible.
    if value is None:
        return 'NA'
    return str(value).replace('%', '%25').replace('|', '%7C').replace(';', '%3B').replace('\r', '%0D').replace('\n', '%0A').replace('NA', '%4E%41')


def pack(records):
    return ' | '.join(';'.join(escape(v) for v in row) for row in records)


def activity_records(payload, permaslug, variant, snapshot_day):
    data = payload.get('data') if isinstance(payload, dict) else None
    records = data.get('analytics') if isinstance(data, dict) else None
    if not isinstance(records, list):
        raise support.SourceError('Unrecognized activity schema: expected data.analytics list.')
    result = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get('date'), str):
            raise support.SourceError('Activity record lacks a source date.')
        day = date.fromisoformat(record['date'][:10])
        if day > date.fromisoformat(snapshot_day):
            raise support.SourceError('Activity contains a future usage date.')
        if record.get('model_permaslug', permaslug) != permaslug or record.get('variant', variant) != variant:
            raise support.SourceError('Activity identity does not match requested model/variant.')
        values = [record.get(k) for k in ('total_prompt_tokens', 'total_completion_tokens', 'total_native_tokens_reasoning')]
        if any(v is not None and support.valid_number(v) is None for v in values):
            raise support.SourceError('Unexpected token value.')
        # Preserve raw timestamp and raw completion; do not infer UI timezone or subtract reasoning.
        result.append([record['date'], *values])
    return result


def collect_activity(client, day, permaslug, variant):
    original = 'https://openrouter.ai/api/frontend/v1/stats/model-activity?' + urlencode({'permaslug': permaslug, 'variant': variant})
    # Reuse the exact-day CDX lookup, cache, pacing, and access-limit handling.
    support.SOURCES['activity'] = original
    candidates = client.discover(day, 'activity')
    failures = []
    for _, stamp, url in candidates[:3]:
        archive_url = 'https://web.archive.org/web/' + stamp + 'id_/' + url
        try:
            payload, raw, final_url = client.request(archive_url)
            actual = re.search(r'/web/(\d{14})(?:id_)?/', final_url)
            if not actual or support.timestamp_day(actual[1]) != day:
                raise support.SourceError('Excluded wrong-day archive redirect.')
            records = activity_records(payload, permaslug, variant, day)
            raw_file = client.output / 'raw' / day / ('activity-' + actual[1] + '.json')
            raw_file.parent.mkdir(parents=True, exist_ok=True)
            raw_file.write_bytes(raw)
            return records, {'status': 'available' if records else 'empty', 'archive_url': final_url, 'capture_timestamp_utc': actual[1], 'failures': failures}
        except (support.SourceError, ValueError) as error:
            failures.append(str(error))
    return None, {'status': 'no_same_day_capture' if not candidates else 'failed', 'candidate_count': len(candidates), 'failures': failures}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', default='2026-07-01')
    parser.add_argument('--catalog', type=Path, required=True, help='Existing daily catalog CSV; never filtered to active models.')
    parser.add_argument('--model', required=True, help='Exact model_id from the catalog. Start with one model.')
    parser.add_argument('--variant', default='standard')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    date.fromisoformat(args.date)
    with args.catalog.open(encoding='utf-8-sig', newline='') as handle:
        matches = [r for r in csv.DictReader(handle) if r['model_id'] == args.model and r['variant'] == args.variant and r['snapshot_date'] == args.date]
    if len(matches) != 1:
        parser.error('Expected exactly one matching date/model/variant row; found %s.' % len(matches))
    model = matches[0]
    permaslug = model['model_permaslug']
    if not permaslug or permaslug == 'NA':
        parser.error('Catalog row has no permaslug; cannot safely identify the activity request.')
    # Separate cache per identity prevents CDX manifests from crossing models.
    import hashlib
    identity = hashlib.sha256((permaslug + ':' + args.variant).encode()).hexdigest()[:20]
    client = support.ArchiveClient(args.output / 'sources' / identity, 15)
    try:
        records, metadata = collect_activity(client, args.date, permaslug, args.variant)
    except support.AccessLimited as error:
        support.dump(client.output / (args.date + '-access-error.json'), {'error': str(error)})
        print(error)
        return 2
    row = {'snapshot_date': args.date, 'model_id': args.model, 'model_permaslug': permaslug, 'variant': args.variant,
           'providers': 'NA', 'providers_status': 'not_implemented', 'public_apps': 'NA', 'public_apps_status': 'not_implemented',
           'daily_activity': pack(records) if records else 'NA',
           'activity_day_count': len({r[0][:10] for r in records}) if records is not None else 'NA',
           'activity_status': metadata['status'], 'activity_source_url': metadata.get('archive_url', 'NA'),
           'activity_capture_timestamp_utc': metadata.get('capture_timestamp_utc', 'NA'),
           'same_day_activity_may_be_partial': any(r[0][:10] == args.date for r in records) if records else 'NA'}
    destination = args.output / 'samples' / (args.date + '-' + identity + '.csv')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.csv.tmp')
    with temporary.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    temporary.replace(destination)
    support.dump(destination.with_suffix('.json'), metadata)
    print('Saved:', destination)
    print('Activity status:', metadata['status'], '| days:', row['activity_day_count'])
    return 0 if records is not None else 1


if __name__ == '__main__':
    raise SystemExit(main())
