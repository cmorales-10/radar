# Radar

A stock research scanner that runs around the clock on GitHub's servers, so your PC can be
off. It ranks US stocks by unusual activity, shows them on a dashboard you can add to your
phone's home screen, and sends push notifications while the market is open.

This is a research tool, not investment advice. It does not know your finances, it never
trades, and it does not tell you what to buy. Neither this app nor Claude is a licensed advisor.

## What it does

| | |
| --- | --- |
| Research | Every 15 minutes during US market hours, hourly otherwise |
| Notifications | Only while the market is open: one opening digest, then up to 6 alerts a day |
| What people are buying and discussing | Reddit mentions, Stocktwits and Yahoo trending, SEC insider purchases |
| Price and volume | Yahoo Finance, which can be delayed |
| Cost | Free. The optional Claude brief is billed by Anthropic |

### How the score works

| Category | Max | Earned by |
| --- | --- | --- |
| Attention | 35 | Reddit rank, a surge in mentions, trending lists |
| Price and volume | 45 | Volume against its normal pace, today's move, 20-session trend, nearness to the 52-week high |
| Insiders | 20 | Company insiders buying on the open market |

An alert needs a score of at least 55 and at least 10 points in two categories, so chatter
alone cannot trigger one. Each alert also lists risk flags. Settings are in `config.json`.

The score has not been shown to predict returns. The **Scorecard** tab measures this by
comparing every past alert with SPY after 1, 5, and 20 trading days.

## Setup (about 15 minutes)

### 1. Install notifications on your phone

1. Install **ntfy** from the App Store or Google Play.
2. Create a private topic name by running this on your PC:

   ```bash
   py -c "import secrets; print('radar-' + secrets.token_urlsafe(18))"
   ```

3. In the app, tap **+** and subscribe to that exact name. Anyone who knows the name can read
   the alerts or send you fake ones, so keep it private.

### 2. Put the project on GitHub

1. Sign up at [github.com](https://github.com) if you need an account.
2. Create a [new repository](https://github.com/new) named `radar`. Set it to **Public**, and
   add no README or other files. Public repositories get free unlimited scheduled runs and a
   free dashboard. The code, scan results, and dashboard are visible to anyone. No personal
   data is stored in them.
3. From this `radar` folder, run these one at a time, using your GitHub username:

   ```bash
   git init -b main
   ```

   ```bash
   git add .
   ```

   ```bash
   git commit -m "Add Radar"
   ```

   ```bash
   git remote add origin https://github.com/YOUR-USERNAME/radar.git
   ```

   ```bash
   git push -u origin main
   ```

Only this `radar` folder gets uploaded. Do not upload the parent AutoRobin folder, because
its `risk_rules.json` contains your brokerage account number.

### 3. Add your settings on GitHub

In your repository, open **Settings > Secrets and variables > Actions > New repository secret**:

| Name | Value | Needed? |
| --- | --- | --- |
| `NTFY_TOPIC` | The topic name from step 1 | Yes |
| `SEC_CONTACT` | Your email address. The SEC asks automated tools to identify themselves | Recommended |
| `ANTHROPIC_API_KEY` | A key from [console.anthropic.com](https://console.anthropic.com) | Optional, see below |

### 4. Turn on the dashboard and start the scanner

1. **Settings > Pages**: set **Source** to **GitHub Actions**.
2. **Actions** tab: open **scan**, then **Run workflow**. Wait about a minute for a green check.
3. Open `https://YOUR-USERNAME.github.io/radar/` on your phone.
4. Add it to your home screen. On iPhone, use Safari's Share button, then **Add to Home
   Screen**. On Android, use Chrome's menu, then **Add to Home screen**.

### 5. Test a notification

Run these from the `radar` folder, in PowerShell, with your topic name:

```bash
$env:NTFY_TOPIC = "your-topic-name"
```

```bash
py -m scanner.run --test-notify
```

## Optional: Claude's written brief

With `ANTHROPIC_API_KEY` set, Claude writes a short summary of the top signals and their
headlines at 9:00 and 12:30 ET on trading days. It explains what is going on and does not
make buy or sell calls. If it declines a request, Anthropic's server retries it on another
model (the `fallbacks` setting in `scanner/brief.py`).

It uses `claude-opus-5-5` at about $0.02 per brief, or about $1 a month. On a $50 account
that is a real cost, about 2% a month, so the scanner works fully without it. To reduce the
cost, set `brief.model` in `config.json` to `claude-haiku-5-5`, which is cheaper per token.

## Things to know

- **Timing.** GitHub can start scheduled runs late, sometimes by 15 minutes or more, and
  public data can lag. By the time you read an alert the move may be over, and fast-moving
  stocks are the worst place to be late.
- **Late signals.** Reddit and trending lists often highlight stocks after they have moved.
- **Manipulation.** Coordinated hype is common in small stocks. The default filters require
  a $5 price, $10M in average daily trading, and a Nasdaq or NYSE listing to reduce this.
- **Earnings dates** come from Yahoo. A flag appears when the date is unknown.
- **Not included.** Politician trades, because there is no reliable free feed and disclosures
  can lag by weeks. Robinhood data, because its connection only works inside a Claude session.
- **Unofficial sources.** The Yahoo endpoints are unofficial and can change or block requests
  without warning. If a scan finds no data, you get one warning a day during market hours, and
  the **About** tab shows which source failed.

## If something goes wrong

| Symptom | Check |
| --- | --- |
| Dashboard says the scanner may be down | Actions tab for a failed run |
| No notifications | Market hours, your topic name, and "notifications" in the About tab. A quiet day can have only the digest |
| Runs stopped | GitHub pauses schedules after 60 days without repository activity. The state commits normally prevent this. Re-enable it in the Actions tab |

## Development

```bash
py -m unittest discover -s tests -t .
```

```bash
py -m scanner.run --dry-run
```
