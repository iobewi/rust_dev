"""Versioned campaign constraints. These never replace server/agent authority."""
from copy import deepcopy
import hashlib
import json


class CampaignManifest:
    def __init__(self, document):
        self._document = deepcopy(document)
        required = {'version','id','boards','write_regions','max_interruptions','backup_policy',
            'restore_policy','grant_seconds','operator_steps','start_criteria','close_criteria','private_evidence'}
        if not isinstance(document, dict) or set(document) != required or type(document['version']) is not int or document['version'] != 1:
            raise ValueError('Expected exact campaign manifest version 1 schema')
        for key in ['id','backup_policy','restore_policy','private_evidence']:
            if not isinstance(document[key], str) or not document[key].strip():
                raise ValueError('Manifest text fields must be nonempty')
        for key in ['operator_steps','start_criteria','close_criteria']:
            if not isinstance(document[key], list) or not document[key] or not all(isinstance(x,str) and x.strip() for x in document[key]):
                raise ValueError('Manifest criteria/steps must be nonempty lists of text')
        if type(document['max_interruptions']) is not int or not 0 <= document['max_interruptions'] <= 100:
            raise ValueError('Invalid interruption budget')
        if type(document['grant_seconds']) is not int or not 1 <= document['grant_seconds'] <= 14400:
            raise ValueError('Invalid recommended grant duration')
        boards = document['boards']
        if not isinstance(boards,list) or not boards:
            raise ValueError('Expected target boards')
        ids = set()
        for board in boards:
            if not isinstance(board,dict) or set(board) != {'id','chip','flash_size','transports'}:
                raise ValueError('Invalid board schema')
            if not all(isinstance(board[k],str) and board[k] for k in ['id','chip']) or board['id'] in ids:
                raise ValueError('Invalid or duplicate board identity')
            ids.add(board['id'])
            if type(board['flash_size']) is not int or not 4096 <= board['flash_size'] <= 64 * 1024 * 1024:
                raise ValueError('Invalid expected flash capacity')
            if not isinstance(board['transports'],list) or not board['transports'] or not set(board['transports']) <= {'external_uart','usb_serial_jtag','native_usb_other','unknown'}:
                raise ValueError('Invalid allowed transports')
        regions = document['write_regions']
        if not isinstance(regions,list):
            raise ValueError('Expected write regions, possibly empty')
        spans = {}
        for region in regions:
            if not isinstance(region,dict) or set(region) != {'board','address','size'} or region['board'] not in ids:
                raise ValueError('Invalid write region')
            start,size = region['address'],region['size']
            capacity = next(b['flash_size'] for b in boards if b['id'] == region['board'])
            if (type(start) is not int or type(size) is not int or start < 0 or size <= 0 or
                    start % 4096 or size % 4096 or start + size > capacity):
                raise ValueError('Write regions must contain whole flash sectors within capacity')
            previous = spans.setdefault(region['board'],[])
            if any(start < hi and lo < start + size for lo,hi in previous):
                raise ValueError('Overlapping manifest regions')
            previous.append((start,start+size))
        self.sha256 = hashlib.sha256(json.dumps(self.document,sort_keys=True,separators=(',',':')).encode()).hexdigest()

    @property
    def document(self):
        return deepcopy(self._document)

    def check_device(self, device):
        board = next((b for b in self.document['boards'] if b['id'] == device['id']), None)
        if (board is None or board['chip'] != device['chip'] or board['flash_size'] != device['flash_size'] or
                (device.get('provenance') or {}).get('transport','unknown') not in board['transports']):
            raise ValueError('Observed device does not match campaign manifest')

    def check_region(self, device_id, address, size):
        if type(address) is not int or type(size) is not int or address < 0 or size <= 0:
            raise ValueError('Invalid write region')
        lo,hi = address // 4096 * 4096, (address + size + 4095) // 4096 * 4096
        if not any(r['board'] == device_id and r['address'] <= lo and hi <= r['address']+r['size']
                   for r in self.document['write_regions']):
            raise ValueError('Write erase sectors exceed manifest permission')

    def check_command(self, board, name, args):
        if name == 'flash.erase' or name == 'flash.write' and args.get('erase') is True:
            self.check_region(board.device_id,0,board.client.device(board.device_id)['flash_size'])
        elif name == 'flash.write':
            inventory = {a['id']:a for a in board.client.artifacts()['artifacts']}
            for image in args.get('images',[]):
                if image.get('artifact') not in inventory:
                    raise ValueError('Artifact missing before manifest validation')
                self.check_region(board.device_id,image['address'],inventory[image['artifact']]['size'])
