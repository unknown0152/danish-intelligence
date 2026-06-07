"""Classification: regex patterns, NFO scanning, title/attr classification."""

import os
import re
from pathlib import Path

from .__init__ import ATTR_DK_RE, CATEGORY_ATTR_RE, CATEGORY_RE, DK_AUDIO_NFO, DK_AUDIO_TITLE, DK_SUBS_NFO, DK_SUBS_TITLE, ITEM_RE, MIN_RELEASE_SIZE, MIN_RELEASE_SIZE_MOVIE, MIN_RELEASE_SIZE_TV, _PROXY_TAG_RE, _EXT_RE, _WS_RE, _metrics, log


# ── Scandinavian spelling-fold ───────────────────────────────────────────────
# Scene posters ASCII-fold Danish titles (Ørkenens -> Oerkenens), so a Radarr
# text query carrying ø/æ/å never matches those releases. We drop the words
# containing those chars so a SINGLE upstream query matches every spelling
# variant (cost-neutral — no extra calls). A guard keeps the original query
# when too few distinctive words would survive (avoids over-broadening).
_SCANDI_RE = re.compile(r"[øæåØÆÅ]")
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_DK_STOPWORDS = frozenset({
    "i", "en", "et", "og", "til", "af", "for",
    "med", "de", "den", "det", "på",
})
_FOLD_PUNCT = ".,:;!?-_'\"()[]"


def fold_scandi_query(q: str) -> str:
    """Rewrite a Newznab text query so one upstream call matches both the
    diacritic spelling ('Ørkenens Sønner') and the ASCII-folded spelling
    scene posters use ('Oerkenens Soenner').

    Drops the words containing ø/æ/å, leaving the shared ASCII words (+ year)
    that appear in every variant. Only folds when >= 2 *distinctive* ASCII
    words survive (excluding Danish stop words and the year); otherwise
    returns q unchanged so we never over-broaden (e.g. 'Den skaldede frisør
    2012' must not collapse to 'Den 2012')."""
    if not q or not _SCANDI_RE.search(q):
        return q

    kept = [w for w in q.split() if not _SCANDI_RE.search(w)]

    distinctive = 0
    for w in kept:
        bare = w.strip(_FOLD_PUNCT).lower()
        if bare and bare not in _DK_STOPWORDS and not _YEAR_RE.match(bare):
            distinctive += 1

    if distinctive < 2:
        return q
    return " ".join(kept)


def _is_status_probe(params: dict) -> bool:
    """A "status probe" is the kind of generic search Servarr's
    IndexerStatusCheck sends with no specific identifier — those are what
    we need to keep non-empty so the circuit breaker doesn't disable the
    indexer. Real searches (with q/imdbid/tmdbid/tvdbid/tvmazeid/season/ep)
    should be allowed to legitimately return empty results."""
    if params.get("t") not in ("search", "movie", "tvsearch"):
        return False
    for k in ("q", "imdbid", "tmdbid", "tvdbid", "tvmazeid", "season", "ep"):
        if params.get(k):
            return False
    return True


def _inject_probe_filler_if_empty(content: str) -> str:
    """Inject PROBE_FILLER_ITEM into content if it contains zero <item>s,
    and also bump newznab:response total="0" → "1" so consumers that trust
    total don't disagree with the item count.
    No-op when the response already has items or is malformed (no </channel>).
    Callers must have already decided the request is a status probe — this
    function does not gate on params itself."""
    from .__init__ import PROBE_FILLER_ITEM
    if ITEM_RE.search(content):
        return content
    if '</channel>' not in content:
        return content
    _metrics["probe_filler_injected"] += 1
    out = content.replace('</channel>', PROBE_FILLER_ITEM + '</channel>', 1)
    # Best-effort total bump. If the upstream omitted the response tag or
    # used different formatting, this leaves it alone (still better than
    # before, where injection always left total="0").
    out = re.sub(
        r'(<newznab:response[^>]*\btotal=")0(")',
        r'\g<1>1\2',
        out,
        count=1,
    )
    return out

def _empty_or_filler_response(params: dict):
    """Used by error paths in _handle_inner. For status-probe-shaped queries
    returns an empty RSS with the probe filler injected so Servarr's circuit
    breaker stays happy even when upstream Prowlarr is rate-limited / down.
    For real searches (movie/tv with a specific id, or text search) returns
    the bare empty RSS — propagates the upstream error condition honestly
    instead of fabricating a result Servarr might try to grab."""
    from aiohttp import web as _web
    from .__init__ import _EMPTY_RSS
    body = _EMPTY_RSS
    if _is_status_probe(params):
        body = _inject_probe_filler_if_empty(body)
    return _web.Response(text=body, content_type='application/xml')


def _min_size_for(item_xml: str) -> int:
    """Pick the size threshold for a release based on its Newznab category."""
    cats = [int(m.group(1)) for m in CATEGORY_RE.finditer(item_xml)]
    cats += [int(m.group(1)) for m in CATEGORY_ATTR_RE.finditer(item_xml)]
    if any(2000 <= c < 3000 for c in cats):
        return MIN_RELEASE_SIZE_MOVIE or MIN_RELEASE_SIZE
    if any(5000 <= c < 6000 for c in cats):
        return MIN_RELEASE_SIZE_TV or MIN_RELEASE_SIZE
    return MIN_RELEASE_SIZE


def normalize_release_name(title: str) -> str:
    s = title.lower()
    s = _PROXY_TAG_RE.sub('', s)
    s = _EXT_RE.sub('', s)
    s = _WS_RE.sub(' ', s).strip()
    return s

LOWQ_RE      = re.compile(r"\b(CAM|CAMRIP|HDCAM|HDTS|TELESYNC|TELECINE|DVDSCR|XViD|DivX|TS|SD|480p)\b", re.I)
AUDIO_DK_RE  = re.compile(
    r"\b("
    r"danish[\.\-_\s]*audio|"
    r"danish[\.\-_\s]*dub|"
    r"(dk|dan)[\.\-_\s]*multi|"
    # Bare DANISH/DANSK: only count as AUDIO when NOT immediately followed by
    # a SUBS/SUBTITLES qualifier. Prevents `Movie.DANISH.SUBS.1080p` from
    # being tagged `.DKaudio` when it's actually subs-only.
    r"(danish|dansk)(?![\.\-_\s]*subs?\b|[\.\-_\s]*subtitles?\b)"
    r")\b",
    re.I,
)
SUBS_DK_RE   = re.compile(r"\b(nordic|nordic[\.\-_\s]*subs?|danish[\.\-_\s]*(subs?|subtitles?)|dk[\.\-_\s]*subs?|dksubs?|dansubs?|dk|da)\b", re.I)
MI_AUDIO_DK  = re.compile(r"Audio\s*#\d+[\s\S]{1,1500}?Language\s*:\s*(Danish|da|dan)\b", re.I)
MI_SUBS_DK   = re.compile(r"(Text\s*#\d+[\s\S]{1,600}?Language\s*:\s*(Danish|da|dan)\b|S_TEXT[\s\S]{1,300}?Language\s*:\s*(Danish|da|dan)\b)", re.I)

# Scene-NFO header format (BANDOLEROS, NORDiC.MULTI groups, etc.):
#   LANGUAGE.....: Danish, English, Finnish, Norwegian, Swedish
#   SUBTiTLES....: Retail -> Danish, English, Finnish, Norwegian, Swedish
# LANGUAGE = audio tracks; SUBTiTLES = subtitle tracks. Distinct lines, so the
# audio classifier must not also pick up Danish from the SUBTiTLES line. Anchor
# each regex to start-of-line and stop at end-of-line.
SCENE_LANG_DK = re.compile(
    r"^[\s\W]*LANGUAGE\b[\s\.\-:_>]*[^\n]*\b(danish|dansk)\b",
    re.I | re.M,
)
SCENE_SUBS_DK = re.compile(
    r"^[\s\W]*SUB(?:TITLES?|S)\b[\s\.\-:_>]*[^\n]*\b(danish|dansk)\b",
    re.I | re.M,
)

# Scene-NFO AUDIO-label line with Danish in the value.
SCENE_AUDIO_DK = re.compile(
    r"^[\s\W]*"
    r"(?:(?:AUD[A-ZΘиИ\d]*[A-ZΘиИ]\w*"
    r"(?:[\s\.\-_]*(?:track|lang(?:uage)?|i?nfo)[\s\.\-_]*\d*)?)"
    r"|LYD(?:SPOR)?)"
    r"[\s\.\-:_>|]+"
    r"[^\n]*\b(danish|dansk)\b",
    re.I | re.M,
)


# ── Smart NFO classifier (section-aware + signal-proximity) ──────────────────

_DANISH_WORD_RE = re.compile(r"\b(danish|dansk)\b", re.I)

_AUDIO_STRONG_RE = re.compile(
    r"\b("
    r"audio|sound|dub(?:s|bed|bing)?|spoken|voice|"
    r"lyd(?:spor)?|tale|tonspur|"
    r"ac-?3|aac|dts(?:-?hd)?|truehd|atmos|ddp|e-?ac-?3|"
    r"flac|mp3|opus|lpcm|hd[\s-]?ma|mlp"
    r")\b",
    re.I,
)
_AUDIO_WEAK_RE = re.compile(
    r"\b("
    r"language|lang(?:s|uage)?|track|channels?|"
    r"kbps|kbit|kb/s|khz"
    r")\b",
    re.I,
)

_SUBS_TOKEN_RE = re.compile(
    r"\b("
    r"sub(?:s|title?s?|titled)?|"
    r"caption(?:s|ing)?|cc|srt|vtt|sup|ssa|ass|forced|sdh|"
    r"tekst(?:er|ning|et)?|undertekst(?:er)?|untertitel"
    r")\b",
    re.I,
)

_AUDIO_HEADER_LINE_RE = re.compile(
    r"^[\s\W]*("
    r"audio(?:\s*#?\s*\d+)?|sound(?:track)?|lyd(?:spor)?|tonspur|tale|"
    r"voice(?:\s*cast)?|dub(?:s|bed|bing|s?\s*track)?"
    r")\s*[#:\-]?\s*\d*\s*$",
    re.I,
)
_SUBS_HEADER_LINE_RE = re.compile(
    r"^[\s\W]*("
    r"sub(?:s|titles?)?(?:\s*#?\s*\d+)?|text(?:\s*#?\s*\d+)?|"
    r"caption(?:s|ing)?|tekst(?:er|ning)?|undertekst(?:er)?"
    r")\s*[#:\-]?\s*\d*\s*$",
    re.I,
)
_OTHER_HEADER_LINE_RE = re.compile(
    r"^[\s\W]*("
    r"general|video(?:\s*#?\s*\d+)?|menu|chapters?|cover|file|"
    r"complete[\s_-]?name|format|container|"
    r"plot|synopsis|description|story|imdb|name|title|genre|cast|"
    r"actors?|director|year|released|source|notes?"
    r")\s*[#:\-]?\s*\d*\s*$",
    re.I,
)

_PROXIMITY_WINDOW = 12


def _classify_dk_proximity(text: str) -> tuple[bool, bool]:
    """Walk NFO lines; for each Danish/Dansk match, find the nearest
    audio- or subs-context signal in the current line + preceding window,
    plus the active section header. Returns (audio_hit, subs_hit)."""
    audio_hit = False
    subs_hit = False
    lines = text.splitlines()
    current_section: str | None = None
    section_age = 0

    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if stripped:
            if _SUBS_HEADER_LINE_RE.match(ln):
                current_section = "subs"; section_age = 0
            elif _AUDIO_HEADER_LINE_RE.match(ln):
                current_section = "audio"; section_age = 0
            elif _OTHER_HEADER_LINE_RE.match(ln):
                current_section = None; section_age = 0
            else:
                section_age += 1
            if section_age > _PROXIMITY_WINDOW:
                current_section = None

        if not _DANISH_WORD_RE.search(ln):
            continue

        nearest_audio_strong = -1
        nearest_audio_weak = -1
        nearest_subs = -1
        for d in range(_PROXIMITY_WINDOW + 1):
            j = i - d
            if j < 0:
                break
            l2 = lines[j]
            if not l2.strip():
                continue
            if nearest_audio_strong == -1 and _AUDIO_STRONG_RE.search(l2):
                nearest_audio_strong = d
            if nearest_audio_weak == -1 and _AUDIO_WEAK_RE.search(l2):
                nearest_audio_weak = d
            if nearest_subs == -1 and _SUBS_TOKEN_RE.search(l2):
                nearest_subs = d
            if (nearest_audio_strong != -1 and nearest_subs != -1):
                break

        if current_section == "subs":
            subs_hit = True
            continue
        if nearest_subs != -1 and (
            (nearest_audio_strong == -1 or nearest_subs <= nearest_audio_strong)
            and (nearest_audio_weak == -1 or nearest_subs <= nearest_audio_weak)
        ):
            subs_hit = True
            continue
        if nearest_audio_strong != -1 or nearest_audio_weak != -1:
            audio_hit = True
            continue
        if current_section == "audio":
            audio_hit = True

    return audio_hit, subs_hit


def classify_nfo_text(text: str) -> str:
    """Classify NFO text into DK_AUDIO_NFO / DK_SUBS_NFO / "NONE"."""
    if not text:
        return "NONE"
    audio_hit, subs_hit = _classify_dk_proximity(text)
    if audio_hit:
        return DK_AUDIO_NFO
    if subs_hit:
        return DK_SUBS_NFO
    return "NONE"


# ISO 639 + colloquial variants that count as Danish in ffprobe output.
_DANISH_LANG_CODES = frozenset({"dan", "dansk", "danish", "da"})


# v5.7: strip the proxy's appended DK + NFO media tags from a release name
_PROXY_SUFFIX_STRIP_RE = re.compile(
    r"(?:\.DKaudio|\.DKOK)(?:\.NFO[A-Za-z0-9]+)*\s*$",
    re.I,
)


def strip_proxy_suffix(release_name: str) -> str:
    """Remove the proxy's appended .DKaudio/.DKOK + .NFOxxx tags from the
    end of a release name. Returns the canonical original title."""
    return _PROXY_SUFFIX_STRIP_RE.sub("", release_name or "")


def compute_actual_tag(audio_languages: list[str],
                       subtitle_languages: list[str]) -> str:
    """Compute the authoritative DK tag from ffprobe output."""
    def has_dk(langs):
        return any(
            (str(l) or "").strip().lower() in _DANISH_LANG_CODES
            for l in (langs or [])
        )
    if has_dk(audio_languages):
        return ".DKaudio"
    if has_dk(subtitle_languages):
        return ".DKOK"
    return "NONE"


def classify_mismatch(predicted: str, actual: str) -> str:
    """Return one of: agreement / upgrade / missed_dkaudio / false_dkaudio /
    false_dkok. Used by /learn/imported to label audit rows."""
    if predicted == actual:
        return "agreement"
    if actual == ".DKaudio" and predicted == ".DKOK":
        return "upgrade"
    if actual in (".DKaudio", ".DKOK") and predicted == "NONE":
        return "missed_dkaudio"
    if predicted == ".DKaudio" and actual in (".DKOK", "NONE"):
        return "false_dkaudio"
    if predicted == ".DKOK" and actual == "NONE":
        return "false_dkok"
    return "other"


# ── NFO-derived media tags (opt-in, gated by DKSUBS_PROXY_NFO_MEDIA_TAGS) ─────
_MEDIA_NFO_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    ".NFODV": [
        re.compile(r"\bDolby\s+Vision\b", re.I),
        re.compile(r"\bdvhe\.0[5-8]\.0[5-8]\b", re.I),
    ],
    ".NFOHDR10P": [
        re.compile(r"\bHDR10\s*\+", re.I),
        re.compile(r"\bHDR10\s*Plus\b", re.I),
        re.compile(r"\bSMPTE\s*ST\s*2094", re.I),
    ],
    ".NFOHDR10": [
        re.compile(r"\bHDR10\b", re.I),
        re.compile(r"\bSMPTE\s*ST\s*2086", re.I),
    ],
    ".NFOAtmos": [
        re.compile(r"\bDolby\s+Atmos\b", re.I),
    ],
    ".NFOTrueHD": [
        re.compile(r"\bDolby\s+TrueHD\b", re.I),
        re.compile(r"\bMLP\s+FBA\b", re.I),
    ],
    ".NFODTSHDMA": [
        re.compile(r"\bDTS-HD\s+Master\s+Audio\b", re.I),
        re.compile(r"\bDTS-HD\s+MA\b", re.I),
    ],
    ".NFORemux": [
        re.compile(r"\b(?:UHD\s+)?(?:Blu-?ray|BD)\s+Remux\b", re.I),
        re.compile(r"^\s*Source\s*:.*\bRemux\b", re.I | re.M),
        re.compile(r"\bRemuxed\s+from\b", re.I),
    ],
}


def _extract_nfo_media_tags(nfo_text: str) -> list[str]:
    """Run _MEDIA_NFO_PATTERNS against NFO text and return the matching
    tags as a sorted list (HDR10+ suppresses HDR10). Returns [] for empty
    or missing input."""
    if not nfo_text:
        return []
    tags = set()
    for tag, patterns in _MEDIA_NFO_PATTERNS.items():
        if any(p.search(nfo_text) for p in patterns):
            tags.add(tag)
    if ".NFOHDR10P" in tags:
        tags.discard(".NFOHDR10")
    return sorted(tags)


# Native-Danish title list.
NATIVE_DK_FILE = Path(os.environ.get("NATIVE_DK_FILE", "/cache/native-dk-titles.txt"))
_native_dk_cache: tuple = (0.0, [])     # (mtime, [compiled patterns])


def _load_native_dk():
    """Load native-Danish show titles, cached by file mtime. Returns list of compiled patterns."""
    global _native_dk_cache
    try:
        st = NATIVE_DK_FILE.stat()
    except FileNotFoundError:
        return []
    cached_mtime, cached_pats = _native_dk_cache
    if st.st_mtime == cached_mtime:
        return cached_pats
    try:
        titles = []
        with NATIVE_DK_FILE.open() as f:
            for line in f:
                t = line.strip()
                if t and not t.startswith("#"):
                    titles.append(t)
        pats = [re.compile(r"\b" + re.escape(t) + r"\b", re.I) for t in titles]
        _native_dk_cache = (st.st_mtime, pats)
        return pats
    except Exception:
        return cached_pats


NON_DK_LANG_RE = re.compile(
    r'\b('
    r'GERMAN|ITALIAN|SPANISH|FRENCH|RUSSIAN|HINDI|TAMIL|TELUGU|'
    r'JAPANESE|JAP|KOREAN|CHINESE|MANDARIN|CANTONESE|'
    r'POLISH|TURKISH|PORTUGUESE|HEBREW|ARABIC|UKRAINIAN|GREEK|'
    r'CZECH|HUNGARIAN|DUTCH|ROMANIAN|THAI|VIETNAMESE|'
    r'GER\.DUBBED|ITA\.DUBBED|FRE\.DUBBED|SPA\.DUBBED'
    r')\b',
    re.I,
)


def is_native_dk_title(title: str) -> bool:
    """True if release title contains a known native-Danish show/movie name
    AND does NOT explicitly advertise a foreign-language audio."""
    if NON_DK_LANG_RE.search(title):
        return False
    return any(p.search(title) for p in _load_native_dk())


def normalize_result_tag(tag: str) -> str:
    # Pass-through for new granular tags; legacy [DKOK:*] entries collapse to subs.
    if tag == "[DKOK:Title]":
        return DK_SUBS_TITLE
    if tag == "[DKOK:NFO]":
        return DK_SUBS_NFO
    return tag
