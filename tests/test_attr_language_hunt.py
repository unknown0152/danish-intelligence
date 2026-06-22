import asyncio
import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_package():
    if "danish_intelligence" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "danish_intelligence",
            ROOT / "__init__.py",
            submodule_search_locations=[str(ROOT)],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["danish_intelligence"] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)
    import danish_intelligence.hunt as hunt

    return hunt


def rss_item(title: str, attrs: dict[str, str], guid: str = "guid-1") -> str:
    attr_xml = "".join(
        f'<newznab:attr name="{name}" value="{value}"/>'
        for name, value in attrs.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">'
        "<channel>"
        "<item>"
        f"<title>{title}</title>"
        f'<guid isPermaLink="false">{guid}</guid>'
        "<size>10000000000</size>"
        '<newznab:attr name="category" value="2000"/>'
        f"{attr_xml}"
        "</item>"
        "</channel>"
        "</rss>"
    )


def test_language_danish_attr_marks_audio_without_nfo(monkeypatch):
    hunt = load_package()
    cache_writes = []

    async def cache_set(nzb_id, tag, release_name="", media_tags=None, source="nfo"):
        cache_writes.append((nzb_id, tag, source))

    async def fail_fetch(*args, **kwargs):  # pragma: no cover - should not be called
        raise AssertionError("NFO fetch should be skipped for Danish language attr")

    monkeypatch.setattr(hunt, "cache_set", cache_set)
    monkeypatch.setattr(hunt, "fetch_nfo", fail_fetch)
    monkeypatch.setattr(hunt, "fetch_nfo_direct", fail_fetch)

    content, probes = asyncio.run(
        hunt.hunt_danish(
            rss_item("Movie.2025.1080p.WEB-DL-GRP", {"language": "Danish"}),
            "6",
            "apikey",
            session=None,
        )
    )

    assert ".DanishAudio" in content
    assert probes == {}
    assert cache_writes == [("guid-1", ".DanishAudio", "attr")]


def test_non_danish_language_plus_nordic_title_marks_subs_without_nfo(monkeypatch):
    hunt = load_package()
    cache_writes = []

    async def cache_set(nzb_id, tag, release_name="", media_tags=None, source="nfo"):
        cache_writes.append((nzb_id, tag, source))

    async def fail_fetch(*args, **kwargs):  # pragma: no cover - should not be called
        raise AssertionError("NFO fetch should be skipped for reliable non-Danish audio attr")

    monkeypatch.setattr(hunt, "cache_set", cache_set)
    monkeypatch.setattr(hunt, "fetch_nfo", fail_fetch)
    monkeypatch.setattr(hunt, "fetch_nfo_direct", fail_fetch)

    content, probes = asyncio.run(
        hunt.hunt_danish(
            rss_item("Movie.2025.NORDiC.1080p.WEB-DL-GRP", {"language": "en-US"}),
            "6",
            "apikey",
            session=None,
        )
    )

    assert ".DanishSubs" in content
    assert ".DanishAudio" not in content
    assert probes == {}
    assert cache_writes == [("guid-1", ".DanishSubs", "attr")]


def test_audio_danish_attr_marks_audio_even_with_nordic_title(monkeypatch):
    hunt = load_package()
    cache_writes = []

    async def cache_set(nzb_id, tag, release_name="", media_tags=None, source="nfo"):
        cache_writes.append((nzb_id, tag, source))

    async def fail_fetch(*args, **kwargs):  # pragma: no cover - should not be called
        raise AssertionError("NFO fetch should be skipped for explicit Danish audio attr")

    monkeypatch.setattr(hunt, "cache_set", cache_set)
    monkeypatch.setattr(hunt, "fetch_nfo", fail_fetch)
    monkeypatch.setattr(hunt, "fetch_nfo_direct", fail_fetch)

    content, probes = asyncio.run(
        hunt.hunt_danish(
            rss_item("Movie.2025.NORDiC.1080p.WEB-DL-GRP", {"audio": "Danish"}),
            "6",
            "apikey",
            session=None,
        )
    )

    assert ".DanishAudio" in content
    assert probes == {}
    assert cache_writes == [("guid-1", ".DanishAudio", "attr")]


def test_non_danish_language_without_sub_signal_is_not_dk(monkeypatch):
    hunt = load_package()
    cache_writes = []

    async def cache_set(nzb_id, tag, release_name="", media_tags=None, source="nfo"):
        cache_writes.append((nzb_id, tag, source))

    async def fail_fetch(*args, **kwargs):  # pragma: no cover - should not be called
        raise AssertionError("NFO fetch should be skipped for explicit non-Danish language attr")

    monkeypatch.setattr(hunt, "cache_set", cache_set)
    monkeypatch.setattr(hunt, "fetch_nfo", fail_fetch)
    monkeypatch.setattr(hunt, "fetch_nfo_direct", fail_fetch)

    content, probes = asyncio.run(
        hunt.hunt_danish(
            rss_item("Movie.2025.1080p.WEB-DL-GRP", {"language": "en-US"}),
            "6",
            "apikey",
            session=None,
        )
    )

    assert ".DanishSubs" not in content
    assert ".DanishAudio" not in content
    assert probes == {"guid-1": "NONE"}
    assert cache_writes == []


def test_non_danish_audio_without_sub_signal_is_not_dk(monkeypatch):
    hunt = load_package()
    cache_writes = []

    async def cache_set(nzb_id, tag, release_name="", media_tags=None, source="nfo"):
        cache_writes.append((nzb_id, tag, source))

    async def fail_fetch(*args, **kwargs):  # pragma: no cover - should not be called
        raise AssertionError("NFO fetch should be skipped for explicit non-Danish audio attr")

    monkeypatch.setattr(hunt, "cache_set", cache_set)
    monkeypatch.setattr(hunt, "fetch_nfo", fail_fetch)
    monkeypatch.setattr(hunt, "fetch_nfo_direct", fail_fetch)

    content, probes = asyncio.run(
        hunt.hunt_danish(
            rss_item("Movie.2025.1080p.WEB-DL-GRP", {"audio": "English"}),
            "6",
            "apikey",
            session=None,
        )
    )

    assert ".DanishSubs" not in content
    assert ".DanishAudio" not in content
    assert probes == {"guid-1": "NONE"}
    assert cache_writes == []
