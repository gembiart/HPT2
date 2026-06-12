#!/usr/bin/env python3
"""
Tkinter GUI client for the CasparCG playlist daemon.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tkinter as tk
import uuid
from fractions import Fraction
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable


DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8765


def make_item(
    clip: str,
    logo: str = "",
    item_id: str | None = None,
    tc_som: str = "",
    tc_dur: str = "",
) -> dict[str, str]:
    return {
        "id": item_id or str(uuid.uuid4()),
        "clip": clip.strip(),
        "logo": logo.strip(),
        "tc_som": tc_som.strip(),
        "tc_dur": tc_dur.strip(),
    }


def find_ffprobe() -> str | None:
    env_path = os.environ.get("FFPROBE_PATH")
    if env_path and os.path.exists(env_path):
        return env_path

    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(script_dir, "ffprobe.exe"),
        os.path.join(script_dir, "ffprobe"),
        os.path.join(script_dir, "ffmpeg", "bin", "ffprobe.exe"),
        os.path.join(script_dir, "ffmpeg", "bin", "ffprobe"),
    ]

    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate

    return shutil.which("ffprobe")


def read_media_metadata(path: str) -> dict[str, str]:
    ffprobe = find_ffprobe()
    if not ffprobe:
        raise FileNotFoundError(
            "Nie znaleziono ffprobe. Umieść ffprobe.exe obok klienta albo dodaj folder ffmpeg/bin do PATH."
        )

    command = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=True)
    data = json.loads(completed.stdout or "{}")

    video_stream = first_video_stream(data)
    frame_rate = read_frame_rate(video_stream)
    duration_seconds = read_duration_seconds(data, video_stream)

    return {
        "tc_som": read_start_timecode(data, video_stream),
        "tc_dur": seconds_to_timecode(duration_seconds, frame_rate),
    }


def first_video_stream(data: dict[str, Any]) -> dict[str, Any]:
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            return stream
    return {}


def read_start_timecode(data: dict[str, Any], video_stream: dict[str, Any]) -> str:
    for source in (
        video_stream.get("tags", {}),
        data.get("format", {}).get("tags", {}),
    ):
        for key, value in source.items():
            if key.lower() == "timecode" and value:
                return str(value)

    for side_data in video_stream.get("side_data_list", []):
        timecode = side_data.get("timecode")
        if timecode:
            return str(timecode)

    return ""


def read_frame_rate(video_stream: dict[str, Any]) -> Fraction:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = str(video_stream.get(key, ""))
        if raw and raw != "0/0":
            try:
                return Fraction(raw)
            except ValueError:
                continue
    return Fraction(25, 1)


def read_duration_seconds(data: dict[str, Any], video_stream: dict[str, Any]) -> float:
    for value in (
        video_stream.get("duration"),
        data.get("format", {}).get("duration"),
    ):
        if value not in (None, "", "N/A"):
            return float(value)

    tags = data.get("format", {}).get("tags", {})
    for key, value in tags.items():
        if key.lower() == "duration":
            parsed = parse_duration_tag(str(value))
            if parsed is not None:
                return parsed

    return 0.0


def parse_duration_tag(value: str) -> float | None:
    parts = value.split(":")
    if len(parts) != 3:
        return None

    try:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])
    except ValueError:
        return None

    return (hours * 3600) + (minutes * 60) + seconds


def seconds_to_timecode(seconds: float, frame_rate: Fraction) -> str:
    if seconds <= 0:
        return ""

    fps = max(1, round(float(frame_rate)))
    total_frames = int(round(seconds * fps))
    frames = total_frames % fps
    total_seconds = total_frames // fps
    secs = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}:{frames:02d}"


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


class ItemDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Tk,
        title: str,
        clip: str = "",
        logo: str = "",
        tc_som: str = "",
        tc_dur: str = "",
        index: int = 0,
        show_index: bool = False,
    ) -> None:
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.result: dict[str, Any] | None = None
        self.show_index = show_index

        self.clip_var = tk.StringVar(value=clip)
        self.logo_var = tk.StringVar(value=logo)
        self.tc_som_var = tk.StringVar(value=tc_som)
        self.tc_dur_var = tk.StringVar(value=tc_dur)
        self.index_var = tk.IntVar(value=index)

        self._build_ui()
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Return>", lambda _event: self.ok())
        self.bind("<Escape>", lambda _event: self.cancel())

        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() // 2) - (self.winfo_width() // 2)
        y = parent.winfo_rooty() + (parent.winfo_height() // 2) - (self.winfo_height() // 2)
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _build_ui(self) -> None:
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        row = 0
        if self.show_index:
            ttk.Label(frame, text="Index").grid(row=row, column=0, sticky=tk.W, pady=4)
            ttk.Entry(frame, textvariable=self.index_var, width=10).grid(
                row=row,
                column=1,
                sticky=tk.W,
                pady=4,
            )
            row += 1

        ttk.Label(frame, text="Clip MXF/LXF").grid(row=row, column=0, sticky=tk.W, pady=4)
        clip_entry = ttk.Entry(frame, textvariable=self.clip_var, width=42)
        clip_entry.grid(row=row, column=1, sticky=tk.W, pady=4)
        row += 1

        ttk.Label(frame, text="Logo PNG").grid(row=row, column=0, sticky=tk.W, pady=4)
        ttk.Entry(frame, textvariable=self.logo_var, width=42).grid(row=row, column=1, sticky=tk.W, pady=4)
        row += 1

        ttk.Label(frame, text="TC SOM").grid(row=row, column=0, sticky=tk.W, pady=4)
        ttk.Entry(frame, textvariable=self.tc_som_var, width=42).grid(row=row, column=1, sticky=tk.W, pady=4)
        row += 1

        ttk.Label(frame, text="TC DUR").grid(row=row, column=0, sticky=tk.W, pady=4)
        ttk.Entry(frame, textvariable=self.tc_dur_var, width=42).grid(row=row, column=1, sticky=tk.W, pady=4)
        row += 1

        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky=tk.E, pady=(12, 0))
        ttk.Button(buttons, text="OK", command=self.ok).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="Anuluj", command=self.cancel).pack(side=tk.LEFT)

        clip_entry.focus_set()

    def ok(self) -> None:
        clip = self.clip_var.get().strip()
        if not clip:
            messagebox.showwarning("Brak clip", "Podaj nazwę pliku clip.", parent=self)
            return

        self.result = {
            "index": int(self.index_var.get()),
            "item": make_item(
                clip=clip,
                logo=self.logo_var.get(),
                tc_som=self.tc_som_var.get(),
                tc_dur=self.tc_dur_var.get(),
            ),
        }
        self.destroy()

    def cancel(self) -> None:
        self.result = None
        self.destroy()


class PlaylistClientApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("CasparCG Playlist Client")
        self.geometry("1160x640")

        self.items: list[dict[str, str]] = []
        self.current_index = -1
        self.running = False

        self.host_var = tk.StringVar(value=DEFAULT_API_HOST)
        self.port_var = tk.IntVar(value=DEFAULT_API_PORT)
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

        list_box = ttk.LabelFrame(self, text="Playlista", padding=8)
        list_box.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        list_actions = ttk.Frame(list_box)
        list_actions.pack(fill=tk.X, pady=(0, 8))

        ttk.Button(list_actions, text="Dodaj", command=self.add_item_from_list).pack(side=tk.LEFT)
        ttk.Button(list_actions, text="Dodaj MXF", command=self.add_mxf_files).pack(side=tk.LEFT, padx=4)
        ttk.Button(list_actions, text="Zmień", command=self.edit_selected_item).pack(side=tk.LEFT, padx=4)
        ttk.Button(list_actions, text="Usuń", command=self.delete_selected_item).pack(side=tk.LEFT, padx=4)
        ttk.Separator(list_actions, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        ttk.Button(list_actions, text="W górę", command=lambda: self.move_selected(-1)).pack(side=tk.LEFT)
        ttk.Button(list_actions, text="W dół", command=lambda: self.move_selected(1)).pack(side=tk.LEFT, padx=4)
        ttk.Separator(list_actions, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        ttk.Button(list_actions, text="Import JSON", command=self.import_json).pack(side=tk.LEFT)
        ttk.Button(list_actions, text="Export JSON", command=self.export_json).pack(side=tk.LEFT, padx=4)
        ttk.Button(list_actions, text="Wyczyść playlistę", command=self.clear_playlist).pack(side=tk.LEFT, padx=4)

        table_frame = ttk.Frame(list_box)
        table_frame.pack(fill=tk.BOTH, expand=True)

        columns = ("index", "clip", "logo", "tc_som", "tc_dur", "id")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("index", text="#")
        self.tree.heading("clip", text="Clip MXF/LXF")
        self.tree.heading("logo", text="Logo PNG")
        self.tree.heading("tc_som", text="TC SOM")
        self.tree.heading("tc_dur", text="TC DUR")
        self.tree.heading("id", text="ID")
        self.tree.column("index", width=50, anchor=tk.CENTER)
        self.tree.column("clip", width=260)
        self.tree.column("logo", width=200)
        self.tree.column("tc_som", width=110, anchor=tk.CENTER)
        self.tree.column("tc_dur", width=110, anchor=tk.CENTER)
        self.tree.column("id", width=300)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tree.bind("<Double-1>", self.edit_selected_item)
        self.tree.bind("<Delete>", self.delete_selected_item)

        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.configure(yscrollcommand=scrollbar.set)

        hint = ttk.Label(
            self,
            text="Wybierz wiersz na liście. Dodaj wstawia po zaznaczonym wierszu; dwuklik zmienia pozycję; Delete usuwa.",
            anchor=tk.W,
            padding=(8, 0, 8, 6),
        )
        hint.pack(fill=tk.X)

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

    def add_item_from_list(self) -> None:
        index = self.insert_index_after_selection()
        dialog = ItemDialog(self, "Dodaj pozycję", index=index, show_index=True)
        self.wait_window(dialog)

        if dialog.result is None:
            return

        insert_index = max(0, min(int(dialog.result["index"]), len(self.items)))
        self._safe_api_call(
            "insert_item",
            {
                "index": insert_index,
                "item": dialog.result["item"],
            },
            self.apply_state,
        )

    def add_mxf_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Dodaj pliki MXF do playlisty",
            filetypes=[
                ("MXF files", "*.mxf"),
                ("LXF files", "*.lxf"),
                ("Video files", "*.mxf *.lxf"),
                ("All files", "*.*"),
            ],
        )
        if not paths:
            return

        logo = simpledialog.askstring(
            "Logo",
            "Logo PNG dla dodanych plików (opcjonalnie, bez .png):",
            parent=self,
        )
        if logo is None:
            return

        index = self.insert_index_after_selection()
        state: dict[str, Any] = {}

        try:
            for offset, path in enumerate(paths):
                clip_name = os.path.splitext(os.path.basename(path))[0]
                try:
                    metadata = read_media_metadata(path)
                except Exception as exc:
                    metadata = {"tc_som": "", "tc_dur": ""}
                    messagebox.showwarning(
                        "ffprobe",
                        f"Nie udało się odczytać TC z pliku:\n{path}\n\n{exc}\n\n"
                        "Pozycja zostanie dodana bez TC SOM / TC DUR.",
                    )

                item = make_item(
                    clip=clip_name,
                    logo=logo,
                    tc_som=metadata["tc_som"],
                    tc_dur=metadata["tc_dur"],
                )
                state = self.api.request(
                    "insert_item",
                    {
                        "index": index + offset,
                        "item": item,
                    },
                )
            self.apply_state(state)
            self.status_var.set(f"Dodano plików MXF/LXF: {len(paths)}")
        except Exception as exc:
            self.status_var.set(f"Błąd: {exc}")
            messagebox.showerror("Błąd API", str(exc))

    def edit_selected_item(self, _event: tk.Event | None = None) -> None:
        index = self.selected_index()
        if index is None:
            messagebox.showwarning("Brak wyboru", "Zaznacz pozycję do zmiany.")
            return

        current = self.items[index]
        dialog = ItemDialog(
            self,
            "Zmień pozycję",
            clip=current["clip"],
            logo=current.get("logo", ""),
            tc_som=current.get("tc_som", ""),
            tc_dur=current.get("tc_dur", ""),
            index=index,
            show_index=False,
        )
        self.wait_window(dialog)

        if dialog.result is None:
            return

        item = dialog.result["item"]
        item["id"] = current["id"]
        self._safe_api_call("update_item", {"index": index, "item": item}, self.apply_state)

    def delete_selected_item(self, _event: tk.Event | None = None) -> None:
        index = self.selected_index()
        if index is None:
            messagebox.showwarning("Brak wyboru", "Zaznacz pozycję do usunięcia.")
            return

        item = self.items[index]
        if not messagebox.askyesno("Usuń", f"Usunąć pozycję {index}: {item['clip']}?"):
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
                make_item(
                    str(item["clip"]),
                    str(item.get("logo", "")),
                    str(item.get("id") or uuid.uuid4()),
                    str(item.get("tc_som", "")),
                    str(item.get("tc_dur", "")),
                )
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

    def selected_index(self) -> int | None:
        selected = self.tree.selection()
        if not selected:
            return None
        return int(self.tree.item(selected[0], "values")[0])

    def insert_index_after_selection(self) -> int:
        index = self.selected_index()
        if index is None:
            return len(self.items)
        return min(index + 1, len(self.items))

    def apply_state(self, state: dict[str, Any]) -> None:
        self.items = [
            make_item(
                str(item["clip"]),
                str(item.get("logo", "")),
                str(item.get("id") or uuid.uuid4()),
                str(item.get("tc_som", "")),
                str(item.get("tc_dur", "")),
            )
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
                values=(
                    index,
                    item["clip"],
                    item.get("logo", ""),
                    item.get("tc_som", ""),
                    item.get("tc_dur", ""),
                    item["id"],
                ),
                tags=tags,
            )
        self.tree.tag_configure("current", background="#ff0000", foreground="#000000")

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
