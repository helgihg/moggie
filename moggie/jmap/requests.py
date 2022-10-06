# A collection of common requests
#
# FIXME: These should all be rewritten to actually use JMAP.
# FIXME: There should probably be sanity checks and validation etc.
#
import time
import random

from ..email.metadata import Metadata


def _req_id(req_id):
    if not req_id:
        req_id = '%f.%x' % (time.time(), random.randint(0, 0xffffffff))
    return req_id


class RequestBase(dict):
    def update(self, other, req_id=None):
        self['req_id'] = _req_id(req_id)
        dict.update(self, other)
        return self


class RequestPing(RequestBase):
    def __init__(self):
        self.update({'prototype': 'ping', 'ts': int(time.time())})


class RequestSearch(RequestBase):
    def __init__(self, context='', terms='', req_id=None):
        self.update({
            'prototype': 'search',
            'context': context,
            'terms': terms
        }, req_id=req_id)


class RequestCounts(RequestBase):
    def __init__(self, context='', terms_list=[], req_id=None):
        self.update({
            'prototype': 'counts',
            'context': context,
            'terms_list': terms_list
        }, req_id=req_id)


class RequestTag(RequestBase):
    def __init__(self, context='',
            tag_ops=[], tag_undo_id=None, tag_redo_id=None, undoable=True,
            req_id=None):
        self.update({
            'prototype': 'tag',
            'context': context,
            'undoable': undoable,
            'tag_undo_id': tag_undo_id,
            'tag_redo_id': tag_redo_id,
            'tag_ops': tag_ops
        }, req_id=req_id)


class RequestAddToIndex(RequestBase):
    def __init__(self,
            context='', search='', initial_tags=[], force=False,
            req_id=None):
        self.update({
            'prototype': 'add_to_index',
            'context': context,
            'search': search,
            'force': force,
            'tags': initial_tags
        }, req_id=req_id)


class RequestMailbox(RequestBase):
    def __init__(self, context='', mailbox='', limit=50, skip=0, req_id=None):
        self.update({
            'prototype': 'mailbox',
            'context': context,
            'mailbox': mailbox,
            'limit': limit,
            'skip': skip
        }, req_id=req_id)


class RequestEmail(RequestBase):
    def __init__(self,
            metadata=[], text=False, data=False, full_raw=False, parts=None,
            req_id=None):
        self.update({
            'prototype': 'email',
            'metadata': metadata[:Metadata.OFS_HEADERS],
            'text': text,
            'data': data,
            'parts': parts,
            'full_raw': full_raw,
        }, req_id=req_id)


class RequestContexts(RequestBase):
    def __init__(self, req_id=None):
        self.update({
            'prototype': 'contexts',
            # FIXME
        }, req_id=req_id)


class RequestConfigGet(RequestBase):
    def __init__(self,
            which=None,
            urls=False,
            access=False,
            accounts=False,
            identities=False,
            contexts=False,
            req_id=None):
        self.update({
            'prototype': 'config_get',
            'which': which,
            'urls': urls,
            'access': access,
            'identities': identities,
            'accounts': accounts,
            'contexts': contexts,
        }, req_id=req_id)


class RequestConfigSet(RequestBase):
    def __init__(self,
            new=None,
            section=None,
            updates=[],
            req_id=None):
        self.update({
            'prototype': 'config_set',
            'new': new,
            'section': section,
            'updates': updates
        }, req_id=req_id)


class RequestUnlock(RequestBase):
    def __init__(self, passphrase=None, req_id=None):
        self.update({
            'prototype': 'unlock',
            'passphrase': passphrase
        }, req_id=req_id)


class RequestChangePassphrase(RequestBase):
    def __init__(self,
            old_passphrase=None,
            new_passphrase=None,
            disconnect=False, req_id=None):
        self.update({
            'prototype': 'change_passphrase',
            'old_passphrase': old_passphrase,
            'new_passphrase': new_passphrase,
            'disconnect': disconnect
        }, req_id=req_id)


def to_jmap_request(_input):
    cls = {
         'tag': RequestTag,
         'ping': RequestPing,
         'email': RequestEmail,
         'counts': RequestCounts,
         'search': RequestSearch,
         'mailbox': RequestMailbox,
         'contexts': RequestContexts,
         'config_set': RequestConfigSet,
         'config_get': RequestConfigGet,
         'add_to_index': RequestAddToIndex,
         'unlock': RequestUnlock,
         'change_passphrase': RequestChangePassphrase,
         }.get(_input.get('prototype', ''))
    if cls:
        obj = cls()
        obj.update(_input)
        return obj
    raise KeyError('Unrecognized request: %s' % _input)
