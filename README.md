# 🦋 ButterflyDen

ButterflyDen is a self-hosted Discord bot for downloading media from supported platforms using **yt-dlp**.

It is designed to be:

* simple
* honest about its limits
* easy to run on a Raspberry Pi
* safe for long-running, 24/7 use

---

## ✨ Features

* **`/download`**
  Downloads media from supported platforms:

  * videos
  * images
  * image galleries

  Automatically compresses videos when needed to fit Discord upload limits.

* **`/mp3`**
  Extracts audio and delivers it as an MP3.

* Single-message responses (no spam)

* Automatic cleanup of temporary files

* Safe concurrency handling (one job at a time)

* Designed for always-on operation with `systemd`

---

## 📦 Supported platforms

ButterflyDen relies on **yt-dlp**, so it supports many platforms, including:

* YouTube
* Twitter / X
* Instagram
* TikTok
* Reddit

…and more, as supported by yt-dlp.

Unsupported URLs fail gracefully.

---

## 🛠 Requirements

* Python 3.10+
* `ffmpeg`
* A Discord bot token

---

## 🚀 Installation (Linux / Raspberry Pi)

Clone the repository:

```
git clone https://github.com/Whims-Dev/ButterflyDen.git
cd ButterflyDen
```

Create and activate a virtual environment:

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file:

```
DISCORD_TOKEN=your_bot_token_here
TEMP_ROOT=/mnt/storage/temp
```

Run the bot:

```
python bot.py
```

---

## ⚙️ Running as a service (recommended)

ButterflyDen is designed to be run using **systemd** for:

* automatic startup on boot
* automatic restarts on crash
* headless operation

---

## ⚠️ Notes

* Only one download runs at a time to avoid overload.
* If the bot is busy, new requests are politely rejected.
* Discord may block some media uploads due to content scanning.
* This bot does **not** scrape arbitrary websites.

---

## 🤖 AI Assistance

Parts of this project were developed with the assistance of AI tools.

AI was used as a programming aid for:

* brainstorming architecture and workflows
* debugging and explaining errors
* refining code structure and readability

All final decisions, testing, integration, and deployment were performed by the project author.

---

## 📜 License

MIT License
© 2026 Whims-Dev