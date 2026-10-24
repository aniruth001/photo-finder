# Landing page playbook: a blooper story for each couple

A guest landing page that opens with a short **blooper story** of the couple (chapters, photos with captions and a thank-you note), ending with the selfie search. It was first made for one wedding (Srini & Megha) in a single-event version of the photo finder. This document is for studio owners who want to make one for every client. It has three parts:

1. **How the first one was made.** The decisions and why each was made, so you know what to keep and what to change.
2. **The prompt.** Copy it into Claude (Claude Code, opened in this repo) at the start of each client. It tells Claude what to ask you and which steps to follow.
3. **Adding the story section to this repo.** The guest page (`studio/static/guest.html`, served at `/e/<slug>`) has **no story section yet**. Part 3 describes what to build, once, before the first story can go live.

---

## Part 1: How the first landing page was made

### What the page is

Guests open the event link, enter the PIN, and see the couple's blooper story. It ends with the selfie button that finds their own photos. A **📷 Find my photos** button stays pinned at the bottom, so anyone who only wants their photos is one tap away. The story is a gift, not a gate.

### Step 1: Know who the couple is

Start from **one clear photo of the couple together**. Their two faces become face vectors (the same SFace vectors this repo already stores per event), which pull every photo either of them appears in. For that wedding it was about 2,100 of 2,924 photos. Everything after this works only on the couple's photos.

### Step 2: The owner's own picks come first (seed photos)

**This is the most important step, and it's easy to skip.** Before any automated search, the person who knows the couple picks a few photos they find funny, by hand. These seed photos define the *taste* far better than any written description. The event's **client gallery** (`/c/...`) is a convenient place to browse and pick them.

What happened on the first wedding:

- The first automated pass found 28 candidates. Where its picks **matched photos the owner had already chosen**, the owner **endorsed** them, which confirmed the search was looking for the right thing.
- The owner kept 10 and rejected the rest. The kept and rejected photos were then laid out side by side as a **taste sheet**: liked on top, rejected below.
- The second pass used that taste sheet to **find similar photos**, and found the strongest material of the whole project: a gold-chain "moustache", a glare at the groom posing like a mafia boss on a throne, and a dramatic facepalm.

**What the taste turned out to be:** the couple's *own* comic expressions and body language. That meant smirks, eye-rolls, deadpan stares, goofy poses and playful role reversals. **Not** generic laughing, friends being silly, crowd chaos or petal showers. Another couple's taste may be different, which is exactly why seed photos matter.

### Step 3: Search the whole event, then let a human decide

- **Pass 1 (broad):** about 12 small photos per contact sheet, only photos where the couple was recognised. It was fast, but too loose, and it missed photos where their faces were small or turned away.
- **Pass 2 (thorough):** **every** photo, 8 per sheet. Wherever the couple was recognised, the tile is **zoomed in on them**, so expressions are readable. Each reviewer (Claude subagents) studied the taste sheet first and was told to be strict: most sheets have nothing.
- **Every candidate was checked at full size** before being kept. That removed bursts of near-identical frames (only the best frame is kept), photos of the wrong couple, and anything too small to read.
- **The owner made the final call** in rounds: keep, reject, or "these are actually good photos, not bloopers". Rejected photos were set aside, never deleted.

Tip for this repo: **Search by description** (see STUDIO.md) can help shortlist moments such as *laughing* or *dancing* for a pass, but it doesn't replace looking at the couple's expressions.

### Step 4: Turn photos into a story

- **Order by when the photos were taken.** Camera file numbers give the real sequence, and real sequence gives continuity: reception, then wedding, then the last frames of the day.
- **One chapter per event,** each with a short title: "Two villains meet", "The reception", "The wedding", "Happily ever after".
- **Pairs for setup and punchline.** Two photos side by side with linked captions: *"She had a plan." / "So did he."* and *"'Just five more minutes,' said the aunties." / "Five hours later."* These are usually the most-loved moments.
- **End on a photo that feels like a last line.** On the first wedding, the bride patting the kneeling groom on the head: *"And they lived happily ever after. Good boy."*
- **The thank-you note is woven in, not tacked on.** Its first line opens the story, and its remaining lines become the finale that leads straight into the selfie button.

### Step 5: Write captions in the chosen tone

- **Short:** one line, two at most.
- **The caption adds the joke the photo sets up.** It shouldn't just describe the photo.
- **Affectionately funny, never humiliating.** The couple will read every word, and so will their grandparents.
- The first couple chose **funny with a self-roasting note**, written as if *from* them ("We practised our poses for weeks. You can see how that went.").

### Step 6: Design choices that worked

- The story appears **only after the guest PIN**, and story photos are served **only if the story names them**.
- Print-style photo frames with a slight tilt, a serif font for the story, and scenes that fade in as you scroll. People who've turned off motion effects on their phone see no animation.
- Pairs sit side by side, and their crop can be adjusted per photo.
- When a guest searches, **their results replace the story.** It never gets in the way.
- Captions break evenly across lines, with no lonely last word.
- **Preview before publishing.** Review the whole story at phone width before guests see it.

---

## Part 2: The prompt (copy this for each client)

> Paste everything between the lines into Claude, opened in this repo, once the event's photos are uploaded and processed.

---

```text
You are helping me (a photo studio owner) build the guest landing page for a couple's event in this
repo. The page is a short blooper story of the couple shown after the guest PIN, ending with the
selfie search. Read docs/landing-page-playbook.md first and follow its process.

STEP 0: Before doing anything, ask me for:
  1. Which event (name or slug), and one clear photo of the couple together, plus who is who.
  2. 3 to 10 SEED PHOTOS I picked myself as funny/blooper moments of the couple (file names from
     the client gallery are fine). These define the taste. If I have photos I definitely do NOT
     want, ask for a few of those too.
  3. The TONE, one of:
       - funny (light, playful)
       - sarcastic (dry, deadpan)
       - self-roasting (the couple poking fun at themselves; written as "we")
       - neutral / warm (gentle smiles, no jokes; candid and sweet instead of bloopers)
     Also ask: is the story written from the couple ("we") or about them ("they")?
  4. The couple's names as they should appear, and the sign-off.
  5. A thank-you note if I have one; otherwise offer me 3-4 short drafts in the chosen tone and let
     me choose.
  6. Anything off-limits: people, moments, rituals, or kinds of jokes to avoid.
  7. Event order (e.g. haldi, sangeet, wedding, reception) if the photos cover several functions.
  Wait for my answers. Do not start the search until I've given the seed photos and the tone.

STEP 1: Find the couple's photos with this repo's face vectors for that event (faces table,
  model 'sface'; photos under data/files/accounts/<account>/events/<event>/web/). Report how many
  photos each of them is in. Read only; don't change the event.

STEP 2: Build a taste sheet from my seed photos (liked) and any rejects. Describe in one or two
  sentences what the seed photos have in common and ask me to confirm before searching.

STEP 3: Search ALL of the event's photos, not just those where the couple was recognised. Make
  contact sheets of about 8 photos in a working folder outside the repo, zoomed in on the couple
  where they're recognised, numbered so picks can be traced back. Split the sheets across subagents
  if there are many; every reviewer studies the taste sheet first and must open every sheet in its
  range. Be strict: most sheets have nothing. Only the best frame of a burst.

STEP 4: Check every candidate yourself at full size. Drop near-duplicates of photos already chosen,
  photos of the wrong people, and anything that is only unflattering. Tell me which of your picks
  match my seed photos (those confirm the search is on target), then show me the rest to review.
  Never delete anything; rejected picks are just set aside.

STEP 5: Repeat steps 3-4 with my keep/reject feedback added to the taste sheet until I'm happy.

STEP 6: Write the story in my chosen tone (format in the playbook):
  - chapters in the order the photos were taken, one per function, each with a short title
  - look for setup/punchline PAIRS and use them
  - end on a photo that works as a last line
  - first line of the thank-you note = intro; the rest = finale before the selfie button
  - captions: one line, add the joke the photo sets up, affectionate never humiliating
  Show me the full story as text first.

STEP 7: If the guest page has no story section yet, follow Part 3 of the playbook: propose the
  change and wait for my OK before editing any code. Preview the page at phone width before guests
  can see it.

Rules: no new software or packages without asking me first. Never put a PIN, password, token or
anyone's personal data in chat. Keep the story behind the event's guest PIN.
```

---

### Tone guide: the same photo in each tone

Photo: the bride looks thoroughly unimpressed in the middle of a romantic hug.

| Tone | Caption |
|---|---|
| Funny | The romantic shoot. One of us was fully committed. |
| Sarcastic | Romance: achieved. Allegedly. |
| Self-roasting (from the couple) | We were told to "look in love". One of us was busy thinking about lunch. |
| Neutral / warm | A quiet moment between the photos everyone remembers. |

For **neutral / warm**, look for *candid* moments instead of bloopers: real laughter, small glances, hands held during a ritual. The process is the same; only the taste sheet changes.

### Story format

The first version kept the story in a `story.json` file next to the chosen photos:

```json
{
  "title": "Srini & Megha",
  "intro": "First line of the thank-you note.",
  "chapters": [
    {
      "label": "Chapter 1",
      "title": "Two villains meet",
      "items": [
        {
          "pair": [
            {"file": "01a_bride.jpg", "caption": "She had a plan.", "focus": "45% 40%"},
            {"file": "01b_groom.jpg", "caption": "So did he."}
          ],
          "caption": "Optional line under the pair."
        },
        {"file": "02_something.jpg", "caption": "A single photo with its caption."}
      ]
    }
  ],
  "thanks": "Rest of the thank-you note.",
  "cta": "Line that leads into the selfie button.",
  "signoff": "Srini & Megha"
}
```

- `focus` (optional, for pairs) moves the crop: `"50% 30%"` keeps the top-centre in view.
- In this repo, `file` would naturally become the photo's `public_id` within the event.

---

## Part 3: Adding the story section to this repo (one-time)

This repo doesn't show a story yet. What the first version needed, translated to Studio:

- **Storage:** one story per event, the JSON above with photo references by `public_id`, saved with the event (a column or a small table) and editable by roles that can manage events.
- **Guest API:** an endpoint such as `GET /e/<slug>/story` that applies the **same guest-PIN check** as the search and returns the story, or 404 when there is none. Story photos are served only if the story names them, through the existing photo-serving path, so nothing else in the event becomes reachable.
- **Guest page (`studio/static/guest.html`):** after the PIN (or straight away when there is none), render a hero (names + intro), chapters, single photos and pairs, then the finale and the existing selfie flow. Add a pinned **Find my photos** button that scrolls to the selfie box, and hide the story when results are shown. Build everything with `textContent`, never `innerHTML`, so caption text can't inject markup.
- **Studio side:** at minimum, a way to paste or upload the story JSON on the event page. Ideally, preview it exactly as guests will see it before publishing.
- **Tests:** in `tests/` style: story hidden without the PIN, unnamed photos refused, no story returns 404, other events' photos never reachable.

### Checklist before guests see it

- [ ] Every photo checked at full size; no near-duplicates; no photos of the wrong couple
- [ ] Nothing only unflattering or embarrassing; no jokes at a guest's or a child's expense
- [ ] Names and sign-off spelled the way the couple wants
- [ ] Previewed at phone width
- [ ] Opened the guest link and entered the PIN once to confirm the story loads
