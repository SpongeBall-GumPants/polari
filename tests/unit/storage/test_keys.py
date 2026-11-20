from typing import Any, cast

import pytest
from dav_helpers import ROOT_PATH
from polari_app.storage import InvalidKeyError, InvalidResponseError
from polari_app.storage.keys import (
    encode_key,
    encode_root_url,
    href_to_key,
    parent_prefixes,
    validate_key,
    validate_prefix,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("experiment.json", "experiment.json"),
        (
            "2026/p01/2026-11-14/trial_1/mapping.yaml",
            "2026/p01/2026-11-14/trial_1/mapping.yaml",
        ),
        ("my data/notes 1.yaml", "my data/notes 1.yaml"),
        ("Müller/çalışma.json", "Müller/çalışma.json"),
        ("100%/a%20b", "100%/a%20b"),  # literal percent signs, not decoded
        ("...hidden/..x", "...hidden/..x"),  # dots inside names are fine
    ],
)
def test_validate_key_accepts(raw: str, expected: str) -> None:
    assert validate_key(raw) == expected


def test_validate_key_uses_nfc() -> None:
    assert validate_key("Müller.json") == "Müller.json"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "/",
        "a/",  # a trailing slash names a prefix, not an object
        "/etc/passwd",
        "//host/share",
        "../secret.json",
        "a/../../b",
        "a/..",
        "./a",
        "a/./b",
        "a//b",
        "a\\..\\b",
        "a\x00b",
        "line\nbreak",
    ],
)
def test_validate_key_rejects(raw: str) -> None:
    with pytest.raises(InvalidKeyError):
        validate_key(raw)


def test_invalid_key_error_is_value_error() -> None:
    with pytest.raises(ValueError, match="'.' and '..'"):
        validate_key("../x")


def test_validate_key_rejects_non_string() -> None:
    with pytest.raises(InvalidKeyError):
        validate_key(cast(Any, b"a.json"))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", ""), ("2026/", "2026/"), ("2026/p01/", "2026/p01/"), ("ü/", "ü/")],
)
def test_validate_prefix_accepts_whole_levels(raw: str, expected: str) -> None:
    assert validate_prefix(raw) == expected


@pytest.mark.parametrize(
    "raw", ["2026", "2026/p0", "/", "/2026/", "a//", "../", "a/../", "a\\b/"]
)
def test_validate_prefix_rejects(raw: str) -> None:
    with pytest.raises(InvalidKeyError):
        validate_prefix(raw)


@pytest.mark.parametrize(
    ("key", "encoded"),
    [
        ("a/b.json", "a/b.json"),
        ("my data/a b.json", "my%20data/a%20b.json"),
        ("ü/ç.json", "%C3%BC/%C3%A7.json"),
        ("100%", "100%25"),
        ("a?b#c", "a%3Fb%23c"),
        ("%2e%2e", "%252e%252e"),  # cannot turn into ".." on the server
        ("a/b/", "a/b/"),  # prefixes keep their trailing slash
    ],
)
def test_encode_key(key: str, encoded: str) -> None:
    assert encode_key(key) == encoded


def test_parent_prefixes() -> None:
    assert parent_prefixes("a/b/c.json") == ["a/", "a/b/"]
    assert parent_prefixes("c.json") == []


@pytest.mark.parametrize(
    ("root_url", "encoded", "root_path"),
    [
        (
            "https://cloud.example.test/remote.php/dav/files/u/polari data",
            "https://cloud.example.test/remote.php/dav/files/u/polari%20data/",
            "/remote.php/dav/files/u/polari data/",
        ),
        (
            "https://cloud.example.test/dav/Müller/",
            "https://cloud.example.test/dav/M%C3%BCller/",
            "/dav/Müller/",
        ),
        (
            "https://cloud.example.test/dav/100%",  # a literal percent sign
            "https://cloud.example.test/dav/100%25/",
            "/dav/100%/",
        ),
        ("http://localhost:8080", "http://localhost:8080/", "/"),
    ],
)
def test_encode_root_url(root_url: str, encoded: str, root_path: str) -> None:
    assert encode_root_url(root_url) == (encoded, root_path)


@pytest.mark.parametrize(
    "root_url",
    [
        "ftp://dav.example.test/x/",
        "dav.example.test/x/",
        "https:///x/",
        "https://someone:secret@dav.example.test/x/",
        "https://dav.example.test/x/?a=1",
        "https://dav.example.test/x/#frag",
        "https://dav.example.test/x/../y",
    ],
)
def test_encode_root_url_rejects(root_url: str) -> None:
    with pytest.raises(ValueError, match="root_url must"):
        encode_root_url(root_url)


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("/remote.php/dav/files/synthetic-user/polari%20root/", ""),
        ("/remote.php/dav/files/synthetic-user/polari%20root", ""),
        ("/remote.php/dav/files/synthetic-user/polari%20root/a.json", "a.json"),
        ("/remote.php/dav/files/synthetic-user/polari%20root/dir/", "dir"),
        (
            "/remote.php/dav/files/synthetic-user/polari%20root/my%20data/%C3%BC.json",
            "my data/ü.json",
        ),
        (
            "https://dav.example.test/remote.php/dav/files/synthetic-user/polari%20root/x/y",
            "x/y",
        ),
    ],
)
def test_href_to_key(href: str, expected: str) -> None:
    assert href_to_key(href, ROOT_PATH) == expected


@pytest.mark.parametrize(
    "href",
    [
        "/remote.php/dav/files/synthetic-user/other/a.json",
        "/remote.php/dav/files/synthetic-user/polari%20root-evil/a.json",
        "/remote.php/dav/files/synthetic-user/polari%20root/a/../../escape",
        "/remote.php/dav/files/synthetic-user/polari%20root/%2E%2E/escape",
        "/remote.php/dav/files/synthetic-user/polari%20root/a//b",
        "/remote.php/dav/files/synthetic-user/polari%20root/%FF.json",
    ],
)
def test_href_to_key_rejects_outside_or_invalid(href: str) -> None:
    with pytest.raises(InvalidResponseError):
        href_to_key(href, ROOT_PATH)
