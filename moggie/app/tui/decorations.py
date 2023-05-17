from ...config import APPNAME, APPVER

EMOJI = {
    'mailbox':    '\U0001F4C1',
    'search':     '\U0001F50E',
    'attachment': '\U0001F4CE',
    'lock':       '\U0001F512'}


ENVELOPES = ("""\
     _______      x
    |==   []|     x
    |  ==== |____ x
    '-------'  []|x
         |   === |x
         '-------'x
  _______         x
 |==   []|        x
 |  ==== |        x
 '-------'        x
""").replace('x', '')


HELLO = ("""\
  _                        x
  \`*-.                    x
   )  _`-.         %-8.8s
  .  : `. .        v%-7.7s
  : _   '  \               x
  ; *` _.   `*-._          x
  `-.-'          `-.       x
    ;       `       `.     x
    :.       .        \    x
    . \  .   :   .-'   .   x
    '  `+.;  ;  '      :   x
    :  '  |    ;       ;-. x
    ; '   : :`-:     _.`* ;x
  .*' /  .*' ; .*`- +'  `*'x
  `*-*   `*-*  `*-*'       x
""").replace('x', '') % (APPNAME, APPVER)

HELLO_CREDITS = """\
           cat by Blazej Kozlowski"""


# FIXME: generate different palettes based on the contents of our
#        config file; we should let the user specify their own
#        colors, and also provide light/dark themes.
def palette(config):
    return [
            (None,             'light gray',  'black',     ''),
            ('',               'light gray',  'black',     ''),
            ('body',           'light gray',  'black',     ''),
            ('sidebar',        'light gray',  'black',     ''),
            ('content',        'light gray',  'black',     ''),
            ('email',          'brown',       'black',     ''),
            ('hotkey',         'brown',       'black',     ''),
            ('act_hk',         'black',       'brown',     ''),
            ('crumbs',         'white',       'dark blue', ''),
            ('popbg',          'white',       'dark blue', ''),
            ('popsubtle',      'light gray',  'dark blue', ''),
            ('header',         'light gray',  'black',     ''),
            ('top_hk',         'brown',       'black',     ''),
            ('subtle',         'dark gray',   'black',     ''),
            ('list_from',      'light gray',  'black',     ''),
            ('list_attrs',     'dark gray',   'black',     ''),
            ('list_subject',   'light gray',  'black',     ''),
            ('list_date',      'dark gray',   'black',     ''),
            ('check_from',     'light green', 'black',     ''),
            ('check_attrs',    'dark green',  'black',     ''),
            ('check_subject',  'light green', 'black',     ''),
            ('check_date',     'dark green',  'black',     ''),
            ('email_key_from', 'dark gray',   'black',     ''),
            ('email_val_from', 'light blue',  'black',     ''),
            ('email_key_att',  'dark gray',   'black',     ''),
            ('email_val_att',  'light blue',  'black',     ''),
            ('email_key_to',   'dark gray',   'black',     ''),
            ('email_val_to',   'dark gray',   'black',     ''),
            ('email_key_cc',   'dark gray',   'black',     ''),
            ('email_val_cc',   'dark gray',   'black',     ''),
            ('email_key_date', 'dark gray',   'black',     ''),
            ('email_val_date', 'dark gray',   'black',     ''),
            ('email_key_subj', 'dark gray',   'black',  ''),
            ('email_val_subj', 'light green', 'black',  ''),
            ('active',         'light blue',  'black',     ''),
            ('active',         'white',       'brown',     ''),
            ('focus',          'white',       'dark blue', '')]

