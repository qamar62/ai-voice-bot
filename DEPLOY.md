# Deploying Fiva on your website (Proxmox LXC)

> **Using Docker Compose + Cloudflare Tunnel? Read the section at the bottom
> first — it replaces steps 2–5.**

Target setup:

```
five.tours (Next.js)  ──"Talk to Fiva" button──▶  https://voice.five.tours
                                                        │ nginx (LXC)
                                    ┌───────────────────┴───────────────┐
                                    │  /            → web/index.html    │  branded UI
                                    │  /api/offer   → 127.0.0.1:7860    │  WebRTC signaling
                                    └───────────────────┬───────────────┘
                                                 bot.py (systemd)
                                          audio flows browser ⇄ LXC over UDP
```

## 1. Create the LXC

Ubuntu 24.04 container, 2 vCPU / 2 GB RAM is plenty. Give it a static IP on your bridge.
**Important for WebRTC**: audio travels over UDP directly to this container — it must be
reachable from the internet on UDP (see step 5).

```bash
apt update && apt install -y python3.12-venv git nginx
```

## 2. Install the bot

```bash
mkdir -p /opt/fiva && cd /opt/fiva
# copy the realtime/ folder here (git clone or scp), then:
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env /opt/fiva/.env        # your NVIDIA / ElevenLabs / BACKEND keys
```

Also copy the `data/` folder (agents.json + settings.json) one level up, or adjust
`DATA_DIR` in bot.py — it reads the agent prompt from `../data/`.

## 3. systemd service

`/etc/systemd/system/fiva.service`:

```ini
[Unit]
Description=Fiva realtime voice assistant
After=network-online.target

[Service]
WorkingDirectory=/opt/fiva/realtime
ExecStart=/opt/fiva/.venv/bin/python bot.py --host 127.0.0.1 --port 7860
Restart=always
RestartSec=3
EnvironmentFile=/opt/fiva/.env

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now fiva
journalctl -u fiva -f        # watch logs
```

## 4. nginx + HTTPS (required — browsers block mic on plain HTTP)

Point DNS `voice.five.tours` → the LXC's public IP (or your reverse-proxy host).

`/etc/nginx/sites-available/voice.five.tours`:

```nginx
server {
    server_name voice.five.tours;
    listen 443 ssl http2;
    # certbot --nginx -d voice.five.tours   (fills these in)

    # Branded voice UI
    root /opt/fiva/realtime/web;
    index index.html;

    # WebRTC signaling to the bot
    location /api/ {
        proxy_pass http://127.0.0.1:7860;
        proxy_set_header Host $host;
        proxy_read_timeout 3600;
    }
}
```

```bash
ln -s /etc/nginx/sites-available/voice.five.tours /etc/nginx/sites-enabled/
apt install -y certbot python3-certbot-nginx && certbot --nginx -d voice.five.tours
nginx -t && systemctl reload nginx
```

## 5. UDP / firewall (the part people miss)

Signaling goes through nginx, but **audio is UDP directly between browser and the LXC**.

- If the LXC has a public IP: allow inbound/outbound UDP (ephemeral ports) in your
  Proxmox/router firewall. Done.
- If the LXC is NATed behind your router: forward a UDP range to the LXC, or run a
  TURN server. Quick TURN setup on the same LXC:

```bash
apt install -y coturn
# /etc/turnserver.conf:  listening-port=3478, realm=voice.five.tours,
#   user=fiva:STRONG_SECRET, plus cert paths from certbot
systemctl enable --now coturn
```

Then add the TURN server in `web/index.html`:

```js
iceServers: [
  { urls: "stun:stun.l.google.com:19302" },
  { urls: "turn:voice.five.tours:3478", username: "fiva", credential: "STRONG_SECRET" },
]
```

**Test first without TURN** — if calls connect from your phone on mobile data (not
your wifi), you don't need it.

## 6. Button on five.tours

Add next to the Ask AI chat button, or inside it. Simplest — opens the assistant
in a popup:

```tsx
<button
  onClick={() => window.open('https://voice.five.tours', 'fiva',
    'width=480,height=720,menubar=no,toolbar=no')}
  className="... same styling as the Ask AI button ..."
>
  ✦ Talk to Fiva
</button>
```

Embedding as an `<iframe>` in a modal also works, but you MUST pass the mic
permission through: `<iframe src="https://voice.five.tours" allow="microphone" />`.

## 7. Checklist

- [ ] `https://voice.five.tours` shows the teal Fiva page
- [ ] Start call → mic prompt → greeting plays
- [ ] Test from a phone on mobile data (validates UDP/NAT path)
- [ ] Booking creates a Voice Inquiry + Discord ping + email
- [ ] `journalctl -u fiva` clean; `systemctl restart fiva` after any .env change

## Scaling note

One `bot.py` process handles multiple simultaneous calls (each connection spawns
its own pipeline), but CPU adds up — Silero VAD runs per call. Watch load with
2–3 concurrent calls; give the LXC more cores if needed.

---

# Docker Compose + Cloudflare Tunnel (recommended for your setup)

Everything ships in this folder: `Dockerfile`, `docker-compose.yml`, `nginx.conf`.

## The one thing you MUST understand

Cloudflare Tunnel carries **HTTP only**. The voice page and `/api/offer`
signaling pass through it fine — but **WebRTC audio is UDP and cannot go
through the tunnel**. Without a direct UDP path the call "connects" and stays
silent. That's what the `coturn` service is for: a TURN relay the browser and
bot both reach directly, bypassing Cloudflare for audio only.

## Setup

1. **Copy this folder + `../data` to the LXC**, fill `.env`, then:

```bash
cd realtime
# pick a strong TURN secret, set it in BOTH places:
#   docker-compose.yml  → --user=fiva:CHANGE_THIS_SECRET
#   web/index.html      → credential: "CHANGE_THIS_SECRET"
docker compose up -d --build
```

2. **Cloudflare Tunnel**: add a public hostname in your tunnel config:
   `voice.five.tours → http://localhost:8090`
   (same pattern as your other services — orange cloud is fine here.)

3. **TURN needs a direct path (NOT through Cloudflare):**
   - DNS: create `turn.five.tours` as **DNS only (grey cloud)** pointing to
     your public IP.
   - Router/Proxmox firewall: forward **UDP+TCP 3478** and **UDP 49160–49200**
     to this LXC.

4. **Test order:**
   - `docker compose logs -f fiva-bot` — bot starts, Pipecat banner shows
   - Open `https://voice.five.tours` → Start call → greeting plays
   - **Test from a phone on mobile data** — this validates the TURN path.
     If wifi works but mobile data doesn't, the TURN port-forward is wrong.

## Updating

```bash
git pull && docker compose up -d --build   # bot code changes
docker compose restart voice-web           # UI-only changes (web/index.html)
```

## Why not everything through the tunnel?

Cloudflare's tunnel terminates HTTP(S); TURN/UDP relay is exactly the traffic
it does not carry on your plan. Grey-clouding one `turn.` subdomain exposes
only the relay port — the bot, the UI, and all your other services stay behind
Cloudflare as they are today.
