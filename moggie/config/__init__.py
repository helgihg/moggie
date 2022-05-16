import binascii
import base64
import json
import logging
import math
import os
import re
import time
import threading
import struct
from configparser import ConfigParser, NoOptionError, _UNSET
from logging.handlers import TimedRotatingFileHandler

from ..crypto.aes_utils import make_aes_key
from ..crypto.passphrases import stretch_with_scrypt, generate_passcode
from ..util.dumbcode import dumb_decode, dumb_encode_asc
from .helpers import ListItemProxy, DictItemProxy, ConfigSectionProxy


APPNAME    = 'moggie'  #'mailpile'
APPNAME_UC = 'Moggie'  #'Mailpile'
APPVER     = '2.0.0'
APPURL     = 'https://github.com/BjarniRunar/moggie'

LOGDIR     = '/tmp/moggie.%d' % os.getuid()


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


class AccessConfig(ConfigSectionProxy):
    _KEYS = {
        'name': str,
        #tokens = dict of token->creation ts
        #roles = dict of context->role
        # These are optional
        'description': str,
        'password': str,
        'username': str}

    MAX_TOKEN_AGE = 7 * 24 * 3600  #FIXME: is this sane?

    def __init__(self, *args, **kwarg):
        super().__init__(*args, **kwarg)
        self._role_dict = DictItemProxy(self.config, self.config_key, 'roles')
        self._token_dict = DictItemProxy(self.config, self.config_key, 'tokens')

    roles = property(lambda self: self._role_dict)
    tokens = property(lambda self: self._token_dict)

    def expire_tokens(self, max_age=MAX_TOKEN_AGE):
        oldest = time.time() - max_age
        expired = [t for t, c in self.tokens.items()
            if int(c) and (int(c) < oldest)]
        for token in expired:
            del self.tokens[token]

    def new_token(self):
        # Tokens: 80 bits of entropy, encoded using base32
        token = str(base64.b32encode(os.urandom(10)), 'latin-1')
        self.tokens[token] = int(time.time())
        return token

    def get_fresh_token(self):
        age, tok = max((int(a), t) for t, a in self.tokens.items())
        exp = age + self.MAX_TOKEN_AGE
        if exp < time.time() + (self.MAX_TOKEN_AGE/2):
            tok = self.new_token()
        return tok, int(self.tokens[tok])


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
        'tag_namespace': str,
        'tag_required': str,
        'flags': str}

    def __init__(self, *args, **kwarg):
        super().__init__(*args, **kwarg)
        self._ids_list = ListItemProxy(self.config, self.config_key, 'identities')
        self._tags_list = ListItemProxy(self.config, self.config_key, 'tags')
        self._flags_list = ListItemProxy(self.config, self.config_key, 'flags')
        self._accts_list = ListItemProxy(self.config, self.config_key, 'accounts')

    tags = property(lambda self: self._tags_list)
    flags = property(lambda self: self._flags_list)
    identities = property(lambda self: self._ids_list)
    accounts = property(lambda self: self._accts_list)

    def as_dict(self):
        accounts = [
            (a, AccountConfig(self.config, a))
            for a in self.accounts if a]

        tags = set()
        for akey, acct in accounts:
            tags |= set(acct.get_tags())
        tags = list(tags)
        tags.extend(t.lower() for t in self.tags if t)

        return {
            'name': self.name,
            'description': self.description,
            'accounts': dict((k, a.as_dict()) for k, a in sorted(accounts)),
            'identities': dict(
                 (i, IdentityConfig(self.config, i).as_dict())
                 for i in self.identities if i),
            'tags': tags,
            'key': self.config_key}


class IdentityConfig(ConfigSectionProxy):
    _KEYS = {
        'name': str,
        'address': str}



class AppConfig(ConfigParser):

    GENERAL = 'App'
    SECRETS = 'Secrets'
    RECOVERY = 'Recovery Data'
    SMTP_BRIDGE_SVC = 'SMTP Bridge Service'
    ACCESS_PREFIX = 'Access '
    ACCOUNT_PREFIX = 'Account '
    IDENTITY_PREFIX = 'Identity '
    CONTEXT_PREFIX = 'Context '
    CONTEXT_ZERO = 'Context 0'

    INITIAL_SETTINGS = [
       (GENERAL, 'config_backups', '10')]

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

    ALLOWED_SECTIONS = [GENERAL, SECRETS, RECOVERY]
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

    def __enter__(self, *args, **kwargs):
        self.lock.acquire()
        self.suppress_saves.append(0)
        return self

    def __exit__(self, *args, **kwargs):
        if self.suppress_saves.pop(-1):
            self.save()
        self.lock.release()

    def access_zero(self):
        with self:
            azero = self.ACCESS_PREFIX + '0'
            roles = ', '.join(['%s:owner' % p
                for p in self if p.startswith(self.CONTEXT_PREFIX)])
            self[azero].update({
                'name': 'Local access',
                'roles': roles})
            return AccessConfig(self, azero)

    def context_zero(self):
        with self:
            czero = self.CONTEXT_ZERO
            roles = ', '.join(['%s:owner' % p
                for p in self if p.startswith(self.CONTEXT_PREFIX)])
            self[czero].update({
                'name': 'My Mail'})
            return ContextConfig(self, czero)

    def access_from_token(self, token):
        if 'tokens' not in self._caches:
            with self:
                token_cache = {}
                for acl in self.all_access.values():
                    acl.expire_tokens()
                    for token in acl.tokens:
                        token_cache[token] = acl
            self._caches['tokens'] = token_cache
        acl = self._caches.get('tokens', {}).get(token)
        if acl is not None:
            return acl
        raise PermissionError('No access granted')

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

        self._caches = {}  # A save means something changed

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

    def get(self, section, option, *, raw=False, vars=None, fallback=_UNSET):
        if not self.has_section(section):
            if self.allowed_section(section):
                self.add_section(section)
        val = super().get(section, option, raw=raw, vars=vars, fallback=fallback)
        if isinstance(val, str) and val[:2] == '::':
            val = dumb_decode(val[2:], aes_key=self.aes_key)
        return val

    def set(self, section, option, value=None, save=True):
        if self.key_desc(section, option) in self.keep_private:
            return self.set_private(section, option, value=value, save=save)

        if not self.has_section(section):
            if self.allowed_section(section):
                self.add_section(section)
        if value is not None:
            encoded = dumb_encode_asc(value)
            if encoded[:1] != 'U':
                value = '::' + encoded
            super().set(section, option, value=value)
        else:
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

    def set_private(self, section, option, value=None, save=True):
        if value is not None:
            value = '::' + dumb_encode_asc(value, aes_key_iv=self._aes_key_iv())
        if self.key_desc(section, option) not in self.keep_private:
            self.keep_private.add(self.key_desc(section, option))
        if value is None:
            del self[section][option]
        else:
            super().set(section, option, value=value)
        if save:
            self.save()


if __name__ == '__main__':
    import sys
    if os.path.exists('/tmp/config.rc'):
        os.remove('/tmp/config.rc')

    ac = AppConfig('/tmp')
    ac.provide_passphrase('Hello world, this is my passphrase')
    ac.provide_passphrase('Hello world, this is my passphrase')
    try:
        ac.provide_passphrase('Bogus')
        assert(not 'reached')
    except PermissionError:
        pass
    try:
        ac.generate_master_key()
    except PermissionError:
        pass

    ac[ac.IDENTITY_PREFIX + '1'].update({
        'name': 'Bjarni',
        'address': 'bre@example.org',
        'signature': 'Multiline\nsignature'})

    ac[ac.CONTEXT_PREFIX + '1'].update({
        'username': 'Bjarni',
        'context.1.foo': 'bar',
        'context.2.foo': 'bar',
        'context.1.account.1.password': 'hello world',
        'context.1.account.2.password': 'hello world',
        'context.2.account.2.password': 'hello world'})

    ac.set_private(ac.CONTEXT_PREFIX + '1', 'password', 'very secret password')
    ac.set(ac.CONTEXT_PREFIX + '1', 'password', 'another very secret password')

    with ac:
      ac.access_zero()
      ac[ac.ACCESS_PREFIX + '1'].update({
        'name': 'Test access',
        'tokens': '12341234:0, 9999:1',
        'roles': 'Context 1:owner, Context 2:guest'})

      for acl in ac.all_access.values():
        #print('%s: tokens=%s, roles=%s' % (acl.name, acl.tokens, acl.roles))
        acl.roles['Context 2'] = 'admin'
        acl.tokens['abacab'] = int(time.time())

    assert(ac.access_from_token('12341234').name == 'Test access')
    try:
        ac.access_from_token('9999')
        assert(not 'reached')
    except PermissionError:
        pass

    assert(len(ac.get_aes_keys()) == 1)
    ac.change_master_key()
    old_keys = ac.get_aes_keys()
    assert(len(old_keys) == 2)

    ac.change_config_key('this is my new passphrase')

    assert(ac.get_aes_keys() == old_keys)
    assert(len(old_keys) == 2)
    assert(len(old_keys[0]) > 20)
    assert(len(old_keys[1]) > 20)

    ac.write(sys.stderr)
    os.remove('/tmp/config.rc')
    print('Tests passed OK')
