"""Every committed cassette is checked for credentials, in CI and not by eye.

A cassette is a recording of a real exchange, so the one way it can go wrong
badly is by carrying a credential into the repository. The vcrpy filters
scrub what they were told about; this walks what actually landed on disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.testing import assert_cassette_is_scrubbed

_ROOT = Path(__file__).resolve().parents[3]
_CASSETTES = sorted(_ROOT.glob("libs/providers/*/tests/*/cassettes/**/*.yaml"))
_IDS = [str(p.relative_to(_ROOT).as_posix()) for p in _CASSETTES]


@pytest.mark.skipif(not _CASSETTES, reason="no cassettes are committed yet")
@pytest.mark.parametrize("cassette", _CASSETTES, ids=_IDS)
def test_cassette_carries_no_credential(cassette):
    """A committed recording has no unscrubbed header or query credential."""
    assert_cassette_is_scrubbed(cassette)


def _cassette(tmp_path: Path, body: object) -> Path:
    """Write a one-interaction cassette whose response carries `body`."""
    import yaml

    document = {
        "interactions": [
            {
                "request": {
                    "method": "GET",
                    "uri": "https://example.invalid/userinfo",
                    "headers": {},
                },
                "response": {"status": {"code": 200}, "body": {"string": body}},
            }
        ],
        "version": 1,
    }
    path = tmp_path / "cassette.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def test_an_identity_response_body_is_caught(tmp_path):
    """An unscrubbed identity field in a JSON response fails the check."""
    leaky = '{"sub":"abc-123","preferred_username":"someone","scope":"openid"}'
    with pytest.raises(AssertionError, match="response body"):
        assert_cassette_is_scrubbed(_cassette(tmp_path, leaky))


def test_a_token_response_body_is_caught(tmp_path):
    """A bearer credential in a response body fails the check anywhere."""
    leaky = '{"access_token":"ey.live.token","token_type":"Bearer"}'
    with pytest.raises(AssertionError, match="access_token"):
        assert_cassette_is_scrubbed(_cassette(tmp_path, leaky))


def test_a_data_payload_reusing_identity_words_is_not_flagged(tmp_path):
    """`name` / `family_name` are data outside an identity document.

    IUCN answers carry a `name` on every synonym and a `family_name` on every
    taxon. Redacting those by key alone would corrupt the payload and report
    a clean cassette as a leak.
    """
    taxonomy = (
        '{"taxon":{"family_name":"Felidae","scientific_name":"Panthera leo",'
        '"synonyms":[{"name":"Felis leo"}]}}'
    )
    assert_cassette_is_scrubbed(_cassette(tmp_path, taxonomy))

    from earthlens.testing._cassettes import _scrub_response

    response = {"status": {"code": 200}, "body": {"string": taxonomy}}
    assert _scrub_response(response)["body"]["string"] == taxonomy


def test_a_redacted_identity_response_body_passes(tmp_path):
    """The same body passes once the recording filter has rewritten it."""
    from earthlens.testing._cassettes import _scrub_response

    leaky = '{"sub":"abc-123","preferred_username":"someone","scope":"openid"}'
    scrubbed = _scrub_response({"status": {"code": 200}, "body": {"string": leaky}})
    assert "someone" not in scrubbed["body"]["string"], "the login was redacted"
    assert "openid" in scrubbed["body"]["string"], "a non-identity field survives"
    assert_cassette_is_scrubbed(_cassette(tmp_path, scrubbed["body"]["string"]))


def test_a_non_json_body_is_left_alone(tmp_path):
    """A binary payload is neither parsed nor rewritten."""
    from earthlens.testing._cassettes import _scrub_response

    payload = b"\x89PNG\r\n\x1a\n not json at all"
    response = {"status": {"code": 200}, "body": {"string": payload}}
    assert _scrub_response(response)["body"]["string"] == payload
    assert_cassette_is_scrubbed(_cassette(tmp_path, payload))
