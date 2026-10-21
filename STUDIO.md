# Photo Finder Studio: multi-company edition

Photography companies sign in, add **clients**, create **events** for each client, upload the photos, and
share one **link per event**. Anyone who opens the link takes a selfie and sees only the photos they appear in.

```
Company (login)  ->  Clients  ->  Events  ->  Photos
                                     |
                                     +--> share link + QR  ->  guest selfie  ->  their photos
```

Clients and guests never need an account: the event link is their way in. Each company only ever sees its
own data (every query is scoped by company; there is a test that fails if that scoping is removed).

## Run it

```bash
pip install -r requirements.txt
python -m studio run
```

Open the dashboard address it prints, choose **Create account**, then:

1. **New client** (the couple, company or school you shoot for).
2. **New event** under that client: name, date, optional subtitle.
3. **Upload photos**: drag and drop, or choose photos or a whole folder. Faces are detected in the background;
   you can keep uploading while it works.
4. **Publish for guests**, then **Copy link**, **Share on WhatsApp**, or download the **QR code** for a poster.

Guests open the link on their phone, tick the consent box, take a selfie and see their photos, with a full-screen
viewer, "Not me", single download and "Download all" (zip). The page uses your company name and brand colour
(Settings).

### Client gallery: every photo, for your client

Guests only ever see the photos their selfie matches. Your client (the couple, the company) usually wants **all**
of them. On the event page, turn on **Client gallery**: it makes a second, private link (`/c/...`) that shows every
photo of the event, no selfie needed, with a full-screen viewer, single downloads, **Select** and **Download all**.

- It works while the event is still hidden from guests, so the client can look first.
- **PIN (recommended):** add a 4 to 8 digit PIN and send it separately from the link (for example the link on
  WhatsApp, the PIN by phone). Each device enters it once. Changing the PIN locks out every device that used the old
  one. Wrong guesses are limited (10 per 15 minutes per visitor, 60 an hour in total).
- **New link** replaces the link at once, if it was forwarded too widely. Turning the gallery off stops it; turning
  it back on brings the same link back.
- **Find a person:** the client can take a selfie, or choose a photo of anyone (a parent, a friend), and the gallery
  narrows to the photos that person is in, best match first. **Show all photos** goes back. While filtered, Select
  works on those photos, "Download all" becomes "Download these N", and the viewer has "Not this person" for a
  wrong match. It uses the same face matching as the guest page (same results for the same photo), the photo is
  never stored, and these searches don't count in the guest statistics. Deleting an event's face data hides the
  button; browsing every photo keeps working.
- "Client can download" switches single downloads and "Download all" off or on (viewing stays on).
- "Download all" sends one zip that is built while it downloads, so even a 20 GB wedding needs no space on the
  server. The dashboard shows how many times the gallery was opened.
- Only roles that can manage events (owner, admin, manager) can change these settings.

**Open the dashboard using the computer's network address** (the one printed at start), not `localhost`:
share links are built from it, and a phone can't open a `localhost` link.

### With an iPhone over the USB cable

Follow "Use an iPhone over a USB cable" in `README.md` to get the `172.20.10.x` address, then:

```bash
python -m studio run --ip 172.20.10.2
```

The dashboard and every share link now use that address, so the iPhone opens them directly.

### Command line

| Command | What it does |
| --- | --- |
| `python -m studio run [--port 8000] [--public-url https://photos.example.com] [--no-signup] [--workers N]` | Start the app |
| `python -m studio create-account --company "Lumen" --email a@b.com` | Create a company from the terminal (asks for a password) |
| `python -m studio set-plan a@b.com pro` | Move a company to another plan |
| `python -m studio reset-password a@b.com` | Set a new password for someone who is locked out |
| `python -m studio grant-admin you@yourcompany.com` | Give a user the product-owner dashboard at `/admin` (`revoke-admin` removes it) |
| `python -m studio --data /path run` | Use another data folder (also `PF_DATA`) |

Settings that can come from the environment: `PF_DATA` (data folder), `PF_PUBLIC_URL` (address in share links),
`PF_ALLOW_SIGNUP=0` (close public sign-up), `PF_TRUST_PROXY` (`auto` by default: the visitor's address is read from a reverse proxy on this machine or a private network, such as nginx or Docker; `0` never, `1` always),
`PF_GOOGLE_CLIENT_ID` and `PF_GOOGLE_CLIENT_SECRET` (Google sign-in, see below), `PF_SMTP_*` and `PF_MAIL_FROM`
(email, see "Forgot password").

### Forgot password

"Forgot password?" on the sign-in screen emails a one-time link (valid 60 minutes) to choose a new password.
Using it signs the person in and signs them out everywhere else. It also lets someone who signed up with Google
set a password.

**Until you set up email, the message is printed in the terminal where the app runs**, so you can try it (copy the
link from there). To really send it, use any SMTP service: your Google Workspace or Zoho mailbox, Amazon SES,
Brevo, Mailgun and so on.

| Variable | Example |
| --- | --- |
| `PF_SMTP_HOST` | `smtp.gmail.com` |
| `PF_SMTP_PORT` | `587` (or `465` with `PF_SMTP_SECURITY=ssl`) |
| `PF_SMTP_USER` / `PF_SMTP_PASSWORD` | the mailbox and its password (for Gmail: an [app password](https://myaccount.google.com/apppasswords)) |
| `PF_SMTP_SECURITY` | `starttls` (default), `ssl` or `none` |
| `PF_MAIL_FROM` | `Photo Finder <no-reply@yourdomain.com>` (defaults to the SMTP user) |

The links use your public address (`--public-url`), so set it. To keep reset emails out of spam, send from your
own domain with SPF and DKIM set up at your mail provider. Someone locked out with no email access can still be
helped from the terminal: `python -m studio reset-password <email>`.

### Google sign-in

Studios can sign up and sign in with their Google account. New users pick a company name after Google; an
existing email-and-password account with the same (Google-verified) email is linked automatically. Users who
signed up with Google can set a password later in Settings.

1. In [Google Cloud Console](https://console.cloud.google.com/apis/credentials): **Create credentials > OAuth client
   ID**, type **Web application**. (The first time, it asks you to set up the consent screen: app name, support
   email; scopes `openid`, `email`, `profile` are enough.)
2. Under **Authorized redirect URIs** add exactly `<your public URL>/api/auth/google/callback`, for example
   `https://photos.yourdomain.com/api/auth/google/callback`.
3. Start the app with the two values:
   ```bash
   PF_GOOGLE_CLIENT_ID=1234-abc.apps.googleusercontent.com PF_GOOGLE_CLIENT_SECRET=GOCSPX-...      python -m studio run --public-url https://photos.yourdomain.com
   ```
   (Windows PowerShell: `$env:PF_GOOGLE_CLIENT_ID="..."` and so on, on separate lines.) The "Continue with
   Google" button appears once both are set.

**Google only accepts `https://` addresses, or `http://localhost`.** It refuses a LAN address such as
`http://192.168.1.75:8000`, so on a local network use the dashboard at `http://localhost:8000` with
`--public-url http://localhost:8000` and that redirect URI to try it. For real use, run it on your domain with HTTPS.
While the consent screen is in "Testing" mode, only the test users you add there can sign in; publish it for everyone.

## Vendor uploads

On the event page, **Vendor uploads** (under Upload photos) makes an upload link for each vendor: a second shooter,
the drone team, the video editor. Type their name (and their WhatsApp number if you want a one-tap send) and press
**Create upload link**, then **Copy link** or **Send on WhatsApp**. The vendor opens `/v/...` (no account), and drags in
photos and videos, or a whole folder. Photos are processed like yours (guest search, client gallery). Videos go to the
Videos card. Every file is tagged with the vendor's name: a label on the photo, **From vendors** in the Photos
filters, and **View photos** on each vendor. The vendor sees only the names of what they sent, never other photos.

**Turn off** stops a link at once; **New link** replaces the address (sent to the wrong person); **Delete** removes
the link but keeps what they sent. Vendor uploads count towards your plan's photo and storage limits. Only roles
that manage events see the card.

## Album selection (client gallery)

In the client gallery your client taps **Select**, chooses photos and taps **Proceed for album**. They can add a
note ("this one on the cover, 40 sheets") and send. They can also tap **♡ Add to album** on a single photo, and
**♥ My album** shows what they've sent so far, where they can take photos out. Selecting works even when downloads
are turned off.

On the event page, the **Selected for album** card (above Photos) shows those photos apart from the rest, with the
client's note and when they last sent. **Copy file names** puts the names on the clipboard for Lightroom or your
album software. **Download originals** zips just those photos. **Finalise** locks the selection so it stops changing
while you design; **Unlock** lets the client change it again. A deleted photo leaves the selection on its own.

## Videos (client gallery only)

The event page has a **Videos** card under Photos: drag in the highlight film, the first dance or the speeches (MP4,
MOV, M4V or WebM, up to 4 GB each; `PF_MAX_VIDEO_MB` changes it). They appear **only in the client gallery**, above
the photos, with a player and, when the client may download, a Download button. Guests never see them: the guest
page has no way to list or open them. A PIN on the client gallery protects them too. Videos aren't searched by face
and aren't re-encoded; the browser plays the file you uploaded, so **MP4 (H.264) is the safe choice**: it plays on
every phone and computer. iPhone MOV (HEVC) plays in Safari but not everywhere. Videos count towards the plan's
storage. Only roles that can upload add them; roles that can delete photos delete them.

**Minimize / Maximize:** the Photos and Videos cards fold away with the button in their top corner, showing just the
count. The choice is remembered in that browser.

**Behind nginx**, allow big uploads, or nginx refuses videos over 1 MB: `client_max_body_size 5g;` (and
`proxy_request_buffering off;` so nginx streams them to Studio instead of storing them first). Caddy has no limit.

## Billing: estimates and invoices

**Billing > New estimate or invoice.** First choose what you're making:
- **Estimate:** a quotation before the booking. Numbered EST-0001, EST-0002, ...; it has a "valid until" date and
  its total reads **Total package cost**.
- **Invoice:** a bill for the client to pay. Numbered on its own, INV-0001, INV-0002, ...; it has an invoice date and
  a **due date**, says **Bill to**, and its total reads **Total amount due**.

Then fill in, for either:
- **From:** the company name printed on it (your company's name by default; change it per document), your address,
  phone, email or GSTIN, and your logo if you have one (untick **Show our logo** to leave it off).
- **For:** pick one of your clients to fill in their details, or type any name; the event and the dates.
- **Packages:** as many as you need, each with a name, a description (one line per point) and its cost; reorder them
  with the arrows. The total adds them up as you type, with an optional discount and tax (for example GST 18%).
  Choose ₹, $, € or £; rupees print with Indian grouping (₹ 1,50,000).
- **Notes and terms** print at the bottom (advance, bank or UPI details, delivery time).

**Make invoice** on an estimate (the client said yes) creates a new invoice with the same client, packages, discount
and tax, dated today; it shows "Estimate ref. EST-0001" on the PDF. The estimate stays as it was. **Duplicate** copies
a document as the same kind. A document keeps its type and number once saved; a deleted number isn't reused. A new
one starts with the company details, terms and tax of your last one. The list filters by All, Estimates or Invoices.

The PDF is A4, in your brand colour, and always shows the saved version (unsaved changes are saved first). Owners,
admins and managers can create and change them; photographers and viewers can open and download them. Estimates made
before invoices existed stay as they were.

## Camera upload (FTP)

**The platform admin decides which events get it.** In the admin dashboard, open the company and click **Enable** in
the **Camera FTP** column of its Events table. Until then the event screen doesn't show the option at all, and the
company can't switch it on itself. **Turn off** there hides it again and stops that event's camera login at once (the
login is kept, so enabling again makes it work again). Each change is in the admin history. New events start off.

Photographers can send photos straight from the camera. Each enabled event gets its own FTP login (the **Camera upload**
card on the event page: server, port, username, a password shown once, and a setup guide). Every photo the camera
sends goes through the same steps as a dashboard upload: duplicate check, plan limits, thumbnail, web copy, face
detection. Guests can find it within seconds, and the card shows "Last photo received 5 s ago".

What a camera login can do: add photos to its one event, nothing else. It can't download, delete or even list
anything (its folder is always empty; each file moves into the event as it arrives), only image files are taken, a
file over the size limit is stopped mid-transfer, and wrong passwords are limited per camera login: 10 in 15 minutes
from one address pause that login there, so one camera with a typo never locks out the other cameras on the venue
Wi-Fi. **New password** replaces the password at once; **Turn off** and **Remove login** stop the camera. Only roles
that manage events (owner, admin, manager) see the card.

### Turning it on

```bash
python -m studio run --ftp-port 2121 --ftp-host ftp.yourdomain.com --ftp-passive 60000-60049     --ftp-cert /path/fullchain.pem --ftp-key /path/privkey.pem     # the last two enable FTPS
```

(Or `PF_FTP_PORT`, `PF_FTP_HOST`, `PF_FTP_PASSIVE`, `PF_FTP_CERT`, `PF_FTP_KEY`, `PF_FTP_REQUIRE_TLS=1`; the Docker
files map port 2121 and the passive range.) Then:
- **Open the ports** in the server's firewall: the FTP port and the whole passive range.
- **`ftp.yourdomain.com` must point straight at the server.** FTP can't go through Cloudflare's proxy or similar;
  set that DNS record to "DNS only". The server tells cameras its public address for the data connections.
- **Encryption:** with a certificate (your domain's Let's Encrypt one works), cameras that offer FTPS use it.
  `--ftp-require-tls` refuses plain FTP; leave it off if some cameras only do plain FTP (their password then travels
  unencrypted, which is why a camera login can only add photos to one event).

**Try it on a laptop first:** run Studio with `--ftp-port 2121`, put the camera on your phone's hotspot (the laptop
too), and enter the laptop's address shown at start-up as the server.

### Things to know

- **Several cameras, one event:** enter the same login in every camera that shoots the event. They can send at the
  same time, even files with the same name (each connection gets its own folder). Up to 40 connections per
  internet address, about 20 cameras on one venue Wi-Fi (`PF_FTP_MAX_PER_IP` to change it).

- **Cameras:** many current Canon, Nikon and Sony bodies have built-in FTP upload, but menus and protocols differ by
  model and firmware. Test with the cameras your photographers use before promising a list.
- **Venue internet decides the speed.** Send JPEGs at a medium size (2 to 4 MB) for live sharing; face search works
  just as well. Upload the full originals later from the dashboard (same photo = skipped as a duplicate only if it is
  byte-identical, so a smaller live JPEG and its original are both kept).
- **Processing:** about 2 seconds per photo per core. A busy shoot can outpace a small server; size it for peak
  shooting or guests see photos with a delay.
- A guest who searched earlier searches again to see later photos.
- Not included: SFTP, and a desktop app that uploads a folder (for cameras without FTP).

## Light and dark mode for your clients

**Settings > Your studio > Guest and client pages** decides how your guest pages and client galleries look:
**Dark** (the default, as before), **Light**, or **Match each visitor's device** (their phone's light or dark
setting). That is what every visitor sees first. A sun and moon button in the corner lets a visitor switch for
themselves; their choice is remembered on their device. The mode is applied before the page draws, so it never
flashes the other one first.

Both modes keep text readable with your brand colour: buttons use it as it is, while links and labels use a shade
of it adjusted to at least 4.5:1 contrast (a yellow brand turns golden-brown as text on the light pages). The photo
viewer stays black in both modes. A logo that would vanish on one background (dark lettering on the dark pages,
white lettering on the light ones) automatically sits on a small badge there, and only there; the Logo card and
editor preview both modes.

## Your logo

**Settings > Logo > Upload logo.** The logo then replaces the company name everywhere it appears: the dashboard
header, the guest page (top and footer), the client gallery (top and footer), the platform admin's company list,
and the invitation and password-reset emails. Without a logo, the name is shown as before. Only roles that can
change company settings (owner, admin) can upload, replace or remove it.

The editor, before anything is saved:
- **Whole logo** (no cropping), **Wide 3:1** or **Square**; zoom with the slider or mouse wheel, drag to position.
- **Rotate** left or right, **Reset**.
- **Trim empty edges** (on by default): removes transparent or plain-coloured margins.
- **Make the white background transparent:** for JPG logos, which otherwise show a white box on the dark guest pages.
- Live previews on the light dashboard and on the dark guest pages, and the final size in pixels.
- **Size on guest pages:** Small, Medium or Large.

If part of a logo would be hard to see on the dark guest pages (for example dark lettering), it is shown there on a
small light badge automatically; the editor's preview shows it the same way.

The server re-draws every upload as a fresh PNG (no hidden data or camera metadata survives), at most 1200 x 600
pixels. PNG, JPG, WebP and GIF are accepted, up to 8 MB; SVG is not (it can carry scripts). Each upload gets a new,
unguessable address, and the logo is deleted with the company.

## Calendar (bookings)

**Calendar** shows the month (Monday to Sunday) with every booking on its days, plus the dated events from the
Events page in grey (click one to open the event). Click a date to add a booking there, or click a booking to change
it. Each booking has:
- **Event details** (a title such as "Wedding: Arun & Anjali"), the date, an optional last day for multi-day work,
  and optional times;
- **Client type:** **Own client** (blue) or **Expost** (purple); filter the month by either;
- the client: pick one of your clients to fill in the name and phone, or type any name; the venue;
- a **description** of what's included;
- the **payment**: the finalised amount and what has been received. The balance and the status (Not finalised,
  Unpaid, Part paid, Paid) follow, with an optional payment note.

At the top: the month's bookings (own client / Expost), the finalised total, what has been received and the balance
still to collect. Below the calendar, "This month" lists the bookings with their payment. On a phone the calendar
shows dots and a tap opens that day's list. Owners, admins and managers add and change bookings; everyone in the
company sees them.

## Equipment inventory (Team > Inventory)

**Team > Inventory** keeps the studio's equipment: camera bodies, lenses, lights, drones, audio, gimbals, cards,
batteries, computers. Each item has a name, category, brand or model, serial number, quantity, purchase date and
cost, and notes. At the top: how many items there are, how many are available, checked out (and **overdue**), in
maintenance, and what they cost. Filter by status or category, or search by name, serial or person.

- **Check out** an item to a team member, optionally for an event and with a "due back" date. Nobody can take an item
  that's already out, in maintenance or retired. Past the due date it shows **Overdue** in red.
- **Check in** when it comes back, with a note on its condition.
- **Maintenance / Back in service / Retire** for repairs and old kit. Retired items stay in the list (filter
  "Retired") with their history but don't count in the totals; **Delete** removes an item and its history.
- **History:** every item shows who added, edited, took and returned it, for which event, and when. Names are kept,
  so the history stays readable after a team member leaves or an event is deleted. An item a removed member still
  had stays "checked out" until someone checks it in.
- **Export CSV** downloads the whole list (opens in Excel).

Who can do what: owners, admins and managers add, edit, delete and check items out to anyone. Photographers check
available items out to themselves and return what they have. Viewers can only look. Costs and the total value are
shown to those who manage the inventory.

## Team members

The owner (or an admin) opens **Team**, enters an employee's work email, picks a role and clicks **Send
invitation**. The employee gets an email with a link (one-time, valid 7 days), sets a name and a password or uses
**Join with Google** (the Google account must be the invited address), and is in the same company. No new company
is created.

- An email that already has a Photo Finder account, in any company, can't be invited: each person belongs to one
  company.
- Seats per plan are in `PLANS` (`users`: placeholders 2 / 10 / 50). Pending invitations hold a seat until they are
  accepted, cancelled or expire.
- You can only invite, change or remove people ranked below you. An admin manages managers, photographers and
  viewers; only the owner manages admins. Removing someone signs them out everywhere; what they uploaded stays.
- Invitation emails use the same email settings as "Forgot password" (printed to the terminal until SMTP is set).

| Role | Can do |
| --- | --- |
| Owner | Everything, including deleting the company. One per company |
| Admin | Everything except deleting the company; manages the team |
| Manager | Clients and events (create, edit, publish, delete), uploads and deleting photos |
| Photographer | Upload photos to existing events; sees everything else read-only |
| Viewer | Read-only: events, photos, guest stats, share links and QR codes |

The server checks the role on every request (`studio/roles.py`); the dashboard also hides what a role can't use.

## Product-owner dashboard (`/admin`)

For you, the owner of the platform, not for the studios. Sign up (or create an account) for yourself, run
`python -m studio grant-admin <your email>`, sign in, and open **Admin** in the top bar (or go to `/admin`).
Anyone without admin access gets a 404 there.

- **Overview:** active, suspended and new companies; companies active in the last 30 days; estimated monthly
  revenue at list price; storage, photos and events across the platform; free disk space; guest searches; the
  processing queue; new companies and guest searches per month; companies per plan; biggest storage users;
  companies close to a plan limit (upgrade candidates); recent admin actions.
- **Companies:** how many users each company has (and how many are invited), search by name or email, filter by
  status or plan, sort by users, storage, photos, activity and more,
  and export the list as CSV for your accounts or ERP system.
- **One company:** plan usage against its limits (including team seats), every user with their role, pending
  invitations, events, and its admin history. Actions:
  **Suspend** (with a reason), **Enable**, **Change plan**, **Sign out all devices**.
- **Activity log:** every admin action, who did it and when. It is kept even if the company later deletes itself.

Suspending a company signs its users out, blocks sign-in (they see "account suspended" after entering the right
password) and takes its guest links offline. Nothing is deleted; **Enable** brings everything back. You can't
suspend the company you are signed in with.

The revenue figure uses the `price` per plan in `studio/config.py` (placeholders, in `CURRENCY`) for every
non-suspended company. It's an estimate until billing exists.

## Face matching accuracy

The default engine is **YuNet + SFace** (OpenCV; models MIT and Apache-2.0, fine for commercial use). It replaced dlib
after a test on a real wedding: dlib's gap between "same person" and "someone else" was about 0.02 wide, so the
result for one guest swung a lot with the phone the selfie came from (34 photos on one phone, 12 on another) and
pulled in other guests. SFace's gap there was about 0.2. On simulated selfies from different phones it gave 86%
fewer wrong-person photos and 66% fewer missed ones.

Two more things make results the same across phones:
- Every vector is the face plus its mirror image, so a selfie saved mirrored (many Android phones) matches the same.
- Faces in the grey zone just under the threshold count only if they also closely match a face the selfie is sure
  about: a photo-to-photo comparison from the same camera, which is much more reliable than selfie-to-photo.

**Upgrading:** events made before this change are re-indexed with the new engine automatically when the app starts
(about 3-4 photos per second per worker; guest search keeps working, showing photos as they are redone). Events whose
face data was deleted on purpose are left alone until you click "Rebuild face index". `PF_FACE_MODEL=dlib` keeps
the old engine.

**Strictness** (event settings): Strict 0.92, Balanced 0.95 (default), Relaxed 1.0. Children and side-on faces are
the hardest cases; "Relaxed" finds more of them at the cost of a few wrong photos.

## Plans and limits

`studio/config.py` has a `PLANS` table (events, photos and storage per company). **The numbers are placeholders:
set the tiers and prices you want to sell.** Limits are enforced on every upload and when creating events, also when several uploads run at once. New
accounts start on `free`. There is no payment system yet; you move a company between plans with `set-plan`
(see "Before you charge money").

## Where things live

```
data/studio.db        SQLite: companies, users, clients, events, photo records, face vectors
data/files/...        originals, thumbnails and web copies, grouped by company and event
```

Back up the whole `data/` folder (stop the app or use `sqlite3 data/studio.db ".backup copy.db"` for the database).

## Deploying

1. Run it on a server with Docker: `docker compose up -d --build` (the files are included; **I couldn't build the
   image where I wrote it, so test this step**). Put `PF_PUBLIC_URL=https://photos.yourdomain.com` in a `.env` file.
2. Put a reverse proxy with HTTPS in front. With Caddy, `Caddyfile` is two lines:
   ```
   photos.yourdomain.com {
       reverse_proxy localhost:8000
   }
   ```
   Rate limits then see the visitor's real address automatically (the proxy's `X-Forwarded-For`). If Studio is
   reachable from the internet *without* a proxy as well, set `PF_TRUST_PROXY=0`.
3. Size the machine for face processing. Measured on one core with a 24-megapixel JPEG: about 2 seconds per photo
   (roughly 0.5 photos per second per core; face detection is most of it). Phone-sized photos (12 MP or less) are
   faster. A 3,000-photo wedding of 24 MP files takes about 35 minutes on 3 worker processes (a 4-core machine), and
   scales roughly linearly with cores. Uploading is not blocked while this runs. Searching is fast (about 0.1 s
   even with 200,000 faces).

One server process with SQLite is fine for a few dozen active studios. Beyond that, see "Scaling up".

## What is protected

- Passwords: scrypt. Sessions: random tokens stored hashed, httpOnly cookie, 14-day expiry, other devices
  signed out on password change.
- Password reset: one-time links that expire after 60 minutes, stored hashed, carried after `#` so they never reach
  server logs; the same answer whether or not an email has an account; at most 3 emails per address per hour.
- Google sign-in: authorization-code flow with PKCE, state bound to the browser by a cookie, nonce, and the ID
  token's issuer, audience, expiry and verified email all checked. The client secret never reaches the browser.
- Login and sign-up rate limits; guest selfie search and "Download all" limited per visitor and per event.
- Every dashboard request is scoped to the signed-in company. A request-header check blocks cross-site form attacks.
- Event links are unguessable (8 random characters). Photo links are only given to a guest whose selfie matched.
- Unpublished or expired events are fully offline. Expiry dates follow the studio's time zone (Settings). All user text is escaped on the dashboard and guest page.
- Selfies are processed in memory and never stored. Only a count of searches is kept.
- Face vectors can be deleted per event, events can expire, and a company can delete its whole account.
- The face models are public domain (CC0), so there is no model licence to buy.

## Before you charge money

Not built yet, in the order I would do them:

1. **Terms of service and privacy policy**, with a lawyer. Face vectors are biometric data (India's DPDP Act, GDPR,
   some US states). The guest consent box is there; the policy text and your retention promise are yours to write.
2. **Billing**: Razorpay or Stripe subscriptions that change `accounts.plan` (the limits already read from it).
3. **Email**: address verification and "your photos are ready" messages (password reset by email is done).
4. Optional but common: watermarks, client photo selection, custom domains per studio, WhatsApp notifications.

Known limits to be aware of: guests who disable downloads can still view photos on screen (it is a download button
switch, not copy protection); only admin actions are audit-logged, not what studios do; browser pages have no
Content-Security-Policy header yet.

## Scaling up

The code is split so each piece can be swapped: `studio/storage.py` (files -> S3 or R2), `studio/db.py` (SQLite ->
PostgreSQL), `studio/processing.py` (the in-process queue -> a worker fleet) and `VectorIndex` (numpy -> pgvector
or Qdrant). The same ideas as the architecture document, without a rewrite.

## Tests

```bash
pip install pytest
python -m pytest tests            # accounts, isolation between companies, limits, uploads, guest rules
PF_TEST_FACES=/path/to/faces python -m pytest tests    # also runs the real selfie-matching flow
```

`PF_TEST_FACES` must hold `obama.jpg`, `obama2.jpg`, `obama3.jpg` and `biden.jpg` (the `tests/test_images` folder of
the `face_recognition` package has them). Without it those two tests are skipped.
