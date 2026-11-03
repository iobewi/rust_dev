"""Ephemeral browser sessions for direct loopback or an explicitly trusted local forward. External API credentials stay separate."""
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


class UIAccess:
    def __init__(self, origin=None, forward_peers=()):
        self.forward_peers = tuple(ipaddress.ip_network(value, strict=False) for value in forward_peers)
        self.origin = origin
        if self.forward_peers:
            parsed = urlsplit(origin or '')
            require(parsed.scheme in {'http', 'https'} and (parsed.hostname == 'localhost' or loopback(parsed.hostname))
                    and not parsed.username and not parsed.password and not parsed.path and not parsed.query and not parsed.fragment,
                    'invalid_ui_origin', 'Trusted local forwarding requires an exact loopback BENCH_ORIGIN')
            # Validate the port now, rather than failing on a later request.
            parsed.port

    def forwarded(self, request):
        try:
            peer = ipaddress.ip_address(request.remote)
            return any(peer in network for network in self.forward_peers)
        except (ValueError, TypeError):
            return False

    def check(self, request, mutation=False):
        if self.forwarded(request):
            # Forwarded headers and rewritten Host are transport metadata only.
            # The expected public origin comes exclusively from server configuration.
            expected = self.origin
        else:
            require(not any(header in request.headers for header in ('Forwarded', 'X-Forwarded-For', 'X-Forwarded-Host', 'X-Forwarded-Proto', 'X-Real-IP')),
                    'local_session_only', 'Configure the local forwarding peer and exact public origin on the server', 403)
            host = urlsplit('http://' + request.host).hostname
            require((host == 'localhost' or loopback(host)) and loopback(request.remote),
                    'local_session_only', 'Automatic browser sessions require loopback or a configured local forwarding peer', 403)
            expected = f'{request.scheme}://{request.host}'
        origin = request.headers.get('Origin')
        require((not mutation and not origin) or origin == expected,
                'origin_forbidden', 'Same-origin browser request required', 403)
        require(request.headers.get('Sec-Fetch-Site', 'same-origin') in {'same-origin', 'none'},
                'origin_forbidden', 'Cross-site browser session refused', 403)

    def secure_cookie(self, request):
        return self.origin.startswith('https://') if self.forwarded(request) else request.secure


def local_request(request, mutation=False):
    UIAccess().check(request, mutation)


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
