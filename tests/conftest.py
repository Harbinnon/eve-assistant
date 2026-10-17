import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from eve_assistant.config import Config

CLIENT_ID = "test-client-id"
KID = "JWT-Signature-Key"


@pytest.fixture(scope="session")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def jwks(signing_key):
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    public.update({"kid": KID, "alg": "RS256", "use": "sig"})
    return {"keys": [public], "SkipUnresolvedJsonWebKeys": True}


@pytest.fixture
def make_token(signing_key):
    def _make(**overrides):
        claims = {
            "sub": "CHARACTER:EVE:2112000001",
            "name": "Test Pilot",
            "scp": ["esi-skills.read_skills.v1", "esi-skills.read_skillqueue.v1"],
            "aud": [CLIENT_ID, "EVE Online"],
            "iss": "https://login.eveonline.com",
            "exp": int(time.time()) + 1200,
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": KID})

    return _make


@pytest.fixture
def config():
    return Config(
        client_id=CLIENT_ID,
        callback_url="http://localhost:8765/callback",
        contact="tests",
        mode="local",
    )


@pytest.fixture(autouse=True)
def no_static_data_check_over_the_network(request, monkeypatch):
    """Background ticks check CCP for new static data once a day; tests never
    reach the network. Tests of the check itself opt out with the marker."""
    if request.node.get_closest_marker("real_static_data_check"):
        return
    from eve_assistant import refresh

    monkeypatch.setattr(refresh, "_check_static_data", lambda *_args: [])
