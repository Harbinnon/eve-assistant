"""Hosted mode (roadmap 7.2): who may reach which part of the dashboard.

The hosted copy runs in a container on the owner's home network, behind the
home lab's reverse proxy and its Discord sign-in. Two kinds of request are
allowed, and they never overlap:

- **Through the proxy** (the proxy's own address): the dashboard, with the
  sign-in's identity header, matched to the owner.
- **Straight from the home network** (any other address in it): only
  /ingest/, with a token, for the gaming PC's log sender. The proxy's address
  is inside the home network too, so it is excluded by name: nothing that came
  through the proxy (and so possibly from the internet) can reach /ingest/.

The addresses come from the server's config, never from this repository.
"""

import ipaddress

INGEST = "/ingest"


def is_ingest(path: str) -> bool:
    return path == INGEST or path.startswith(INGEST + "/")


def allowed(path: str, client: str | None, proxy: str, home_network: str) -> bool:
    """Whether a connection from `client` may ask for `path`."""
    try:
        address = ipaddress.ip_address(client or "")
    except ValueError:
        return False
    through_proxy = address == ipaddress.ip_address(proxy)
    if is_ingest(path):
        return address in ipaddress.ip_network(home_network) and not through_proxy
    return through_proxy
