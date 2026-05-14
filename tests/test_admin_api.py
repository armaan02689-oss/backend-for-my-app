"""Iteration 6 - Admin (merchant) endpoints tests.

Covers /api/admin/login, /api/admin/stats, /api/admin/refunds,
/api/admin/refunds/mark-refunded, /api/admin/claims and auth enforcement.
"""
import os
import uuid
import pytest
import requests

BASE_URL = os.environ['REACT_APP_BACKEND_URL'].rstrip('/') if os.environ.get('REACT_APP_BACKEND_URL') else 'https://homework-helper-549.preview.emergentagent.com'
ADMIN_PASSWORD = 'buddy2026'


@pytest.fixture(scope='module')
def api():
    s = requests.Session()
    s.headers.update({'Content-Type': 'application/json'})
    return s


@pytest.fixture(scope='module')
def admin_token(api):
    r = api.post(f'{BASE_URL}/api/admin/login', json={'password': ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get('ok') is True
    assert data.get('token') == ADMIN_PASSWORD
    return data['token']


@pytest.fixture(scope='module')
def admin_headers(admin_token):
    return {'X-Admin-Token': admin_token, 'Content-Type': 'application/json'}


# -------- seed: create a pending_manual refund so list endpoints have data --------
@pytest.fixture(scope='module')
def seeded_pending_refund(api):
    session_id = f'pytest_admin_{uuid.uuid4().hex[:8]}'
    utr = f'PYTADM{uuid.uuid4().hex[:8].upper()}'
    # Claim UPI -> activates premium with method=upi_direct
    r = api.post(f'{BASE_URL}/api/upi/claim', json={'session_id': session_id, 'utr': utr})
    assert r.status_code == 200, r.text
    # Cancel -> creates pending_manual refund
    r = api.post(f'{BASE_URL}/api/premium/cancel', json={
        'session_id': session_id, 'reason': 'admin test', 'refund_upi_id': 'tester@upi'
    })
    assert r.status_code == 200, r.text
    return {'session_id': session_id, 'utr': utr}


class TestAdminAuth:
    def test_login_wrong_password(self, api):
        r = api.post(f'{BASE_URL}/api/admin/login', json={'password': 'wrong'})
        assert r.status_code == 401

    def test_login_correct_returns_token(self, admin_token):
        assert admin_token == ADMIN_PASSWORD

    def test_stats_requires_token(self, api):
        r = api.get(f'{BASE_URL}/api/admin/stats')
        assert r.status_code == 401

    def test_stats_wrong_token(self, api):
        r = api.get(f'{BASE_URL}/api/admin/stats', headers={'X-Admin-Token': 'nope'})
        assert r.status_code == 401

    def test_refunds_requires_token(self, api):
        r = api.get(f'{BASE_URL}/api/admin/refunds')
        assert r.status_code == 401

    def test_claims_requires_token(self, api):
        r = api.get(f'{BASE_URL}/api/admin/claims')
        assert r.status_code == 401

    def test_mark_refunded_requires_token(self, api):
        r = api.post(f'{BASE_URL}/api/admin/refunds/mark-refunded',
                     json={'refund_id': 'xxx'})
        assert r.status_code == 401


class TestAdminStats:
    def test_stats_shape(self, api, admin_headers, seeded_pending_refund):
        r = api.get(f'{BASE_URL}/api/admin/stats', headers=admin_headers)
        assert r.status_code == 200
        data = r.json()
        for k in ['total_upi_claims', 'active_subscriptions', 'pending_manual_refunds',
                  'completed_refunds', 'razorpay_refunds', 'amount_collected_rupees',
                  'amount_to_refund_rupees']:
            assert k in data, f'missing key {k}'
        assert isinstance(data['total_upi_claims'], int)
        assert isinstance(data['pending_manual_refunds'], int)
        assert data['pending_manual_refunds'] >= 1  # we seeded one
        # No _id leak
        assert '_id' not in data


class TestAdminRefunds:
    def test_list_all(self, api, admin_headers, seeded_pending_refund):
        r = api.get(f'{BASE_URL}/api/admin/refunds', headers=admin_headers)
        assert r.status_code == 200
        items = r.json().get('items', [])
        assert isinstance(items, list)
        assert len(items) >= 1
        for it in items:
            assert '_id' not in it

    def test_list_pending_manual_has_upi_link(self, api, admin_headers, seeded_pending_refund):
        r = api.get(f'{BASE_URL}/api/admin/refunds',
                    headers=admin_headers, params={'status': 'pending_manual'})
        assert r.status_code == 200
        items = r.json().get('items', [])
        assert len(items) >= 1
        # find one with upi_direct method
        upi_items = [i for i in items if i.get('method') == 'upi_direct' and i.get('refund_upi_id')]
        assert upi_items, 'expected at least one upi_direct refund with refund_upi_id'
        for it in upi_items:
            assert it.get('refund_upi_link', '').startswith('upi://pay?pa=')
            assert it['status'] == 'pending_manual'
            assert '_id' not in it


class TestAdminClaims:
    def test_list_claims(self, api, admin_headers, seeded_pending_refund):
        r = api.get(f'{BASE_URL}/api/admin/claims', headers=admin_headers)
        assert r.status_code == 200
        items = r.json().get('items', [])
        assert isinstance(items, list)
        assert len(items) >= 1
        # we seeded one; find it
        match = [c for c in items if c.get('session_id') == seeded_pending_refund['session_id']]
        assert match, 'seeded claim not in list'
        for c in items:
            assert '_id' not in c


class TestAdminMarkRefunded:
    def test_mark_unknown_returns_404(self, api, admin_headers):
        r = api.post(f'{BASE_URL}/api/admin/refunds/mark-refunded',
                     headers=admin_headers,
                     json={'refund_id': 'nonexistent_' + uuid.uuid4().hex})
        assert r.status_code == 404

    def test_mark_refunded_flow(self, api, admin_headers, seeded_pending_refund):
        # locate the pending refund for our seeded session
        r = api.get(f'{BASE_URL}/api/admin/refunds',
                    headers=admin_headers, params={'status': 'pending_manual'})
        items = r.json()['items']
        mine = [i for i in items if i.get('session_id') == seeded_pending_refund['session_id']]
        assert mine, 'seeded pending refund not found'
        refund_id = mine[0]['id']

        # baseline stats
        s_before = api.get(f'{BASE_URL}/api/admin/stats', headers=admin_headers).json()

        # mark refunded
        r = api.post(f'{BASE_URL}/api/admin/refunds/mark-refunded',
                     headers=admin_headers,
                     json={'refund_id': refund_id,
                           'merchant_utr': 'MERCH123TEST',
                           'note': 'paid via PhonePe'})
        assert r.status_code == 200, r.text
        assert r.json().get('ok') is True

        # idempotency
        r2 = api.post(f'{BASE_URL}/api/admin/refunds/mark-refunded',
                      headers=admin_headers,
                      json={'refund_id': refund_id})
        assert r2.status_code == 200
        assert r2.json().get('already') is True

        # verify persistence via refunded list
        r3 = api.get(f'{BASE_URL}/api/admin/refunds',
                     headers=admin_headers, params={'status': 'refunded'})
        done = r3.json()['items']
        target = [i for i in done if i['id'] == refund_id]
        assert target, 'refund did not move to refunded'
        t = target[0]
        assert t['status'] == 'refunded'
        assert t['merchant_utr'] == 'MERCH123TEST'
        assert t['merchant_note'] == 'paid via PhonePe'
        assert 'refunded_at' in t

        # stats reflect changes
        s_after = api.get(f'{BASE_URL}/api/admin/stats', headers=admin_headers).json()
        assert s_after['completed_refunds'] == s_before['completed_refunds'] + 1
        assert s_after['pending_manual_refunds'] == s_before['pending_manual_refunds'] - 1


class TestRegression:
    """Quick smoke regression on prior endpoints."""
    def test_upi_config(self, api):
        r = api.get(f'{BASE_URL}/api/upi/config')
        assert r.status_code == 200
        assert 'upi_link' in r.json()

    def test_history_empty_for_random(self, api):
        sid = f'reg_{uuid.uuid4().hex[:6]}'
        r = api.get(f'{BASE_URL}/api/history', params={'session_id': sid})
        assert r.status_code == 200
        assert r.json().get('items') == []

    def test_premium_status(self, api):
        sid = f'reg_{uuid.uuid4().hex[:6]}'
        r = api.get(f'{BASE_URL}/api/premium/status', params={'session_id': sid})
        assert r.status_code == 200
        d = r.json()
        assert d['is_premium'] is False
        assert d['scans_used'] == 0
