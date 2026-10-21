# Camera upload over FTP: ports, setup and testing

Photographers can send photos straight from the camera to an event. Every event gets its own FTP login. A photo that
arrives this way is processed exactly like a dashboard upload: duplicate check, plan limits, thumbnail, web copy and
face detection. Guests can find it within seconds.

This guide covers which ports to open, how to turn the feature on, and how to test it: first on a laptop, then on a
server, then with a real camera.

---

## 1. Ports at a glance

| Port | Protocol | What it is for | Who must reach it |
|---|---|---|---|
| **8000** (or 443 behind a proxy) | TCP, HTTP/HTTPS | Dashboard, guest pages, API | Everyone |
| **2121** (your choice; 21 is the FTP standard) | TCP | FTP **control** connection: login and commands | Cameras |
| **60000-60049** (your choice) | TCP | FTP **passive data** connections: the photo bytes | Cameras |

FTP uses two kinds of connection:

- The camera logs in on the control port (2121).
- For each file, the server picks one port from the passive range, and the camera opens a second connection to it to
  send the photo.

**Open both the control port and the whole passive range.** If only 2121 is open, the camera logs in fine and then
hangs or fails on the first photo. This is the most common setup mistake.

**Sizing the passive range:** each file that is being sent at that moment uses one port, for a few seconds. With 50
ports, dozens of cameras can work at the same time. Widen the range, for example to 60000-60199, for very large
shoots.

The server's built-in limits (in `studio/camera.py`):

| Limit | Value |
|---|---|
| Connections in total | 400 (`PF_FTP_MAX_CONNECTIONS`) |
| Connections from one IP address | 40, about 20 cameras on one venue Wi-Fi (`PF_FTP_MAX_PER_IP`) |
| Largest file | 60 MB (`max_file_mb`); a bigger file is stopped mid-transfer with reply `552` |
| Wrong passwords | 10 for one camera login from one address in 15 minutes pause that login there (other cameras on the same Wi-Fi keep working); 60 for one login from anywhere, or 300 from one address, pause it too |
| Unfinished files | Removed after 10 minutes |
| File types taken | `.jpg .jpeg .png .webp .heic .heif` (anything else is ignored) |

---

## 2. Settings

Every setting can be given as a command-line flag on `python -m studio run`, or as an environment variable (which is
what Docker uses).

| Flag | Environment variable | Default | Meaning |
|---|---|---|---|
| `--ftp-port 2121` | `PF_FTP_PORT` | `0` (off) | Turns camera upload on, on this port |
| — | `PF_FTP_BIND` | `0.0.0.0` | Network interface to listen on |
| `--ftp-host ftp.example.com` | `PF_FTP_HOST` | Host of the public URL | Address shown to photographers and sent to cameras for data connections |
| `--ftp-passive 60000-60049` | `PF_FTP_PASSIVE` | Any free port | Passive data port range. **Set it on any server with a firewall.** |
| `--ftp-cert fullchain.pem` | `PF_FTP_CERT` | none | PEM certificate. Setting it turns FTPS on. |
| `--ftp-key privkey.pem` | `PF_FTP_KEY` | none | PEM private key for that certificate |
| `--ftp-require-tls` | `PF_FTP_REQUIRE_TLS=1` | off | Refuses plain FTP, so only FTPS cameras can connect |

When it starts, Studio prints a line like this:

```
Camera upload: FTP on port 2121 (plain FTP: add --ftp-cert for FTPS)
```

If the event page shows **"Not switched on for this server"**, the FTP port was not set.

---

## 3. Test on your laptop (10 minutes, no camera needed)

### 3.1 Install and start

```powershell
pip install -r requirements.txt          # includes pyftpdlib and pyopenssl
python -m studio run --ftp-port 2121 --ftp-passive 60000-60049
```

The start-up output shows the laptop's LAN address, for example `Studio dashboard: http://192.168.1.20:8000`.

### 3.2 Allow the ports in Windows Firewall (only needed for a phone or camera; not needed for tests on the same PC)

Run these in an **Administrator** PowerShell:

```powershell
New-NetFirewallRule -DisplayName "Studio web"      -Direction Inbound -Protocol TCP -LocalPort 8000        -Action Allow
New-NetFirewallRule -DisplayName "Studio FTP"      -Direction Inbound -Protocol TCP -LocalPort 2121        -Action Allow
New-NetFirewallRule -DisplayName "Studio FTP data" -Direction Inbound -Protocol TCP -LocalPort 60000-60049 -Action Allow
```

Remove the rules after testing with `Remove-NetFirewallRule -DisplayName "Studio FTP*"`.

### 3.3 Enable the event (platform admin), then create a camera login

Camera upload is **off for every event** until the platform admin enables it:

1. Open **/admin**, go to **Companies**, and open the company.
2. In the **Events** table, click **Enable** in the **Camera FTP** column for that event. The column then shows
   **On**, and later how many photos came from the camera.
3. **Turn off** in the same place hides the option from the event screen and stops the camera login at once. Photos
   already received stay. Every change is recorded in the company's admin history.

The company can't enable it by itself: until the admin does, the event screen shows no Camera upload card, and
the API answers `403 not_allowed`. To make yourself a platform admin: `python -m studio grant-admin you@example.com`.

Then the company creates the login:

1. Open the dashboard, sign in, and open the enabled event.
2. On the **Camera upload** card, click **Set up camera upload**.
3. Copy the **Server**, **Port**, **Username** (`cam-…`) and **Password**. The password is shown only once. If you
   lose it, click **New password**.

### 3.4 Send a photo the way a camera would

Pick one of these clients.

**curl** (included with Windows 10 and 11):

```powershell
curl.exe -T "C:\Photos\DSC_0001.JPG" "ftp://127.0.0.1:2121/" --user "cam-XXXXXX:PASSWORD"
```

**Python** (no extra install):

```powershell
python -c "import ftplib; f=ftplib.FTP(); f.connect('127.0.0.1',2121); f.login('cam-XXXXXX','PASSWORD'); f.storbinary('STOR DSC_0001.JPG', open(r'C:\Photos\DSC_0001.JPG','rb')); f.quit(); print('sent')"
```

**FileZilla:**

1. Open **File > Site Manager > New site**.
2. Set **Protocol** to FTP, **Host** to `127.0.0.1` and **Port** to `2121`.
3. Set **Encryption** to "Only use plain FTP" (or "Require explicit FTP over TLS" if you set up FTPS).
4. Set **Logon type** to Normal, then enter the user and password.
5. Under **Transfer Settings**, choose **Passive**.
6. Drag photos into the remote pane.

### 3.5 What you should see

- Within a few seconds the card shows **"Last photo received N s ago · 1 photo from the camera"**.
- The photo appears in the event and is processed (thumbnail, faces).
- The remote folder in FileZilla always looks **empty**. This is expected: each file is moved into the event as soon
  as it arrives, and the login cannot list, download or delete anything.

### 3.6 Check that the safety rules work

| Try this | Expected result |
|---|---|
| Send the same photo twice | The second copy is skipped. The card shows "Last file: … already uploaded". |
| Send a `.txt` file | Ignored. Nothing is added to the event. |
| Download or delete a file in FileZilla | Refused (`550`) |
| `cd ..` above `/` | You stay in `/` |
| Click **New password**, then log in with the old one | `530 Wrong camera username or password.` |
| Click **Turn off**, then log in | `530 Camera upload is switched off for this event.` |
| The admin clicks **Turn off** in the Camera FTP column, then log in | `530`, and the card disappears from the event screen |
| Suspend the company in the platform admin dashboard, then log in | `530` |
| Send a file larger than 60 MB | Stopped mid-transfer with `552` |
| Reach the plan's photo limit, then send | Not added. The card shows a note about the limit. |

### 3.7 Test from a phone or a real camera on the same network

1. Connect the laptop and the camera (or a phone with an FTP app) to the **same Wi-Fi or phone hotspot**.
2. Use the laptop's LAN address from step 3.1 as the server, for example `192.168.1.20`, with port `2121`.
3. Open the firewall ports from step 3.2.

---

## 4. Test FTPS (encrypted) locally

Cameras that offer "FTPS", "FTP over TLS" or "Secure FTP" will use it when the server has a certificate. A
self-signed certificate is fine for testing:

```powershell
python -c "from pathlib import Path; from netutil import ensure_cert; print(ensure_cert(Path('data/certs'), '127.0.0.1'))"
python -m studio run --ftp-port 2121 --ftp-passive 60000-60049 --ftp-cert data/certs/lan-127.0.0.1.crt --ftp-key data/certs/lan-127.0.0.1.key
```

The first command creates `data/certs/lan-127.0.0.1.crt` and `.key` (run it from the project folder). Then send a photo with TLS:

```powershell
curl.exe --ssl-reqd -k -T "C:\Photos\DSC_0001.JPG" "ftp://127.0.0.1:2121/" --user "cam-XXXXXX:PASSWORD"
```

`-k` accepts the self-signed certificate. Leave it out on a real server that has a real certificate. Add
`--ftp-require-tls` and try plain FTP again; the server should refuse it.

The camera card shows the protocol in use:

- **FTP**: no certificate is set.
- **FTPS if the camera offers it, else FTP**: a certificate is set.
- **FTPS, required**: `--ftp-require-tls` is set.

---

## 5. Automated tests

```powershell
python -m pytest tests/test_camera.py -v      # 9 camera tests, using a real FTP client
python -m pytest -q                           # whole suite
```

Each test starts its own FTP server on a free local port, so nothing needs to be opened in the firewall. The tests
cover:

- photos arriving in the event, including in sub-folders, with duplicates skipped
- write-only access
- temporary names that are renamed after upload
- wrong, rotated, switched-off, suspended and removed logins
- size and plan limits
- FTPS, including that the size limit also holds over TLS
- who may manage camera logins
- FTP being switched off
- the failed-login blocker

---

## 6. Production server (Linux)

### 6.1 DNS

Create the record `ftp.yourdomain.com` and point it at the server's public IP.

**It must be "DNS only", not proxied.** Cloudflare's orange cloud and similar HTTP proxies cannot carry FTP. The web
domain can stay proxied; FTP needs its own name that points directly at the server.

### 6.2 Firewall

Open the ports on the server itself:

```bash
sudo ufw allow 2121/tcp
sudo ufw allow 60000:60049/tcp
```

On AWS, GCP, Azure, DigitalOcean and similar providers, **also add the same TCP ports to the cloud firewall or
security group**. Both firewalls must allow them.

### 6.3 Run

```bash
python -m studio run --public-url https://photos.yourdomain.com \
  --ftp-port 2121 --ftp-host ftp.yourdomain.com --ftp-passive 60000-60049 \
  --ftp-cert /etc/letsencrypt/live/yourdomain.com/fullchain.pem \
  --ftp-key  /etc/letsencrypt/live/yourdomain.com/privkey.pem
```

- The Let's Encrypt certificate must cover `ftp.yourdomain.com`, or be a wildcard certificate. The user that runs
  Studio must be able to read both files.
- `--ftp-host` matters behind NAT and in the cloud: the server tells cameras this address, resolved to its IP, for
  the data connections.

**Port 21 instead of 2121:** some cameras assume port 21. Linux needs root to listen on ports below 1024. Keep 2121
and redirect 21 to it:

```bash
sudo iptables -t nat -A PREROUTING -p tcp --dport 21 -j REDIRECT --to-ports 2121
sudo ufw allow 21/tcp
```

### 6.4 Docker

`docker-compose.yml` already maps `2121:2121` and `60000-60049:60000-60049`. Set the variables in `.env`:

```env
PF_FTP_PORT=2121
PF_FTP_HOST=ftp.yourdomain.com
PF_FTP_PASSIVE=60000-60049
PF_FTP_CERT=/data/certs/fullchain.pem
PF_FTP_KEY=/data/certs/privkey.pem
# PF_FTP_REQUIRE_TLS=1
```

The certificate files must be inside the mounted `/data` folder. If you change the passive range, change it in
three places, which must always match:

- the `ports:` mapping in `docker-compose.yml`
- `PF_FTP_PASSIVE`
- the firewall

### 6.5 Test the live server from outside

Run this from another network, for example your laptop on a phone hotspot:

```bash
curl --ssl-reqd -T DSC_0001.JPG "ftp://ftp.yourdomain.com:2121/" --user "cam-XXXXXX:PASSWORD" -v
```

In the `-v` output, look for these replies:

| Reply | Meaning |
|---|---|
| `230 Login successful` | The control port works and the login is accepted |
| `227 Entering Passive Mode (a,b,c,d,p1,p2)` | `a.b.c.d` must be your server's **public** IP. If it shows a private IP (10.x, 172.16-31.x, 192.168.x), set `--ftp-host`. |
| `226 Transfer complete` | The passive ports work and the photo was received |

---

## 7. Setting up the camera

Enter these values in the camera's network or FTP menu:

| Camera field | Value |
|---|---|
| Server / Host | The **Server** shown on the card, for example `ftp.yourdomain.com` |
| Port | `2121` (or `21` if you set up the redirect) |
| Protocol / Security | FTPS (FTP over TLS) if offered, otherwise FTP |
| Username / Password | From the card |
| Passive mode | **On** |
| Folder / Directory | `/`, or leave it empty. Any folder works. |
| Auto transfer | On, after each shot |
| Image size for transfer | JPEG, medium or small (2-4 MB) for live sharing |

Where the FTP menu is (names vary by model and firmware):

- **Canon** (R series): Network settings > FTP transfer
- **Nikon** (Z series): Network menu > Connect to FTP server
- **Sony** (Alpha): Network > FTP Transfer function

Before the event, take a test shot. The card should show "Last photo received" within a few seconds.

**Two or more cameras at the same event:** enter the **same** username and password in every camera. They can send
at the same time, even when both name a file `DSC_0001.JPG`: each camera connection gets its own private folder, and
both photos are kept. The Camera upload card counts photos from all of them together. Limit: 40 connections per
internet address, and each camera uses up to 2, so about 20 cameras can share one venue Wi-Fi or hotspot (raise it
with `PF_FTP_MAX_PER_IP`). A camera with a wrong password only pauses its own login, never the others on the Wi-Fi.

---

## 8. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| No Camera upload card on the event screen | The platform admin hasn't enabled this event (§3.3), or your role can't manage events |
| The card says "Not switched on for this server" | Studio was started without `--ftp-port` or `PF_FTP_PORT` |
| The camera can't connect at all | Port 2121 is blocked (server or cloud firewall), the camera has no internet, or the DNS name is proxied (§6.1) |
| Login works, then the camera hangs or fails on the first photo | **The passive ports are blocked**, or the server announces a private IP. Open 60000-60049 and set `--ftp-host` (§6.5). |
| `530` login refused | Wrong or old password (a new one was made), the login is turned off, the company is suspended, or that camera login is paused after 10 wrong passwords (fix the password in the camera, wait 15 minutes) |
| `552 File is larger than 60 MB` | The camera is sending RAW or very large files. Switch the transfer to JPEG. |
| The card shows "Last file: … already uploaded" | The same photo was sent again. It is skipped, which is expected. |
| The card shows a note about the plan limit | The company has reached its plan's photo limit. Upgrade the plan or remove photos. |
| FTPS fails, plain FTP works | The certificate does not match the host name, or the camera does not trust it. Use a real certificate for `ftp.yourdomain.com`, or turn off `--ftp-require-tls`. |
| Photos arrive slowly | The venue's upload speed is the bottleneck, or the server is busy (about 2 s per photo per CPU core). Send smaller JPEGs, or use a bigger server for big shoots. |

---

## 9. Security summary

- **One login per event.** It can only add image files to that event. It cannot list, download, rename into other
  places, or delete anything.
- Passwords are stored as scrypt hashes and shown only once. **New password** replaces the old one immediately.
- **Turn off**, **Remove login**, and suspending the company all stop the login at once.
- Plain FTP sends the password unencrypted. Use FTPS where the cameras support it, and `--ftp-require-tls` if all of
  them do.
- Only the owner, admin and manager roles can see and manage the Camera upload card.
