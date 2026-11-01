"""Compatibility imports for the microportal SMS channel."""
from activities.checkin_common.client import (
    WebCheckinClient, WebCheckinError, WebLoginManager, SCOPE, ENTRY, ORIGIN, BASE,
    WEB_UA, KEY, IV, READ_PATHS, _encrypt, _decrypt, _url, _follow, _snapshot, _json, _identity,
)
