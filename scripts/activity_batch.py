"""Sequential, resumable active-model activity collector for GitHub Actions."""
import argparse
import csv
import hashlib
import json
import time
from datetime import date
from pathlib import Path

import catalog_support as support
from model_details import collect_activity, pack

VERSION = 'activity-v1-20261024'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', default='2026-07-01')
    parser.add_argument('--limit', type=int, default=3)
    parser.add_argument('--minutes', type=int, default=220)
    args = parser.parse_args(argv)
    date.fromisoformat(args.date)
    if not 1 <= args.limit <= 500 or not 1 <= args.minutes <= 240:
        parser.error('limit must be 1–500 and minutes 1–240')
    root = Path('data/model_details')
    catalog = Path('data/model_catalog') / (args.date + '.csv')
    with catalog.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        required = {'snapshot_date', 'model_id', 'model_permaslug', 'variant', 'is_active'}
        if not required <= set(reader.fieldnames or []):
            raise ValueError('Catalog is missing required columns: ' + str(required))
        models = {}
        for row in reader:
            if row['snapshot_date'] == args.date and row['is_active'].strip().lower() == 'true':
                key = (row['model_id'], row['model_permaslug'], row['variant'])
                models.setdefault(key, row)
    if not models:
        raise ValueError('No active models found for the requested date.')
    root.mkdir(parents=True, exist_ok=True)
    destination = root / (args.date + '.csv')
    rows = {}
    if destination.exists():
        with destination.open(encoding='utf-8', newline='') as handle:
            for row in csv.DictReader(handle):
                key = (row['model_id'], row['model_permaslug'], row['variant'])
                if key in models and row.get('collector_version') == VERSION:
                    rows[key] = row
    started = time.monotonic()
    completed = 0
    client = support.ArchiveClient(root / 'sources', 15)
    print('Active catalog identities:', len(models), '| Previously checked:', len(rows), flush=True)
    for key, model in models.items():
        if key in rows:
            continue
        if completed >= args.limit or time.monotonic() - started >= args.minutes * 60:
            break
        model_id, permaslug, variant = key
        identity = hashlib.sha256(json.dumps(key).encode()).hexdigest()[:24]
        # Shared client maintains request spacing across all models.
        client.output = root / 'sources' / identity
        print('Inspecting:', model_id, variant, flush=True)
        records = None
        try:
            if not permaslug or permaslug == 'NA':
                metadata = {'status': 'missing_permaslug'}
            else:
                records, metadata = collect_activity(client, args.date, permaslug, variant)
        except support.AccessLimited as error:
            support.dump(root / 'coverage' / (args.date + '-access-error.json'), {'error': str(error)})
            print(error, flush=True)
            return 2
        except (support.SourceError, ValueError, OSError) as error:
            support.dump(root / 'coverage' / args.date / (identity + '-error.json'), {'error': str(error), 'model_id': model_id, 'variant': variant})
            print('Failed:', error, flush=True)
            return 1
        row = {
            'snapshot_date': args.date, 'model_id': model_id,
            'model_name': model.get('model_name', 'NA'),
            'model_permaslug': permaslug, 'variant': variant,
            'collector_version': VERSION,
            'providers': 'NA', 'providers_status': 'not_implemented',
            'public_apps': 'NA', 'public_apps_status': 'not_implemented',
            'daily_activity': pack(records) if records else 'NA',
            'activity_day_count': len({r[0][:10] for r in records}) if records is not None else 'NA',
            'activity_status': metadata['status'],
            'activity_source_url': metadata.get('archive_url', 'NA'),
            'activity_capture_timestamp_utc': metadata.get('capture_timestamp_utc', 'NA'),
            'same_day_activity_may_be_partial': any(r[0][:10] == args.date for r in records) if records is not None else 'NA',
        }
        support.dump(root / 'coverage' / args.date / (identity + '.json'), metadata)
        # Transient failures remain eligible on the next run.
        if metadata['status'] == 'failed':
            print('All inspected captures failed; stopping for diagnosis.', flush=True)
            return 1
        rows[key] = row
        temporary = destination.with_suffix('.csv.tmp')
        with temporary.open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row))
            writer.writeheader()
            writer.writerows(rows.values())
        temporary.replace(destination)
        completed += 1
        print('Status:', metadata['status'], '| days:', row['activity_day_count'], '| checked:', len(rows), '/', len(models), flush=True)
    print('Batch finished. Rerun to continue. CSV:', destination, flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
