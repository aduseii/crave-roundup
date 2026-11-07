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
3. After posting, tick them again and press **Mark selected as posted**. They're hidden from then on (tick **Show posted** to see them; Shift+click a posted story's box to unmark it).
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
4. **First run:** **Actions → Roundup → Run workflow**, pick `morning`.

## Changing what it watches

- `config/watchlist_shows.txt`: shows. Add `*` after titles that are also everyday words or phrases (`Ghosts*`, `Full House*`, `Jury Duty*`) so they only count when the headline puts them in quotes.
- `config/watchlist_people.txt`: actors, comedians, creators.
- `config/sources.json`: news feeds, Google News searches, YouTube channels, and `allowed_outlets`, the outlets Google News results are kept from (add one there if a good site is missing).

Edit them right on GitHub (pencil icon). The next run uses the changes. The **Sources** box at the bottom of each roundup shows which sources worked.

## Notes

- GitHub's scheduler can run a few minutes late, especially on the hour.
- Stories are kept for 7 days to avoid repeats. Old roundups stay in the Archive.
- `python tests/offline_test.py` runs the whole thing on made-up sample feeds, with no internet.
