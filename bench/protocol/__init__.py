"""Versioned, transport-independent hardware command validation."""
import base64
import binascii
import math
import ipaddress
from urllib.parse import urlsplit

VERSION = 1
MAX_ARTIFACT = 16 * 1024 * 1024
MAX_UART = 16384
CAPABILITIES = {'identify', 'reset', 'partitions', 'flash.read', 'flash.md5', 'flash.write', 'flash.erase',
                'uart.write', 'uart.configure', 'console', 'io0', 'boot.download', 'mark', 'trigger', 'http.request'}
PROGRAM = {'flash.write', 'flash.erase'}
# Experimental flasher stubs the agent may upload (bench/web/stub-variants.js is the agent's copy of this registry; a test keeps the two in step).
# Selected only by an explicit name + the pinned SHA-256, only for the chip below, only by read commands.
STUB_VARIANTS = {'p4-no-clock-boost': {'chip': 'esp32p4', 'sha256': '911c86173efd9e51a12f289b4b8f1adb9046d1326b426a7ecfd9a646f8366f24'}}
STUB_VARIANT_COMMANDS = {'flash.read', 'flash.md5'}
PRIVILEGED = PROGRAM | {'uart.write', 'io0', 'boot.download', 'trigger', 'http.request'}


def trigger_disarm(name, args):
    return name == 'trigger' and args == {'action': 'disarm'}


def requires_grant(name, args):
    return name in PRIVILEGED and not trigger_disarm(name, args)


class Fault(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def require(condition, code='invalid_request', message='Invalid request', status=400):
    if not condition:
        raise Fault(code, message, status)


def number(value, minimum, maximum):
    require(type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum)
    return value


def integer(value, minimum, maximum):
    require(type(value) is int and minimum <= value <= maximum)
    return value


def binary(value, limit=MAX_UART):
    require(isinstance(value, str) and len(value) <= (limit + 2) // 3 * 4)
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise Fault('invalid_base64', 'Expected base64 bytes') from None
    require(len(data) <= limit)
    return data


def region(address, size, capacity):
    integer(address, 0, capacity - 1)
    integer(size, 1, min(MAX_ARTIFACT, capacity))
    require(address + size <= capacity, 'flash_bounds', 'Region exceeds flash capacity')


def validate_command(name, args, device, artifacts):
    require(name in CAPABILITIES and name in device['capabilities'], 'unsupported', 'Unsupported command')
    require(isinstance(args, dict))
    schemas = {
        'identify': set(), 'reset': set(), 'console': {'return_console'},
        'partitions': {'address', 'return_console'}, 'flash.read': {'address', 'size', 'return_console', 'stub_digest', 'stub_variant', 'stub_sha256'},
        'flash.md5': {'address', 'size', 'return_console', 'loader', 'stub_variant', 'stub_sha256'},
        'flash.write': {'images', 'chip', 'erase', 'return_console'}, 'flash.erase': {'chip', 'return_console'},
        'uart.write': {'data'}, 'uart.configure': {'baudrate'}, 'io0': {'duration_ms'}, 'boot.download': {'hold_ms'},
        'http.request': {'url', 'method', 'body', 'timeout_ms'},
        'mark': {'label'}, 'trigger': {'data', 'action', 'delay_ms', 'expires_ms'},
    }
    require(not (args.keys() - schemas[name]), message='Unknown command fields')
    for key in ('erase', 'return_console', 'stub_digest'):
        if key in args:
            require(type(args[key]) is bool)
    if 'stub_variant' in args or 'stub_sha256' in args:
        require('stub_variant' in args and 'stub_sha256' in args, 'stub_variant_invalid', 'stub_variant and stub_sha256 go together')
        entry = STUB_VARIANTS.get(args['stub_variant']) if isinstance(args['stub_variant'], str) else None
        require(entry is not None, 'stub_variant_unknown', 'Unknown stub variant')
        require(name in STUB_VARIANT_COMMANDS, 'stub_variant_not_allowed', 'A stub variant is only for read commands')
        require(device['chip'] == entry['chip'], 'stub_variant_chip_mismatch', 'This stub variant does not apply to this chip')
        require(args['stub_sha256'] == entry['sha256'], 'stub_variant_sha_mismatch', 'The SHA-256 does not match the pinned stub variant')
        require(args.get('loader', 'stub') == 'stub', 'stub_variant_not_allowed', 'A stub variant cannot be combined with the ROM loader')
        require(not device.get('quarantined'), 'device_quarantined', 'No experimental stub while the device is quarantined', 409)
    if name == 'flash.md5' and 'loader' in args:
        require(args['loader'] in ('stub', 'rom'), message='loader must be stub or rom')
    capacity = device['flash_size']
    if name in ('flash.read', 'flash.md5'):
        region(args.get('address'), args.get('size'), capacity)
    if name == 'partitions':
        region(args.get('address', 0x8000), 0xc00, capacity)
    if name in PROGRAM:
        require(isinstance(args.get('chip'), str) and args['chip'] == device['chip'],
                'chip_mismatch', 'Expected chip family must match the device')
    if name == 'flash.write':
        images = args.get('images')
        require(isinstance(images, list) and 1 <= len(images) <= 16)
        spans = []
        for image in images:
            require(isinstance(image, dict) and set(image) == {'artifact', 'address', 'sha256'})
            artifact = artifacts.get(image['artifact'])
            require(artifact is not None, 'artifact_missing', 'Upload the image first')
            require(image['sha256'] == artifact['sha256'], 'digest_mismatch', 'SHA-256 mismatch')
            region(image['address'], artifact['size'], capacity)
            # The loader erases whole 4-KiB sectors. Disjoint bytes in the same sector are unsafe too.
            lo, hi = image['address'] // 4096 * 4096, (image['address'] + artifact['size'] + 4095) // 4096 * 4096
            require(not any(lo < end and start < hi for start, end in spans), 'flash_overlap', 'Flash erase sectors overlap')
            spans.append((lo, hi))
    if name == 'uart.write':
        require(len(binary(args.get('data'))) > 0)
    if name == 'uart.configure':
        integer(args.get('baudrate'), 1200, 3000000)
    if name == 'io0':
        integer(args.get('duration_ms'), 1, 10000)
    if name == 'boot.download':
        integer(args.get('hold_ms'), 100, 3000)     # required: there is no default hold
    if name == 'mark':
        require(isinstance(args.get('label'), str) and len(args['label']) <= 256)
    if name == 'http.request':
        target = args.get('url')
        require(isinstance(target, str) and len(target) <= 2048)
        u = urlsplit(target)
        require(u.scheme == 'http' and not u.username and not u.password and not u.fragment)
        require(local_origin(target) in device.get('http_origins', []), 'http_origin_forbidden', 'Local HTTP origin not authorized', 403)
        require(args.get('method', 'GET') in {'GET', 'POST'})
        require(isinstance(args.get('body', ''), str) and len(args.get('body', '').encode()) <= 16384)
        require(args.get('method', 'GET') == 'POST' or not args.get('body'))
        integer(args.get('timeout_ms', 15000), 100, 30000)
    if name == 'trigger':
        if args.get('action') == 'disarm':
            require(set(args) == {'action'}, message='Disarm accepts only action')
            return
        require(0 < len(binary(args.get('data'), 256)) <= 256)
        require(args.get('action') in {'mark', 'reset'})
        integer(args.get('delay_ms', 0), 0, 10000)
        integer(args.get('expires_ms'), 1, 120000)


def local_origin(url):
    require(isinstance(url, str))
    u = urlsplit(url)
    require(u.scheme == 'http' and not u.username and not u.password)
    try:
        ip = ipaddress.IPv4Address(u.hostname)
    except (ValueError, TypeError):
        raise Fault('http_origin_forbidden', 'Literal RFC1918 address required') from None
    require(any(ip in ipaddress.IPv4Network(net) for net in ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16']),
            'http_origin_forbidden', 'Only RFC1918 destinations are allowed')
    return f'http://{ip}' + (f':{u.port}' if u.port and u.port != 80 else '')
