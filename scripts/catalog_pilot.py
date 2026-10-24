import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

URL = 'https://web.archive.org/web/20261018092638/https://openrouter.ai/models'
DAY = '2026-10-18'


def original_url(href):
    match = re.search(r'https?://openrouter\.ai/[^\s]*', href or '')
    return match.group(0) if match else ''


def parse_cards(html, capture_url, collected_at):
    soup = BeautifulSoup(html, 'html.parser')
    records = {}
    for card in soup.select('li'):
        if not card.select_one('[aria-label="Pin model"]'):
            continue
        model_link = None
        creator = ''
        for link in card.select('a[href]'):
            original = original_url(link['href'])
            parts = urlsplit(original).path.strip('/').split('/')
            if len(parts) == 2 and model_link is None:
                model_link = (link, original, '/'.join(parts))
            elif len(parts) == 1 and link.get_text(strip=True):
                creator = link.get_text(strip=True)
        if model_link is None:
            continue
        link, source, model_id = model_link
        text = card.get_text(' ', strip=True)
        # Match standalone metadata spans, not dates in the description.
        release = next((s.get_text(' ', strip=True) for s in card.select('span')
                        if re.fullmatch(r'[A-Z][a-z]{2} \d{1,2}, \d{4}', s.get_text(' ', strip=True))), '')
        usage = re.search(r'\b([\d,.]+\s*[KMBT]?)\s+tokens\b', text)
        input_price = re.search(r'\$([\d,.]+)\s*/M\s*input', text)
        output_price = re.search(r'\$([\d,.]+)\s*/M\s*output', text)
        other = re.search(r'from\s+\$[\d,.]+\s*/\w+', text)
        raw_usage = usage.group(1).replace(' ', '') if usage else ''
        numeric = ''
        if raw_usage:
            m = re.fullmatch(r'([\d,.]+)([KMBT]?)', raw_usage)
            if m:
                from decimal import Decimal
                numeric = str(int(Decimal(m[1].replace(',', '')) *
                                  {'': 1, 'K': 1000, 'M': 10**6, 'B': 10**9, 'T': 10**12}[m[2]]))
        records[model_id] = {
            'snapshot_date': DAY, 'model_id': model_id,
            'model_name': link.get_text(' ', strip=True), 'creator_name': creator,
            'release_date_displayed': release,
            'input_price_per_million': input_price[1] if input_price else '',
            'output_price_per_million': output_price[1] if output_price else '',
            'currency': 'USD' if input_price or output_price or other else '',
            'other_price_displayed': other[0] if other else '',
            'tokens_last_7_days_displayed': raw_usage,
            'tokens_last_7_days_approx': numeric,
            'usage_window_label': 'last 7 days' if usage else '',
            'source_url': source, 'archive_url': capture_url,
            'collected_at': collected_at,
        }
    return list(records.values())


def main():
    from playwright.sync_api import sync_playwright

    evidence = Path('pilot-output')
    evidence.mkdir(exist_ok=True)
    report = {'requested_url': URL, 'snapshot_date': DAY,
              'coverage_status': 'unverified', 'observations': [], 'access_errors': []}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})

        def response_seen(response):
            if response.status in (403, 429):
                report['access_errors'].append({'url': response.url, 'status': response.status})

        page.on('response', response_seen)
        try:
            response = page.goto(URL, wait_until='domcontentloaded', timeout=90000)
            report['http_status'] = response.status if response else None
            if response is None or response.status >= 400:
                raise RuntimeError('Page failed; no retry will be attempted.')
            # Ten-second checks allow an access error to stop the pilot promptly.
            # First allow three minutes, then six spaced scroll/loading checks.
            for step in range(27):
                page.wait_for_timeout(10000)
                if report['access_errors']:
                    raise RuntimeError('Access error: stopping without retry.')
                if step >= 18 and step % 2 == 0:
                    page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                if step % 3 == 2:
                    rows = parse_cards(page.content(), page.url, '')
                    observation = {'elapsed_seconds': (step + 1) * 10, 'model_count': len(rows)}
                    report['observations'].append(observation)
                    print(observation, flush=True)
            report['final_url'] = page.url
            if not re.search(r'/web/20261018\d{6}/https://openrouter\.ai/models', page.url):
                raise RuntimeError('Wrong-day redirect: refusing to write dated CSV.')
            collected = datetime.now(timezone.utc).isoformat()
            rows = parse_cards(page.content(), page.url, collected)
            if not rows:
                raise RuntimeError('No model cards detected.')
            folder = Path('data/catalog')
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / f'{DAY}.csv').open('w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows({k: v if v != '' else 'NA' for k, v in row.items()} for row in rows)
            report['model_count'] = len(rows)
            report['result'] = 'csv_saved'
            report['note'] = 'Coverage is unverified. Stable counts do not establish a complete catalog.'
            print(f'Saved {len(rows)} models; full coverage remains unverified.', flush=True)
        except Exception as exc:
            report['result'] = 'failed'
            report['error'] = str(exc)
            raise
        finally:
            report['final_url'] = page.url
            report['collected_at'] = datetime.now(timezone.utc).isoformat()
            try:
                (evidence / 'catalog.html').write_text(page.content(), encoding='utf-8')
                (evidence / 'catalog-text.txt').write_text(page.locator('body').inner_text(timeout=5000), encoding='utf-8')
                page.screenshot(path=str(evidence / 'catalog-bottom.png'))
            except Exception:
                pass
            folder = Path('data/coverage')
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f'{DAY}-catalog.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
            browser.close()


if __name__ == '__main__':
    main()
