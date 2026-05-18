# Intraday price update — launchd setup

Runs `run-and-push.sh` every **15 minutes from 09:30 to 13:00** Kuwait local
time, Sun–Thu (75 calendar-interval entries in the plist).

This covers the full Boursa Kuwait trading session (9:30 AM – 12:30 PM)
with a buffer for the official close to settle by 13:00.

### How "today's price" updates intraday

`update_prices.py` fetches a rolling 10-day window from Yahoo Finance.
Yahoo's daily bar for today contains the latest intraday price during the
trading session (Yahoo data is ~15 min delayed for the free feed). The
merge logic in the script keys rows by date — so each run **overwrites**
the row for today rather than appending a duplicate. After the market
closes at 12:30, the next runs pull the official settled close.

The shell wrapper invokes `update_prices.py`, then `git commit && git push`
the fresh data to <https://github.com/alialm98/hamoor-data>. The iOS app
polls the GitHub URL every 15 minutes while in foreground, so users see
today's intraday tick within ~15 minutes of it being published.

## Install (one-time)

```bash
# Copy the plist into LaunchAgents (symlink so edits in the repo take effect)
ln -sf "/Users/alialmutawa/Desktop/personal/Personaal Projects/tawze3at/data/launchd/com.alialmutawa.hamoor.dailyupdate.plist" \
       ~/Library/LaunchAgents/com.alialmutawa.hamoor.dailyupdate.plist

# Load the job
launchctl load -w ~/Library/LaunchAgents/com.alialmutawa.hamoor.dailyupdate.plist
```

## Manage

```bash
# Trigger manually right now (doesn't wait for next 14:00)
launchctl start com.alialmutawa.hamoor.dailyupdate

# See if it's loaded and last exit code
launchctl list | grep hamoor

# Stop scheduled runs
launchctl unload ~/Library/LaunchAgents/com.alialmutawa.hamoor.dailyupdate.plist

# Reload after editing the plist
launchctl unload ~/Library/LaunchAgents/com.alialmutawa.hamoor.dailyupdate.plist
launchctl load -w ~/Library/LaunchAgents/com.alialmutawa.hamoor.dailyupdate.plist
```

## Logs

- `data/logs/update.out.log` — stdout (per-stock progress)
- `data/logs/update.err.log` — stderr (yfinance warnings, errors)

Logs are appended; rotate or truncate manually if they grow.

## Notes

- If the Mac is asleep at a scheduled time, launchd fires the job at the next wake. Missed fires are NOT made up.
- The script defaults to a 10-day rolling window so a missed day self-heals on the next run.
- For a manual catch-up across a wider gap: `python3 update_prices.py --from 2026-05-13 --to 2026-05-14`
- The push step uses the `gh` CLI's stored credentials. If a push ever fails, run `gh auth status` to confirm the token is still valid.
- For an on-demand push, just run `bash data/run-and-push.sh` from any terminal.
- Each run takes ~2-3 min (141 stocks × ~1 sec per Yahoo request). With 15 fires/day that's ~30-45 min of total runtime per trading day.

## After updating the plist

Whenever you edit this plist (e.g. to change the schedule), reload it:

```bash
launchctl unload ~/Library/LaunchAgents/com.alialmutawa.hamoor.dailyupdate.plist
launchctl load -w ~/Library/LaunchAgents/com.alialmutawa.hamoor.dailyupdate.plist
```
