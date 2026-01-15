from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import os
import subprocess
import sys
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Static
from textual_image.widget import Image


BASE_URL = "https://wallhaven.cc/api/v1"
CACHE_ROOT = Path.home() / ".cache" / "walls"
THUMB_DIR = CACHE_ROOT / "thumbs"
FULL_DIR = CACHE_ROOT / "full"
ASCII_GRADIENT = " .:-=+*#%@"


class WallhavenError(RuntimeError):
    """Errors raised when the Wallhaven API fails."""


@dataclass(frozen=True)
class Wallpaper:
    identifier: str
    thumb_url: str
    full_url: str
    resolution: str
    category: str
    purity: str
    file_type: str


class WallhavenClient:
    def __init__(self, api_key: str | None) -> None:
        self.api_key = api_key
        self.session = requests.Session()
        if api_key:
            self.session.headers.update({"X-API-Key": api_key})

    def search(
        self,
        query: str,
        purity: int,
        page: int = 1,
    ) -> tuple[list[Wallpaper], dict[str, Any]]:
        params = {"q": query, "page": page, "purity": purity}
        try:
            response = self.session.get(f"{BASE_URL}/search", params=params, timeout=15)
        except requests.RequestException as exc:
            raise WallhavenError(f"Request failed: {exc}") from exc

        if response.status_code == 401:
            raise WallhavenError("Unauthorized. Check WALLHAVEN_API_KEY.")
        if response.status_code == 429:
            raise WallhavenError("Rate limit reached. Try again later.")

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise WallhavenError(f"API error: {exc}") from exc

        payload = response.json()
        results = [self._parse_wallpaper(item) for item in payload.get("data", [])]
        meta = payload.get("meta", {})
        return results, meta

    @staticmethod
    def _parse_wallpaper(item: dict[str, Any]) -> Wallpaper:
        thumbs = item.get("thumbs", {})
        thumb_url = thumbs.get("small") or thumbs.get("large") or ""
        return Wallpaper(
            identifier=item.get("id", ""),
            thumb_url=thumb_url,
            full_url=item.get("path", ""),
            resolution=item.get("resolution", ""),
            category=item.get("category", ""),
            purity=item.get("purity", ""),
            file_type=item.get("file_type", ""),
        )


class CacheManager:
    def __init__(self) -> None:
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        FULL_DIR.mkdir(parents=True, exist_ok=True)

    def thumbnail_path(self, wallpaper: Wallpaper) -> Path:
        return self._path_for_url(wallpaper.thumb_url, THUMB_DIR, wallpaper.identifier)

    def full_path(self, wallpaper: Wallpaper) -> Path:
        return self._path_for_url(wallpaper.full_url, FULL_DIR, wallpaper.identifier)

    @staticmethod
    def _path_for_url(url: str, directory: Path, identifier: str) -> Path:
        suffix = Path(urlparse(url).path).suffix or ".jpg"
        return directory / f"{identifier}{suffix}"

    def download(self, url: str, destination: Path) -> Path:
        if destination.exists() and destination.stat().st_size > 0:
            return destination

        try:
            response = requests.get(url, stream=True, timeout=20)
            response.raise_for_status()
            with destination.open("wb") as file_handle:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        file_handle.write(chunk)
        except Exception as exc:
            if destination.exists():
                destination.unlink(missing_ok=True)
            raise WallhavenError(f"Download failed: {exc}") from exc
        return destination


def format_details(wallpaper: Wallpaper) -> str:
    lines = [
        f"ID: {wallpaper.identifier}",
        f"Resolution: {wallpaper.resolution}",
        f"Category: {wallpaper.category}",
        f"Purity: {wallpaper.purity}",
        f"Type: {wallpaper.file_type}",
        f"URL: {wallpaper.full_url}",
    ]
    return "\n".join(lines)


class WallItem(ListItem):
    def __init__(self, wallpaper: Wallpaper) -> None:
        label = Label(
            f"{wallpaper.identifier} • {wallpaper.resolution} • "
            f"{wallpaper.category} • {wallpaper.purity}"
        )
        super().__init__(label)
        self.wallpaper = wallpaper


class WallsApp(App):
    CSS = """
    Screen {
        layout: vertical;
    }

    #body {
        height: 1fr;
        padding: 1 2;
    }

    #query {
        margin-bottom: 1;
    }

    #content {
        height: 1fr;
        layout: horizontal;
    }

    #results {
        width: 1fr;
        border: round $border;
    }

    #preview-pane {
        width: 1fr;
        layout: vertical;
    }

    #preview-text {
        height: 1fr;
        border: round $border;
        padding: 1;
    }

    #details {
        height: auto;
        border: round $border;
        padding: 1;
    }

    #status {
        height: auto;
        padding: 0 1;
        color: $text-muted;
    }

    #cache-content {
        height: 1fr;
        layout: vertical;
    }

    #cache-preview {
        height: 1fr;
        border: round $border;
    }

    #cache-info {
        height: 8;
        border: round $border;
        padding: 1;
        margin-top: 1;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("right", "next_page_or_cache", "Next"),
        ("left", "previous_page_or_cache", "Previous"),
        ("c", "toggle_cache_mode", "Cache mode"),
        ("x", "nsfw_filter", "NSFW_FILTER"),
    ]

    TITLE = "Walls"

    def __init__(self, client: WallhavenClient, cache: CacheManager) -> None:
        super().__init__()
        self.client = client
        self.cache = cache
        self.search_query = ""
        self.current_page = 1
        self.last_page = 1
        self.purity = 100  # default sfw
        self.results: list[Wallpaper] = []
        self.cache_mode = False
        self.cached_wallpapers: list[Wallpaper] = []
        self.current_cache_index = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="body"):
            yield Input(placeholder="Search Wallhaven...", id="query")
            with Horizontal(id="content"):
                yield ListView(id="results")
                with Vertical(id="preview-pane"):
                    yield Image(id="preview-text")
                    yield Static("", id="details")
            with Vertical(id="cache-content"):
                yield Image(id="cache-preview")
                yield Static("", id="cache-info")
            yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#query", Input).focus()
        self.query_one("#cache-content").display = False
        message = "Enter a search term and press Enter."
        if not self.client.api_key:
            message += " No API key detected; NSFW results unavailable."
        self.update_status(message)

    def update_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

    def update_preview(self, preview_path: Path | str | None, details: str) -> None:
        image_widget = self.query_one("#preview-text", Image)
        if preview_path:
            image_widget.image = preview_path
        else:
            image_widget.image = None
        self.query_one("#details", Static).update(details)

    def action_next_page_or_cache(self) -> None:
        if self.cache_mode:
            self.action_next_cache_item()
        else:
            self.action_next_page()

    def action_previous_page_or_cache(self) -> None:
        if self.cache_mode:
            self.action_previous_cache_item()
        else:
            self.action_previous_page()

    def action_next_page(self) -> None:
        if not self.search_query:
            self.update_status("Enter a search term first.")
            return
        if self.current_page >= self.last_page:
            self.update_status(f"Already at last page ({self.last_page}).")
            return
        self.current_page += 1
        self.start_search()

    def action_previous_page(self) -> None:
        if not self.search_query:
            self.update_status("Enter a search term first.")
            return
        if self.current_page <= 1:
            self.update_status("Already at first page.")
            return
        self.current_page -= 1
        self.start_search()

    def action_next_cache_item(self) -> None:
        if not self.cached_wallpapers:
            return
        if self.current_cache_index < len(self.cached_wallpapers) - 1:
            self.current_cache_index += 1
            self.update_cache_view()
        else:
            self.update_status("At last cached wallpaper.")

    def action_previous_cache_item(self) -> None:
        if not self.cached_wallpapers:
            return
        if self.current_cache_index > 0:
            self.current_cache_index -= 1
            self.update_cache_view()
        else:
            self.update_status("At first cached wallpaper.")

    def action_purity(self) -> None:
        from constants import purity_state_mapping as next_state

        self.purity = next_state[self.purity]

    def action_toggle_cache_mode(self) -> None:
        self.cache_mode = not self.cache_mode
        content_view = self.query_one("#content")
        cache_view = self.query_one("#cache-content")
        input_field = self.query_one("#query", Input)

        if self.cache_mode:
            content_view.display = False
            cache_view.display = True
            input_field.display = False
            self.load_cached_wallpapers()
        else:
            content_view.display = True
            cache_view.display = False
            input_field.display = True
            self.update_status("Switched to search mode.")

    def load_cached_wallpapers(self) -> None:
        self.update_status("Loading cached wallpapers...")
        self.list_cached_wallpapers()

    def list_cached_wallpapers(self) -> None:
        if not FULL_DIR.exists():
            self.update_status("No cached wallpapers found.")
            return

        cached_files = list(FULL_DIR.iterdir())
        if not cached_files:
            self.update_status("No cached wallpapers found.")
            return

        self.cached_wallpapers = []
        for file_path in sorted(
            cached_files, key=lambda p: p.stat().st_mtime, reverse=True
        ):
            identifier = file_path.stem
            cached_wallpaper = Wallpaper(
                identifier=identifier,
                thumb_url=str(file_path),
                full_url=str(file_path),
                resolution="",
                category="cached",
                purity="sfw",
                file_type=file_path.suffix,
            )
            self.cached_wallpapers.append(cached_wallpaper)

        if self.cached_wallpapers:
            self.current_cache_index = 0
            self.update_cache_view()
            self.update_status(
                f"Loaded {len(self.cached_wallpapers)} cached wallpapers. "
                f"Use left/right arrows to navigate, Enter to set wallpaper."
            )
        else:
            self.update_status("No cached wallpapers found.")

    def update_cache_view(self) -> None:
        if not self.cached_wallpapers:
            self.query_one("#cache-preview", Image).image = None
            self.query_one("#cache-info", Static).update("")
            return

        wallpaper = self.cached_wallpapers[self.current_cache_index]
        self.query_one("#cache-preview", Image).image = wallpaper.full_url

        details = format_details(wallpaper)
        info = f"{details}\n\n[{self.current_cache_index + 1}/{len(self.cached_wallpapers)}]"
        self.query_one("#cache-info", Static).update(info)

    def on_input_submitted(self, message: Input.Submitted) -> None:
        query = message.value.strip()
        if not query:
            self.update_status("Enter a non-empty search query.")
            return
        self.search_query = query
        self.current_page = 1
        self.start_search()

    def on_key(self, event: events.Key) -> None:
        if self.cache_mode and event.key == "enter":
            if self.cached_wallpapers:
                wallpaper = self.cached_wallpapers[self.current_cache_index]
                self.set_cached_wallpaper(wallpaper)

    def on_list_view_highlighted(self, message: ListView.Highlighted) -> None:
        if not message.item or not isinstance(message.item, WallItem):
            self.update_preview(None, "")
            return
        wallpaper = message.item.wallpaper
        self.load_preview(wallpaper)

    def on_list_view_selected(self, message: ListView.Selected) -> None:
        if isinstance(message.item, WallItem):
            if self.cache_mode:
                self.set_cached_wallpaper(message.item.wallpaper)
            else:
                self.set_wallpaper(message.item.wallpaper)

    def start_search(self) -> None:
        self.update_status(
            f"Searching '{self.search_query}' (page {self.current_page})..."
        )
        self.search_wallpapers(self.search_query, self.current_page)

    def show_results(self, results: list[Wallpaper], meta: dict[str, Any]) -> None:
        self.results = results
        list_view = self.query_one("#results", ListView)
        list_view.clear()
        list_view.extend(WallItem(result) for result in results)
        if results:
            list_view.index = 0
        else:
            self.update_preview(None, "")

        self.current_page = meta.get("current_page", self.current_page)
        self.last_page = meta.get("last_page", self.current_page)
        total = meta.get("total")
        if total is not None:
            status = (
                f"Loaded {len(results)} results "
                f"(page {self.current_page}/{self.last_page}, total {total})."
            )
        else:
            status = (
                f"Loaded {len(results)} results "
                f"(page {self.current_page}/{self.last_page})."
            )
        self.update_status(status)

    def show_error(self, message: str) -> None:
        self.update_status(f"Error: {message}")

    @work(thread=True, exclusive=True, group="search")
    def search_wallpapers(self, query: str, page: int) -> None:
        try:
            results, meta = self.client.search(query, self.purity, page)
        except WallhavenError as exc:
            self.call_from_thread(self.show_error, str(exc))
            return
        self.call_from_thread(self.show_results, results, meta)

    @work(thread=True, exclusive=True, group="preview")
    def load_preview(self, wallpaper: Wallpaper) -> None:
        if not wallpaper.thumb_url:
            self.call_from_thread(
                self.update_preview,
                None,
                format_details(wallpaper),
            )
            return

        try:
            if self.cache_mode:
                preview_path = Path(wallpaper.full_url)
                if preview_path.exists():
                    cached_thumbnail = preview_path
                else:
                    cached_thumbnail = None
            else:
                thumbnail_path = self.cache.thumbnail_path(wallpaper)
                cached_thumbnail = self.cache.download(
                    wallpaper.thumb_url, thumbnail_path
                )
            details = format_details(wallpaper)
        except WallhavenError as exc:
            cached_thumbnail = None
            details = f"{format_details(wallpaper)}\nError: {exc}"

        self.call_from_thread(self.update_preview, cached_thumbnail, details)

    @work(thread=True, exclusive=True, group="wallpaper")
    def set_wallpaper(self, wallpaper: Wallpaper) -> None:
        self.call_from_thread(
            self.update_status, f"Downloading {wallpaper.identifier}..."
        )
        try:
            full_path = self.cache.full_path(wallpaper)
            cached_full = self.cache.download(wallpaper.full_url, full_path)
            self._set_macos_wallpaper(cached_full)
        except WallhavenError as exc:
            self.call_from_thread(self.show_error, str(exc))
            return
        except Exception as exc:
            self.call_from_thread(self.show_error, f"Wallpaper set failed: {exc}")
            return

        self.call_from_thread(
            self.update_status, f"Wallpaper set to {wallpaper.identifier}."
        )

    @work(thread=True, exclusive=True, group="wallpaper")
    def set_cached_wallpaper(self, wallpaper: Wallpaper) -> None:
        self.call_from_thread(
            self.update_status,
            f"Setting wallpaper from cache: {wallpaper.identifier}...",
        )
        try:
            full_path = Path(wallpaper.full_url)
            if not full_path.exists():
                raise WallhavenError(f"Cached file not found: {full_path}")
            self._set_macos_wallpaper(full_path)
        except WallhavenError as exc:
            self.call_from_thread(self.show_error, str(exc))
            return
        except Exception as exc:
            self.call_from_thread(self.show_error, f"Wallpaper set failed: {exc}")
            return

        self.call_from_thread(
            self.update_status, f"Wallpaper set to {wallpaper.identifier} (from cache)."
        )

    @staticmethod
    def _set_macos_wallpaper(path: Path) -> None:
        if sys.platform != "darwin":
            raise WallhavenError("Wallpaper setting is supported on macOS only.")
        safe_path = str(path).replace('"', '\\"')
        script = (
            'tell application "System Events"\n'
            "repeat with desktop_item in desktops\n"
            f'set picture of desktop_item to POSIX file "{safe_path}"\n'
            "end repeat\n"
            "end tell"
        )
        subprocess.run(["osascript", "-e", script], check=True)


def main() -> None:
    load_dotenv()
    api_key = os.getenv("WALLHAVEN_API_KEY")
    client = WallhavenClient(api_key)
    cache = CacheManager()
    app = WallsApp(client, cache)
    app.run()


if __name__ == "__main__":
    main()
