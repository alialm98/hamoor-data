# Daily price update — launchd setup

Runs `update_prices.py` every Sun–Thu at 14:00 local time (after Boursa Kuwait close).

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

- If the Mac is asleep at 14:00, launchd fires the job at the next wake. Misses are not retried.
- The script defaults to a 10-day rolling window so a missed day self-heals on the next run.
- For a manual catch-up across a wider gap: `python3 update_prices.py --from 2026-05-13 --to 2026-05-14`
