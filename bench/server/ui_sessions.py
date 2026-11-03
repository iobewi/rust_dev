"""Ephemeral, loopback-only browser sessions. External API credentials stay separate."""
import ipaddress
import secrets
from urllib.parse import urlsplit
from protocol import require

COOKIE = 'bench_ui_v1'
LIFETIME = 8 * 3600


def loopback(value):
    try:
        address = ipaddress.ip_address(value)
        return address.is_loopback or bool(getattr(address, 'ipv4_mapped', None) and address.ipv4_mapped.is_loopback)
    except (ValueError, TypeError):
        return False


def local_request(request, mutation=False):
    require(not any(header in request.headers for header in ('Forwarded', 'X-Forwarded-For', 'X-Forwarded-Host', 'X-Forwarded-Proto', 'X-Real-IP')),
            'local_session_only', 'Automatic browser sessions cannot use proxy forwarding headers', 403)
    host = urlsplit('http://' + request.host).hostname
    require((host == 'localhost' or loopback(host)) and loopback(request.remote),
            'local_session_only', 'Automatic browser sessions require a direct loopback connection', 403)
    origin = request.headers.get('Origin')
    expected = f'{request.scheme}://{request.host}'
    require((not mutation and not origin) or origin == expected,
            'origin_forbidden', 'Same-origin browser request required', 403)
    require(request.headers.get('Sec-Fetch-Site', 'same-origin') in {'same-origin', 'none'},
            'origin_forbidden', 'Cross-site browser session refused', 403)


class UISessions:
    def __init__(self, clock, devices):
        self.clock, self.devices = clock, devices
        self.entries = {}

    def get(self, key):
        session = self.entries.get(key)
        require(session and session['until'] > self.clock(), 'unauthenticated', 'Browser session expired', 401)
        return session

    def open(self, key=None):
        session = self.entries.get(key)
        if not session or session['until'] <= self.clock():
            require(len(self.entries) < 128, 'capacity', 'Browser session store full', 503)
            key = secrets.token_urlsafe(32)
            identity = secrets.token_hex(8)
            session = {'client': 'web-' + identity, 'agent': 'browser-' + identity,
                       'devices': self.devices, 'until': self.clock() + LIFETIME}
            self.entries[key] = session
        return key, session

    def sweep(self):
        expired = [session for session in self.entries.values() if session['until'] <= self.clock()]
        self.entries = {key: session for key, session in self.entries.items() if session['until'] > self.clock()}
        return expired
