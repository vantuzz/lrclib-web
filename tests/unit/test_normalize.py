from lrclib_web.normalize import (
    artist_parts,
    has_explicit_feat,
    has_feat_separator,
    norm,
    norm_core,
    primary_artist,
    request_artist,
    same_artist,
    split_title,
)


def test_norm_basics():
    assert norm(None) == ""
    assert norm("  The Beatles ") == "beatles"
    assert norm("Beyoncé") == "beyonce"
    assert norm("Simon & Garfunkel") == "simon and garfunkel"
    assert norm("AC/DC!!") == "ac dc"
    assert norm("snake_case") == "snake case"


def test_split_title_qualifiers():
    assert split_title("Song (Live)") == ("song", frozenset({"live"}))
    assert split_title("Song - Acoustic") == ("song", frozenset({"acoustic"}))
    assert split_title("Song [Remix]") == ("song", frozenset({"remix"}))
    core, q = split_title("Song - Remastered 2011")
    assert core == "song" and q == frozenset()
    assert split_title("Song (feat. X)") == ("song", frozenset())


def test_split_title_all_bracketed():
    assert norm_core("(Intro)") == "intro"
    assert split_title("") == ("", frozenset())


def test_artist_parts():
    assert artist_parts("A feat. B") == ["A", "B"]
    assert artist_parts("A ft. B") == ["A", "B"]
    assert artist_parts("A, B") == ["A", "B"]
    assert artist_parts("A x B") == ["A", "B"]
    assert artist_parts("Solo") == ["Solo"]
    assert artist_parts(None) == []
    assert primary_artist("A feat. B") == "A"
    assert has_feat_separator("A & B")
    assert not has_feat_separator("Solo")


def test_same_artist():
    assert same_artist("The Beatles", "beatles")
    assert same_artist("A feat. B", "A")
    assert same_artist("A", "A & B")
    assert not same_artist("A", "C")
    assert not same_artist("", "A")
    assert not same_artist(None, None)


def test_explicit_feat_and_request_artist():
    for a in ("A feat. B", "A FEAT B", "A ft. B", "A Featuring B", "A; B"):
        assert has_explicit_feat(a), a
        assert request_artist(a) == "A", a
    for a in ("Earth, Wind & Fire", "Simon & Garfunkel", "A x B", "A with B", "Featherstone", "Solo"):
        assert not has_explicit_feat(a), a
        assert request_artist(a) == a
    assert request_artist(None) == "" and not has_explicit_feat(None)

