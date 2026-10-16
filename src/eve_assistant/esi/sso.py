"""EVE SSO login with PKCE.

PKCE is the OAuth flow for apps that cannot keep a client secret, so the
application's secret key is never used. The steps:

1. Make a random code verifier and its SHA-256 challenge.
2. Send the player's browser to EVE's login page with the challenge.
3. EVE redirects back to the local callback with a one-time code.
4. Exchange the code plus the verifier for an access token (short-lived)
   and a refresh token (long-lived, the secret we store).
5. Check the access token's signature and claims before trusting it.

Refresh tokens may change on every refresh; callers must always store the
newest one.
"""

import base64
import contextlib
import hashlib
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import jwt

AUTHORIZE_URL = "https://login.eveonline.com/v2/oauth/authorize"
TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"  # noqa: S105 - a URL, not a password
JWKS_URL = "https://login.eveonline.com/oauth/jwks"

# CCP's docs: accept either form of the issuer, reject anything else.
ACCEPTED_ISSUERS = frozenset(
    {"login.eveonline.com", "https://login.eveonline.com", "https://login.eveonline.com/"}
)
EVE_AUDIENCE = "EVE Online"
ALLOWED_ALGORITHMS = ("RS256", "ES256")


class SSOError(Exception):
    """Login failed. The message is safe to show and never contains a token."""


@dataclass(frozen=True)
class PkcePair:
    verifier: str
    challenge: str


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str
    expires_at: float


@dataclass(frozen=True)
class CharacterIdentity:
    character_id: int
    name: str
    scopes: tuple[str, ...]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def challenge_for(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def make_pkce_pair() -> PkcePair:
    verifier = _b64url(secrets.token_bytes(32))
    return PkcePair(verifier=verifier, challenge=challenge_for(verifier))


def make_state() -> str:
    return secrets.token_urlsafe(24)


def authorize_url(
    client_id: str, callback_url: str, scopes: tuple[str, ...], state: str, challenge: str
) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "redirect_uri": callback_url,
            "client_id": client_id,
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def _token_request(client: httpx.Client, form: dict[str, str]) -> TokenSet:
    try:
        response = client.post(
            TOKEN_URL,
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except httpx.HTTPError as exc:
        raise SSOError(f"Could not reach EVE's login service: {exc.__class__.__name__}") from None
    if response.status_code != 200:
        # The body can echo request details; report only the status and error code.
        error = ""
        with contextlib.suppress(ValueError, AttributeError):
            error = str(response.json().get("error", ""))[:40]
        raise SSOError(f"EVE's login service refused the request ({response.status_code} {error}).")
    body = response.json()
    return TokenSet(
        access_token=body["access_token"],
        refresh_token=body["refresh_token"],
        expires_at=time.time() + int(body.get("expires_in", 1200)),
    )


def exchange_code(client: httpx.Client, client_id: str, code: str, verifier: str) -> TokenSet:
    return _token_request(
        client,
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )


def refresh(client: httpx.Client, client_id: str, refresh_token: str) -> TokenSet:
    return _token_request(
        client,
        {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id},
    )


def fetch_jwks(client: httpx.Client) -> dict:
    try:
        response = client.get(JWKS_URL)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise SSOError(f"Could not fetch EVE's signing keys: {exc.__class__.__name__}") from None
    return response.json()


def verify_access_token(token: str, jwks: dict, client_id: str) -> CharacterIdentity:
    """Check signature, expiry, issuer and audience, then read who logged in."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise SSOError("The login token is malformed.") from None

    key = None
    for jwk in jwks.get("keys", []):
        if jwk.get("kid") == header.get("kid") and jwk.get("alg") in ALLOWED_ALGORITHMS:
            key = jwt.PyJWK(jwk)
            break
    if key is None:
        raise SSOError("The login token was signed with a key EVE does not publish.")

    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=list(ALLOWED_ALGORITHMS),
            audience=EVE_AUDIENCE,
            options={"require": ["exp", "iss", "sub", "aud"], "verify_iss": False},
            leeway=30,
        )
    except jwt.ExpiredSignatureError:
        raise SSOError("The login token has expired.") from None
    except jwt.InvalidAudienceError:
        raise SSOError("The login token was not issued for EVE Online.") from None
    except jwt.PyJWTError as exc:
        raise SSOError(f"The login token failed verification ({exc.__class__.__name__}).") from None

    if claims["iss"] not in ACCEPTED_ISSUERS:
        raise SSOError("The login token was not issued by EVE's login service.")
    audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    if client_id not in audiences:
        raise SSOError("The login token was issued to a different application.")

    prefix = "CHARACTER:EVE:"
    if not str(claims["sub"]).startswith(prefix):
        raise SSOError("The login token is not for an EVE character.")
    scopes = claims.get("scp", [])
    if isinstance(scopes, str):
        scopes = [scopes]
    return CharacterIdentity(
        character_id=int(str(claims["sub"])[len(prefix) :]),
        name=str(claims.get("name", "")),
        scopes=tuple(scopes),
    )
