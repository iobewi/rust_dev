"""Versioned, transport-independent hardware command validation."""
import base64
import binascii
import json
import math
import re
import ipaddress
from urllib.parse import urlsplit

VERSION = 1
MAX_ARTIFACT = 16 * 1024 * 1024
MAX_UART = 16384
# Experimental ESP32-P4 USB OTG diagnostic: loads a reviewed diagnostic stub into RAM and reads flash
# with it. It is NOT a flash read: it needs the programming scope, the programming consent AND its own
# consent bound to the board, the stub SHA-256 and the region. First version, fixed parameters.
P4_TRACE = 'diagnostic.p4_usb_trace'
P4_TRACE_CHIP = 'esp32p4'
P4_TRACE_ADDRESS, P4_TRACE_SIZE, P4_TRACE_PACKET = 0x8000, 1024 * 1024, 4096
P4_TRACE_CPU_HZ = 200_000_000   # assumed from the stub code, NOT measured: recorded with the operation
P4_STUB_MAX = 64 * 1024
P4_STUB_IRAM, P4_STUB_DRAM = (0x4FF50000, 0x4500), (0x4FF60000, 0x18000)
P4_STUB_KEYS = {'entry', 'text', 'text_start', 'data', 'data_start', 'bss_start'}
CAPABILITIES = {'identify', 'reset', 'partitions', 'flash.read', 'flash.write', 'flash.erase',
                'uart.write', 'uart.configure', 'console', 'io0', 'boot.download', 'mark', 'trigger', 'http.request',
                P4_TRACE}
PROGRAM = {'flash.write', 'flash.erase', P4_TRACE}
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


def strict_equal(left, right):
    """Equality that never confuses 1 with True or 1.0 with 1 (consents are compared exactly)."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(strict_equal(left[k], right[k]) for k in left)
    return left == right


def p4_trace_consent(device_id, stub_sha256):
    """The only consent that authorizes the diagnostic: specific to ONE board, ONE stub and ONE region."""
    return {'scenario': P4_TRACE, 'device': device_id, 'stub_sha256': stub_sha256,
            'address': P4_TRACE_ADDRESS, 'size': P4_TRACE_SIZE,
            'ram_code_execution': True, 'session_effects_acknowledged': True, 'rom_reset_may_be_required': True}


def validate_p4_stub_json(data):
    """The loader JSON of the diagnostic stub: exact keys, integer addresses, RAM windows of the ESP32-P4 stub."""
    try:
        stub = json.loads(data)
    except (ValueError, TypeError):
        raise Fault('diagnostic_stub_invalid', 'The stub artifact is not JSON') from None
    require(isinstance(stub, dict) and stub.keys() == P4_STUB_KEYS, 'diagnostic_stub_invalid', 'Unexpected stub fields')
    for key in ('entry', 'text_start', 'data_start', 'bss_start'):
        require(type(stub[key]) is int and 0 <= stub[key] < 2 ** 32, 'diagnostic_stub_invalid', 'Bad stub address')
    try:
        text, blob = base64.b64decode(stub['text'], validate=True), base64.b64decode(stub['data'], validate=True)
    except (ValueError, binascii.Error, TypeError):
        raise Fault('diagnostic_stub_invalid', 'Stub segments are not base64') from None
    inside = lambda window, start, length: window[0] <= start and start + length <= window[0] + window[1]  # noqa: E731
    require(text and inside(P4_STUB_IRAM, stub['text_start'], len(text)), 'diagnostic_stub_invalid', 'Stub text outside IRAM')
    require(blob and inside(P4_STUB_DRAM, stub['data_start'], len(blob)), 'diagnostic_stub_invalid', 'Stub data outside DRAM')
    require(P4_STUB_DRAM[0] <= stub['bss_start'] < P4_STUB_DRAM[0] + P4_STUB_DRAM[1], 'diagnostic_stub_invalid', 'Stub bss outside DRAM')
    require(stub['text_start'] <= stub['entry'] < stub['text_start'] + len(text), 'diagnostic_stub_invalid', 'Entry outside text')


def validate_p4_usb_trace(args, device, artifacts, allowlist=frozenset()):
    require(device['chip'] == P4_TRACE_CHIP, 'diagnostic_chip_unsupported', 'Only the ESP32-P4 is supported')
    require((device.get('provenance') or {}).get('transport') == 'native_usb_other', 'diagnostic_transport_unsupported',
            'The diagnostic needs the native USB OTG port')
    for key, value in (('address', P4_TRACE_ADDRESS), ('size', P4_TRACE_SIZE), ('packet_size', P4_TRACE_PACKET),
                       ('cpu_hz', P4_TRACE_CPU_HZ)):
        require(type(args.get(key)) is int and args[key] == value, 'diagnostic_parameter_not_allowed',
                f'{key} is fixed to {value} in this version')
    sha = args.get('stub_sha256')
    require(isinstance(sha, str) and re.fullmatch(r'[0-9a-f]{64}', sha), 'diagnostic_stub_sha256_invalid',
            'A full lowercase SHA-256 of the stub is required')
    key = args.get('stub_artifact')
    artifact = artifacts.get(key) if isinstance(key, str) else None
    require(artifact is not None, 'artifact_missing', 'Upload the diagnostic stub first')
    require(artifact['sha256'] == sha, 'digest_mismatch', 'SHA-256 mismatch')
    # Fail closed: only a stub whose SHA-256 the qualification configuration lists may ever be executed.
    require(sha in allowlist, 'diagnostic_stub_not_qualified', 'This stub SHA-256 is not in the qualified list', 403)
    require(1 <= artifact['size'] <= P4_STUB_MAX, 'diagnostic_stub_invalid', 'Unexpected stub size')
    validate_p4_stub_json(artifact['data'])
    require(strict_equal(args.get('consent'), p4_trace_consent(device['id'], sha)), 'diagnostic_consent_invalid',
            'The consent must name this board, this stub SHA-256, the region and the RAM execution', 403)


def region(address, size, capacity):
    integer(address, 0, capacity - 1)
    integer(size, 1, min(MAX_ARTIFACT, capacity))
    require(address + size <= capacity, 'flash_bounds', 'Region exceeds flash capacity')


def validate_command(name, args, device, artifacts, p4_stub_allowlist=frozenset()):
    require(name in CAPABILITIES and name in device['capabilities'], 'unsupported', 'Unsupported command')
    require(isinstance(args, dict))
    schemas = {
        'identify': set(), 'reset': set(), 'console': {'return_console'},
        'partitions': {'address', 'return_console'}, 'flash.read': {'address', 'size', 'return_console', 'reader', 'packet_size'},
        'flash.write': {'images', 'chip', 'erase', 'return_console'}, 'flash.erase': {'chip', 'return_console'},
        'uart.write': {'data'}, 'uart.configure': {'baudrate'}, 'io0': {'duration_ms'}, 'boot.download': {'hold_ms'},
        'http.request': {'url', 'method', 'body', 'timeout_ms'},
        'mark': {'label'}, 'trigger': {'data', 'action', 'delay_ms', 'expires_ms'},
        P4_TRACE: {'chip', 'stub_artifact', 'stub_sha256', 'address', 'size', 'packet_size', 'cpu_hz', 'consent'},
    }
    require(not (args.keys() - schemas[name]), message='Unknown command fields')
    for key in ('erase', 'return_console'):
        if key in args:
            require(type(args[key]) is bool)
    capacity = device['flash_size']
    if name in {'flash.read', 'partitions', 'flash.write', 'flash.erase'}:
        require(isinstance(capacity, int) and not isinstance(capacity, bool), 'flash_capacity_unknown',
                'This session does not know the flash capacity')
    if name == 'flash.read':
        reader = args.get('reader', 'reference')
        require(isinstance(reader, str) and reader in {'reference', 'preallocated', 'packet-diagnostic', 'md5-only'},
                'invalid_flash_reader', 'Unknown flash reader')
        if reader == 'packet-diagnostic':
            require(type(args.get('packet_size')) is int and args['packet_size'] in {1024, 2048, 4096},
                    'invalid_flash_packet_size', 'Diagnostic packet size must be 1024, 2048 or 4096')
        else:
            require('packet_size' not in args,
                    'invalid_flash_packet_size', 'Packet size can only be set in diagnostic mode')
        region(args.get('address'), args.get('size'), capacity)
        if reader == 'md5-only':
            require(args['size'] <= 1024 * 1024, 'invalid_flash_md5_size',
                    'MD5-only diagnostics are limited to 1 MiB per operation')
    if name == 'partitions':
        region(args.get('address', 0x8000), 0xc00, capacity)
    if name in PROGRAM:
        require(isinstance(args.get('chip'), str) and args['chip'] == device['chip'],
                'chip_mismatch', 'Expected chip family must match the device')
    if name == P4_TRACE:
        validate_p4_usb_trace(args, device, artifacts, p4_stub_allowlist)
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
