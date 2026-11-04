# spoty-trade-bot

A Telegram bot that uses the [Sportmonks Football API v3](https://docs.sportmonks.com/football) to list fixtures, analyse matchups and estimate outcome probabilities.

## What it does

| Command | Description |
|---|---|
| `/upcoming` | All matches in the next 7 days, grouped by day, with a button per match for its prediction. Each day has a `/tips_YYYYMMDD` link for that day's predictions |
| `/today` | Today's fixtures, with a button per match to analyse it. On days without matches it shows `/upcoming` |
| `/fixtures 2026-11-08` | Fixtures on a given date |
| `/predict <fixture_id>` | A **simple prediction** in plain words: winner, the expected **goal range** with the best Over line and the best Under line for that match (the highest Over and lowest Under that are each at least 60% likely, e.g. "Over 1.5 · Under 3.5 → 2–3 goals"), and both teams to score, each with its chance and the **lowest SportyBet odds worth taking**. Tap **📊 Full details** for every market (over and under, 1.5/2.5/3.5, corners, cards), form, head-to-head and the verdict |
| `/tips [YYYY-MM-DD]` | A predictions overview for a day (default today): the most likely result, over 2.5 goals and any corners lead |
| `/remind <fixture_id>` | Sends a fresh analysis 1 hour before kick-off, when line-ups are usually out. Also available as a button under each analysis |
| `/team <name>` | Looks up a team's Sportmonks ID |
| `/alerts on` / `/alerts off` | Turns automatic alerts on or off. On: a daily scan at `SCAN_TIME` messages you each new corners lead, it's re-checked 1 hour before kick-off, and you get the result after the match |
| `/value <fixture_id> <bet> <odds>` | Checks whether the odds you see at your bookmaker (for example SportyBet) beat the **market's fair price**, for any market. Example: `/value 19722750 over2.5 1.35`. Bets you can check: `1 x 2 1x x2 12 over2.5 under3.5 btts btts-no corners-over9.5 cards-under4.5`. Each ✅ check is recorded and settled after the match |
| `/value <odds>` | Checks the odds for the next corners lead |
| `/follow <fixture_id>` / `/unfollow` | Live updates: kick-off, goals with the scorer, red cards, half-time and full-time with corners and cards. Also available as a button under each analysis, and automatic for leads when alerts are on. Checks every ~2 minutes, for up to 3 matches at once |
| `/record` | Scorecards: corners leads, plus your own ✅ value checks settled at the odds you entered |

### Reading the analysis

- **Fair odds = 1 ÷ probability.** A bet only has value when a bookmaker pays **more** than the fair odds.
- **Headline probabilities** come from bookmaker prices with the margin removed, averaged across bookmakers. In testing these were the most accurate numbers available. Our own models are shown alongside them.
- **Probabilities from the bookmakers** come from averaging all of them and removing their margin with the **power method**, which corrects for long shots being overpriced. On past matches it was more accurate than simple normalisation. If a line has fewer than 3 bookmakers, or a margin above 9%, the bot shows our model's number instead and labels it.
- **The verdict** is based only on what the backtests proved (see below). For the match result, goals and BTTS it says "No bet", because no rule beat bookmaker prices. **Corners leads** are labelled experimental and are for paper-trading only.

## How the models work

- **Team ratings** (`spotybot/ratings.py`). For goals, corners and cards, each team gets an attack factor A and a defence factor D. They're estimated by maximum-a-posteriori Poisson fitting, with exponential time decay (half-life about 87 days) and Gamma shrinkage toward the league average:
  `expected_home = base_home × A_home × D_away × R_referee` and `expected_away = base_away × A_away × D_home × R_referee`.
  The referee factor R is used for cards only.
- **Distributions.** Goals use a Dixon–Coles-corrected Poisson. Corners use a negative binomial, because they're slightly over-dispersed. Cards use a Poisson.
- **Form model** (`spotybot/model.py`). Recent form and head-to-head give a second opinion on the result and goals.
- **League history** is downloaded once every 12 hours, about 15 requests per league. Each analysis then needs only **one** request, which keeps the bot within the free plan's limit of 180 requests per hour.

## How accurate is it?

Every backtest is in `tools/` and replays real past matches using only data available before kick-off. `tools/advanced_backtest.py` tunes its settings on 2024/25 and reports only on later matches (536 test matches from July 2025 to Nov 2026). All betting results are at the **average** bookmaker price.

| Market | Our model vs bookmakers | Betting result (test period) |
|---|---|---|
| Match result (1X2) | Bookmakers right 57.3%, our model 53.7%. Bookmakers are more accurate | Every rule lost money (−15% to −25%) |
| Goals over/under, all lines | Bookmakers slightly more accurate. A 30% blend is a hair better | Every rule lost money |
| Both teams to score | Bookmakers more accurate | Lost money |
| **Corners**, all lines | A **30% model blend beat the bookmakers** on accuracy | Edge > 5%: **+11% ± 16%** over 126 bets. Promising, but not statistically proven |
| Cards (with referee) | Bookmakers more accurate. Little data | Roughly break-even, inconclusive |

So the bot can show you the **true probability** of each outcome, as well as anything available. It has **no proven way to beat bookmakers**. Corners is the one lead worth tracking forward.

To re-run the backtests:

```bash
python -m tools.advanced_backtest
```

```bash
python -m tools.odds_backtest --leagues 271,501
```

```bash
python -m tools.lines_backtest --leagues 271,501
```

Downloaded pages are cached in `.cache/`.

## Sportmonks plan coverage

On the free Sportmonks plan, only the Danish Superliga and the Scottish Premiership are available, so `/today` is empty on days when neither league plays. Upgrade the Sportmonks plan to add more leagues. The bot picks them up automatically.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in the tokens
python -m spotybot.bot
```

- `SPORTMONKS_API_TOKEN`: your Sportmonks token. Never commit it, because `.env` is gitignored.
- `TELEGRAM_BOT_TOKEN`: create a bot with [@BotFather](https://t.me/BotFather) and paste its token here.
- `BOT_TIMEZONE`: an IANA timezone for "today" and kick-off times, for example `Africa/Lagos`.
- `DEFAULT_LEAGUE_IDS`: optional comma-separated league IDs, which limit `/today` and `/tips`.

## Deploy on Render

`render.yaml` defines a **free web service** that runs the bot in webhook mode:

1. Push this repo to GitHub. That's already done.
2. In Render, click **New → Blueprint** and select this repo.
3. When prompted, enter `SPORTMONKS_API_TOKEN`, `TELEGRAM_BOT_TOKEN` and `DATABASE_URL` (see below). Leave `ALLOWED_CHAT_IDS` empty for now.
4. Deploy. The bot registers its webhook automatically, using the `RENDER_EXTERNAL_URL` that Render sets.
5. Send `/start` to your bot. It replies with your chat ID. Set `ALLOWED_CHAT_IDS` to that number in Render so that only you can use the bot. This protects your Sportmonks request allowance and your alerts.
6. Send `/alerts on`.

### Keep the scorecard: free Supabase database

Render's free disk is wiped on every restart, so alerts and the scorecard need a database:

1. Create a free project at [supabase.com](https://supabase.com).
2. Click **Connect**, choose **Session pooler**, and copy the URI. It looks like `postgresql://postgres.xxxx:[YOUR-PASSWORD]@aws-0-....pooler.supabase.com:5432/postgres`.
3. Replace `[YOUR-PASSWORD]` with your database password, then add the result in Render as `DATABASE_URL`.

The bot creates its tables automatically. Use the Session pooler, not the direct connection, because Render needs IPv4.

### Keep it awake

Free Render services sleep after about 15 minutes without traffic. To keep yours awake, add a job at [cron-job.org](https://cron-job.org):

- **URL:** `https://<your-service>.onrender.com/health`. The response is `{"status":"ok", ...}`.
- **Schedule:** every 10 minutes.

Render's free tier includes 750 hours a month, which is enough for one service running all the time.

Run the bot without `RENDER_EXTERNAL_URL` or `WEBHOOK_URL` set and it uses polling instead, which is what you want for local runs.

## Tests

```bash
pip install pytest
python -m pytest -q
```

The tests use hand-built sample data and a mocked HTTP API, so they don't need network access or tokens.
