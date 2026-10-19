"""Regression coverage for credential boundaries, cache integrity and idle polling."""

import io
import json
import os
import threading
import types
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.message import Message

import pytest

import usage_widget as w

# Save the real method before the autouse fixture blocks networking. Our fake transport
# below exercises urllib's actual handler chain without ever opening a socket.
OPEN = urllib.request.OpenerDirector.open


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_redirect_never_sends_credentials_to_second_host(monkeypatch, code, method):
    sent = []

    class Transport(urllib.request.HTTPSHandler):
        def https_open(self, req):
            sent.append(req)
            headers = Message()
            headers['Location'] = 'https://untrusted.test/collect'
            response = urllib.response.addinfourl(io.BytesIO(b'{}'), headers, req.full_url, code)
            response.msg = 'redirect'
            return response

    build = urllib.request.build_opener
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *handlers: build(
        *(h for h in handlers if not isinstance(h, urllib.request.HTTPSHandler)),
        Transport(), urllib.request.ProxyHandler({})))
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', OPEN)
    with pytest.raises(w.HTTPStatusError) as error:
        w.http_json('https://issuer.test/token', {'Authorization': 'Bearer secret'},
                    form={'refresh_token': 'secret'} if method == 'POST' else None, auth_errors=False)
    assert error.value.code == code
    assert len(sent) == 1
    assert sent[0].host == 'issuer.test'


@pytest.mark.parametrize('url', ['http://issuer.test', 'file:///etc/passwd',
                                'https://issuer.test@evil.test', 'https://issuer.test/\npath'])
def test_http_rejects_unsafe_urls(url):
    with pytest.raises(ValueError):
        w.http_json(url, {'Authorization': 'Bearer secret'})


def test_bounded_response_and_object_shape():
    with pytest.raises(ValueError, match='size limit'):
        w._read_response(io.BytesIO(b' ' * 101), limit=100)
    for payload in (b'[]', b'null', b'42'):
        with pytest.raises(ValueError, match='JSON object'):
            w._read_response(io.BytesIO(payload))


@pytest.mark.parametrize('url', ['https://evil.test/page', 'https://management.azure.com.evil.test/page',
                                'http://management.azure.com/page', 'https://management.azure.com:444/page',
                                'https://management.azure.com@evil.test/page', '//evil.test/page'])
def test_azure_pagination_rejects_untrusted_destination(fake_http, url):
    fake_http.responses[w.ARM + '/first'] = {'nextLink': url}
    with pytest.raises(ValueError):
        w._arm_list({'Authorization': 'Bearer secret'}, '/first')
    assert len(fake_http.calls) == 1


def test_azure_pagination_cycle(fake_http):
    fake_http.responses[w.ARM + '/first'] = {'nextLink': w.ARM + '/first'}
    with pytest.raises(RuntimeError, match='pagination'):
        w._arm_list({}, '/first')
    assert len(fake_http.calls) == 1


def test_vertex_pagination_cycle(fake_http):
    fake_http.responses['https://monitoring.googleapis.com/'] = {'nextPageToken': 'again'}
    with pytest.raises(RuntimeError, match='pagination'):
        w._vertex_tokens({}, 'project', datetime.now(timezone.utc))
    assert len(fake_http.calls) == 2


@pytest.mark.parametrize('contents', [[], None, {'k': {'at': 'bad', 'value': 1}},
                                     {'k': {'at': 1}}, {'k': {'at': float('nan'), 'value': 1}}])
def test_cache_recovers_from_malformed_entries(contents):
    path = w.config_dir() / 'cloud-cache.json'
    w._write_json(path, contents)
    assert w._cached('k', 60, lambda: 7) == 7


def test_cache_preserves_overlapping_provider_writes():
    barrier = threading.Barrier(2)

    def query(key):
        def compute():
            barrier.wait(timeout=5)
            return key
        return w._cached(key, 60, compute)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(query, ['a', 'b'])) == ['a', 'b']
    assert set(json.loads((w.config_dir() / 'cloud-cache.json').read_text())) == {'a', 'b'}


def test_json_write_ignores_predictable_temp_symlink(tmp_path):
    victim = tmp_path / 'victim'
    victim.write_text('keep')
    path = tmp_path / 'config.json'
    try:
        path.with_suffix('.json.tmp').symlink_to(victim)
    except OSError:
        pytest.skip('symlinks unavailable')
    w._write_json(path, {'ok': True})
    assert victim.read_text() == 'keep'
    assert json.loads(path.read_text()) == {'ok': True}
    if os.name != 'nt':
        assert path.stat().st_mode & 0o077 == 0


def test_failed_atomic_write_preserves_previous_file(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    w._write_json(path, {'old': True})
    def fail(*args):
        raise OSError('disk failure')
    monkeypatch.setattr(w.os, 'replace', fail)
    with pytest.raises(OSError):
        w._write_json(path, {'new': True})
    assert json.loads(path.read_text()) == {'old': True}
    assert not list(tmp_path.glob('config.json.*.tmp'))


def test_cached_update_url_is_not_trusted():
    w._write_json(w.config_dir() / 'cloud-cache.json', {
        'update:latest': {'at': w.time.time(), 'value': {'version': '99.0', 'url': 'file:///tmp/evil'}}})
    assert w.check_for_update()['url'] == w.REPO_URL + '/releases/tag/v99.0'


def test_empty_widget_honors_poll_interval(monkeypatch):
    widget = w.Widget.__new__(w.Widget)
    calls = []
    widget.data, widget.fetching, widget.interval, widget.last_fetch = [], False, 180, 1000
    widget.root = types.SimpleNamespace(after=lambda ms, fn: calls.append(ms))
    widget.refresh = lambda: calls.append('refresh')
    monkeypatch.setattr(w.time, 'time', lambda: 1001)
    widget._tick()
    assert calls == [30_000]
    monkeypatch.setattr(w.time, 'time', lambda: 1180)
    widget._tick()
    assert calls[-2:] == ['refresh', 30_000]


def test_timezone_missing_does_not_crash_countdown():
    when = w.parse_time('2099-01-01T00:00:00')
    assert when.tzinfo == timezone.utc
    assert isinstance(w.format_remaining(when), str)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf')])
def test_nonfinite_percent_is_rejected_before_render(value):
    with pytest.raises(ValueError, match='finite'):
        w.Limit('Session', value)


def test_azure_pagination_page_limit(fake_http, monkeypatch):
    monkeypatch.setattr(w, 'MAX_PAGES', 2)
    fake_http.responses[w.ARM + '/1'] = {'nextLink': w.ARM + '/2'}
    fake_http.responses[w.ARM + '/2'] = {'nextLink': w.ARM + '/3'}
    with pytest.raises(RuntimeError, match='page limit'):
        w._arm_list({}, '/1')
    assert len(fake_http.calls) == 2


def test_bedrock_cost_pagination_cycle():
    calls = []
    client = types.SimpleNamespace(get_cost_and_usage=lambda **kw: calls.append(kw) or {'NextPageToken': 'again'})
    session = types.SimpleNamespace(client=lambda *args, **kwargs: client)
    with pytest.raises(RuntimeError, match='pagination'):
        w._bedrock_month_cost(session)
    assert len(calls) == 2


def test_cloudwatch_pagination_page_limit(monkeypatch):
    monkeypatch.setattr(w, 'MAX_PAGES', 2)
    paginator = types.SimpleNamespace(paginate=lambda **kwargs: iter([{}, {}, {}]))
    client = types.SimpleNamespace(get_paginator=lambda name: paginator)
    session = types.SimpleNamespace(client=lambda *args, **kwargs: client)
    with pytest.raises(RuntimeError, match='pagination'):
        w._bedrock_tokens(session, 'us-east-1', datetime.now(timezone.utc))


def test_cached_update_preserves_release_tag_without_v(fake_http):
    fake_http.responses[w.RELEASES_API] = {
        'tag_name': '99.0', 'html_url': w.REPO_URL + '/releases/tag/99.0'}
    assert w.check_for_update()['url'] == w.REPO_URL + '/releases/tag/99.0'


def test_non_utf8_cache_is_a_cache_miss():
    path = w.config_dir() / 'cloud-cache.json'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'\xff')
    assert w._cached('k', 60, lambda: 3) == 3


def test_json_serialization_failure_cleans_temporary_file(tmp_path):
    path = tmp_path / 'config.json'
    w._write_json(path, {'old': True})
    with pytest.raises(TypeError):
        w._write_json(path, {'bad': object()})
    assert json.loads(path.read_text()) == {'old': True}
    assert not list(tmp_path.glob('config.json.*.tmp'))


@pytest.mark.parametrize('hostname', ['foo.openai.azure.com.evil.test', 'foo.services.ai.azure.com.evil.test'])
def test_azure_detection_requires_complete_hostname(hostname):
    assert w.AZURE_HOST_RE.match('https://' + hostname + '/') is None
