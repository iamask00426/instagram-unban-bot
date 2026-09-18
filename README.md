# Instagram Status & Ban Monitor Bot 🚀

Telegram Bot for real-time monitoring of Instagram account status changes (Unban detection & Ban alerts).

## Features ✨
- 🟢 `/monitor <username>` — Monitor disabled account until it gets **UNBANNED** (`Banned ➔ Active`).
- 🔴 `/banmonitor <username>` — Monitor active account until it gets **BANNED** (`Active ➔ Banned`).
- 🛑 `/stop <username>` — Stop active monitoring for a specific handle.
- 📊 `/active` — View all currently active monitoring tasks.

## Setup & Installation 🛠️

1. Clone the repository:
   ```bash
   git clone https://github.com/iamask00426/instagram-unban-bot.git
   cd instagram-unban-bot
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Configure environment variables:
   Copy `.env.example` to `.env` and fill in your Bot Token from @BotFather:
   ```bash
   cp .env.example .env
   ```

4. Run the Bot:
   ```bash
   python bot.py
   ```
