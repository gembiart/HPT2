#!/usr/bin/env python3
"""
Tkinter GUI client for the CasparCG playlist daemon.
"""

from __future__ import annotations

import json
import socket
import tkinter as tk
import uuid
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable


DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8765


def make_item(clip: str, logo: str = "", item_id: str | None = None) -> dict[str, str]:
    return {
        "id": item_id or str(uuid.uuid4()),
        "clip": clip.strip(),
        "logo": logo.strip(),
    }


class ApiClient:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    def request(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request = {
            "id": str(uuid.uuid4()),
            "action": action,
            "payload": payload or {},
        }
        raw = json.dumps(request, ensure_ascii=False) + "\n"

        with socket.create_connection((self.host, self.port), timeout=5) as sock:
            sock.sendall(raw.encode("utf-8"))
            fh = sock.makefile("r", encoding="utf-8", newline="\n")
            response = json.loads(fh.readline())

        if not response.get("ok"):
            raise RuntimeError(response.get("error", "Unknown daemon error"))

        return response.get("result") or {}


class PlaylistClientApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("CasparCG Playlist Client")
        self.geometry("920x620")

        self.items: list[dict[str, str]] = []
        self.current_index = -1
        self.running = False

        self.host_var = tk.StringVar(value=DEFAULT_API_HOST)
        self.port_var = tk.IntVar(value=DEFAULT_API_PORT)
        self.clip_var = tk.StringVar()
        self.logo_var = tk.StringVar()
        self.index_var = tk.IntVar(value=0)
        self.status_var = tk.StringVar(value="Nie połączono")

        self._build_ui()

    @property
    def api(self) -> ApiClient:
        return ApiClient(self.host_var.get().strip(), int(self.port_var.get()))

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.pack(fill=tk.X)

        ttk.Label(top, text="Daemon host").pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.host_var, width=15).pack(side=tk.LEFT, padx=(4, 12))

        ttk.Label(top, text="port").pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.port_var, width=7).pack(side=tk.LEFT, padx=(4, 12))

        ttk.Button(top, text="Połącz / odśwież", command=self.refresh_state).pack(side=tk.LEFT)
        ttk.Button(top, text="Wyślij całą listę", command=self.send_full_playlist).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="Start", command=self.play_from_selection).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="Stop", command=self.stop_playout).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="Stop + clear", command=lambda: self.stop_playout(clear=True)).pack(side=tk.LEFT, padx=4)

        table_frame = ttk.Frame(self, padding=(8, 0, 8, 8))
        table_frame.pack(fill=tk.BOTH, expand=True)

        columns = ("index", "clip", "logo", "id")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("index", text="#")
        self.tree.heading("clip", text="Clip MXF/LXF")
        self.tree.heading("logo", text="Logo PNG")
        self.tree.heading("id", text="ID")
        self.tree.column("index", width=50, anchor=tk.CENTER)
        self.tree.column("clip", width=260)
        self.tree.column("logo", width=220)
        self.tree.column("id", width=300)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.configure(yscrollcommand=scrollbar.set)

        editor = ttk.LabelFrame(self, text="Edycja pozycji", padding=8)
        editor.pack(fill=tk.X, padx=8, pady=(0, 8))

        ttk.Label(editor, text="Index").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(editor, textvariable=self.index_var, width=8).grid(row=0, column=1, padx=4)

        ttk.Label(editor, text="Clip").grid(row=0, column=2, sticky=tk.W)
        ttk.Entry(editor, textvariable=self.clip_var, width=28).grid(row=0, column=3, padx=4)

        ttk.Label(editor, text="Logo").grid(row=0, column=4, sticky=tk.W)
        ttk.Entry(editor, textvariable=self.logo_var, width=28).grid(row=0, column=5, padx=4)

        ttk.Button(editor, text="Dodaj na index", command=self.insert_item).grid(row=0, column=6, padx=4)
        ttk.Button(editor, text="Zmień zaznaczony", command=self.update_selected_item).grid(row=0, column=7, padx=4)
        ttk.Button(editor, text="Usuń zaznaczony", command=self.delete_selected_item).grid(row=0, column=8, padx=4)

        buttons = ttk.Frame(self, padding=(8, 0, 8, 8))
        buttons.pack(fill=tk.X)

        ttk.Button(buttons, text="W górę", command=lambda: self.move_selected(-1)).pack(side=tk.LEFT)
        ttk.Button(buttons, text="W dół", command=lambda: self.move_selected(1)).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="Import JSON", command=self.import_json).pack(side=tk.LEFT, padx=12)
        ttk.Button(buttons, text="Export JSON", command=self.export_json).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Wyczyść playlistę", command=self.clear_playlist).pack(side=tk.LEFT, padx=12)

        status = ttk.Label(self, textvariable=self.status_var, anchor=tk.W, padding=8)
        status.pack(fill=tk.X)

    def refresh_state(self) -> None:
        self._safe_api_call("get_state", {}, self.apply_state)

    def send_full_playlist(self) -> None:
        payload = {
            "items": self.items,
            "start_index": max(0, self.current_index),
        }
        self._safe_api_call("replace_playlist", payload, self.apply_state)

    def play_from_selection(self) -> None:
        index = self.selected_index()
        if index is None:
            index = 0
        self._safe_api_call("play", {"start_index": index}, self.apply_state)

    def stop_playout(self, clear: bool = False) -> None:
        self._safe_api_call("stop", {"clear": clear}, self.apply_state)

    def insert_item(self) -> None:
        clip = self.clip_var.get().strip()
        if not clip:
            messagebox.showwarning("Brak clip", "Podaj nazwę pliku clip.")
            return

        index = max(0, min(int(self.index_var.get()), len(self.items)))
        item = make_item(clip=clip, logo=self.logo_var.get())
        self._safe_api_call("insert_item", {"index": index, "item": item}, self.apply_state)

    def update_selected_item(self) -> None:
        index = self.selected_index()
        if index is None:
            messagebox.showwarning("Brak wyboru", "Zaznacz pozycję do zmiany.")
            return

        clip = self.clip_var.get().strip()
        if not clip:
            messagebox.showwarning("Brak clip", "Podaj nazwę pliku clip.")
            return

        item = make_item(clip=clip, logo=self.logo_var.get(), item_id=self.items[index]["id"])
        self._safe_api_call("update_item", {"index": index, "item": item}, self.apply_state)

    def delete_selected_item(self) -> None:
        index = self.selected_index()
        if index is None:
            messagebox.showwarning("Brak wyboru", "Zaznacz pozycję do usunięcia.")
            return

        self._safe_api_call("delete_item", {"index": index}, self.apply_state)

    def move_selected(self, delta: int) -> None:
        old_index = self.selected_index()
        if old_index is None:
            return
        new_index = max(0, min(old_index + delta, len(self.items) - 1))
        if old_index == new_index:
            return

        self._safe_api_call(
            "move_item",
            {"old_index": old_index, "new_index": new_index},
            self.apply_state,
        )

    def clear_playlist(self) -> None:
        if not messagebox.askyesno("Wyczyść", "Wyczyścić playlistę w daemonie?"):
            return
        self._safe_api_call("clear_playlist", {}, self.apply_state)

    def import_json(self) -> None:
        path = filedialog.askopenfilename(
            title="Import playlisty JSON",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            items = data.get("playlist", data if isinstance(data, list) else [])
            self.items = [
                make_item(str(item["clip"]), str(item.get("logo", "")), str(item.get("id") or uuid.uuid4()))
                for item in items
            ]
            self.render_table()
            self.status_var.set(f"Zaimportowano lokalnie {len(self.items)} pozycji. Kliknij 'Wyślij całą listę'.")
        except Exception as exc:
            messagebox.showerror("Import error", str(exc))

    def export_json(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Export playlisty JSON",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return

        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"playlist": self.items}, fh, indent=2, ensure_ascii=False)
            self.status_var.set(f"Zapisano {path}")
        except Exception as exc:
            messagebox.showerror("Export error", str(exc))

    def on_select(self, _event: tk.Event) -> None:
        index = self.selected_index()
        if index is None:
            return
        item = self.items[index]
        self.index_var.set(index)
        self.clip_var.set(item["clip"])
        self.logo_var.set(item.get("logo", ""))

    def selected_index(self) -> int | None:
        selected = self.tree.selection()
        if not selected:
            return None
        return int(self.tree.item(selected[0], "values")[0])

    def apply_state(self, state: dict[str, Any]) -> None:
        self.items = [
            make_item(str(item["clip"]), str(item.get("logo", "")), str(item.get("id") or uuid.uuid4()))
            for item in state.get("playlist", [])
        ]
        self.current_index = int(state.get("current_index", -1))
        self.running = bool(state.get("running", False))
        self.render_table()
        mode = "PLAY" if self.running else "STOP"
        self.status_var.set(
            f"{mode} | pozycji: {len(self.items)} | current_index: {self.current_index} | "
            f"gra: {state.get('last_playing', '')}"
        )

    def render_table(self) -> None:
        selected_index = self.selected_index()
        for row in self.tree.get_children():
            self.tree.delete(row)

        for index, item in enumerate(self.items):
            tags = ("current",) if index == self.current_index else ()
            self.tree.insert(
                "",
                tk.END,
                values=(index, item["clip"], item.get("logo", ""), item["id"]),
                tags=tags,
            )
        self.tree.tag_configure("current", background="#d9ead3")

        if selected_index is not None and 0 <= selected_index < len(self.items):
            row_id = self.tree.get_children()[selected_index]
            self.tree.selection_set(row_id)
            self.tree.focus(row_id)

    def _safe_api_call(
        self,
        action: str,
        payload: dict[str, Any],
        on_success: Callable[[dict[str, Any]], None],
    ) -> None:
        try:
            result = self.api.request(action, payload)
            on_success(result)
        except Exception as exc:
            self.status_var.set(f"Błąd: {exc}")
            messagebox.showerror("Błąd API", str(exc))


def main() -> None:
    app = PlaylistClientApp()
    app.mainloop()


if __name__ == "__main__":
    main()
