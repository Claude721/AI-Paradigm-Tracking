"""Registered, unauthenticated Substack archive fallback; never unlock content."""
from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlencode, urlparse

from paradigms.models import EvidenceType, technical_evidence_from_dict
from runtime_clock import research_now

VERSION = 1
PAGE_SIZE = 12
PAGE_LIMIT = 32


class _Scripts(HTMLParser):
    def __init__(self): super().__init__(); self.capture = False; self.parts = []; self.blocks = []; self.author_names = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta' and attrs.get('name', '').lower() == 'author':
            self.author_names.append(attrs.get('content', '').strip())
        if tag == 'script': self.capture = not attrs.get('src'); self.parts = []
    def handle_data(self, data):
        if self.capture: self.parts.append(data)
    def handle_endtag(self, tag):
        if tag == 'script' and self.capture: self.blocks.append(''.join(self.parts)); self.capture = False


def public_payload(text, *, root, publication_id):
    parser = _Scripts(); parser.feed(text)
    matches = [match for block in parser.blocks if (match := re.fullmatch(
        r'\s*window\._preloads\s*=\s*JSON\.parse\(("(?:[^"\\]|\\.)*")\)\s*;?\s*', block))]
    if len(matches) != 1: raise ValueError('Substack public data missing or ambiguous')
    payload = json.loads(json.loads(matches[0][1]))
    if (not isinstance(payload, dict) or not isinstance(payload.get('pub'), dict)
            or payload['pub'].get('id') != publication_id or payload.get('base_url') != root
            or (payload.get('publicationSettings') or {}).get('block_ai_crawlers') is not False):
        raise ValueError('Substack publication identity/public access changed')
    return payload


def document_author(text, aliases):
    parser = _Scripts(); parser.feed(text)
    names = set(parser.author_names)
    return next(iter(names)) if len(names) == 1 and names <= set(aliases) else ''


def post_record(post, *, root, publication_id, aliases, allow_missing_byline=False, author_hint=''):
    if not isinstance(post, dict): raise ValueError('Invalid Substack publication record')
    url = post.get('canonical_url'); parsed = urlparse(url if isinstance(url, str) else '')
    if (type(post.get('id')) is not int or post['id'] <= 0 or post.get('publication_id') != publication_id
            or parsed.scheme != 'https' or parsed.netloc != urlparse(root).netloc
            or not re.fullmatch(r'/p/[a-zA-Z0-9-]+', parsed.path) or parsed.query or parsed.fragment
            or not isinstance(post.get('title'), str) or not post['title'].strip()
            or not isinstance(post.get('post_date'), str)):
        raise ValueError('Substack post identity/canonical URL changed')
    date = datetime.fromisoformat(post['post_date'].replace('Z', '+00:00'))
    if date.tzinfo is None: raise ValueError('Substack post publication date is not exact')
    date = date.astimezone(timezone.utc)
    bylines = post.get('publishedBylines')
    if (not isinstance(bylines, list)
            or any(not isinstance(item, dict) or not isinstance(item.get('name'), str) for item in bylines)):
        raise ValueError('Substack registered author is not in the public byline')
    author = next((item['name'] for item in bylines if item['name'] in aliases), '')
    if not bylines and author_hint in aliases: author = author_hint
    if not author and not (not bylines and allow_missing_byline):
        raise ValueError('Substack registered author is not in the public byline')
    if post.get('audience') not in {'everyone', 'only_paid', 'only_free', 'founding'}:
        raise ValueError('Substack post audience is unknown')
    preview = post.get('truncated_body_text') or post.get('description') or ''
    if not isinstance(preview, str): raise ValueError('Substack public preview is not text')
    return {'id': post['id'], 'url': url, 'title': post['title'], 'published_at': date.isoformat(),
        'audience': post['audience'], 'author': author,
        'preview': preview}


async def fetch_archive(get, *, record, feed_url, lookback_days, stage_cache=None, sleeper=None, detail_limit=None):
    if detail_limit is not None and (type(detail_limit) is not int or detail_limit < 0):
        raise ValueError('Invalid public-post sample limit')
    registered = record.get('substack_public_archive') or {}
    root = str(record.get('homepage', '')).rstrip('/')
    publication_id = registered.get('publication_id')
    archive = registered.get('url')
    if (record.get('origin_policy') != 'secondary_only' or type(publication_id) is not int
            or archive != root + '/archive' or feed_url != root + '/feed'):
        raise ValueError('Unregistered Substack fallback')
    aliases = tuple(record.get('aliases', ())) + (str(record.get('name', '')),)
    signature = hashlib.sha256(json.dumps([VERSION, feed_url, publication_id, aliases, lookback_days,
        research_now().isoformat()], ensure_ascii=False).encode()).hexdigest()
    saved = stage_cache.cache_get('substack_archive_catalog', signature) if stage_cache else None
    manifest = {'records': {}, 'offset': 0, 'complete': False, 'last_date': '', 'failures': []} if saved is None else saved
    if (not isinstance(manifest, dict) or not isinstance(manifest.get('records'), dict)
            or type(manifest.get('offset')) is not int or manifest['offset'] < 0
            or manifest['offset'] != len(manifest['records'])
            or type(manifest.get('complete')) is not bool or not isinstance(manifest.get('failures'), list)
            or any(not isinstance(event, dict) or set(event) != {'stage', 'reason'}
                   or event['stage'] not in {'catalogue', 'public_post'} or not isinstance(event['reason'], str)
                   for event in manifest['failures'])):
        raise ValueError('Invalid Substack catalogue checkpoint')
    cutoff = research_now() - timedelta(days=lookback_days)
    # Revalidate persisted identities, not only newly fetched posts.
    for key, row in manifest['records'].items():
        if (not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0 or str(row['id']) != key
                or not isinstance(row.get('preview'), str) or row.get('author') not in (*aliases, '')
                or urlparse(row.get('url', '')).netloc != urlparse(root).netloc
                or not re.fullmatch(r'/p/[a-zA-Z0-9-]+', urlparse(row.get('url', '')).path)
                or urlparse(row['url']).scheme != 'https' or urlparse(row['url']).query or urlparse(row['url']).fragment
                or not isinstance(row.get('title'), str) or not row['title']
                or row.get('audience') not in {'everyone', 'only_paid', 'only_free', 'founding'}
                or datetime.fromisoformat(row['published_at']).tzinfo is None):
            raise ValueError('Invalid Substack cached post identity')
    saved_dates = [row['published_at'] for row in manifest['records'].values()]
    # SQLite serializes object keys canonically. Dict insertion order is not a
    # paging receipt; the durable cursor boundary is the oldest exact date.
    if manifest.get('last_date') != (min(saved_dates) if saved_dates else ''):
        raise ValueError('Invalid Substack cached pagination boundary')

    def save(value):
        if stage_cache: stage_cache.cache_save('substack_archive_catalog', signature, value)

    def failure(stage, exc):
        event = {'stage': stage, 'reason': type(exc).__name__}
        manifest['failures'].append(event)
        save(manifest)

    for _ in range(PAGE_LIMIT):
        if manifest['complete']: break
        try:
            if sleeper: await sleeper(1.0)
            offset = manifest['offset']
            url = archive if offset == 0 else root + '/api/v1/archive?' + urlencode(
                {'sort': 'new', 'offset': offset, 'limit': PAGE_SIZE})
            response = await get(url); response.raise_for_status()
            if str(response.url) != url: raise ValueError('Substack archive redirected')
            if offset == 0:
                payload = public_payload(response.text, root=root, publication_id=publication_id)
                pages = payload.get('newPostsForArchive')
                if not isinstance(pages, dict) or set(pages) != {'pub'}:
                    raise ValueError('Substack newest-archive structure changed')
                posts = pages['pub']
            else:
                posts = response.json()
            if not isinstance(posts, list) or len(posts) > PAGE_SIZE:
                raise ValueError('Substack public pagination changed')
            rows = [post_record(post, root=root, publication_id=publication_id, aliases=aliases,
                                allow_missing_byline=True) for post in posts]
            dates = [datetime.fromisoformat(row['published_at']) for row in rows]
            previous = datetime.fromisoformat(manifest['last_date']) if manifest['last_date'] else None
            if (dates != sorted(dates, reverse=True) or (previous and dates and dates[0] > previous)
                    or len({row['id'] for row in rows}) != len(rows)
                    or any(str(row['id']) in manifest['records'] for row in rows)):
                raise ValueError('Substack newest pagination is repeated or unordered')
            next_manifest = {**manifest, 'records': {**manifest['records'], **{str(row['id']):row for row in rows}},
                'offset': offset + len(rows), 'last_date': rows[-1]['published_at'] if rows else manifest['last_date'],
                'complete': not rows or len(rows) < PAGE_SIZE or bool(dates and dates[-1] < cutoff)}
            save(next_manifest); manifest = next_manifest
        except Exception as exc:
            failure('catalogue', exc); break

    from sources.curated_intelligence_source import _parse_feed, curated_feed_policy
    policy = curated_feed_policy()
    results, filtered, pending, failed, sampled = [], 0, 0, 0, 0
    for row in sorted(manifest['records'].values(), key=lambda value: (value['published_at'], value['id']), reverse=True):
        date = datetime.fromisoformat(row['published_at'])
        if date < cutoff or date > research_now(): continue
        detail_key = hashlib.sha256(json.dumps([VERSION, policy, signature, row], sort_keys=True).encode()).hexdigest()
        try:
            cached = stage_cache.cache_get('substack_public_post', detail_key) if stage_cache else None
            if cached is None:
                if detail_limit is not None and sampled >= detail_limit:
                    pending += 1
                    continue
                sampled += 1
                boundary, body, verified_author = 'public_preview_not_full_text', row['preview'], row['author']
                author_basis = 'explicit_public_byline'
                if row['audience'] == 'everyone' or not verified_author:
                    if sleeper: await sleeper(1.0)
                    response = await get(row['url']); response.raise_for_status()
                    if str(response.url) != row['url']: raise ValueError('Substack post redirected')
                    payload = public_payload(response.text, root=root, publication_id=publication_id)
                    post = payload.get('post'); actual = post_record(post, root=root, publication_id=publication_id,
                        aliases=aliases, author_hint=document_author(response.text, aliases))
                    if (any(actual[field] != row[field] for field in ('id','url','title','published_at','audience'))
                            or (verified_author and actual['author'] != verified_author)):
                        raise ValueError('Substack archive/detail identity mismatch')
                    if not verified_author: author_basis = 'registered_alias_exact_public_html_author_metadata'
                    verified_author = actual['author']
                    if row['audience'] == 'everyone':
                        body = post.get('body_html')
                        boundary = 'unauthenticated_public_rendered_body_not_full_text_certification'
                if not isinstance(body, str) or not body.strip(): raise ValueError('Substack public text is missing')
                xml_root = ET.Element('rss'); channel = ET.SubElement(xml_root, 'channel'); item = ET.SubElement(channel, 'item')
                for tag, value in (('title',row['title']),('link',row['url']),('pubDate',row['published_at']),
                                   ('author',verified_author),('description',body)):
                    ET.SubElement(item,tag).text=value
                xml = ET.tostring(xml_root, encoding='unicode')
                values, rejected = _parse_feed(xml, feed_url=feed_url, lookback_days=lookback_days, record=record, forum=None)
                for value in values:
                    value.raw.update(public_text_boundary=boundary, archive_fallback_url=archive,
                                     full_text_verified=False, substack_post_id=row['id'], author_identity_basis=author_basis)
                cached = {'post_id':row['id'],'url':row['url'],'verified_author':verified_author,
                          'evidence':[value.to_dict() for value in values], 'filtered':rejected}
                if stage_cache: stage_cache.cache_save('substack_public_post',detail_key,cached)
            if (not isinstance(cached,dict) or cached.get('post_id') != row['id'] or cached.get('url') != row['url']
                    or cached.get('verified_author') not in aliases
                    or (row['author'] and cached['verified_author'] != row['author'])
                    or not isinstance(cached.get('evidence'),list) or len(cached['evidence'])>1
                    or type(cached.get('filtered')) is not int or cached['filtered'] not in (0,1)
                    or len(cached['evidence'])+cached['filtered']!=1):
                raise ValueError('Invalid Substack post completion receipt')
            values=[technical_evidence_from_dict(item) for item in cached['evidence']]
            if any(value.url!=row['url'] or value.title!=row['title'] or value.published_at!=row['published_at']
                   or value.source!='curated-kol-blog' or value.authors != [cached['verified_author']]
                   or value.evidence_type != EvidenceType.SECONDARY_INTERPRETATION
                   or value.raw.get('full_text_verified') is not False for value in values):
                raise ValueError('Substack cached evidence identity mismatch')
            results.extend(values); filtered += cached['filtered']
        except Exception as exc:
            pending += 1; failed += 1; failure('public_post',exc)
    return results, filtered, {'catalogue_complete':manifest['complete'], 'records':len(manifest['records']),
        'pending_public_posts':pending, 'failed_public_posts':failed, 'sample_limit':detail_limit,
        'failure_history':manifest['failures'],
        'complete':manifest['complete'] and not pending,
        'public_boundary':'registered_author_public_archive_and_rendered_text_only_no_subscription_unlock'}
