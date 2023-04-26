import binascii
import base64
import copy
import hashlib
import logging
import math
import os
import re
import time
import threading
import traceback
import struct
from configparser import ConfigParser, NoOptionError, _UNSET
from logging.handlers import TimedRotatingFileHandler

try:
    from passcrow.client import PasscrowServerPolicy, PasscrowIdentityPolicy
    from passcrow.client import PasscrowClientPolicy, PasscrowClient
except ImportError:
    pass

from ..crypto.aes_utils import make_aes_key
from ..crypto.passphrases import stretch_with_scrypt, generate_passcode
from ..crypto.openpgp.keystore.registry import DEFAULT_LOCAL_KEYSTORES
from ..crypto.openpgp.keystore.registry import DEFAULT_KEYSTORES
from ..crypto.openpgp.sop import DEFAULT_SOP_CONFIG
from ..util.dumbcode import dumb_decode, dumb_encode_asc, to_json, from_json
from ..email.addresses import AddressInfo
from .helpers import cfg_bool, ListItemProxy, EncodingListItemProxy
from .helpers import DictItemProxy, ConfigSectionProxy


APPNAME    = 'moggie'  #'mailpile'
APPNAME_UC = 'Moggie'  #'Mailpile'
APPVER     = '0.0.1'   # => 1.0 when useful, 2.0 when Mailpile replacement
APPURL     = 'https://github.com/BjarniRunar/moggie'

LOGDIR     = '/tmp/moggie.%d' % os.getuid()

CACHE_VERSION = int(time.time() * 10) % (30 * 24 * 36000)


def configure_logging(
        worker_name=APPNAME,
        logdir=None,
        profile_dir=None,
        stdout=False,
        level=logging.DEBUG):
    global LOGDIR
    if profile_dir:
        logdir = os.path.join(profile_dir, 'logs')
    if logdir:
        LOGDIR = logdir
    if not os.path.exists(LOGDIR):
        os.mkdir(LOGDIR, 0o700)

    logfile = os.path.join(LOGDIR, worker_name)
    handlers = [TimedRotatingFileHandler(logfile,
        when='D', interval=1, backupCount=7)]
    if stdout:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        format='%(asctime)s.%(msecs)03d %(levelname)s: %(message)s',
        datefmt='%Y%m%d-%H%M%S',
        level=level,
        handlers=handlers,
        force=True)
    return logfile


class PasscrowConfig(ConfigSectionProxy):
    _KEYS = {
        'enabled': cfg_bool,
        'quick': cfg_bool,
        'env_override': cfg_bool}
    _EXTRA_KEYS = ['myself', 'others', 'servers']

    _DEFAULT_SERVERS = ['tel, mailto via passcrow.mailpile.is']
    _MOGGIE_HOME = '~moggie'
    _RECOVERY_VER = 'moggie-recovery-1.0'
    _RECOVERY_NAME = 'Moggie Settings'
    _RECOVERY_DESC = 'Moggie configuration recovery data'

    def __init__(self, *args, **kwarg):
        super().__init__(*args, **kwarg)
        if 'enabled' not in self:
            self.enabled = False
        if 'quick' not in self:
            self.quick = True
        if 'servers' not in self:
            self.servers.extend(self._DEFAULT_SERVERS)
        for opt in ('myself', 'others'):
            self.config.set_private(
                self.config_key, opt, save=False, delete=False)

    myself = property(lambda s: ListItemProxy(s.config, s.config_key, 'myself', delim=';'))
    others = property(lambda s: ListItemProxy(s.config, s.config_key, 'others', delim=';'))
    servers = property(lambda s: ListItemProxy(s.config, s.config_key, 'servers', delim=';'))

    def client(self):
        pc_dir = os.path.join(self.config.profile_dir, 'passcrow')
        client = PasscrowClient(
            config_dir=pc_dir,
            data_dir=pc_dir,
            env_override=False if (self.env_override is False) else True,
            create_dirs=True,
            logger=logging.info)
        if not client.default_policy.servers:
            # FIXME: This might be a place to invoke some sort of recovery
            #        server discovery mechanism. Or alternately, do we want
            #        to always use Mailpile's servers by default? Nah?
            client.default_policy.servers=[
                PasscrowServerPolicy().parse(srv)
                for srv in self.servers]
            client.save_default_policy()
        return client

    def policy(self, client=None, myself=True):
        dp = (client or self.client()).default_policy
        idps = [
            PasscrowIdentityPolicy().parse(idp, defaults=dp)
            for idp in (self.myself if myself else self.others)]
        for idp in idps:
            if not idp.usable:
                raise ValueError('Unusable identity policy: %s' % idp)
        return PasscrowClientPolicy(
            n=dp.n,
            m=dp.m,
            idps=idps,
            expiration_days=dp.expiration_days,
            timeout_minutes=dp.timeout_minutes)

    def protect_json(self):
        if not self.config.aes_key:
            raise PermissionError('Please unlock the app first!')
        _b64str = lambda d: str(base64.b64encode(d), 'utf-8')
        return to_json({
            'version': self._RECOVERY_VER,
            'description': self._RECOVERY_DESC,
            'aes_key': _b64str(self.config.aes_key),
            'config': _b64str(open(self.config.filepath, 'rb').read())})

    def protect(self, name=None, client=None, desc=None, policy=None, data=None):
        if not self.enabled:
            return False
        client = client or self.client()
        return client.protect(
            name or self._RECOVERY_NAME,
            data or self.protect_json(),
            policy or self.policy(client=client),
            pack_description=desc or self._RECOVERY_DESC,
            verify_description=name or self._RECOVERY_NAME,
            quick=self.quick)

    def request_codes(self, name=None):
        client = self.client()
        pack = client.pack(name or self._RECOVERY_NAME)
        # FIXME: Is the pack obsolete? Try anyway? Hmm.
        return client.verify(pack, quick=self.quick)

    def recover(self, codes, name=None):
        client = self.client()
        pack = client.pack(name or self._RECOVERY_NAME)
        return client.recover(pack, codes, quick=self.quick)

    # FIXME: Do we want to test the recovery settings?
    #        Maybe no need, if we are doing e-mail based verification
    #        and we can grab the e-mails from working configs?
    #        When do we prompt the user to switch this on?
    #        We need a process to re-up the recovery packs!


class AccessConfig(ConfigSectionProxy):
    _KEYS = {
        'name': str,
        #tokens = dict of token->expiration ts
        #roles = dict of context->role
        # These are optional
        'description': str,
        'default_context': str,
        'password': str,
        'username': str}
    _EXTRA_KEYS = ['roles', 'tokens']

    DEFAULT_TOKEN_TTL = 7 * 24 * 3600

    GRANT_ROLE = {
        'owner': ('A',          'Unlimited access'),
        'admin': ('aPpEeTtrwx', 'Context admin'),
        'user':  ('PpEeTtrwx',  'Normal user, can read/write e-mail and data'),
        'guest': ('rcp',        'Guest access, read-only')}

    GRANT_ALL          = 'A'  # Everything
    GRANT_ACCESS       = 'a'  # Add/remove access controls
    GRANT_FS           = 'F'  # Local files, including mailboxes
    GRANT_NETWORK      = 'N'  # Network resources; remote mailboxes
    GRANT_TAG_X        = 'T'  # Edit/add/remove tags.
    GRANT_TAG_RW       = 't'  # Tag/untag operations
    GRANT_CONTACT_WX   = 'P'  # Edit/add/remove contacts
    GRANT_CONTACT_R    = 'p'  # View contacts
    GRANT_CALENDAR_WX  = 'E'  # Edit/add/remove calendar events
    GRANT_CALENDAR_R   = 'e'  # View calendar events
    GRANT_SEND         = 'x'  # Send messages
    GRANT_COMPOSE      = 'w'  # Compose messages
    GRANT_READ         = 'r'  # Read messages

    def __init__(self, *args, **kwarg):
        super().__init__(*args, **kwarg)
        self._role_dict = DictItemProxy(self.config, self.config_key, 'roles')
        self._token_dict = DictItemProxy(self.config, self.config_key, 'tokens')
        self._live_token = None

    roles = property(lambda self: self._role_dict)
    tokens = property(lambda self: self._token_dict)

    def expire_tokens(self):
        now = time.time()
        expired = [t for t, c in self.tokens.items()
            if int(c) and (int(c) < now)]
        for token in expired:
            del self.tokens[token]

    def new_token(self, ttl=None):
        # Tokens: 80 bits of entropy, encoded using base32
        token = str(base64.b32encode(os.urandom(10)), 'latin-1')
        ttl = ttl or self.DEFAULT_TOKEN_TTL
        self.tokens[token] = int(time.time()) + ttl
        return token

    def get_fresh_token(self):
        with self.config:
            tokens = self.tokens.items()
            if tokens:
                exp, tok = max((int(a), t) for t, a in tokens)
                if exp < time.time() + (self.DEFAULT_TOKEN_TTL/2):
                    tok = self.new_token()
            else:
                tok = self.new_token()
            return tok, int(self.tokens[tok])

    def get_default_context(self):
        if self.default_context:
            return self.default_context
        for ctx, role in sorted(list(self.roles.items())):
            return ctx

    def make_signature(self, *data, token=None):
        sig = hashlib.sha1(bytes(token or self._live_token, 'utf-8'))
        for d in data:
            sig.update(bytes(d, 'utf-8'))
        return sig.hexdigest()

    def check_signature(self, sig, *data):
        for t in self.tokens:
            tsig = self.make_signature(*data, token=t)
            if sig == tsig:
                logging.debug('OK: %s == %s' % (sig, tsig))
                return t
            logging.debug('BAD: %s != %s' % (sig, tsig))
        return False

    def grants(self, context, roles):
        with self.config:
            role = self.roles.get(context, None)
            ctx = self.config.contexts.get(context)

            if role is None or ctx is None:
                return None
            if self.GRANT_ALL not in role:
                for rc in roles:
                    if rc not in role:
                        return False

            scope_search = ' '.join('+in:%s' % t.lower() for t in ctx.tags)[1:]
            if ctx.scope_search:
                scope_search += ' ' + ctx.scope_search
                if scope_search.startswith(' -'):
                    scope_search = 'all:mail' + scope_search

            self.config.do_not_save()
            return (role, ctx.tag_namespace, scope_search.strip())


class AccountConfig(ConfigSectionProxy):
    ACCOUNT_TAGS = ['inbox', 'spam', 'trash']
    OUTGOING_TAGS = ['outbox', 'sent']
    _KEYS = {
        'name': str,
        #addresses = list of e-mails
        'mailbox_proto': str,    # none, imap, imaps, jmap, pop3, pop3s, files
        'mailbox_config': str,   # move or read or copy or sync?
        'sendmail_proto': str,   # none, smtp, jmap, imap, imaps, proc
        # Optional...
        'mailbox_server': str,
        'mailbox_username': str,  # unset=no auth
        'mailbox_password': str,  # unset=no pass
        'mailbox_inbox': str,     # Which "mailbox" is the inbox?
        'mailbox_sent': str,
        'mailbox_spam': str,
        'mailbox_trash': str,
# So what about other mailboxes?
#
# If they've been "configured", they should appear in the UK under "All Mail".
# If unconfigured, they should be findable using a browsing UI.
#
        'sendmail_username': str,  # unset=no auth, special: ==mailbox_username
        'sendmail_password': str,  # unset=no pass, special: ==mailbox_password
        'description': str}
    _EXTRA_KEYS = ['addresses', 'watched', 'archives']

    def __init__(self, *args, **kwarg):
        super().__init__(*args, **kwarg)
        self._watched = EncodingListItemProxy(self.config, self.config_key, 'watched')
        self._archives = EncodingListItemProxy(self.config, self.config_key, 'archives')
        self._addresses = EncodingListItemProxy(self.config, self.config_key, 'addresses')

    watched = property(lambda self: self._watched)
    archives = property(lambda self: self._archives)
    addresses = property(lambda self: self._addresses)

    def get_tags(self):
        tags = []
        if self.mailbox_proto and self.mailbox_config:
            tags += self.ACCOUNT_TAGS
        if self.sendmail_proto:
            tags += self.OUTGOING_TAGS
        return tags


class ContextConfig(ConfigSectionProxy):
    _KEYS = {
        'name': str,
        'description': str,
        'default_identity': str,
        # Optional...
        'scope_search': str,
        'tag_namespace': str,
        'openpgp_sop_client': str,
        'openpgp_key_sources': str}
    _EXTRA_KEYS = [
        'identities', 'tags', 'extra_tags', 'flags', 'accounts', 'secrets']

    def __init__(self, *args, **kwarg):
        super().__init__(*args, **kwarg)
        self._ids_list = ListItemProxy(self.config, self.config_key, 'identities')
        self._tags_list = EncodingListItemProxy(self.config, self.config_key, 'tags')
        self._etags_list = EncodingListItemProxy(self.config, self.config_key, 'extra_tags')
        self._flags_list = ListItemProxy(self.config, self.config_key, 'flags')
        self._accts_list = ListItemProxy(self.config, self.config_key, 'accounts')
        self._secrets = DictItemProxy(self.config, self.config_key, 'secrets')

    tags = property(lambda self: self._tags_list)
    extra_tags = property(lambda self: self._etags_list)
    flags = property(lambda self: self._flags_list)
    identities = property(lambda self: self._ids_list)
    accounts = property(lambda self: self._accts_list)
    secrets = property(lambda self: self._secrets)

    def _accounts(self):
        return [
            (a, AccountConfig(self.config, a))
            for a in self.accounts if a]

    def _extra_tags(self, accounts=None):
        etags = []
        etags.extend(self._etags_list)
        accounts = self._accounts() if (accounts is None) else accounts
        for akey, acct in accounts:
            etags.extend(acct.get_tags())
        return set(etags)

    def _volatile(self, what):
        vol_id = '%s/%s' % (self.config_key, what)
        if vol_id not in self.config.volatile:
            self.config.volatile[vol_id] = {}
        return self.config.volatile[vol_id]

    def set_secret(self, key, secret, ttl=None):
        key_id = self.get_secret(key, _get_key=True)
        if secret is None or ttl:
            try:
                del self.secrets[key_id]
            except KeyError:
                pass

        if secret is None:
            pass
        elif ttl:
            exp = ttl + int(time.time())
            self._volatile('secrets')[key_id] = (exp, dumb_encode_asc(
                [key, secret],
                aes_key_iv=self.config._aes_key_iv()))
        else:
            self.secrets[key_id] = dumb_encode_asc(
                [key, secret],
                aes_key_iv=self.config._aes_key_iv())

    def get_secret(self, key, _get_key=False):
        key_id = hashlib.sha1(bytes(key, 'utf-8')).hexdigest()
        key_len = 4
        def items():
            now = time.time()
            expired = []
            volatile = self._volatile('secrets')
            for k, (exp, v) in volatile.items():
                if exp > now:
                    yield k, v
                else:
                    expired.append(k)
            for k in expired:
                del volatile[k]
            for k, v in self.secrets.items():
                yield k, v

        for k, v in items():
            if key_id.startswith(k):
                key_len = len(k) + 1
                decoded = dumb_decode(v, aes_key=self.config.aes_key)
                if decoded[0] == key:
                    return k if _get_key else decoded[1]
        return key_id[:key_len] if _get_key else None

    def get_openpgp_settings(self):
        _parent = lambda v: self.config.get(self.config.GENERAL, v)
        keys_cfg = self.openpgp_key_sources or _parent('openpgp_key_sources')
        sop_cfg = self.openpgp_sop_client or _parent('openpgp_sop_client')
        return keys_cfg, sop_cfg

    def as_dict(self, deep=True):
        if not deep:
            return super().as_dict()

        accounts = self._accounts()
        tags = set(t.lower() for t in self.tags if t)
        etags = self._extra_tags(accounts=accounts)

        keys, sopc = self.get_openpgp_settings()
        return {
            'name': self.name,
            'description': self.description,
            'tag_namespace': self.tag_namespace,
            'scope_search': self.scope_search,
            'accounts': dict((k, a.as_dict()) for k, a in sorted(accounts)),
            'identities': dict(
                 (i, IdentityConfig(self.config, i).as_dict())
                 for i in self.identities if i),
            'tags': list(tags),
            'extra_tags': list(etags),
            'key': self.config_key}


class IdentityConfig(ConfigSectionProxy):
    _KEYS = {
        'name': str,
        'address': str}

    def as_address_info(self):
        return AddressInfo(address=self.address, fn=self.name)


class AppConfig(ConfigParser):

    GENERAL = 'App'
    SECRETS = 'Secrets'
    PASSCROW = 'Passcrow Recovery'
    SMTP_BRIDGE_SVC = 'SMTP Bridge Service'
    ACCESS_PREFIX = 'Access '
    ACCOUNT_PREFIX = 'Account '
    IDENTITY_PREFIX = 'Identity '
    CONTEXT_PREFIX = 'Context '

    ACCESS_ZERO = 'Access 0'
    CONTEXT_ZERO = 'Context 0'

    STANDARD_ATTRIBUTE_TAGS = (
       'Unread', 'Replied', 'Forwarded', 'Flagged')

    STANDARD_CONTAINER_TAGS = (
       'Inbox', 'Drafts', 'Outbox', 'Sent', 'Spam', 'Trash')

    # These are settings which have top-level settings/defaults, but
    # can be overridden on a per-context basis.
    PREF_YES = 'yes'
    PREF_NO  = 'no'
    PREF_ASK = 'ask'
    PREF_BOOL = (PREF_YES, PREF_NO)
    PREF_BASK = (PREF_YES, PREF_NO, PREF_ASK)
    PREFERENCES = [
        ('display_html',               PREF_YES, PREF_BOOL),
        ('display_html_target_blank',  PREF_YES, PREF_BOOL),
        ('display_html_inline_images', PREF_YES, PREF_BASK),
        ('display_html_remote_images', PREF_ASK, PREF_BASK)]

    INITIAL_SETTINGS = [
       (GENERAL, 'config_backups', '10'),
       (GENERAL, 'default_cli_context', 'Context 0'),
       (GENERAL, 'openpgp_sop_client', DEFAULT_SOP_CONFIG),
       (GENERAL, 'openpgp_key_sources', DEFAULT_KEYSTORES),
       (GENERAL, 'log_level', '40')]

    PREAMBLE = """\
# This file was auto-generated by %s v%s.
#
# NOTE: Do not edit this file while %s is running!
#       Also note that if you do edit it by hand, comments will be lost and
#       sections may get reordered when the app next saves its settings.
#
# To check whether the app is running: python3 -m moggie status
#
#############################################################################

""" % (APPNAME_UC, APPVER, APPNAME_UC)

    DIGIT_RE = re.compile('\d')

    ALLOWED_SECTIONS = [GENERAL, SECRETS, PASSCROW]
    ALLOWED_SECTION_PREFIXES = [
        ACCESS_PREFIX,
        ACCOUNT_PREFIX,
        IDENTITY_PREFIX,
        CONTEXT_PREFIX]

    def __init__(self, profile_dir):
        self.lock = threading.RLock()
        self.suppress_saves = []

        global LOGDIR
        LOGDIR = os.path.join(profile_dir, 'logs')

        self.profile_dir = profile_dir
        self.filepath = os.path.join(profile_dir, 'config.rc')
        self.backups = os.path.join(profile_dir, 'backups')
        super().__init__(
            delimiters=('=',),
            comment_prefixes=('#',),
            strict=True,
            interpolation=None)

        self.aes_key = None
        self.iv = (
            list(struct.unpack('II', os.urandom(8))) + [int(time.time())] + [0])
        self.keep_private = {
            self.SECRETS + '/config_key',
            self.SECRETS + '/master_key',
            self.SECRETS + '/master_key_N',
            self.ACCOUNT_PREFIX+'N/mailbox_password',
            self.ACCOUNT_PREFIX+'N/sendmail_password'}

        # Ad-hoc storage for data which doesn't live forever
        self.volatile = {}

        self.read(self.filepath)
        with self:
            for sec, opt, val in self.INITIAL_SETTINGS:
                if sec not in self:
                    self.add_section(sec)
                if opt not in self._sections[sec]:
                    self.set(sec, opt, val, save=False)

            try:
                self.last_rotate = os.path.getmtime(self.filepath)
            except OSError:
                self.last_rotate = 0

            self._caches = {}
            if 'passphrase' in self[self.SECRETS]:
                # This is the insecure self-auto-unlock mode: start this way?
                try:
                    self.provide_passphrase(self[self.SECRETS]['passphrase'])
                    if 'master_key' not in self[self.SECRETS]:
                        self.generate_master_key()
                except PermissionError:
                    pass
            self.context_zero()
            self.access_zero()

    all_access = property(lambda self:
        dict((a, AccessConfig(self, a))
            for a in self if a.startswith(self.ACCESS_PREFIX)))

    accounts = property(lambda self:
        dict((a, AccountConfig(self, a))
            for a in self if a.startswith(self.ACCOUNT_PREFIX)))

    identities = property(lambda self:
        dict((a, IdentityConfig(self, a))
            for a in self if a.startswith(self.IDENTITY_PREFIX)))

    contexts = property(lambda self:
        dict((p, ContextConfig(self, p))
            for p in self if p.startswith(self.CONTEXT_PREFIX)))

    passcrow = property(lambda self: PasscrowConfig(self, self.PASSCROW))

    def get_preferences(self, context=None, which=None):
        source = []
        preferences = {}
        for pref, default, valid in self.PREFERENCES:
            if which and pref not in which:
                next
            preferences[pref] = self.get(self.GENERAL, pref, fallback=default)
            if context:
                cpref = self.get(context, pref, fallback=None)
                if cpref is not None:
                    source.append(context)
                    preferences[pref] = cpref
            if preferences[pref] not in valid:
                preferences[pref] = default
        preferences['_source'] = ''.join(set(source or ['global']))
        return preferences

    def __enter__(self, *args, **kwargs):
        self.lock.acquire()
        self.suppress_saves.append(0)
        return self

    def __exit__(self, *args, **kwargs):
        if self.suppress_saves.pop(-1):
            self.save()
        self.lock.release()

    def do_not_save(self):
        if self.suppress_saves:
            self.suppress_saves[-1] = 0

    def access_zero(self):
        with self:
            azero = self.ACCESS_ZERO
            roles = ', '.join([
                '%s:%s' % (p, AccessConfig.GRANT_ALL)
                for p in self if p.startswith(self.CONTEXT_PREFIX)])
            self[azero].update({
                'name': 'Local access',
                'roles': roles})
            self.do_not_save()
            return AccessConfig(self, azero)

    def context_zero(self):
        with self:
            czero = self.CONTEXT_ZERO
            self[czero].update({'name': 'My Mail'})
            self.do_not_save()
            return ContextConfig(self, czero)

    def get_context(self, which):
        if which.startswith(self.CONTEXT_PREFIX):
            if which in self:
                return ContextConfig(self, which)
        return None

    def get_account(self, which):
        if which.startswith(self.ACCOUNT_PREFIX):
            if which in self:
                return AccountConfig(self, which)
        for p in self:
            if p.startswith(self.ACCOUNT_PREFIX):
                acct = AccountConfig(self, p)
                if which == acct.name or which in acct.addresses:
                    return acct
        return None

    def access_from_token(self, token, _raise=True):
        with self:
            if 'tokens' not in self._caches:
                token_cache = {}
                for acl in self.all_access.values():
                    acl.expire_tokens()
                    for t in acl.tokens:
                        token_cache[t] = acl
                self._caches['tokens'] = token_cache
            acl = self._caches.get('tokens', {}).get(token)
        if acl is not None:
            acl = copy.copy(acl)
            acl._live_token = token
            return acl
        if _raise:
            raise PermissionError('No access granted')
        return None

    def access_from_user(self, username, password):
        #FIXME
        raise PermissionError('No access granted')

    def rotate(self):
        now = time.time()
        if not os.path.exists(self.filepath):
            return
        if not os.path.exists(self.backups):
            os.mkdir(self.backups, 0o700)

        exp = 2
        count = int(self.get(self.GENERAL, 'config_backups', fallback=5))
        fudge = 300
        last_min_age = 0
        for i in reversed(range(0, count+1)):
            dest = os.path.join(self.backups, 'config.rc.%2.2d' % (i+1,))
            if i > 0:
                src = os.path.join(self.backups, 'config.rc.%2.2d' % (i,))
            else:
                src = self.filepath
            if os.path.exists(src):
                min_age = min(last_min_age + 24*3600, int(fudge * (exp**i)))
                if os.path.exists(dest):
                   if now - os.path.getmtime(dest) > min_age:
                       os.remove(dest)
                if not os.path.exists(dest):
                    os.rename(src, dest)
                last_min_age = min_age

        self.last_rotate = now

    def save(self):
        if self.suppress_saves:
            self.suppress_saves[-1] += 1
            return

        # A save means something changed
        global CACHE_VERSION
        CACHE_VERSION += 1
        self._caches = {}

        sections = list(self.keys())
        sections.sort(key=lambda k: (
            self.ALLOWED_SECTIONS.index(k)
            if k in self.ALLOWED_SECTIONS else 99+len(k)))

        reordered = {}
        for section in sections:
            if len(self[section]) == 0:
                self.remove_section(section)
            else:
                reordered[section] = self._sections[section]
        self._sections = reordered

        self.rotate()
        with open(self.filepath, 'w') as fd:
            fd.write(self.PREAMBLE)
            self.write(fd)
        os.chmod(self.filepath, 0o600)
        #logging.debug('Saved config(%s):\n%s' % (
        #    self.filepath,
        #    ''.join(traceback.format_stack()[-5:-1])))

    def temp_aes_key(config, temp_key):
        old_key = config.aes_key
        class ctx:
            def __enter__(self, *args):
                config.aes_key = temp_key
            def __exit__(self, *args):
                config.aes_key = old_key
        return ctx()

    def key_desc(self, section, option):
        return re.sub(self.DIGIT_RE, 'N', section+'/'+option)

    def provide_passphrase(self, passphrase, contacts=None):
        # FIXME: We want to start encrypting from the start and we will
        #        incrementally ask the user to ratchet up their security
        #        posture, rotating keys as we do so. So this needs to
        #        change! Also, we have Passcrow now.
        pass_key = make_aes_key(
            stretch_with_scrypt(bytes(passphrase, 'utf-8'), b'config'))

        is_new = False
        config_key = None
        with self.temp_aes_key(pass_key):
            if 'config_key' not in self[self.SECRETS]:
                config_key = 'CONF_KEY:%s' % generate_passcode()
                self.set_private(self.SECRETS, 'config_key', config_key)
                is_new = True
            try:
                config_key = self[self.SECRETS]['config_key']
                if not config_key.startswith('CONF_KEY:'):
                    raise PermissionError('Incorrect Passphrase')
            except (UnicodeDecodeError, binascii.Error):
                raise PermissionError('Incorrect Passphrase')

        if config_key is not None:
            aes_key = make_aes_key(bytes(config_key, 'latin-1'))
            if (self.aes_key is not None) and self.aes_key != aes_key:
                raise PermissionError('Oh dear, we already have an AES key')
            self.aes_key = aes_key

    has_crypto_enabled = property(lambda s: ('master_key' in s[s.SECRETS]))

    def generate_master_key(self, suffix=''):
        if self.aes_key is None:
            raise PermissionError('Refusing to set a master key without a passphrase')
        mk_key = 'master_key' + suffix
        if mk_key in self[self.SECRETS]:
            raise PermissionError('Cravenly refusing to overwrite master key')
        self.set_private(self.SECRETS, mk_key, generate_passcode())
        # Record this, in case we want to auto-rotate keys now and then?
        self[self.SECRETS]['last_key_rotation'] = '%d' % int(time.time())

    def change_master_key(self):
        for suffix in ('_%d' % i for i in range(1, 1000)):
            if 'master_key'+suffix not in self[self.SECRETS]:
                self.generate_master_key(suffix)
                return True
        return False

    def change_config_key(self, new_passphrase):
        with self:
            old_aes_key = self.aes_key
            self.aes_key = None
            if 'config_key' in self[self.SECRETS]:
                del self[self.SECRETS]['config_key']
            self.provide_passphrase(new_passphrase)
            if 'passphrase' in self[self.SECRETS]:
                if new_passphrase != self[self.SECRETS]['passphrase']:
                    del self[self.SECRETS]['passphrase']

            if old_aes_key is None:
                return

            for section in self:
                for option in self[section]:
                    if section == self.SECRETS and option == 'config_key':
                        continue
                    val = super().get(section, option)
                    if isinstance(val, str) and val[:2] == '::':
                        with self.temp_aes_key(old_aes_key):
                            val = self.get(section, option)
                        self.set_private(section, option, val)

    def get_aes_keys(self):
        keys = [self.get(self.SECRETS, 'master_key', fallback=None)]
        if keys[0] is None:
            raise KeyError('Master key is unset')
        for N in range(1, 1000):
            mkN = self.get(self.SECRETS, 'master_key_%d' % N, fallback=None)
            if mkN is None:
                break
            keys.append(mkN)
        return [bytes(k, 'latin-1') for k in keys]

    def allowed_section(self, section):
        if section in self.ALLOWED_SECTIONS:
            return True
        for prefix in self.ALLOWED_SECTION_PREFIXES:
            if section.startswith(prefix):
                return True
        return False

    def _aes_key_iv(self):
        if self.aes_key is None:
            raise PermissionError('AES key is not set')
        self.iv[-1] += 1
        return (self.aes_key, struct.pack('IIII', *self.iv))

    def __getitem__(self, section):
        if not self.has_section(section):
            if self.allowed_section(section):
                self.add_section(section)
        return super().__getitem__(section)

    def get(self, section, option, *,
            raw=False, vars=None, fallback=_UNSET, permerror=False):
        if not self.has_section(section):
            if self.allowed_section(section):
                self.add_section(section)
        val = super().get(section, option, raw=raw, vars=vars, fallback=fallback)
        if isinstance(val, str) and val[:2] == '::':
            if permerror and not self.aes_key:
                raise PermissionError('AES key is not set')
            val = dumb_decode(val[2:], aes_key=self.aes_key)
        return val

    def set(self, section, option, value=None, save=True, delete=True):
        if not self.has_section(section):
            if self.allowed_section(section):
                self.add_section(section)

        if self.key_desc(section, option) in self.keep_private:
            return self.set_private(section, option,
                value=value, save=save, delete=delete)

        if value is not None:
            encoded = dumb_encode_asc(value)
            if encoded[:1] != 'U':
                value = '::' + encoded
            super().set(section, option, value=value)
        elif delete and option in self[section]:
            del self[section][option]
        if save:
            self.save()

    def _write_section(self, fp, section_name, section_items, delimiter):
        def sort_key(k):
            return (['%8.8d' % int(p) for p in k[0].split('.')
                         if self.DIGIT_RE.match(p)]
                + ['00000000', ('%4.4d' % len(k[0]))] + list(k))
        section_items = sorted(list(section_items), key=sort_key)
        return super()._write_section(fp, section_name, section_items, delimiter)

    def set_private(self, section, option, value=None, save=True, delete=True):
        if self.key_desc(section, option) not in self.keep_private:
            self.keep_private.add(self.key_desc(section, option))
        if value is not None:
            encoded = '::' + dumb_encode_asc(value, aes_key_iv=self._aes_key_iv())
            super().set(section, option, value=encoded)
        elif delete and option in self[section]:
            del self[section][option]
        if save:
            self.save()



