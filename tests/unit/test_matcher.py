from lrclib_web.lrclib import Response
from lrclib_web.matcher import Matcher, filter_candidates, record_kind


def rec(id=1, name="Song", artist="Art", album="Alb", dur=200.0, synced=None, plain=None, instr=False):
    return {"id": id, "trackName": name, "artistName": artist, "albumName": album, "duration": dur,
            "instrumental": instr, "syncedLyrics": synced, "plainLyrics": plain}


def test_record_kind():
    assert record_kind(rec(instr=True)) == "instrumental"
    assert record_kind(rec(synced="[00:01.00]x")) == "synced"
    assert record_kind(rec(plain="x")) == "plain"
    assert record_kind(rec(synced="  ", plain="")) == "empty"


def test_duration_edges():
    base = {"synced": "[00:00.00]x"}
    cands = [rec(1, dur=202.0, **base), rec(2, dur=202.1, **base), rec(3, dur=198.0, **base),
             rec(4, dur=197.9, **base), rec(5, dur=None, **base)]
    got = [c["id"] for c in filter_candidates(cands, "Song", "Art", "Alb", 200.0)]
    assert sorted(got) == [1, 3]


def test_unknown_duration_requires_equal_album():
    cands = [rec(1, album="Alb", plain="x"), rec(2, album="Other", plain="x")]
    assert [c["id"] for c in filter_candidates(cands, "Song", "Art", "Alb", 0)] == [1]
    assert filter_candidates(cands, "Song", "Art", None, 0) == []


def test_qualifier_one_side_rejects():
    c = [rec(1, name="Song (Live)", plain="x")]
    assert filter_candidates(c, "Song", "Art", "Alb", 200.0) == []
    c = [rec(1, name="Song", plain="x")]
    assert filter_candidates(c, "Song - Live", "Art", "Alb", 200.0) == []
    c = [rec(1, name="Song (Live)", plain="x")]
    assert len(filter_candidates(c, "Song - Live", "Art", "Alb", 200.0)) == 1


def test_wrong_artist_or_title_rejected():
    c = [rec(1, artist="Other", plain="x"), rec(2, name="Different", plain="x")]
    assert filter_candidates(c, "Song", "Art", "Alb", 200.0) == []


def test_ranking_synced_over_plain():
    c = [rec(1, plain="p"), rec(2, synced="[00:00.00]s", dur=201.5), rec(3, instr=True)]
    assert [x["id"] for x in filter_candidates(c, "Song", "Art", "Alb", 200.0)] == [2, 1, 3]


def test_ranking_album_then_duration():
    c = [rec(1, album="Z", synced="s", dur=200.0), rec(2, album="Alb", synced="s", dur=201.0),
         rec(3, album="Alb", synced="s", dur=200.2)]
    assert [x["id"] for x in filter_candidates(c, "Song", "Art", "Alb", 200.0)] == [3, 2, 1]


class FakeClient:
    def __init__(self, gets=(), searches=()):
        self.gets, self.searches = list(gets), list(searches)
        self.calls = []

    def get(self, title, artist, album, dur):
        self.calls.append(("get", artist, dur))
        return self.gets.pop(0)

    def search(self, title, artist):
        self.calls.append(("search", artist))
        return self.searches.pop(0)


NF = Response(404)


def test_get_synced_one_request():
    c = FakeClient([Response(200, rec(synced="[00:00.00]x"))])
    m = Matcher(c).match("Song", "Art", "Alb", 200000)
    assert m.kind == "synced" and m.requests == 1 and m.lyrics == "[00:00.00]x"


def test_get_plain_returned_without_search_when_not_searching_for_synced():
    c = FakeClient([Response(200, rec(plain="words"))])
    m = Matcher(c, search_for_synced=False).match("Song", "Art", "Alb", 200000)
    assert m.kind == "plain" and m.requests == 1 and len(c.calls) == 1


def test_get_instrumental():
    c = FakeClient([Response(200, rec(instr=True))])
    assert Matcher(c).match("Song", "Art", "Alb", 200000).kind == "instrumental"


def test_feat_artist_second_get_with_primary():
    c = FakeClient([NF, Response(200, rec(artist="Main", synced="s"))])
    m = Matcher(c).match("Song", "Main feat. Guest", "Alb", 200000)
    assert m.kind == "synced" and m.requests == 2
    assert c.calls == [("get", "Main feat. Guest", 200), ("get", "Main", 200)]


def test_search_fallback_and_not_found():
    c = FakeClient([NF, NF], [Response(200, [])])
    m = Matcher(c).match("Song", "Art", "Alb", 200000)
    assert m.kind == "not_found" and m.requests == 2
    c = FakeClient([NF], [Response(404)])
    assert Matcher(c).match("Song", "Art", "Alb", 200000).kind == "not_found"


def test_search_rejected():
    c = FakeClient([NF], [Response(200, [rec(artist="Nobody", plain="x")])])
    m = Matcher(c).match("Song", "Art", "Alb", 200000)
    assert m.kind == "rejected" and m.requests == 2


def test_search_picks_synced():
    c = FakeClient([NF], [Response(200, [rec(1, plain="p"), rec(2, synced="s", dur=200.5)])])
    m = Matcher(c).match("Song", "Art", "Alb", 200000)
    assert m.kind == "synced" and m.record["id"] == 2


def test_unknown_duration_skips_get():
    c = FakeClient(searches=[Response(200, [rec(synced="s")])])
    m = Matcher(c).match("Song", "Art", "Alb", 0)
    assert m.kind == "synced" and c.calls[0][0] == "search"


def test_plain_from_get_fallback_when_searching_for_synced():
    c = FakeClient([Response(200, rec(plain="words"))], [Response(200, [])])
    m = Matcher(c, search_for_synced=True).match("Song", "Art", "Alb", 200000)
    assert m.kind == "plain" and m.requests == 2
    # search finds synced -> preferred
    c = FakeClient([Response(200, rec(plain="words"))], [Response(200, [rec(2, synced="s")])])
    assert Matcher(c, search_for_synced=True).match("Song", "Art", "Alb", 200000).kind == "synced"
    # search only plain -> keep get's plain
    c = FakeClient([Response(200, rec(plain="words"))], [Response(200, [rec(2, plain="other")])])
    m = Matcher(c, search_for_synced=True).match("Song", "Art", "Alb", 200000)
    assert m.kind == "plain" and m.record["plainLyrics"] == "words"
    # search errors -> plain from get
    c = FakeClient([Response(200, rec(plain="words"))], [Response(500, error="x", transient=True)])
    assert Matcher(c, search_for_synced=True).match("Song", "Art", "Alb", 200000).kind == "plain"


def test_error_propagates():
    c = FakeClient([Response(0, error="net", transient=True)])
    m = Matcher(c).match("Song", "Art", "Alb", 200000)
    assert m.kind == "error" and m.transient


def test_empty_record_falls_to_search():
    c = FakeClient([Response(200, rec())], [Response(200, [])])
    m = Matcher(c).match("Song", "Art", "Alb", 200000)
    assert m.kind == "not_found"


def test_no_album_skips_get_goes_to_search():
    for album in (None, "", "  "):
        c = FakeClient(searches=[Response(200, [rec(synced="s")])])
        m = Matcher(c).match("Song", "Art feat. Guest", album, 200000)
        assert c.calls == [("search", "Art")]
        assert m.kind == "synced"


def test_400_on_get_falls_through_to_next_step_and_search():
    c = FakeClient([Response(400, error="bad request"), Response(404)], [Response(200, [rec(synced="s")])])
    m = Matcher(c).match("Song", "Art ft. Guest", "Alb", 200000)
    assert m.kind == "synced"
    assert [x[0] for x in c.calls] == ["get", "get", "search"]
    c = FakeClient([Response(400, error="bad request")], [Response(200, [])])
    assert Matcher(c).match("Song", "Art", "Alb", 200000).kind == "not_found"
    assert [x[0] for x in c.calls] == ["get", "search"]


def test_ampersand_artist_not_split_for_requests():
    c = FakeClient([NF], [Response(200, [])])
    Matcher(c).match("Song", "Earth, Wind & Fire", "Alb", 200000)
    assert c.calls == [("get", "Earth, Wind & Fire", 200), ("search", "Earth, Wind & Fire")]


def test_explicit_feat_search_uses_primary():
    c = FakeClient([NF, NF], [Response(200, [])])
    Matcher(c).match("Song", "Main feat. Guest", "Alb", 200000)
    assert c.calls[-1] == ("search", "Main")

