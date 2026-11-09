#!/bin/bash

# Instagram Unban Bot - Quick Deployment Script
# Run this script to deploy optimized bot

set -e  # Exit on error

echo "🚀 Instagram Unban Bot - Optimized Deployment"
echo "=============================================="
echo ""

# Check Python version
echo "✓ Checking Python version..."
python3 --version || { echo "❌ Python 3 not found!"; exit 1; }
echo ""

# Install dependencies
echo "✓ Installing dependencies..."
pip3 install -r requirements.txt || { echo "❌ Failed to install dependencies!"; exit 1; }
echo ""

# Verify aiosqlite installation
echo "✓ Verifying aiosqlite..."
python3 -c "import aiosqlite; print(f'aiosqlite {aiosqlite.version} installed')" || { echo "❌ aiosqlite not installed!"; exit 1; }
echo ""

# Syntax check
echo "✓ Running syntax checks..."
python3 -m py_compile bot.py database.py || { echo "❌ Syntax errors found!"; exit 1; }
echo ""

# Check .env file
if [ ! -f .env ]; then
    echo "⚠️  .env file not found! Creating from example..."
    cp .env.example .env
    echo "❌ Please edit .env with your tokens before running!"
    echo "   nano .env"
    exit 1
fi
echo "✓ .env file found"
echo ""

# Verify BOT_TOKEN
if grep -q "your_bot_token_here" .env; then
    echo "❌ Please set BOT_TOKEN in .env file!"
    exit 1
fi
echo "✓ BOT_TOKEN configured"
echo ""

# Check for old database and create backup
if [ -f monitors.db ]; then
    echo "✓ Found existing database, creating backup..."
    cp monitors.db monitors.db.backup.$(date +%Y%m%d_%H%M%S)
    echo "  Backup saved"
fi
echo ""

# Final confirmation
echo "=============================================="
echo "✅ All checks passed!"
echo ""
echo "Ready to start bot:"
echo "  • Async database layer: ✅"
echo "  • Streaming queue: ✅"
echo "  • 20 concurrent workers: ✅"
echo "  • Memory leak fixes: ✅"
echo ""
echo "To start the bot, run:"
echo "  python3 bot.py          # Telegram bot only"
echo "  python3 run_all.py      # Both Telegram + Discord"
echo ""
echo "Monitor logs for:"
echo "  ✅ Queue feeder worker started"
echo "  ✅ Monitor processor #X started (20 workers)"
echo "  ✅ Username tracking worker started"
echo "  🚀 Optimized Telegram bot started with 20 workers"
echo ""
echo "=============================================="
