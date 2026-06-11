"""Shared Danish marker and Arr naming contract.

The clean runtime contract is:

- Proxy title markers: `.DanishAudio` and `.DanishSubs`
- Arr Custom Formats: `Danish Audio` and `Danish Subtitles`
- Arr Quality Profiles: `Danish Audio` and `Danish Subtitles`

Legacy names remain as input aliases only so old cache rows, old filenames, and
old Arr objects can be cleaned up safely during idempotent repainting.
"""

from __future__ import annotations

import re

CF_DANISH_AUDIO = "Danish Audio"
CF_DANISH_SUBTITLES = "Danish Subtitles"

PROFILE_DANISH_AUDIO = "Danish Audio"
PROFILE_DANISH_SUBTITLES = "Danish Subtitles"

DK_AUDIO_TITLE = ".DanishAudio"
DK_SUBS_TITLE = ".DanishSubs"
DK_AUDIO_NFO = DK_AUDIO_TITLE
DK_SUBS_NFO = DK_SUBS_TITLE
NO_DK_TAG = "NONE"

LEGACY_DK_AUDIO_TITLE = ".DKaudio"
LEGACY_DK_SUBS_TITLE = ".DKOK"
LEGACY_CF_NAMES = {"DK", "DKAudio", "DKSubs", "NORDIC.ENG"}
LEGACY_PROFILE_NAMES = {"NORDIC"}

AUDIO_TAG_ALIASES = (DK_AUDIO_TITLE, LEGACY_DK_AUDIO_TITLE)
SUBS_TAG_ALIASES = (DK_SUBS_TITLE, LEGACY_DK_SUBS_TITLE)

PROXY_TAG_RE = re.compile(
    r"\.(DKOK|DKaudio|DanishAudio|DanishSubs)\b|"
    r"\[Danish Audio\]|\[Danish Subtitles\]",
    re.I,
)

SCENE_GROUP_RE = re.compile(
    r"-([A-Za-z0-9]+?)(?:\.(?:DK|DanishAudio|DanishSubs)|\.nzb|$)",
    re.I,
)


def normalize_dk_tag(tag: str) -> str:
    """Return the clean marker for a clean or legacy Danish tag."""
    tag = (tag or "").strip()
    if tag in {DK_AUDIO_TITLE, DK_AUDIO_NFO}:
        return DK_AUDIO_TITLE
    if tag in {DK_SUBS_TITLE, DK_SUBS_NFO}:
        return DK_SUBS_TITLE
    if tag in {LEGACY_DK_AUDIO_TITLE, "[Danish Audio]", "[DKAudio]"}:
        return DK_AUDIO_TITLE
    if tag in {LEGACY_DK_SUBS_TITLE, "[Danish Subtitles]", "[DKSubs]", "[DKOK:Title]"}:
        return DK_SUBS_TITLE
    if tag == "[DKOK:NFO]":
        return DK_SUBS_NFO
    return tag or NO_DK_TAG
