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

No model can make football results certain. Well-calibrated models usually pick the 1X2 result correctly about 50–55% of the time. The confidence label and the **double chance** line are there to help you judge risk. Treat the outputs as probabilities.

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

## Tests

```bash
pip install pytest
python -m pytest -q
```

The tests use hand-built sample data and a mocked HTTP API, so they don't need network access or tokens.
