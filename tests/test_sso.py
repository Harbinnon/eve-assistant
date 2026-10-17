import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from eve_assistant.esi import SCOPES, sso
from tests.conftest import CLIENT_ID


def test_pkce_challenge_matches_rfc_7636_example():
    # Appendix B of RFC 7636.
    # Published example values, not secrets.
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"  # pragma: allowlist secret
    expected = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"  # pragma: allowlist secret
    assert sso.challenge_for(verifier) == expected


def test_pkce_pairs_are_random_and_consistent():
    a, b = sso.make_pkce_pair(), sso.make_pkce_pair()
    assert a.verifier != b.verifier
    assert sso.challenge_for(a.verifier) == a.challenge
    assert len(a.verifier) >= 43
    assert len(a.verifier) <= 128


def test_authorize_url_carries_pkce_and_scopes():
    url = sso.authorize_url(CLIENT_ID, "http://localhost:8765/callback", SCOPES, "st", "ch")
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == sso.AUTHORIZE_URL
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [CLIENT_ID]
    assert query["code_challenge"] == ["ch"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == ["st"]
    assert query["scope"][0].split(" ") == list(SCOPES)


def test_scopes_are_read_only():
    assert len(SCOPES) == len(set(SCOPES)) == 18
    assert not [s for s in SCOPES if "write" in s or "corporation" in s]


@respx.mock
def test_exchange_code_sends_verifier_and_no_secret():
    route = respx.post(sso.TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "a", "refresh_token": "r", "expires_in": 1199}
        )
    )
    with httpx.Client() as client:
        tokens = sso.exchange_code(client, CLIENT_ID, "the-code", "the-verifier")
    sent = parse_qs(route.calls.last.request.content.decode())
    assert sent == {
        "grant_type": ["authorization_code"],
        "code": ["the-code"],
        "client_id": [CLIENT_ID],
        "code_verifier": ["the-verifier"],
    }
    assert "authorization" not in route.calls.last.request.headers
    assert tokens.refresh_token == "r"
    assert tokens.expires_at > time.time() + 1000


@respx.mock
def test_refused_token_request_does_not_echo_the_response_body():
    respx.post(sso.TOKEN_URL).mock(
        return_value=httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "secret-ish detail"}
        )
    )
    with httpx.Client() as client, pytest.raises(sso.SSOError) as err:
        sso.refresh(client, CLIENT_ID, "old")
    assert "invalid_grant" in str(err.value)
    assert "secret-ish" not in str(err.value)


def test_valid_token_gives_the_character(make_token, jwks):
    identity = sso.verify_access_token(make_token(), jwks, CLIENT_ID)
    assert identity.character_id == 2112000001
    assert identity.name == "Test Pilot"
    assert "esi-skills.read_skills.v1" in identity.scopes


@pytest.mark.parametrize("issuer", sorted(sso.ACCEPTED_ISSUERS))
def test_both_issuer_forms_are_accepted(make_token, jwks, issuer):
    sso.verify_access_token(make_token(iss=issuer), jwks, CLIENT_ID)


def test_single_scope_as_string(make_token, jwks):
    identity = sso.verify_access_token(make_token(scp="esi-skills.read_skills.v1"), jwks, CLIENT_ID)
    assert identity.scopes == ("esi-skills.read_skills.v1",)


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"iss": "https://evil.example"}, "not issued by EVE"),
        ({"aud": ["someone-else", "EVE Online"]}, "different application"),
        ({"aud": [CLIENT_ID]}, "not issued for EVE Online"),
        ({"exp": int(time.time()) - 3600}, "expired"),
        ({"sub": "CORPORATION:EVE:1"}, "not for an EVE character"),
        ({"sub": None}, "failed verification"),
    ],
)
def test_bad_tokens_are_rejected(make_token, jwks, overrides, message):
    token = make_token(**overrides)
    with pytest.raises(sso.SSOError, match=message):
        sso.verify_access_token(token, jwks, CLIENT_ID)


def test_token_signed_with_another_key_is_rejected(jwks):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(
        {
            "sub": "CHARACTER:EVE:1",
            "aud": [CLIENT_ID, "EVE Online"],
            "iss": "https://login.eveonline.com",
            "exp": int(time.time()) + 600,
        },
        other,
        algorithm="RS256",
        headers={"kid": "JWT-Signature-Key"},
    )
    with pytest.raises(sso.SSOError, match="failed verification"):
        sso.verify_access_token(forged, jwks, CLIENT_ID)


def test_unknown_key_id_is_rejected(make_token, jwks):
    unknown = {"keys": [dict(jwks["keys"][0], kid="other")]}
    token = make_token()
    with pytest.raises(sso.SSOError, match="does not publish"):
        sso.verify_access_token(token, unknown, CLIENT_ID)
