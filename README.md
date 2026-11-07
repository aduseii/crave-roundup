# Sitcom Crave Roundup

A plain website that gathers comedy TV news for Sitcom Crave and hands picked stories to Grok for captioning.

- **Morning roundup** around 7am GMT: overnight news, anniversaries (birthdays, death anniversaries, premieres, finales), upcoming premieres.
- **Evening roundup** around 6pm GMT: only what's new since the morning.
- **Breaking check** every 30 minutes: deaths, cancellations and endings, renewals, exits, arrests, revivals. Sends a Telegram alert if set up, and updates the Breaking page.

Instagram, X and captions are handled by the Grok bot, not this site.

## Using the page

1. Tick the stories you want (in the **Post today** list or any section).
2. **Copy** copies just the stories (each headline with its source links, or the plain facts for birthdays and anniversaries, separated by a blank line). Nothing is reworded or added; your Grok setup does the writing. Paste it into your Grok chat or project.
   **Open in Grok** opens grok.com with the packet already typed in (and copies it too, as a backup).
3. After posting, tick them again and press **Posted**. They stay on the page with a green **Posted** badge; tap the badge to undo.
4. Under **Settings** you can add an optional line to put above the stories. It's empty by default.

Posted marks and the header line are saved in your browser, so they're per device.

### On iPhone

- The action bar slides up from the bottom once you tick a story: **Copy**, **Open Grok**, **Share**, **Posted**.
- **Share** opens the iPhone share sheet, so you can send the packet straight to the Grok app.
- The pill buttons under the top links jump between sections.
- **Settings** (bottom of the page) has "Show stories I already posted" and the Grok header line.
- To get it as an app: open the site in Safari, tap **Share → Add to Home Screen**. (Posted marks saved in the home-screen app are separate from the ones in Safari.)

## Setup (one time)

1. **Turn on the website:** repo **Settings → Pages → Build and deployment**: Source **Deploy from a branch**, Branch **main**, folder **/docs**, Save.
   The site will be at `https://aduseii.github.io/crave-roundup/`.
2. **TMDB (upcoming premieres):** repo **Settings → Secrets and variables → Actions → New repository secret**, name `TMDB_API_KEY`, value your TMDB API key.
3. **Telegram alerts:**
   - In Telegram, open `@BotFather`, send `/newbot`, give it a name (e.g. Sitcom Crave Roundup) and a username ending in `bot`. Copy the token it gives you.
   - Open your new bot and tap **Start**. Bots can't message you until you do.
   - Add one secret with the token. It's named `TELEGRAMBOT` in this repo (`TELEGRAM_BOT_TOKEN` also works).
   - The next run finds your chat from that Start message (it looks for the Telegram username in `config/sources.json` → `telegram`), remembers it in `data/telegram.json`, and sends a "connected" message. You then get every breaking alert plus a short note when each morning and evening roundup is ready.
4. **Caption drafts (optional, free):** get a Gemini API key at aistudio.google.com (Get API key, no card needed) and add it as the secret `GEMINI_API_KEY`. Each roundup then drafts captions for the posting schedule and breaking stories, written only from the article itself and following `config/caption_rules.md`. Drafts show under each story (tap "Draft caption") and in Telegram breaking alerts. Always check them against the source. On the free tier, Google may use what's sent to improve its products.
5. **First run:** **Actions → Roundup → Run workflow**, pick `morning`.

## Captions

- **On this day** posts (birthdays, death anniversaries, premieres, finales) get a ready caption in your exact formats, filled in from Wikidata. No AI involved.
- **News** gets a Gemini draft if the key is set (see setup). Edit `config/caption_rules.md` to change the rules it follows.
- **Caption maker (on demand):** paste your Gemini key once in **Settings** on each phone or computer you use (it's saved only on that device, never on the site). Then tap **Write caption** under any story, or paste any link or story text into the **Caption maker** box at the top. Gemini reads the article itself and follows `config/caption_rules.md`.
- **Grok** still gets only the plain story (headline and links) when you use Copy, Open Grok or Share.

## Only fresh news

- Each roundup shows only what's new since the last one, and only stories published in the last 30 days. A story is never repeated in a later roundup (even when another outlet covers it again); this memory lasts 30 days.
- Breaking alerts on Telegram fire only for stories less than 6 hours old. Slightly older breaking stories still appear on the Breaking page.
- Rehashed "old news" (ranked lists, "you totally missed", "before they were famous", "why X left", "everything we know"...) is filtered out. The patterns are in `config/not_news.txt`; delete a line if it drops stories you want.
- The hours are in `config/sources.json` → `freshness`.

## Changing what it watches

- `config/watchlist_shows.txt`: shows. Add `*` after titles that are also everyday words or phrases (`Ghosts*`, `Full House*`, `Jury Duty*`) so they only count when the headline puts them in quotes.
- `config/watchlist_people.txt`: actors, comedians, creators.
- `config/sources.json`: news feeds, Google News searches, YouTube channels, and `allowed_outlets`, the outlets Google News results are kept from (add one there if a good site is missing).

Edit them right on GitHub (pencil icon). The next run uses the changes. The **Sources** box at the bottom of each roundup shows which sources worked.

## Notes

- GitHub's scheduler can run a few minutes late, especially on the hour.
- Stories are kept for 7 days to avoid repeats. Old roundups stay in the Archive.
- `python tests/offline_test.py` runs the whole thing on made-up sample feeds, with no internet.
