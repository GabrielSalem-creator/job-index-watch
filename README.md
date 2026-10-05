# job-index-watch

Every 5 minutes, GitHub Actions runs `watch.py`. If the job index server (`mcp-agent-vps`) is down,
it first tries to wake or restart it; if it stays down for ~7 minutes, it switches on the standby
machine on the second account, which loads the latest database copy from the private
`job-index-data` repository and continues scraping. No AI involved: fixed rules only.

Secrets (Settings → Secrets → Actions): `BOXD_TOKEN`, `BOXD_TOKEN_2`.
