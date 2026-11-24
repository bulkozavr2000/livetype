"""Test target: a plain text window that saves its content to a file every 150 ms.

python tests/target_window.py <out.txt> <x> <y> <w> <h>
"""

import ctypes
import json
import sys
import tkinter as tk

ctypes.windll.shcore.SetProcessDpiAwareness(2)
out, x, y, w, h = sys.argv[1], *map(int, sys.argv[2:6])

root = tk.Tk()
root.title("LiveType — тестовое окно")
root.geometry(f"{w}x{h}+{x}+{y}")
text = tk.Text(root, font=("Segoe UI", 12), wrap="word", padx=12, pady=10, undo=True)
text.pack(fill="both", expand=True)


def save() -> None:
    with open(out, "w", encoding="utf-8") as f:
        f.write(text.get("1.0", "end-1c"))
    root.after(150, save)


def ready() -> None:
    root.lift()
    root.focus_force()
    text.focus_set()
    info = {"text_x": text.winfo_rootx(), "text_y": text.winfo_rooty()}
    with open(out + ".json", "w", encoding="utf-8") as f:
        json.dump(info, f)


root.after(150, save)
root.after(300, ready)
root.after(240_000, root.destroy)
root.mainloop()
