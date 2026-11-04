# spoty-trade-bot

A Telegram bot that uses the [Sportmonks Football API v3](https://docs.sportmonks.com/football) to list fixtures, analyse matchups and estimate outcome probabilities.

## What it does

| Command | Description |
|---|---|
| `/today` | Today's fixtures, with a button per match to analyse it |
| `/fixtures 2026-11-08` | Fixtures on a given date |
| `/predict <fixture_id>` | Full analysis: 1X2, double chance, expected goals, over/under 1.5/2.5/3.5, BTTS, likely scores, form and head-to-head |
| `/tips` | Scans today's upcoming fixtures and ranks the strongest picks |
| `/team <name>` | Looks up a team's Sportmonks ID |

## How predictions work

1. **Team strength:** attack and defence ratings come from each team's last ~12 finished matches. Recent games are weighted more heavily, and the ratings are shrunk toward the average so that small samples don't produce extreme numbers.
2. **Expected goals:** `xG_home = base_home × attack_home × defence_away`, and the same for the away side.
3. **Score model:** a Dixon–Coles corrected Poisson matrix over scores from 0–0 to 10–10. All the markets are derived from it.
4. **Head-to-head:** a small adjustment of at most ±5%.
5. **Sportmonks Predictions add-on:** when your plan includes it, the model blends Sportmonks' own 1X2 probabilities in at a 35% weight.

## How accurate is it?

`tools/backtest.py` replays real past matches. Each prediction uses only data from before kick-off. The results below come from 896 Danish Superliga and Scottish Premiership matches played between Aug 2024 and Nov 2026:

| Market | Result |
|---|---|
| 1X2 pick correct | **51.6%**. Always picking the home team would be 44.9% |
| Picks at 60% or more | Right about 65–76% of the time. The percentages are well calibrated |
| Picks at 45–60% | Right about 50% of the time |
| Best double chance | Right 71–91%, depending on the probability shown |
| Over/under, BTTS | Only slightly better than the league average. These figures are shrunk toward league rates, so a stated 56% happens about 56% of the time |

1X2 has a real but modest edge. Goal markets have very little edge from team form alone. No model makes football results certain, so treat every number as a probability.

To re-run the backtest:

```bash
python -m tools.backtest
```

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
3. When prompted, enter `SPORTMONKS_API_TOKEN` and `TELEGRAM_BOT_TOKEN`.
4. Deploy. The bot registers its webhook automatically, using the `RENDER_EXTERNAL_URL` that Render sets.

Free web services sleep after about 15 minutes without traffic, so the first reply after a quiet spell can take about a minute. If you want instant replies, switch the service to a paid plan.

Run the bot without `RENDER_EXTERNAL_URL` or `WEBHOOK_URL` set and it uses polling instead, which is what you want for local runs.

## Tests

```bash
pip install pytest
python -m pytest -q
```

The tests use hand-built sample data and a mocked HTTP API, so they don't need network access or tokens.
