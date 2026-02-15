# Deploy sauna_control backend on Ubuntu VPS

Step-by-step guide to run the FastAPI server (with schedule + UI) on a fresh Ubuntu VPS.

## 1. Prerequisites on the VPS

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git
```

Check Python version (need 3.9+):

```bash
python3 --version
```

## 2. Get the code

**Option A – Clone from GitHub:**

```bash
sudo mkdir -p /opt/sauna
sudo chown "$USER:$USER" /opt/sauna
cd /opt/sauna
git clone https://github.com/stevetheaiassistant/sauna_control_working.git .
cd backend_ui
```

This clones the default branch. Use `git checkout On-Scheduling` for the schedule branch. The app runs from the `backend_ui` directory (where `server.py` and `requirements.txt` live).

**If Git asks for username/password:** GitHub no longer accepts account passwords for HTTPS. Use one of these:

- **Personal Access Token (PAT)**  
  1. On GitHub: **Settings** → **Developer settings** → **Personal access tokens** → **Tokens (classic)** → **Generate new token**.  
  2. Give it the **repo** scope, generate, and copy the token.  
  3. When the VPS prompts for password, paste the **token** (not your GitHub password).  
  Username can stay `stevetheaiassistant` (or your GitHub username).

- **Clone over SSH (no prompt)**  
  On the VPS, create an SSH key, add the public key to GitHub (or as a deploy key for this repo), then clone with:

  ```bash
  cd /opt/sauna
  git clone git@github.com:stevetheaiassistant/sauna_control_working.git .
  cd backend_ui
  ```

- **Make the repo public**  
  If the repo is public, `git clone` over HTTPS usually does not ask for credentials. You can make it public in **Settings** → **General** → **Danger Zone** → **Change repository visibility**, then run the clone again.

**Option B – Copy only backend_ui from your machine:**

On your Mac, from the project directory:

```bash
scp -r backend_ui user@YOUR_VPS_IP:/opt/sauna/
```

Then on the VPS use `/opt/sauna/backend_ui` as the app directory for the steps below.

## 3. Python app setup

On the VPS, go to the app directory. If you cloned the full repo, that’s `backend_ui`:

```bash
# If you cloned: you're already in /opt/sauna; then:
cd /opt/sauna/backend_ui

# Or if you copied only backend_ui:
cd /opt/sauna/backend_ui

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 4. Configure secrets

From the **backend_ui** directory (so `server.py` is in the current folder):

```bash
cd /opt/sauna/backend_ui
cp .env.example .env
nano .env   # or vim / your editor
```

Set **at least** these (no quotes needed; replace with your real values):

- `SAUNA_DEVICE_TOKEN` – same value you put in the ESP32 `secrets.h` as `DEVICE_TOKEN`.
- `SAUNA_APP_TOKEN` – a secret you’ll paste into the web UI “Auth” field (e.g. generate with `openssl rand -hex 24`).

Optional:

- `SAUNA_DEVICE_ID=sauna-1` (default)
- `SAUNA_DB=sauna.db` (default; will be created in the current directory)

Save and exit. **Do not commit `.env`.**

## 5. Test run

```bash
cd /opt/sauna/backend_ui
source venv/bin/activate
uvicorn server:app --host 0.0.0.0 --port 8000
```

- From the VPS: `curl http://127.0.0.1:8000/` should return HTML.
- From your computer: open `http://YOUR_VPS_IP:8000/` (if the firewall allows port 8000).

Stop with `Ctrl+C` when done testing.

## 6. Run as a systemd service (survives reboot)

Create the service file:

```bash
sudo nano /etc/systemd/system/sauna.service
```

Paste this (replace `YOUR_USERNAME`; paths assume app is in `/opt/sauna/backend_ui`):

```ini
[Unit]
Description=Sauna Control API
After=network.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/opt/sauna/backend_ui
EnvironmentFile=/opt/sauna/backend_ui/.env
ExecStart=/opt/sauna/backend_ui/venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Replace `YOUR_USERNAME` with your actual Linux username (e.g. `ubuntu` on many clouds).

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable sauna
sudo systemctl start sauna
sudo systemctl status sauna
```

Useful commands later:

- Logs: `sudo journalctl -u sauna -f`
- Restart: `sudo systemctl restart sauna`

## 7. Firewall (allow port 8000)

If you use `ufw`:

```bash
sudo ufw allow 8000/tcp
sudo ufw status
sudo ufw enable   # if you haven’t already
```

Then the UI is at `http://YOUR_VPS_IP:8000/`.

## 8. Cloud provider firewall (required for many VPS)

If the site won't load from your browser even though the service is running, the **cloud provider's firewall** is likely blocking port 8000. UFW only controls the OS firewall; the provider has a separate firewall.

**IONOS:** Server & Cloud → Network → Firewall Policies → select your server's firewall → edit incoming rules → add rule: TCP port 8000, source 0.0.0.0/0. Save.

**Other providers:** DigitalOcean (Firewalls), AWS (Security Groups), Linode (Firewalls), Vultr (Firewall) — add inbound rule for TCP 8000.

## 9. HTTPS with a domain (recommended for production)

You need a domain name (e.g. `sauna.wilsondesignllc.com`) with a DNS A record pointing to your VPS IP. Caddy will obtain a free Let's Encrypt certificate automatically.

### 9.1 Install Caddy

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy
```

### 9.2 Configure Caddy

```bash
sudo nano /etc/caddy/Caddyfile
```

Replace with your actual domain:

```
sauna.wilsondesignllc.com {
    reverse_proxy localhost:8000
}
```

Save and reload:

```bash
sudo systemctl reload caddy
```

### 9.3 Firewall

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw reload
```

Also open ports 80 and 443 in your **cloud provider firewall** (IONOS, etc.).

### 9.4 ESP32 configuration

In `secrets.h`, set `API_HOST` to your domain **without** a port (HTTPS uses 443 by default):

```c
#define SECRET_API_HOST        "sauna.wilsondesignllc.com"
```

The firmware uses HTTPS when `API_HOST` has no port (e.g. domain only). For plain HTTP during development, use `IP:8000`.

### 9.5 Access the UI

- **HTTPS**: `https://sauna.wilsondesignllc.com/`
- The sauna app and ESP32 will communicate over encrypted HTTPS.

---

## 10. Deploy updates (after code changes)

From your Mac, in the project directory:

```bash
scp -r backend_ui root@YOUR_VPS_IP:/opt/sauna/
ssh root@YOUR_VPS_IP "systemctl restart sauna && systemctl status sauna"
```

Replace `YOUR_VPS_IP` with your VPS IP (e.g. `74.208.194.144`). This copies the updated `backend_ui` folder and restarts the sauna service.

---

## 11. ESP32 stability

**Power supply**: Use a dedicated 5V 2A+ supply instead of USB from a computer. USB can brown out under WiFi+HTTPS+relay load and cause crashes or freezes.

**Stack size (platform.local.txt)**: If the ESP32 still crashes or freezes under HTTPS load, increase the main loop stack size. Create this file in your Arduino ESP32 hardware folder:

**Path:** `~/Arduino/hardware/espressif/esp32/platform.local.txt` (or equivalent for PlatformIO)

**Content:**
```
compiler.c.extra_flags=-DARDUINO_LOOP_STACK_SIZE=16384
```

Then recompile and flash. Stable poll intervals: 3s desired, 5s telemetry, 30s schedule.

---

## Quick checklist

- [ ] Python 3.9+ and venv created
- [ ] `pip install -r requirements.txt` in venv
- [ ] `.env` created in `backend_ui/` from `.env.example` with `SAUNA_DEVICE_TOKEN` and `SAUNA_APP_TOKEN`
- [ ] Test: `uvicorn server:app --host 0.0.0.0 --port 8000` and open UI in browser
- [ ] systemd service installed and `systemctl status sauna` shows active
- [ ] UFW allows 8000 (`sudo ufw allow 8000/tcp`)
- [ ] Cloud provider firewall (IONOS, AWS, etc.) allows inbound TCP 8000
- [ ] ESP32 `secrets.h` has same `DEVICE_TOKEN` and `API_HOST` pointing to this VPS
