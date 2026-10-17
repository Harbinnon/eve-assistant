# Setting up your ESI application

Each person who runs the tool registers their own ESI application. This
takes a few minutes and is done once (twice if you also run a hosted copy;
see the design, section 6.2).

## 1. Register

1. Go to <https://developers.eveonline.com/>, log in with your EVE account,
   and accept the developer licence if asked. Accepting it makes you
   responsible for your application.
2. Create a new application.
   - **Name:** anything you'll recognise; characters see it on EVE's login
     page. Don't present it as CCP's.
   - **Description:** e.g. `Personal planning assistant. Read-only.`
   - **Callback URL** for a local copy, exactly:
     ```
     http://localhost:8765/callback
     ```
     The portal allows plain `http` only for `localhost`. A hosted copy
     needs its own application with its public `https://…/callback`.
   - **Scopes:** tick these 21, all read-only. Leave everything else
     unticked, especially anything with "write", "manage" or "corporation"
     in the name.
     ```
     esi-skills.read_skills.v1
     esi-skills.read_skillqueue.v1
     esi-clones.read_clones.v1
     esi-clones.read_implants.v1
     esi-assets.read_assets.v1
     esi-wallet.read_character_wallet.v1
     esi-industry.read_character_mining.v1
     esi-industry.read_character_jobs.v1
     esi-characters.read_blueprints.v1
     esi-characters.read_standings.v1
     esi-markets.read_character_orders.v1
     esi-contracts.read_character_contracts.v1
     esi-location.read_location.v1
     esi-location.read_ship_type.v1
     esi-location.read_online.v1
     esi-fittings.read_fittings.v1
     esi-universe.read_structures.v1
     esi-markets.structure_markets.v1
     esi-characters.read_freelance_jobs.v1
     esi-characters.read_loyalty.v1
     esi-killmails.read_killmails.v1
     ```
     The last three were added in version 0.6: an application made earlier
     needs them ticked before anyone logs in again, or EVE refuses the login.
     Scopes can be changed later on the application page, but characters
     then need to log in to the tool again to pick up the new ones.
3. Save. The page shows a **Client ID** and a **Secret Key**.

## 2. Client ID and secret

- **Client ID:** not secret. It goes in your local config file
  (`config/local.toml`, which is gitignored), never in the repository.
  ```toml
  [esi]
  client_id = "<your client id>"
  callback_url = "http://localhost:8765/callback"
  ```
- **Secret Key:** **not used.** The tool uses the PKCE login flow, which
  doesn't need it. Don't paste it anywhere: not in chat, not in config,
  not in the repository. If it ever leaks, regenerate it on the application
  page; nothing breaks.

## 3. Logging characters in

Done from the tool once it's running: it opens EVE's own login page in
your browser for each character. Refresh tokens are stored encrypted on
your machine and never leave your copy.
