# Photo Finder: local face-search gallery

> **Selling this to photography companies?** See [STUDIO.md](STUDIO.md): logins, clients, events, share links.

Point it at a folder of event photos. Guests on the same Wi-Fi scan a QR code, take a selfie, and get
only the photos they appear in, with a lightbox, "Not me", single download and "Download all" (zip).
No cloud, no database, no account. Everything stays on your computer.

## Setup (once)

Needs Python 3.9 to 3.13.

```bash
cd photo-finder
python -m venv .venv
# Windows:  .venv\Scripts\activate        Mac/Linux:  source .venv/bin/activate
pip install -r requirements.txt
```

The face models are already in `models/` (YuNet + SFace, MIT / Apache-2.0, used by default; plus the older dlib ones).
Nothing is downloaded at run time.

## Run

```bash
python app.py "D:\Photos\Wedding" --title "Wedding Photos"
```

1. First run indexes every photo (about 1 to 2 photos/s per CPU core). Results are cached in `cache/`,
   so the next start only processes new or changed photos.
2. It prints a URL such as `http://192.168.1.75:8000` and a QR code. The printable image is `cache/qr.png`.
3. Guests open it on their phone (same Wi-Fi), tick the consent box, take a selfie, and see their photos.

Add photos while it is stopped, then restart; new ones are picked up automatically.

## Use your phone over a USB cable (Android, no Wi-Fi)

One-time phone setup:
1. Settings > About phone > tap **Build number** 7 times (turns on Developer options).
2. Settings > Developer options > turn on **USB debugging**.

One-time computer setup: install Android
[platform-tools](https://developer.android.com/tools/releases/platform-tools) and make sure `adb` works in a
terminal (`adb version`). On Windows, unzip it and add that folder to PATH.

Then, each time:
1. Plug the phone in, set USB mode to **File transfer**, unlock it, and tap **Allow** on "Allow USB debugging?".
2. Run:
   ```bash
   python app.py "D:\Photos\Wedding" --title "Wedding Photos" --usb
   ```
3. The page opens on the phone at `http://localhost:8000`. The cable carries the traffic, so Wi-Fi and the
   laptop firewall don't matter. If you unplug and replug the cable, the link is restored within a few seconds.

Why this is nicer than Wi-Fi: on the phone, `localhost` counts as a secure address, so the camera and the
"Download" button should work without the "can't be downloaded securely" prompt and without `--https`.
In this mode the server listens only on the computer itself, so nobody else on the network can reach it.
Guests cannot use it (no QR code); for them, run without `--usb`.

**No adb? USB tethering instead:** on the phone, Settings > Network > Hotspot & tethering > **USB tethering**.
Run the app normally (without `--usb`); it prints your computer's other addresses. Open the one that starts
with the same first three numbers as the phone's tethering address (often `192.168.42.x`), for example
`http://192.168.42.50:8000`.

## Use an iPhone over a USB cable (Personal Hotspot)

iPhones have no `adb`, so `--usb` does not work. Instead the cable carries a network: the iPhone's Personal
Hotspot over USB. Wi-Fi is not needed, and no mobile data is used (the traffic never leaves the cable).

One-time setup:
- **Windows:** install **Apple Devices** from the Microsoft Store (or iTunes). It provides the USB network driver.
- **Mac:** nothing to install.

Each time:
1. Plug in the iPhone and tap **Trust This Computer**.
2. On the iPhone: Settings > **Personal Hotspot** > turn on **Allow Others to Join**.
3. Turn off Wi-Fi on the computer, so it uses the cable. Its address on that link starts with `172.20.10.`
   (check with `ipconfig` on Windows, or `ifconfig` on Mac).
4. Run the app normally, without `--usb`:
   ```bash
   python app.py "D:\Photos\Wedding" --title "Wedding Photos"
   ```
   It prints `Detected an iPhone Personal Hotspot network` and an address like `http://172.20.10.2:8000`.
   If it shows a different address, pass the right one: `--ip 172.20.10.2`.
5. On the iPhone, open that address in **Safari** (or scan the QR code with the Camera app).
6. **Windows firewall:** the first time, tick **both** Private and Public when Windows asks to allow Python.
   The USB network is often treated as Public. Or allow it once from an admin terminal:
   `netsh advfirewall firewall add rule name="PhotoFinder" dir=in action=allow protocol=TCP localport=8000`

On the iPhone: the selfie button opens the front camera. Downloads land in the **Files** app, not Photos. In
the full-screen view, touch and hold the photo and tap **Add to Photos** to keep it in your camera roll.
"Download all" saves a zip to Files. iPhone `.heic` photos in your folder are supported.

If **Personal Hotspot** is greyed out or missing, your mobile plan doesn't include it; use a Wi-Fi router, or
an Android phone with `--usb`. `--https` is not needed for Safari.

## Options

| Flag | Default | What it does |
| --- | --- | --- |
| `--title`, `--subtitle` | Event Photos | Text on the guest page |
| `--port` | 8000 | Port to serve on |
| `--usb` | off | Serve to an Android phone over the USB cable (see above). Not for iPhone |
| `--ip` | auto | Address shown to guests and in the QR code. Use it if the wrong one is detected (for example `172.20.10.2`) |
| `--https` | off | Self-signed https. Removes the browser's "can't be downloaded securely" prompt on downloads (see below) |
| `--model` | sface | Face engine. `sface` is far better at telling people apart; `dlib` is the original one |
| `--tolerance` | 0.95 (sface), 0.5 (dlib) | Match strictness. Lower = fewer wrong photos, but may miss you. sface: try 0.92 to 1.0 |
| `--detect-size` | 2000 | Photos are shrunk to this long side before detection. Raise to 3000 to find small faces in big group shots (slower) |
| `--detector` | hog | dlib only: `cnn` finds side-on and small faces better but is slow without a GPU |
| `--workers` | cores - 1 | Parallel indexing processes |
| `--reindex` | off | Ignore the cache and reprocess everything |

## Troubleshooting

- **Phone can't open the URL:** phone and laptop must be on the same Wi-Fi, and the laptop firewall must allow
  Python on the chosen port. Some guest or office Wi-Fi blocks device-to-device traffic; use a phone hotspot or
  home router instead.
- **"File can't be downloaded securely" (as in your recording):** the page is on plain http. Tapping Keep still
  downloads. For no prompt, run with `--https`; the phone shows a one-time certificate warning (Advanced > Proceed).
- **Camera opens the gallery instead:** use the "choose a photo" link, or open the page in Chrome or Safari.
- **Someone is missing from their results:** raise `--detect-size` or try
  `--detector cnn`, then `--reindex`. Sunglasses, masks, and faces turned more than about 45 degrees are the hard cases.
- **Wrong people appear:** lower `--tolerance` to 0.92. Guests can also tap "Not me" (hides it for them only).
- **`dlib-bin` fails to install:** your Python version may have no wheel. Use Python 3.12, or install `dlib`
  (needs cmake and a C++ compiler).

## Privacy

- The selfie is processed in memory and never written to disk.
- Only face vectors (128 numbers per face) and thumbnails are cached, in `cache/`. Delete that folder to erase them.
- A guest can only fetch photos that matched their own selfie; there is no "browse everything" page.
- Face vectors are biometric data. If you run this for people outside your household, tell them what you do
  with it and delete `cache/` after the event. Check local rules (India's DPDP Act, GDPR, and so on).

## How it works

```
app.py        web server (FastAPI): selfie search, thumbnails, downloads, zip, optional https, QR code
netutil.py    LAN address detection and the self-signed certificate (shared with the studio)
indexer.py    scans the folder, detects + embeds faces in parallel, caches vectors and thumbnails
faces.py      face engines: YuNet detector + SFace embedding (default), or dlib HOG + ResNet (the original)
static/       the single-page mobile UI (consent, selfie, grid, lightbox)
models/       dlib model files (public domain / CC0)
```

Matching is the Euclidean distance between the selfie vector and every face vector in the folder; a photo
matches if any face is within `--tolerance`. This is brute force in numpy, which is instant for tens of
thousands of faces.

## Growing this into the full product

Each local piece has a direct replacement in the project plan:

| Local now | Production version |
| --- | --- |
| Folder of photos | S3 or R2 bucket with pre-signed uploads |
| `cache/index.pkl` + numpy search | PostgreSQL + pgvector (or Qdrant) |
| Process pool in `indexer.py` | Job queue + Python workers |
| One event per run | `events` table and a slug per event |
| `--https` self-signed | Real domain + CDN |
