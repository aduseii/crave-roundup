# Sitcom Crave Roundup

A plain website that gathers comedy TV news for Sitcom Crave and hands picked stories to Grok for captioning.

- **Morning roundup** around 7am GMT: overnight news, anniversaries (birthdays, death anniversaries, premieres, finales), upcoming premieres.
- **Evening roundup** around 6pm GMT: only what's new since the morning.
- **Breaking check** every 30 minutes: deaths, cancellations and endings, renewals, exits, arrests, revivals. Sends a Telegram alert if set up, and updates the Breaking page.

Instagram, X and captions are handled by the Grok bot, not this site.

## Using the page

1. Tick the stories you want (in the **Post today** list or any section).
2. **Copy for Grok** copies them as one numbered packet; paste it into your Grok chat or project.
   **Open in Grok** opens grok.com with the packet already typed in (and copies it too, as a backup).
3. After posting, tick them again and press **Mark selected as posted**. They're hidden from then on (tick **Show posted** to see them; Shift+click a posted story's box to unmark it).
4. Change the line at the top of every packet under **Grok header line**.

Posted marks and the header line are saved in your browser, so they're per device.

## Setup (one time)

1. **Turn on the website:** repo **Settings → Pages → Build and deployment**: Source **Deploy from a branch**, Branch **main**, folder **/docs**, Save.
   The site will be at `https://aduseii.github.io/crave-roundup/`.
2. **TMDB (upcoming premieres):** repo **Settings → Secrets and variables → Actions → New repository secret**, name `TMDB_API_KEY`, value your TMDB API key.
3. **Telegram alerts (optional):**
   - In Telegram, message `@BotFather`, send `/newbot`, follow the steps and copy the bot token.
   - Send any message to your new bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy the `chat` → `id` number.
   - Add two secrets: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
4. **First run:** **Actions → Roundup → Run workflow**, pick `morning`.

## Changing what it watches

- `config/watchlist_shows.txt`: shows. Add `*` after one-word titles that are normal words (`Ghosts*`) so they only count in quotes.
- `config/watchlist_people.txt`: actors, comedians, creators.
- `config/sources.json`: news feeds, Google News searches, YouTube channels.

Edit them right on GitHub (pencil icon). The next run uses the changes. The **Sources** box at the bottom of each roundup shows which sources worked.

## Notes

- GitHub's scheduler can run a few minutes late, especially on the hour.
- Stories are kept for 7 days to avoid repeats. Old roundups stay in the Archive.
- `python tests/offline_test.py` runs the whole thing on made-up sample feeds, with no internet.
