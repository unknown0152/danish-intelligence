"""Pure NZB parsing helpers.

The OldBoys API reports ``size`` as the size of the *.nzb file itself* (a few
hundred KB), not the size of the media it describes. The real media size is the
sum of the ``bytes`` attribute across every ``<segment>`` element. NZBs are also
password protected, with the password carried in ``<meta type="password">``.
"""

from __future__ import annotations

import re
from typing import NamedTuple

# Match <segment ... bytes="123" ...> regardless of attribute order.
_SEGMENT_BYTES = re.compile(rb'<segment\b[^>]*?\bbytes="(\d+)"', re.IGNORECASE)

# Match <meta type="password">SECRET</meta> regardless of attribute order.
_PASSWORD_META = re.compile(
    rb'<meta\b[^>]*?\btype="password"[^>]*?>(.*?)</meta>',
    re.IGNORECASE | re.DOTALL,
)


class NzbMeta(NamedTuple):
    size: int          # sum of segment bytes (real media size), 0 if none found
    segments: int      # number of <segment> elements
    password: str | None  # password from <meta>, or None if absent/empty


def parse_nzb(data: bytes) -> NzbMeta:
    """Extract real media size, segment count and password from raw NZB bytes."""
    seg_bytes = _SEGMENT_BYTES.findall(data)
    size = sum(int(b) for b in seg_bytes)

    password: str | None = None
    m = _PASSWORD_META.search(data)
    if m:
        pw = m.group(1).strip()
        if pw:
            password = pw.decode("utf-8", "replace")

    return NzbMeta(size=size, segments=len(seg_bytes), password=password)
