# watch.py — runs on GitHub Actions every 5 minutes and keeps the job index alive on a chain of
# machines, one per boxd account, only one of them running at a time (the others stay off and
# cost nothing):
#   1. find the active machine (each answers /api/role); healthy -> make sure the public domain
#      is attached to it, done
#   2. none healthy -> the last active one (the machine the domain is attached to) gets a few
#      minutes to come back: start / wake / reboot. An account that is out of credits cannot start
#      a machine: then there is nothing to wait for
#   3. still down -> start the next machine of the chain (skipping any that cannot start), run its
#      takeover (latest copy + hourly change files from GitHub, then scraping and the website),
#      and move the public domain to it
# Secrets: BOXD_TOKEN, BOXD_TOKEN_2, BOXD_TOKEN_3, BOXD_TOKEN_4 (one per account).
import os
import sys
import time

import requests
from boxd import Boxd

DOMAIN = "jobs.centrality.lol"
CHAIN = [("mcp-agent-vps", "BOXD_TOKEN"), ("jobs-standby", "BOXD_TOKEN_2"),
         ("jobs-node-3", "BOXD_TOKEN_3"), ("jobs-node-4", "BOXD_TOKEN_4")]
REMOTE = "/home/boxd/jobscraper"
REVIVE_MINUTES = 7
TAKEOVER_MINUTES = 40


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def client(token_var):
    tok = os.environ.get(token_var)
    return Boxd(api_key=tok) if tok else None


def role(name):
    try:
        r = requests.get(f"https://{name}.boxd.sh/api/role", timeout=30, allow_redirects=False)
        return r.json() if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def healthy(name):
    s = role(name)
    return bool(s and s.get("role") == "active" and s.get("healthy")), s


def domain_owner():
    """The machine the public domain is attached to (= the last active one), or None."""
    for name, var in CHAIN:
        b = client(var)
        if not b:
            continue
        try:
            if any(d.domain == DOMAIN for d in b.domains.list()):
                return name
        except Exception:
            pass
        finally:
            b.close()
    return None


def attach_domain(name):
    """Attach the public domain to this machine (removed from the others first)."""
    for other, var in CHAIN:
        b = client(var)
        if not b:
            continue
        try:
            bound = [d for d in b.domains.list() if d.domain == DOMAIN]
            if other == name:
                if not bound:
                    mid = b.machines.get(name).id
                    b.domains.create(DOMAIN, mid)
                    log(f"{DOMAIN} attached to {name}")
            elif bound:
                b.domains.delete(DOMAIN)
                log(f"{DOMAIN} detached from {other}")
        except Exception as e:
            log(f"domain on {other}: {type(e).__name__}: {str(e)[:150]}")
        finally:
            b.close()


def start(name, var):
    """Start / wake a machine -> True when it runs. Out of credits -> False at once."""
    b = client(var)
    if not b:
        return False
    try:
        state = (b.machines.get(name).status or "").lower()
        if state == "running":
            return True
        try:
            if "hibernat" in state:
                b.machines.wake(name)
            elif "pause" in state or "suspend" in state:
                b.machines.resume(name)
            else:
                b.machines.start(name)
        except Exception as e:
            log(f"{name} cannot start: {str(e)[:160]}")
            return False
        b.machines.wait_until_ready(name)
        return True
    except Exception as e:
        log(f"{name}: {type(e).__name__}: {str(e)[:160]}")
        return False
    finally:
        b.close()


def revive(name, var):
    """A few minutes for the last active machine to come back by itself or after a start."""
    if not start(name, var):
        return False
    for _ in range(REVIVE_MINUTES):
        ok, _ = healthy(name)
        if ok:
            return True
        time.sleep(60)
    b = client(var)
    try:
        log(f"rebooting {name} (running but not answering)")
        b.machines.reboot(name)
    except Exception:
        pass
    finally:
        b.close()
    for _ in range(4):
        time.sleep(60)
        if healthy(name)[0]:
            return True
    return False


def takeover(name, var):
    if not start(name, var):
        return False
    b = client(var)
    try:
        r = b.machines.exec(name, f"cd {REMOTE} && sudo systemd-run --uid=boxd --gid=boxd --working-directory={REMOTE} "
                                  f"--unit=takeover-$(date +%s) --collect -p StandardOutput=append:{REMOTE}/logs/takeover.log "
                                  f"-p StandardError=append:{REMOTE}/logs/takeover.log {REMOTE}/.venv/bin/python failover.py --takeover")
        log(f"takeover started on {name} (exit {r.exit_code})")
    finally:
        b.close()
    for _ in range(TAKEOVER_MINUTES):
        time.sleep(60)
        ok, s = healthy(name)
        if ok:
            log(f"{name} is active and scraping")
            return True
    log(f"{name} did not report active in {TAKEOVER_MINUTES} minutes")
    return False


def main():
    for name, var in CHAIN:                      # 1. someone is healthy: keep the domain on it
        ok, s = healthy(name)
        if ok:
            log(f"{name} is active and healthy")
            attach_domain(name)
            return 0
    last = domain_owner() or CHAIN[0][0]
    names = [n for n, _ in CHAIN]
    i = names.index(last) if last in names else 0
    log(f"no healthy machine; the last active was {last}")
    if revive(last, CHAIN[i][1]):                # 2. the last active comes back
        log(f"{last} recovered")
        attach_domain(last)
        return 0
    for k in range(1, len(CHAIN)):               # 3. the next machines of the chain, in order
        name, var = CHAIN[(i + k) % len(CHAIN)]
        log(f"switching to {name}")
        if takeover(name, var):
            attach_domain(name)
            return 0
    log("no machine of the chain could take over")
    return 1


if __name__ == "__main__":
    sys.exit(main())
