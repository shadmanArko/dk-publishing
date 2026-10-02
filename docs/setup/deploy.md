# Deploy to a server

One small Linux server (Ubuntu 22.04+ or Debian 12+, 2 GB RAM is enough) runs everything in Docker:
the scheduler, the database, daily backups and a web server for temporary media links. Only ports
22 (SSH), 80 and 443 are open. The Dagster dashboard is **not** published; you reach it through an SSH
tunnel.

## What you need
| Need | Example |
| --- | --- |
| A server you can SSH into as root with a key | `root@203.0.113.10` |
| A domain name for media links, pointing at the server | `media.yourbusiness.com` (an `A` record to the server's IP) |
| A working `dk.json` | `make check-setup` is green on your computer |
| `rsync` and `python3` on your computer | already on a Mac |

The domain is needed only for Instagram and Threads photos and Instagram reels: those platforms
download the file from a public address. Caddy gets the HTTPS certificate by itself.

## Fresh server or shared server?
- **Fresh server (nothing else on ports 80/443):** use the steps below as written. This stack brings
  its own web server and gets the HTTPS certificate itself.
- **Shared server (another web server such as Caddy already owns 80/443, for example your ordering
  system):** follow the steps, but use `DK_SHARED_SERVER=1` in step 1 and `--shared NETWORK` in step 3
  (see "Shared server" at the end). Nothing existing is touched except one block you add to the
  existing web server.

## 1. Prepare the server (once)
```bash
ssh root@SERVER 'bash -s' < deploy/bootstrap.sh
```
On a shared server use `ssh root@SERVER 'DK_SHARED_SERVER=1 bash -s' < deploy/bootstrap.sh`, which
only creates this project's folders. For a fresh server this installs Docker, the firewall (22, 80, 443), fail2ban and automatic security updates. If your
SSH key is installed it also turns password login off.

## 2. Decide what is live
Open [`deploy/platforms.production.yaml`](../../deploy/platforms.production.yaml). Each platform's
`mode` is `live` (posts for real), `dry_run` (rehearses, posts nothing) or `off`. Facebook, Instagram,
Threads and YouTube are `live` as shipped. Set the ones you are not ready for to `dry_run`.

## 3. Deploy
```bash
deploy/deploy.sh root@SERVER media.yourbusiness.com --secrets
```
It copies the code, writes the server's own `dk.json` (your local one with the media settings filled
in for you), copies the Google key, builds the image and starts everything. At the end it runs the
configuration check inside the server and prints the result. Later deploys: leave off `--secrets`
unless a key or token changed.

## 4. Look at it
```bash
ssh -L 3000:localhost:3000 root@SERVER
```
Then open <http://localhost:3000>. Runs, schedules and logs are there.

## What runs by itself
| What | When |
| --- | --- |
| Sheet sync | within 2 minutes of a change, and every 15 minutes as a safety net |
| Publishing, preparing, checking uncertain posts | as each is due |
| Telegram alerts | within a minute of a failure |
| Morning digest | 08:00 Berlin time |
| Renewal of the Threads token | nightly check; renews when it is about a week old |
| Cleanup of abandoned media links | every 5 minutes |
| Database backup | daily, last 14 kept in `/srv/dk/backups` on the server |

## Safe habits
- **Update a token or a key:** edit `dk.json` on your computer, then deploy with `--secrets`. Do not
  edit the server's copy by hand: the system rewrites it when it renews a token.
- **Keep `dk.json` closed in your editor** after saving. An old window saved later overwrites newer
  values (this happened once).
- **Backups live on the same server.** Copy `/srv/dk/backups` somewhere else now and then
  (`rsync -a root@SERVER:/srv/dk/backups/ ~/dk-backups/`).
- **Roll back:** deploy the previous version of the code again; the database only ever moves forward.

## If something is wrong
| Symptom | Look here |
| --- | --- |
| Deploy ends with `[FAIL]` lines | read them: they name the missing value in `dk.json` |
| No Telegram message arrives | `ssh root@SERVER 'cd /srv/dk/app/deploy && docker compose -f compose.prod.yaml --env-file .env logs daemon \| tail -50'` |
| Instagram says it cannot fetch the media | the domain must point at the server and port 443 must be open: `curl -I https://media.yourbusiness.com/` should answer `404` (that is correct: only exact links work) |

## Shared server (another web server already owns ports 80 and 443)
1. Find the existing web server's Docker network: `docker inspect <its container> --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'`
2. Deploy with that network: `deploy/deploy.sh root@SERVER media.yourbusiness.com --secrets --shared NETWORK`.
   This stack then publishes no ports and is reachable on that network as `dk-media`.
3. Add this block to the existing web server's Caddyfile and reload it:
   ```
   media.yourbusiness.com {
   	reverse_proxy dk-media:80
   }
   ```
   Back the file up first, check it with `caddy validate`, then `caddy reload` (a reload keeps
   serving; an invalid file is refused and the old one stays active).
4. Check from outside: `curl -I https://media.yourbusiness.com/` answers `404` (correct).
The project is always named `dk-publishing`, so it can never be confused with another Compose project
on the server.
