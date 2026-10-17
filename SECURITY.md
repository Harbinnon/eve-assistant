# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Report it privately through GitHub: open the repository's **Security** tab
and choose **Report a vulnerability**. Include what you found, how to
reproduce it, and what an attacker could do with it.

You can expect an acknowledgement within a week. Fixes are made on a
private branch and released before details are discussed publicly.

## What counts

Anything that could expose a user's EVE login tokens, character data or
computer, for example:

- reading or changing dashboard data from another website or another
  device;
- getting a token, webhook URL or other secret out of the tool;
- making the tool run code or commands it should not;
- a dependency with a known vulnerability that the tool actually uses.

## Not a vulnerability here

- Malware already running as the user: by design the tool cannot protect
  tokens from programs running under the same Windows login (see the threat
  model in docs/design.md).
- Problems in EVE Online or CCP's services themselves: report those to CCP.

## Supported versions

Only the latest version on `main` is supported.
