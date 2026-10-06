# Running on WSL2 with Docker

Sluice is developed and verified inside WSL2 (Ubuntu 24.04) with Docker Engine
running in the distro. This page collects what that setup needs, and the pitfalls already
hit, so nobody has to rediscover them.

## Recommended setup: everything inside WSL

**Keep the repository on the Linux filesystem** (`~/...`), not under `/mnt/c`. The journal
fsyncs on every commit, which is far slower across the Windows mount. SQLite's WAL mode
also relies on shared-memory file locking that WSL2's 9P-mounted Windows drives do not
guarantee.

**Docker Engine with systemd.** Enable systemd in the distro so Docker starts with it:

```ini
# /etc/wsl.conf
[boot]
systemd=true
```

Then `wsl --shutdown` from Windows, reopen the distro, and:

```bash
ps -p 1 -o comm=                      # prints: systemd
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"       # then open a new shell
docker info >/dev/null && echo ok
```

Without systemd, start the daemon by hand each time the distro starts:
`sudo service docker start`. Docker Desktop with WSL integration also works: the `docker`
CLI in WSL talks to it, and ports are published the same way.

**Then, from the repo root:**

```bash
docker compose up -d                  # sluice-redis on :6379
docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'
python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
scripts/verify.sh
```

Ports bound inside WSL are reachable from Windows on `localhost` (WSL2's localhost
forwarding), so the dashboard at `http://localhost:8000` opens in a Windows browser.

## Pitfall: the container name and the compose project

`docker-compose.yml` pins `container_name: sluice-redis`. Compose labels every
container with a **project name**, which defaults to the name of the directory holding
the compose file. If the existing container was created from a directory with a different
name, `docker compose up -d` refuses it:

```
Error response from daemon: Conflict. The container name "/sluice-redis" is already
in use by container "4e6a90c4...". You have to remove (or rename) that container to be
able to reuse that name.
```

This is why the compose file **stays at the repo root**: moving it into a subfolder would
change the project name and trigger exactly this. To see which project owns the container:

```bash
docker inspect sluice-redis --format '{{ index .Config.Labels "com.docker.compose.project" }}'
```

Any of these resolves it:

```bash
docker start sluice-redis                       # reuse it as it is
docker compose -p <that project> up -d              # adopt it under its own project name
docker rm -f sluice-redis && docker compose up -d   # recreate it
```

Recreating loses nothing: Redis runs with persistence off (`--appendonly no --save ""`).
`scripts/verify.sh` falls back to `docker start sluice-redis` on its own.

## Pitfall: Redis disappears

The container has **no restart policy** and **no persistence**. After `wsl --shutdown`, a
Windows reboot, or a Docker daemon restart, it is stopped and its streams are gone. Bring
it back with `docker start sluice-redis` (or `docker compose up -d`).

If you would rather it always come back, add `restart: unless-stopped` to the `redis`
service. The trade-off: it then holds port 6379 whenever Docker is running.

**When driving WSL from Windows** (`scripts\verify.bat`, or Python on the Windows side):
WSL tears the distro down shortly after the last `wsl.exe` client disconnects, and Docker
and Redis go with it. It has been observed dying 16 seconds after `docker compose up`.
Keep a WSL terminal open, or let `verify.bat` hold a hidden client open for its run.
Inside-WSL runs do not have this problem while your WSL terminal is open.

Symptom in the logs: `XAUTOCLAIM returned nothing`, or smoke's Redis checks failing after
earlier steps passed; `docker ps -a` shows the container exited with a short lifetime and
`exit=0`.

## Pitfall: line endings

A shell script with CRLF line endings fails in WSL with `$'\r': command not found`, and
`cmd.exe` mis-parses labels in LF-only batch files. `.gitattributes` pins `*.sh` to LF and
`*.bat` to CRLF in every checkout. If a checkout predates it:

```bash
git add --renormalize . && git checkout -- scripts/   # or: sed -i 's/\r$//' scripts/verify.sh
```

## Pitfall: stale processes on the service ports

A service process left over from an earlier run keeps serving **pre-edit code**: you fix a
bug, rerun, and watch the old process fail in exactly the old way. `scripts/verify.sh` stops
anything on 8100-8103 before it starts. By hand:

```bash
ss -ltnp | grep -E ':(8000|810[0-3])'      # who holds the ports
kill <pid of `sluice services`>            # stops all four children too
pkill -f "sluice services"                 # if the supervisor is already gone
```

## Ports

| Port | What | Started by |
|---|---|---|
| 6379 | Redis | `docker compose up -d` |
| 8000 | dashboard | `sluice dashboard` |
| 8100 | ground-truth ledger | `sluice services` |
| 8101-8103 | ticket, channel, pager | `sluice services` |

The test suite uses none of these: it starts its own services on free ports and uses
Redis DB 15.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Cannot connect to the Docker daemon` | daemon not running | `sudo systemctl start docker` (systemd) or `sudo service docker start` |
| `permission denied ... docker.sock` | user not in the `docker` group | `sudo usermod -aG docker "$USER"`, new shell |
| `container name "/sluice-redis" is already in use` | compose project mismatch | see [above](#pitfall-the-container-name-and-the-compose-project) |
| `[ingest] redis unavailable ... falling back` | Redis is stopped | `docker start sluice-redis` |
| `Address already in use` on 8100-8103 | stale services | see [above](#pitfall-stale-processes-on-the-service-ports) |
| `$'\r': command not found` | CRLF shell script | see [line endings](#pitfall-line-endings) |
| journal writes very slow, or `database is locked` | repo under `/mnt/c` | move the repo into the Linux filesystem |
| `verify.bat`: `.venv\Scripts\python.exe not found` | no Windows venv | create one on the Windows side, or use `scripts/verify.sh` inside WSL |
