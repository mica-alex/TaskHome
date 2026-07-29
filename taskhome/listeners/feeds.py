"""RSS / Atom digest (MASTER_PLAN P5-2 #5).

Top items from chosen feeds, printed as **one digest receipt** rather than one
receipt per article. That is the whole design decision: a news feed with forty
items a day would otherwise bury the room in paper, and forty separate receipts
are harder to read than one list.

It also covers subreddits, YouTube channels, GitHub releases and podcast feeds,
all of which publish Atom or RSS.

Parsed with the standard library rather than feedparser. The subset that
matters here -- title, link, date, per entry -- is a dozen lines of ElementTree,
and it avoids a dependency on an appliance that has to keep working untouched
for years.
"""
import html
import re
from datetime import datetime, timezone
from xml.etree import ElementTree

import requests

from . import base
from .. import layouts, receipt
from ..logsetup import log

USER_AGENT = 'TaskHome/2.0 (+https://github.com/mica-alex/TaskHome)'
MAX_BYTES = 4 * 1024 * 1024
TIMEOUT = 20

#: Namespaces that show up in real feeds.
NS = {
    'atom': 'http://www.w3.org/2005/Atom',
    'dc': 'http://purl.org/dc/elements/1.1/',
}


def _text(element):
    return (element.text or '').strip() if element is not None else ''


def _strip_html(value):
    """Feed titles routinely contain entities and the odd inline tag."""
    return html.unescape(re.sub(r'<[^>]+>', '', value or '')).strip()


def parse_feed(content):
    """Bytes -> (feed_title, [entry, ...]). Handles both RSS and Atom.

    Returns entries in feed order, which is newest-first for essentially every
    real feed. Malformed XML raises; the caller turns that into backoff.
    """
    root = ElementTree.fromstring(content)

    # RSS: <rss><channel><item>. Atom: <feed><entry>.
    channel = root.find('channel')
    if channel is not None:
        feed_title = _strip_html(_text(channel.find('title')))
        nodes = channel.findall('item')
        entries = [{
            'title': _strip_html(_text(node.find('title'))),
            'link': _text(node.find('link')),
            'id': _text(node.find('guid')) or _text(node.find('link')),
            'published': _text(node.find('pubDate')) or _text(node.find('dc:date', NS)),
            'summary': _strip_html(_text(node.find('description')))[:400],
        } for node in nodes]
    else:
        feed_title = _strip_html(_text(root.find('atom:title', NS)))
        nodes = root.findall('atom:entry', NS)
        entries = []
        for node in nodes:
            link = ''
            for candidate in node.findall('atom:link', NS):
                if candidate.get('rel', 'alternate') == 'alternate':
                    link = candidate.get('href', '')
                    break
            entries.append({
                'title': _strip_html(_text(node.find('atom:title', NS))),
                'link': link,
                'id': _text(node.find('atom:id', NS)) or link,
                'published': (_text(node.find('atom:published', NS))
                              or _text(node.find('atom:updated', NS))),
                'summary': _strip_html(_text(node.find('atom:summary', NS)))[:400],
            })

    return feed_title, [e for e in entries if e['title'] or e['link']]


def fetch_feed(url, etag=None, modified=None):
    """Fetch one feed. Returns (entries, feed_title, validators).

    Conditional requests are sent when the server previously gave an ETag or
    Last-Modified: a feed polled every fifteen minutes is almost always
    unchanged, and a 304 costs a fraction of the bandwidth. Feed publishers
    care about this, and some rate-limit clients that ignore it.
    """
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/rss+xml, application/atom+xml, application/xml, text/xml'}
    if etag:
        headers['If-None-Match'] = etag
    if modified:
        headers['If-Modified-Since'] = modified

    response = requests.get(url, headers=headers, timeout=TIMEOUT, stream=True)
    if response.status_code == 304:
        return [], '', {'etag': etag, 'modified': modified}
    response.raise_for_status()

    chunks, total = [], 0
    for chunk in response.iter_content(8192):
        total += len(chunk)
        if total > MAX_BYTES:
            raise ValueError(f'Feed exceeds {MAX_BYTES} bytes: {url}')
        chunks.append(chunk)

    feed_title, entries = parse_feed(b''.join(chunks))
    return entries, feed_title, {
        'etag': response.headers.get('ETag'),
        'modified': response.headers.get('Last-Modified'),
    }


class FeedListener(base.Listener):
    name = 'feeds'
    title = 'News digest'
    description = ('One receipt with the latest from your RSS and Atom feeds. '
                   'Also works for subreddits, YouTube channels and releases.')
    default_interval = 60
    max_prints_per_poll = 1          # a digest is one receipt by definition

    CONFIG_SCHEMA = (
        base.field('enabled', 'Enabled', 'bool', default=False),
        base.field('urls', 'Feed URLs', 'multiselect', default=[], group='Feeds',
                   help='RSS or Atom. A subreddit works as '
                        'reddit.com/r/name/.rss; a YouTube channel publishes one too.'),
        base.field('interval', 'Check every (minutes)', 'int', default=60,
                   min=15, max=1440, group='Feeds',
                   help="Fifteen minutes is the floor: polling someone else's "
                        "server harder than that is rude, and news is not urgent."),
        base.field('max_items', 'Items per digest', 'int', default=10, min=1, max=40,
                   group='Digest',
                   help='Across all feeds. The rest are marked seen so they do '
                        'not pile up for tomorrow.'),
        base.field('max_per_feed', 'Maximum from one feed', 'int', default=4,
                   min=1, max=20, group='Digest',
                   help='Stops a busy feed crowding out the quiet ones.'),
        base.field('include_summary', 'Include summaries', 'bool', default=False,
                   group='Digest',
                   help='Roughly triples the paper. Titles alone are usually '
                        'enough to decide what to look up.'),
        base.field('quiet_hours', 'Quiet hours', 'time_range',
                   default={'start': '22:00', 'end': '07:00'}, group='Digest',
                   help='A digest that arrives at 3am is just noise on the floor '
                        'in the morning. Items are held, not dropped.'),
    )

    PLACEHOLDERS = {
        'count': '7',
        'feeds': '3',
        'items': '1. Something happened today\n   - The Guardian\n'
                 '2. A release you follow shipped 4.2\n   - GitHub',
        'printed': '8:30 AM 7/27/26',
    }

    #: Rows for a `list` block, which is how the digest gets one QR per entry:
    #: the count varies per digest, so it cannot be a fixed run of blocks.
    #: These samples are what the Studio previews against.
    LIST_PLACEHOLDERS = {
        'entries': [
            {'item_index': '1', 'item_title': 'Something happened today',
             'item_source': 'The Guardian',
             'item_link': 'https://www.theguardian.com/world/2026/jul/27/something'},
            {'item_index': '2', 'item_title': 'A release you follow shipped 4.2',
             'item_source': 'GitHub',
             'item_link': 'https://github.com/python/cpython/releases/tag/v4.2'},
        ],
    }

    # --- polling -------------------------------------------------------------

    def poll(self, config, since):
        """One digest item, or nothing.

        Returns at most a single item because the receipt IS the digest -- the
        runtime's dedup, cap and queueing then apply to the digest as a whole,
        which is what makes "printed" mean "the whole digest reached paper".
        """
        urls = [u for u in (config.get('urls') or []) if u.strip()]
        if not urls:
            return []

        runtime = self.state()
        validators = runtime.get('validators') or {}
        seen_links = set(runtime.get('seen_links') or [])
        known_feeds = list(runtime.get('known_feeds') or [])

        per_feed = max(int(config.get('max_per_feed', 4) or 4), 1)
        collected, failures = [], []

        for url in urls:
            cached = validators.get(url) or {}
            try:
                entries, feed_title, fresh_validators = fetch_feed(
                    url, cached.get('etag'), cached.get('modified'))
            except Exception as e:
                # One dead feed must not stop the digest; the others are still
                # worth printing, and a permanent failure would otherwise mean
                # never seeing news again.
                log.warning(f'Feed failed ({url}): {e}')
                failures.append(url)
                continue

            validators[url] = fresh_validators

            if url not in known_feeds:
                # First sight of this feed. Everything in it is "new", and a
                # busy feed carries 30-40 items -- printed max_per_feed at a
                # time that is a week of digests catching up on old news.
                # Mark the backlog seen and start from the next thing
                # published, which is the same call SCF's catch-up policy
                # makes for the same reason.
                for entry in entries:
                    key = entry.get('id') or entry.get('link')
                    if key:
                        seen_links.add(key)
                known_feeds.append(url)
                log.info(f'First poll of {url}: {len(entries)} existing item(s) '
                         f'marked seen rather than printed')
                continue

            taken = 0
            for entry in entries:
                key = entry.get('id') or entry.get('link')
                if not key or key in seen_links:
                    continue
                entry['feed'] = feed_title or _domain(url)
                collected.append(entry)
                seen_links.add(key)
                taken += 1
                if taken >= per_feed:
                    break

        runtime['validators'] = validators
        runtime['known_feeds'] = known_feeds
        # Bounded, or a year of headlines lives in listeners.json forever.
        runtime['seen_links'] = list(seen_links)[-3000:]
        runtime['last_failures'] = failures

        if not collected:
            return []

        limit = max(int(config.get('max_items', 10) or 10), 1)
        chosen, dropped = collected[:limit], collected[limit:]
        if dropped:
            log.info(f'News digest: {len(dropped)} item(s) over the limit')

        return [{
            'id': f"digest-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M')}",
            'entries': chosen,
            'feeds': len({e['feed'] for e in chosen}),
        }]

    def should_print(self, config, item, now=None):
        if self.in_quiet_hours(config, now):
            # Held rather than dropped: the runtime only marks an item seen
            # when it prints, so the next poll outside quiet hours rebuilds a
            # digest containing these entries.
            return False, 'quiet hours'
        return True, ''

    def in_quiet_hours(self, config, now=None):
        window = config.get('quiet_hours') or {}
        start, end = window.get('start'), window.get('end')
        if not start or not end:
            return False
        now = now or datetime.now()
        try:
            start_h, start_m = (int(x) for x in start.split(':'))
            end_h, end_m = (int(x) for x in end.split(':'))
        except (ValueError, AttributeError):
            return False
        minutes = now.hour * 60 + now.minute
        start_min, end_min = start_h * 60 + start_m, end_h * 60 + end_m
        if start_min <= end_min:
            return start_min <= minutes < end_min
        return minutes >= start_min or minutes < end_min

    # --- receipt -------------------------------------------------------------

    def dedup_key(self, item):
        return item.get('id')

    def describe(self, item):
        return f"News digest ({len(item.get('entries', []))} items)"

    def context(self, item):
        entries = item.get('entries', [])
        lines = []
        for index, entry in enumerate(entries, start=1):
            lines.append(f"{index}. {entry['title']}")
            source = entry.get('feed') or _domain(entry.get('link', ''))
            if source:
                # A visible prefix rather than indentation: wrap() strips
                # leading whitespace (it has to, to word-wrap sensibly), so
                # spaces would silently vanish and the source would read as a
                # second headline.
                lines.append(f'   - {source}')
        return {
            'count': str(len(entries)),
            'feeds': str(item.get('feeds', 0)),
            'items': '\n'.join(lines),
            'entries': self.rows(entries),
            'printed': layouts._stamp(),
        }

    def rows(self, entries):
        """The digest as list-block rows -- one per entry, with its link."""
        rows = []
        for index, entry in enumerate(entries, start=1):
            rows.append({
                'item_index': str(index),
                'item_title': entry.get('title', ''),
                'item_source': entry.get('feed') or _domain(entry.get('link', '')),
                'item_link': entry.get('link', ''),
            })
        return rows

    def blocks_from_context(self, context, qr=True):
        """The digest layout. With `qr`, each headline carries its own QR.

        A digest is the one receipt whose length is not known when the template
        is written, so the entries are a `list` block rather than a run of text
        blocks -- see styles._expand_list. The block keeps its `{item_*}`
        markers even here, because the rows supply those at fill time whether
        this is a Studio preset or the printed fallback.
        """
        blocks = [
            receipt.text('News digest', font='a', width=2, height=2, bold=True),
            receipt.gap(6),
            receipt.text(f"{context['count']} items from {context['feeds']} feed(s)",
                         font='b'),
            receipt.rule(),
        ]
        if qr:
            blocks.append({'type': 'list', 'source': 'entries',
                           'value': '{item_index}. {item_title}\n   - {item_source}',
                           'qr_value': '{item_link}',
                           'font': 'b', 'width': 1, 'height': 1, 'bold': False,
                           'align': 'left', 'size': 3, 'gap': 8})
        else:
            blocks.append(receipt.text(context['items'], font='b', align='left'))
        blocks.append(receipt.rule())
        blocks.append(receipt.text(f"Printed {context['printed']}", font='b'))
        return blocks

    def receipt_blocks(self, item):
        """Fallback layout when no template is configured.

        Filled rather than returned raw: the layout holds a `list` block, and
        expanding it is styles.fill's job. Going through the same expander is
        what stops the fallback and a Studio template disagreeing about a
        digest with, say, no entries at all.
        """
        from .. import styles
        context = self.context(item)
        return styles.fill({'blocks': self.blocks_from_context(context)}, context)

    def template_presets(self):
        """Links first, because a headline you cannot open is a dead end.

        `feeds-plain` is the older layout, for anyone who would rather have a
        short receipt than a scannable one -- ten QR codes is roughly a hand of
        extra paper.
        """
        markers = {key: '{%s}' % key for key in self.PLACEHOLDERS}
        return [
            (f'{self.name}-default', self.blocks_from_context(markers, qr=True)),
            (f'{self.name}-plain', self.blocks_from_context(markers, qr=False)),
        ]

    def history_record(self, item):
        entries = item.get('entries', [])
        return {
            'type': 'feeds',
            'id': item.get('id'),
            'category': 'News digest',
            'title': f'News digest ({len(entries)} items)',
            'description': '; '.join(e['title'] for e in entries)[:500],
            # Carried so a reprint rebuilds the digest that actually printed.
            # Without them the reprint path falls back to the Studio's sample
            # placeholders and puts invented headlines on paper.
            'items': self.context(item)['items'],
            'entries': self.rows(entries),
            'print_time': datetime.now().isoformat(),
        }

    def summary(self):
        config = self.config()
        urls = config.get('urls') or []
        if not urls:
            return 'No feeds yet -- nothing will print.'
        failures = self.state().get('last_failures') or []
        note = f' ({len(failures)} failing)' if failures else ''
        return f"{len(urls)} feed(s){note}"


def _domain(url):
    match = re.match(r'https?://(?:www\.)?([^/]+)', url or '')
    return match.group(1) if match else ''


listener = base.register(FeedListener())
