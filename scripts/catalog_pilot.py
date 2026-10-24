import csv
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

DAY = os.environ.get('SNAPSHOT_DATE', '2026-07-19')
# Request the capture nearest noon; reject redirects to another calendar day.
URL = os.environ.get('SNAPSHOT_URL',
    f"https://web.archive.org/web/{DAY.replace('-', '')}120000/https://openrouter.ai/models")


def original_url(href):
    match = re.search(r'https?://openrouter\.ai/[^\s]*', href or '')
    return match.group(0) if match else ''


def parse_cards(html, capture_url, collected_at):
    soup = BeautifulSoup(html, 'html.parser')
    records = {}
    cards = []
    seen_cards = set()
    for anchor in soup.select('a[href]'):
        parts = urlsplit(original_url(anchor['href'])).path.strip('/').split('/')
        if len(parts) != 2 or not anchor.get_text(strip=True):
            continue
        for parent in anchor.parents:
            if parent.name not in ('li', 'div', 'article'):
                continue
            text = parent.get_text(' ', strip=True)
            if not re.search(r'\bby\s+', text):
                continue
            creator_links = [a for a in parent.select('a[href]')
                             if len(urlsplit(original_url(a['href'])).path.strip('/').split('/')) == 1
                             and a.get_text(strip=True)]
            if not any(re.search(r'\bby\s+' + re.escape(a.get_text(' ', strip=True)), text) for a in creator_links):
                continue
            model_ids = set()
            for a in parent.select('a[href]'):
                path = urlsplit(original_url(a['href'])).path.strip('/').split('/')
                if len(path) == 2:
                    model_ids.add('/'.join(path))
            if len(model_ids) != 1:
                break
            if id(parent) not in seen_cards:
                cards.append(parent)
                seen_cards.add(id(parent))
            break
    for card in cards:
        model_link = None
        creator = ''
        for link in card.select('a[href]'):
            original = original_url(link['href'])
            parts = urlsplit(original).path.strip('/').split('/')
            if len(parts) == 2 and model_link is None and link.get_text(strip=True):
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
        input_price = re.search(r'\$\s*([\d,.]+)\s*/M\s*input', text)
        output_price = re.search(r'\$\s*([\d,.]+)\s*/M\s*output', text)
        other = re.search(r'(?:from\s+)?\$\s*[\d,.]+\s*/\s*(?:image|minute|M\s+characters)', text)
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

    evidence = Path('pilot-output') / DAY
    evidence.mkdir(parents=True, exist_ok=True)
    report = {'requested_url': URL, 'snapshot_date': DAY,
              'coverage_status': 'unverified', 'observations': [], 'access_errors': [],
              'failed_requests': [], 'http_errors': [], 'javascript_errors': [],
              'console_errors': [], 'review_threshold_models': 500,
              'capture_selection': 'one candidate nearest noon UTC; not compared with other captures'}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})

        def response_seen(response):
            if response.status >= 400:
                report['http_errors'].append({'url': response.url, 'status': response.status})
            if response.status in (403, 429):
                report['access_errors'].append({'url': response.url, 'status': response.status})

        page.on('response', response_seen)
        page.on('requestfailed', lambda request: report['failed_requests'].append(
            {'url': request.url, 'failure': request.failure}))
        page.on('pageerror', lambda error: report['javascript_errors'].append(str(error)))
        page.on('console', lambda message: report['console_errors'].append(message.text)
                if message.type == 'error' else None)
        try:
            response = page.goto(URL, wait_until='domcontentloaded', timeout=90000)
            report['http_status'] = response.status if response else None
            if response is None or response.status >= 400:
                raise RuntimeError('Page failed; no retry will be attempted.')
            # Ten-second checks allow an access error to stop the pilot promptly.
            # Accumulate virtualized cards before scrolling the inner list.
            accumulated = {}
            for step in range(36):
                page.wait_for_timeout(10000)
                if report['access_errors']:
                    raise RuntimeError('Access error: stopping without retry.')
                visible_rows = parse_cards(page.content(), page.url, datetime.now(timezone.utc).isoformat())
                for row in visible_rows:
                    old = accumulated.get(row['model_id'], {})
                    accumulated[row['model_id']] = {k: v if v != '' else old.get(k, '') for k, v in row.items()}
                if step % 3 == 2:
                    observation = {'elapsed_seconds': (step + 1) * 10,
                                   'model_count': len(accumulated),
                                   'currently_rendered': len(visible_rows)}
                    report['observations'].append(observation)
                    print(observation, flush=True)
                if step >= 5:
                    scroll = page.evaluate("""() => {
                        const lists = [...document.querySelectorAll('body *')].filter(e =>
                            e.clientHeight > 200 && e.scrollHeight > e.clientHeight + 100 &&
                            /auto|scroll/.test(getComputedStyle(e).overflowY));
                        const e = lists.sort((a,b) => b.scrollHeight-a.scrollHeight)[0]
                            || document.scrollingElement;
                        e.scrollTop += Math.max(300, e.clientHeight * 0.8);
                        return {top: e.scrollTop, height: e.scrollHeight, viewport: e.clientHeight,
                                at_bottom: e.scrollTop + e.clientHeight >= e.scrollHeight - 5};
                    }""")
                    report['last_scroll'] = scroll
            report['final_url'] = page.url
            if not re.search(rf"/web/{DAY.replace('-', '')}\d{{6}}/https://openrouter\.ai/models", page.url):
                raise RuntimeError('Wrong-day redirect: refusing to write dated CSV.')
            for row in parse_cards(page.content(), page.url, datetime.now(timezone.utc).isoformat()):
                accumulated.setdefault(row['model_id'], row)
            rows = list(accumulated.values())
            if not rows:
                raise RuntimeError('No model cards detected.')
            folder = Path('data/catalog')
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / f'{DAY}.csv').open('w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows({k: v if v != '' else 'NA' for k, v in row.items()} for row in rows)
            report['model_count'] = len(rows)
            report['coverage_status'] = 'unverified'
            report['below_review_threshold'] = len(rows) < 500
            if len(rows) < 500:
                print('::warning::Fewer than 500 models: review archive coverage.')
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
                report['visible_controls'] = page.locator('button, [role=button], select').evaluate_all(
                    "els => els.filter(e => e.getClientRects().length).map(e => ({text: e.innerText, label: e.getAttribute('aria-label'), disabled: e.disabled}))")
                report['scroll_containers'] = page.locator('body *').evaluate_all(
                    "els => els.filter(e => e.clientHeight > 200 && e.scrollHeight > e.clientHeight + 100 && /auto|scroll/.test(getComputedStyle(e).overflowY)).map(e => ({tag: e.tagName, id: e.id, class: e.className, height: e.clientHeight, scrollHeight: e.scrollHeight}))")
            except Exception as exc:
                report['diagnostic_error'] = str(exc)
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
