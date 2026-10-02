# Telegram alerts

Optional but recommended. Fill `dk.json` → `telegram`: `bot_token` and `chat_id`.

1. In Telegram, open **@BotFather**, send `/newbot`, choose a name and a username ending in `bot`.
   It replies with a token; put it in `telegram.bot_token`.
2. Open your new bot (the link BotFather gives) and send it any message, such as `hi`.
3. Run:
   ```bash
   make telegram-chat      # finds your chat id and saves it
   make telegram-test      # you receive a message
   ```

You then get: an alert when a post fails, misses its slot or ends up uncertain; an alert when a
job such as the Sheet sync fails; and a digest at 08:00 Berlin time with today's posts, yesterday's
results and anything needing you (for example a token about to expire).

**Group instead of a private chat:** add the bot to the group, send a message there, then paste the
group's id (a negative number) into `telegram.chat_id` by hand.

**Dead-man's switch (optional):** create a check at [healthchecks.io](https://healthchecks.io) with a
2-minute period, and paste its ping URL into `alerts.heartbeat_url`. You are then alerted even when the
whole server is down.
