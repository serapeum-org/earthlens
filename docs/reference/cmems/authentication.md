# Copernicus Marine — authentication

The CMEMS backend wraps the `copernicusmarine` toolbox's
`login()` primitive. After one successful authentication the toolbox
writes a credentials file under
`~/.copernicusmarine/.copernicusmarine-credentials`; every subsequent
process on the same machine reads it transparently and never has to
re-authenticate. This page covers how to get credentials in place for
the first run, and the four ways to supply them at request time.

## 1. Register a Copernicus Marine portal account

CMEMS access is free for everyone, including commercial use. Sign up
once at <https://marine.copernicus.eu/register>. The portal asks for
an email, a password, and an affiliation; account approval is
usually immediate.

You will end up with a username (the email or a chosen handle, as set
during registration) and the password you picked. Those two strings
are the entire credential set.

## 2. Pick a credential source

The earthlens backend recognises four credential sources, resolved in
this order on the first `download()` call:

1. **Explicit kwargs** — `service_username=` and `service_password=`
   passed directly to `CMEMS(...)`. Most explicit; right for
   notebooks where the credentials come from a secrets manager.
2. **Environment variables** — `COPERNICUSMARINE_SERVICE_USERNAME` /
   `COPERNICUSMARINE_SERVICE_PASSWORD`. Toolbox-native. Right for CI
   runs and worker processes that inherit secrets from the
   environment.
3. **Saved configuration directory** — a
   `.copernicusmarine-credentials` file under
   `~/.copernicusmarine/`. Written automatically by the first
   successful authentication (or by running `copernicusmarine login`
   from a shell). Right for interactive workstations.
4. **Explicit `credentials_file=`** — a path to a pre-existing
   `.copernicusmarine-credentials` file. Right for CI runs that
   mount the credentials as a secret rather than passing them as
   strings (avoids the username/password ever appearing in the
   process environment or argv).

If none of the four resolve, `CMEMS(...)` raises
`AuthenticationError` rather than blocking on the toolbox's
interactive prompt.

## 3. The interactive path (workstation)

```bash
pip install earthlens[cmems]
copernicusmarine login
```

The CLI prompts for the username and password once, validates them
against the auth server, and writes
`~/.copernicusmarine/.copernicusmarine-credentials`. Subsequent
`CMEMS()` calls — including ones in long-running notebook kernels and
on background workers spawned from the same machine — read that file
automatically; no further kwargs needed:

```python
from earthlens.cmems import CMEMS

cmems = CMEMS(
    start="2020-01-01",
    end="2020-01-07",
    variables={"cmems_mod_glo_phy_my_0.083deg_P1D-m": ["thetao"]},
    lat_lim=[30.0, 36.0],
    lon_lim=[-10.0, -4.0],
    path="data/cmems",
)
cmems.download()                    # auto-reads ~/.copernicusmarine/
```

## 4. The environment-variable path (CI / containers)

In a GitHub Actions / GitLab CI / Kubernetes setting, push the
username and password as repo / job secrets and let the toolbox pick
them up from the environment:

```yaml
env:
  COPERNICUSMARINE_SERVICE_USERNAME: ${{ secrets.CMEMS_USERNAME }}
  COPERNICUSMARINE_SERVICE_PASSWORD: ${{ secrets.CMEMS_PASSWORD }}
```

```python
from earthlens.cmems import CMEMS

# Toolbox reads COPERNICUSMARINE_SERVICE_USERNAME / _PASSWORD from env.
cmems = CMEMS(
    start="2020-01-01",
    end="2020-01-07",
    variables={"cmems_mod_glo_phy_my_0.083deg_P1D-m": ["thetao"]},
    lat_lim=[30.0, 36.0],
    lon_lim=[-10.0, -4.0],
    path="data/cmems",
)
cmems.download()
```

The same env vars are read by the live `e2e` test suite
(`pytest -m "cmems and e2e"`); set them locally to exercise the
end-to-end paths.

## 5. The mounted-file path (CI / secret managers)

For setups where the credentials file is mounted as a secret rather
than handed as a string (Kubernetes Secret, Docker secret, sealed-
secrets / SOPS):

```python
from pathlib import Path

from earthlens.cmems import CMEMS

cmems = CMEMS(
    start="2020-01-01",
    end="2020-01-07",
    variables={"cmems_mod_glo_phy_my_0.083deg_P1D-m": ["thetao"]},
    lat_lim=[30.0, 36.0],
    lon_lim=[-10.0, -4.0],
    path="data/cmems",
    credentials_file=Path("/var/run/secrets/cmems/.copernicusmarine-credentials"),
)
cmems.download()
```

The file at `credentials_file=` must be in the toolbox's native
format — i.e. one produced by
`copernicusmarine login --configuration-file-directory <dir>` and
then copied / mounted into place. The format is internal to the
toolbox and is not stable across major versions; pin to the
toolbox version you generated it under.

## 6. Verifying credentials

A quick smoke test that authenticates without downloading anything
substantial:

```python
import copernicusmarine as cm

# Reads the same credential sources as CMEMS() above.
cm.login(check_credentials_valid=True)
```

A `True` return means the auth server accepted the credentials.
`InvalidUsernameOrPassword` means the portal rejected them;
`CouldNotConnectToAuthenticationSystem` is a network / firewall
problem. earthlens wraps both into
`earthlens.cmems.AuthenticationError` with a pointer at the fix.

## 7. Rotating credentials

If you regenerate the password on the portal, the saved
configuration file goes stale. Either delete
`~/.copernicusmarine/` and re-run `copernicusmarine login`, or pass
`force_overwrite=True` to `copernicusmarine.login(...)` to rewrite
the file in place. The earthlens auth wrapper calls
`force_overwrite=True` on every `configure()` so explicit-kwarg
calls always refresh the saved file as well.

## 8. Two patterns to avoid

- **Do not commit the credentials file.** It contains the password
  in clear text. Add `.copernicusmarine-credentials` to your
  `.gitignore`.
- **Do not pass credentials through process argv.** Use one of the
  four documented sources rather than building `subprocess`
  invocations with the password on the command line — `ps`,
  shell history, and CI build logs all capture argv.

## References

- Toolbox installation: <https://help.marine.copernicus.eu/en/articles/8230433-copernicus-marine-toolbox-installation>
- Portal registration: <https://marine.copernicus.eu/register>
- Service commitments + licence: <https://marine.copernicus.eu/user-corner/service-commitments-and-licence>
- earthlens CMEMS usage: [Usage](usage.md)
- earthlens CMEMS API: [Reference](cmems.md)
