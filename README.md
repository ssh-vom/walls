# Walls

Textual TUI for searching Wallhaven and setting wallpapers on macOS.

## Setup

- Create a `.env` file with `WALLHAVEN_API_KEY=...` (optional, required for NSFW).
- Install dependencies: `uv sync`.
- Run the app: `uv run python main.py`.

## Usage

- Type a search query and press `Enter`.
- Use `Up` / `Down` to move through results.
- Press `Enter` on a result to download and set the wallpaper.
- Use `n` / `p` to move between pages.
- Press `q` to quit.

Cache paths:

- `~/.cache/walls/thumbs`
- `~/.cache/walls/full`
