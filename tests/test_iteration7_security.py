"""Iteration 7 SECURITY tests.

Validates the new pending_verification flow:
- /api/upi/claim no longer auto-activates premium
- /api/admin/claims/approve activates premium for the session
- /api/admin/claims/reject blocks future approval
- /api/admin/stats includes new counters
- Duplicate UTR still 409, no _id leaks, regression on cancel/Razorpay-order
"""
import os
import uuid
import pytest
import requests

def _read_env(path, key):
    try:
        with open(path) as f:
            for line in f:
                if line.startswith(key + '='):
                    return line.split('=', 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return None


BASE_URL = (os.environ.get('REACT_APP_BACKEND_URL')
            or _read_env('/app/frontend/.env', 'REACT_APP_BACKEND_URL')).rstrip('/')
API = f"{BASE_URL}/api"
ADMIN_PASSWORD = 'buddy2026'


@pytest.fixture(scope='module')
def api():
    s = requests.Session()
    s.headers.update({'Content-Type': 'application/json'})
    return s


@pytest.fixture(scope='module')
def admin_headers():
    return {'X-Admin-Token': ADMIN_PASSWORD, 'Content-Type': 'application/json'}


def _new_sid(prefix='it7'):
    return f'{prefix}_{uuid.uuid4().hex[:8]}'


def _new_utr(prefix='IT7'):
    return f'{prefix}{uuid.uuid4().hex[:9].upper()}'


# ---------- 1. /api/upi/claim no longer auto-activates ----------
class TestClaimNoAutoActivate:
    def test_claim_returns_pending_not_premium(self, api):
        sid = _new_sid('it7_pending')
        utr = _new_utr()
        r = api.post(f'{API}/upi/claim', json={'session_id': sid, 'utr': utr})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d['ok'] is True
        assert d['is_premium'] is False, "SECURITY: claim must NOT auto-activate premium"
        assert d['status'] == 'pending_verification'
        assert 'expires_at' not in d or d.get('expires_at') is None
        assert '_id' not in d

    def test_premium_status_still_false_after_claim(self, api):
        sid = _new_sid('it7_status')
        utr = _new_utr()
        api.post(f'{API}/upi/claim', json={'session_id': sid, 'utr': utr})
        s = api.get(f'{API}/premium/status', params={'session_id': sid}).json()
        assert s['is_premium'] is False
        assert s.get('expires_at') is None
        assert '_id' not in s

    def test_fake_utr_does_not_unlock_premium(self, api):
        """SECURITY: the user-reported bug — random fake UTR must not unlock."""
        sid = _new_sid('it7_fake')
        r = api.post(f'{API}/upi/claim',
                     json={'session_id': sid, 'utr': 'FAKE123ABC456'})
        assert r.status_code == 200
        assert r.json()['is_premium'] is False
        s = api.get(f'{API}/premium/status', params={'session_id': sid}).json()
        assert s['is_premium'] is False, "FAKE UTR unlocked premium — SECURITY HOLE"

    def test_science_blocked_after_pending_claim(self, api):
        sid = _new_sid('it7_sci')
        api.post(f'{API}/upi/claim', json={'session_id': sid, 'utr': _new_utr()})
        r = api.post(f'{API}/ask',
                     json={'session_id': sid, 'subject': 'science',
                           'question': 'why is sky blue?'})
        assert r.status_code == 402, "science must be blocked while claim is pending"


# ---------- 2. Approve flow activates premium ----------
class TestApproveFlow:
    def test_approve_activates_premium(self, api, admin_headers):
        sid = _new_sid('it7_approve')
        utr = _new_utr()
        r = api.post(f'{API}/upi/claim', json={'session_id': sid, 'utr': utr})
        assert r.status_code == 200

        # find claim_id via admin list
        lst = api.get(f'{API}/admin/claims', headers=admin_headers).json()['items']
        mine = [c for c in lst if c.get('session_id') == sid]
        assert mine, 'claim not found in admin list'
        claim_id = mine[0]['id']
        assert mine[0]['status'] == 'pending_verification'

        # approve
        r = api.post(f'{API}/admin/claims/approve',
                     headers=admin_headers, json={'claim_id': claim_id})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body['ok'] is True
        assert body['session_id'] == sid
        assert body.get('expires_at')

        # premium status now true
        s = api.get(f'{API}/premium/status', params={'session_id': sid}).json()
        assert s['is_premium'] is True
        assert s.get('expires_at')

        # claim status now 'verified'
        lst2 = api.get(f'{API}/admin/claims', headers=admin_headers).json()['items']
        again = [c for c in lst2 if c['id'] == claim_id][0]
        assert again['status'] == 'verified'

    def test_approve_twice_idempotent(self, api, admin_headers):
        sid = _new_sid('it7_idem')
        utr = _new_utr()
        api.post(f'{API}/upi/claim', json={'session_id': sid, 'utr': utr})
        lst = api.get(f'{API}/admin/claims', headers=admin_headers).json()['items']
        claim_id = [c for c in lst if c['session_id'] == sid][0]['id']

        r1 = api.post(f'{API}/admin/claims/approve',
                      headers=admin_headers, json={'claim_id': claim_id})
        assert r1.status_code == 200
        r2 = api.post(f'{API}/admin/claims/approve',
                      headers=admin_headers, json={'claim_id': claim_id})
        assert r2.status_code == 200
        assert r2.json().get('already') is True

    def test_approve_invalid_claim_id_404(self, api, admin_headers):
        r = api.post(f'{API}/admin/claims/approve',
                     headers=admin_headers, json={'claim_id': 'nope_' + uuid.uuid4().hex})
        assert r.status_code == 404

    def test_approve_no_token_401(self, api):
        r = requests.post(f'{API}/admin/claims/approve',
                          json={'claim_id': 'any'})
        assert r.status_code == 401

    def test_approve_wrong_token_401(self, api):
        r = requests.post(f'{API}/admin/claims/approve',
                          json={'claim_id': 'any'},
                          headers={'X-Admin-Token': 'bad'})
        assert r.status_code == 401


# ---------- 3. Reject flow ----------
class TestRejectFlow:
    def test_reject_marks_status_and_blocks_premium(self, api, admin_headers):
        sid = _new_sid('it7_rej')
        utr = _new_utr()
        api.post(f'{API}/upi/claim', json={'session_id': sid, 'utr': utr})
        lst = api.get(f'{API}/admin/claims', headers=admin_headers).json()['items']
        claim_id = [c for c in lst if c['session_id'] == sid][0]['id']

        r = api.post(f'{API}/admin/claims/reject',
                     headers=admin_headers,
                     json={'claim_id': claim_id, 'reason': 'no money received'})
        assert r.status_code == 200
        assert r.json().get('ok') is True

        # premium still false
        s = api.get(f'{API}/premium/status', params={'session_id': sid}).json()
        assert s['is_premium'] is False

        # claim now rejected
        lst2 = api.get(f'{API}/admin/claims', headers=admin_headers).json()['items']
        again = [c for c in lst2 if c['id'] == claim_id][0]
        assert again['status'] == 'rejected'
        assert again.get('reject_reason') == 'no money received'

    def test_approve_after_reject_400(self, api, admin_headers):
        sid = _new_sid('it7_rejapp')
        utr = _new_utr()
        api.post(f'{API}/upi/claim', json={'session_id': sid, 'utr': utr})
        lst = api.get(f'{API}/admin/claims', headers=admin_headers).json()['items']
        claim_id = [c for c in lst if c['session_id'] == sid][0]['id']

        api.post(f'{API}/admin/claims/reject',
                 headers=admin_headers, json={'claim_id': claim_id})
        r = api.post(f'{API}/admin/claims/approve',
                     headers=admin_headers, json={'claim_id': claim_id})
        assert r.status_code == 400
        assert 'reject' in r.text.lower()


# ---------- 4. Stats include new counters + amount counts verified only ----------
class TestStatsCounters:
    def test_stats_has_new_keys(self, api, admin_headers):
        s = api.get(f'{API}/admin/stats', headers=admin_headers).json()
        for k in ['pending_verification', 'verified_claims',
                  'total_upi_claims', 'amount_collected_rupees']:
            assert k in s, f'missing key {k}'
        assert '_id' not in s

    def test_amount_collected_only_counts_verified(self, api, admin_headers):
        # Create a pending claim that we will NOT approve.
        sid = _new_sid('it7_amt_pending')
        api.post(f'{API}/upi/claim', json={'session_id': sid, 'utr': _new_utr()})

        s_before = api.get(f'{API}/admin/stats', headers=admin_headers).json()

        # Create another claim and approve it
        sid2 = _new_sid('it7_amt_ok')
        api.post(f'{API}/upi/claim', json={'session_id': sid2, 'utr': _new_utr()})
        lst = api.get(f'{API}/admin/claims', headers=admin_headers).json()['items']
        cid = [c for c in lst if c['session_id'] == sid2][0]['id']
        api.post(f'{API}/admin/claims/approve',
                 headers=admin_headers, json={'claim_id': cid})

        s_after = api.get(f'{API}/admin/stats', headers=admin_headers).json()
        # verified delta = +1, pending delta should be 0 net (one pending added before, one verified now)
        assert s_after['verified_claims'] == s_before['verified_claims'] + 1
        assert s_after['amount_collected_rupees'] == \
            s_before['amount_collected_rupees'] + 99.0
        # pending didn't decrease below baseline (still has the unapproved one)
        assert s_after['pending_verification'] >= 1


# ---------- 5. Regression / duplicate UTR ----------
class TestRegression:
    def test_duplicate_utr_409(self, api):
        utr = _new_utr('DUP')
        s1 = _new_sid('it7_dup1')
        s2 = _new_sid('it7_dup2')
        r1 = api.post(f'{API}/upi/claim', json={'session_id': s1, 'utr': utr})
        assert r1.status_code == 200
        r2 = api.post(f'{API}/upi/claim', json={'session_id': s2, 'utr': utr})
        assert r2.status_code == 409

    def test_razorpay_order_create_still_works(self, api):
        sid = _new_sid('it7_rzp')
        r = api.post(f'{API}/subscription/create', json={'session_id': sid})
        assert r.status_code == 200
        d = r.json()
        assert d['order_id'].startswith('order_')
        assert '_id' not in d

    def test_cancel_no_active_returns_404(self, api):
        sid = _new_sid('it7_cancel_no')
        r = api.post(f'{API}/premium/cancel', json={'session_id': sid})
        assert r.status_code == 404

    def test_root(self, api):
        r = api.get(f'{API}/')
        assert r.status_code == 200
