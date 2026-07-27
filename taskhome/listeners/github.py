"""GitHub activity (MASTER_PLAN P5-2 #8).

New issues, pull requests, releases and failed workflow runs for chosen repos.

**A token is optional.** Public repositories are readable unauthenticated, so
this works the moment you name a repo -- it just gets 60 requests an hour
instead of 5,000, and cannot see private repos. That matters more than it
sounds: a listener that demands a personal access token before it does
anything is a listener most people never switch on.

Conditional requests are the reason the unauthenticated tier is usable at all.
GitHub returns an `ETag` on every list endpoint, and **a 304 does not count
against the rate limit**. Polling four repos every fifteen minutes is 16
requests an hour if nothing changed, and all 16 are free.
"""
import re
from datetime import datetime

import requests

from . import base
from .. import layouts, receipt
from ..logsetup import log

API = 'https://api.github.com'
TIMEOUT = 20
USER_AGENT = 'TaskHome/2.0 (+https://github.com/mica-alex/TaskHome)'

EVENT_TYPES = ('issues', 'pulls', 'releases', 'failed_runs')

REPO_RE = re.compile(r'^[\w.-]+/[\w.-]+$')


def parse_repo(value):
    """'owner/name' from a slug or a URL. Returns None if it is neither."""
    value = (value or '').strip()
    value = re.sub(r'^https?://(?:www\.)?github\.com/', '', value)
    value = value.rstrip('/').removesuffix('.git')
    return value if REPO_RE.match(value) else None


class GitHubListener(base.Listener):
    name = 'github'
    title = 'GitHub'
    description = ('New issues, pull requests, releases and failed workflow '
                   'runs for repositories you follow.')
    default_interval = 15
    max_prints_per_poll = 10

    CONFIG_SCHEMA = (
        base.field('enabled', 'Enabled', 'bool', default=False),
        base.field('repos', 'Repositories', 'multiselect', default=[],
                   group='Repositories',
                   help='owner/name, or paste a GitHub URL.'),
        base.field('token', 'Personal access token', 'secret', default='',
                   group='Repositories',
                   help='Optional. Public repos work without one at 60 '
                        'requests an hour; a token raises that to 5,000 and '
                        'allows private repos. Needs only the "repo" scope, '
                        'and read access is enough.'),
        base.field('interval', 'Check every (minutes)', 'int', default=15,
                   min=5, max=1440, group='Repositories'),
        base.field('events', 'What to print', 'multiselect',
                   default=['releases', 'failed_runs'], options=EVENT_TYPES,
                   group='Events',
                   help='Issues and pull requests are noisy on a busy repo. '
                        'Releases and failed builds are the ones worth paper.'),
        base.field('ignore_bots', 'Ignore bots', 'bool', default=True,
                   group='Events',
                   help='Applies to issues and pull requests only -- Dependabot '
                        'alone can be a receipt a day. Releases and builds are '
                        'often published by a CI bot and are never filtered.'),
        base.field('max_per_poll', 'Maximum receipts per check', 'int', default=5,
                   min=1, max=20, group='Events'),
    )

    PLACEHOLDERS = {
        'kind': 'Release',
        'repo': 'python/cpython',
        'title': 'v3.15.0b4',
        'author': 'hugovk',
        'body': 'Python 3.15.0 beta 4',
        'url': 'https://github.com/python/cpython/releases/tag/v3.15.0b4',
        'printed': '8:30 AM 7/27/26',
    }

    # --- fetching ------------------------------------------------------------

    def _headers(self, config, etag=None):
        headers = {'User-Agent': USER_AGENT,
                   'Accept': 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28'}
        token = (config.get('token') or '').strip()
        if token:
            headers['Authorization'] = f'Bearer {token}'
        if etag:
            headers['If-None-Match'] = etag
        return headers

    def _get(self, config, path, etag=None, params=None):
        """Returns (payload, etag, unchanged). Raises on transport errors.

        A 304 is not an error and does not count against the rate limit, which
        is what makes the unauthenticated tier workable.
        """
        response = requests.get(f'{API}{path}', headers=self._headers(config, etag),
                                params=params or {}, timeout=TIMEOUT)
        if response.status_code == 304:
            return None, etag, True
        if response.status_code == 403 and 'rate limit' in response.text.lower():
            remaining = response.headers.get('X-RateLimit-Remaining')
            raise RuntimeError(
                f'GitHub rate limit reached (remaining={remaining}). '
                f'Adding a token raises the limit from 60/hour to 5,000.')
        if response.status_code == 404:
            raise RuntimeError('Not found -- check the name, or add a token if '
                               'the repository is private.')
        response.raise_for_status()
        return response.json(), response.headers.get('ETag'), False

    def poll(self, config, since):
        repos = [parse_repo(r) for r in (config.get('repos') or [])]
        repos = [r for r in repos if r]
        if not repos:
            return []

        wanted = config.get('events') or list(EVENT_TYPES)
        runtime = self.state()
        etags = runtime.get('etags') or {}
        failures = []
        items = []

        for repo in repos:
            for kind in wanted:
                key = f'{repo}:{kind}'
                try:
                    found, etags[key] = self._fetch_kind(config, repo, kind,
                                                         etags.get(key))
                    items.extend(found)
                except Exception as e:
                    log.warning(f'GitHub {key} failed: {e}')
                    failures.append(f'{repo} ({kind}): {e}')

        runtime['etags'] = etags
        runtime['last_failures'] = failures[:5]

        items.sort(key=lambda i: i.get('created_at') or '')
        cap = max(int(config.get('max_per_poll', 5) or 5), 1)
        if len(items) > cap:
            log.info(f'GitHub: capping {len(items)} items at {cap}')
            items = items[-cap:]
        return items

    def _fetch_kind(self, config, repo, kind, etag):
        if kind == 'releases':
            payload, new_etag, unchanged = self._get(
                config, f'/repos/{repo}/releases', etag, {'per_page': 10})
            if unchanged or not payload:
                return [], new_etag
            return [self._release_item(repo, r) for r in payload
                    if not r.get('draft')], new_etag

        if kind == 'failed_runs':
            payload, new_etag, unchanged = self._get(
                config, f'/repos/{repo}/actions/runs', etag,
                {'per_page': 10, 'status': 'completed'})
            if unchanged or not payload:
                return [], new_etag
            runs = [r for r in payload.get('workflow_runs', [])
                    if r.get('conclusion') in ('failure', 'timed_out')]
            return [self._run_item(repo, r) for r in runs], new_etag

        # Issues and pull requests share an endpoint; `pull_request` is what
        # distinguishes them, and asking for issues returns both.
        payload, new_etag, unchanged = self._get(
            config, f'/repos/{repo}/issues', etag,
            {'per_page': 15, 'state': 'open', 'sort': 'created'})
        if unchanged or not payload:
            return [], new_etag

        found = []
        for entry in payload:
            is_pull = 'pull_request' in entry
            if kind == 'pulls' and not is_pull:
                continue
            if kind == 'issues' and is_pull:
                continue
            found.append(self._issue_item(repo, entry, is_pull))
        return found, new_etag

    def _release_item(self, repo, data):
        return {'id': f"release:{repo}:{data.get('id')}", 'kind': 'Release',
                'repo': repo, 'title': data.get('name') or data.get('tag_name', ''),
                'author': (data.get('author') or {}).get('login', ''),
                'body': (data.get('body') or '')[:600],
                'url': data.get('html_url', ''),
                'created_at': data.get('published_at') or data.get('created_at', '')}

    def _run_item(self, repo, data):
        return {'id': f"run:{repo}:{data.get('id')}", 'kind': 'Build failed',
                'repo': repo, 'title': data.get('name') or 'Workflow',
                'author': (data.get('actor') or {}).get('login', ''),
                'body': f"{data.get('head_branch', '')} - "
                        f"{(data.get('head_commit') or {}).get('message', '')}"[:400],
                'url': data.get('html_url', ''),
                'created_at': data.get('updated_at') or data.get('created_at', '')}

    def _issue_item(self, repo, data, is_pull):
        user = data.get('user') or {}
        return {'id': f"{'pr' if is_pull else 'issue'}:{repo}:{data.get('number')}",
                'kind': 'Pull request' if is_pull else 'Issue',
                'repo': repo,
                'title': f"#{data.get('number')} {data.get('title', '')}",
                'author': user.get('login', ''),
                'author_type': user.get('type', ''),
                'body': (data.get('body') or '')[:600],
                'url': data.get('html_url', ''),
                'created_at': data.get('created_at', '')}

    # --- filtering -----------------------------------------------------------

    #: Bot filtering applies only to these. A release cut by github-actions[bot]
    #: or a build failed by a CI account is still the event you asked for --
    #: filtering those out means the listener silently prints nothing, which is
    #: exactly what happened against pallets/flask, whose every release is
    #: published by a bot.
    BOT_FILTERED_KINDS = ('Issue', 'Pull request')

    def should_print(self, config, item, now=None):
        if config.get('ignore_bots', True) and item.get('kind') in self.BOT_FILTERED_KINDS:
            author = (item.get('author') or '').lower()
            if item.get('author_type') == 'Bot' or author.endswith('[bot]'):
                return False, f"{item.get('author')} is a bot"
        return True, ''

    # --- receipt -------------------------------------------------------------

    def dedup_key(self, item):
        return item.get('id')

    def describe(self, item):
        return f"{item.get('kind')} {item.get('repo')}: {item.get('title', '')[:40]}"

    def sort_key(self, item):
        return item.get('created_at') or ''

    def context(self, item):
        return {
            'kind': item.get('kind', ''),
            'repo': item.get('repo', ''),
            'title': item.get('title', ''),
            'author': item.get('author', ''),
            'body': item.get('body', ''),
            'url': item.get('url', ''),
            'printed': layouts._stamp(),
        }

    def blocks_from_context(self, context, qr=True):
        blocks = []
        if qr and context.get('url'):
            blocks.append(receipt.qr(context['url'], size=4))
        blocks.append(receipt.text(context['title'], font='a', width=2, height=2,
                                   bold=True))
        blocks.append(receipt.gap(6))
        blocks.append(receipt.text(f"{context['kind']}  -  {context['repo']}",
                                   font='b', bold=True))
        if context.get('author'):
            blocks.append(receipt.text(f"by {context['author']}", font='b'))
        if context.get('body'):
            blocks.append(receipt.rule())
            blocks.append(receipt.text(context['body'], font='b', align='left'))
        blocks.append(receipt.rule())
        blocks.append(receipt.text(f"Printed {context['printed']}", font='b'))
        return blocks

    def receipt_blocks(self, item):
        return self.blocks_from_context(self.context(item))

    def template_presets(self):
        markers = {key: '{%s}' % key for key in self.PLACEHOLDERS}
        return [
            (f'{self.name}-default', self.blocks_from_context(markers, qr=True)),
            (f'{self.name}-compact', self.blocks_from_context(markers, qr=False)),
        ]

    def history_record(self, item):
        return {
            'type': 'github',
            'id': item.get('id'),
            'category': f"{item.get('kind')} - {item.get('repo')}",
            'title': item.get('title', ''),
            'description': item.get('body', '')[:500],
            'url': item.get('url', ''),
            'reported_at': item.get('created_at', ''),
            'print_time': datetime.now().isoformat(),
        }

    def notice(self):
        config = self.config()
        if config.get('token'):
            return None
        return {
            'title': 'Working without a token',
            'body': 'Public repositories work as-is at 60 requests an hour. '
                    'Conditional requests mean an unchanged repo costs nothing '
                    'against that, so a handful of repos is comfortable. Add a '
                    'token for private repos or a higher limit.',
        }

    def summary(self):
        config = self.config()
        repos = [parse_repo(r) for r in (config.get('repos') or [])]
        repos = [r for r in repos if r]
        if not repos:
            return 'No repositories yet -- nothing will print.'
        failures = self.state().get('last_failures') or []
        note = f' ({len(failures)} failing)' if failures else ''
        token = '' if config.get('token') else ', no token'
        return f"{len(repos)} repo(s){token}{note}"


listener = base.register(GitHubListener())
