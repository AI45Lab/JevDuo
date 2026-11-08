"""Disposable native form used by the local onboarding tutorial."""

import json
from pathlib import Path
import sys
import tkinter as tk


def main(directory: Path):
    window = tk.Tk()
    window.title("NorthCUA tutorial — disposable form")
    window.geometry("540x190+100+150")
    window.attributes("-topmost", True)
    tk.Label(window, text="Type the requested text, then press Enter.").pack(pady=10)
    entry = tk.Entry(window, width=40, font=("Arial", 18))
    entry.pack(padx=15, pady=15)
    status = tk.Label(window, text="Waiting for the Desktop agent")
    status.pack()

    def save(_event):
        (directory / "desktop-result.txt").write_text(entry.get(), encoding="utf-8")
        status.config(text="Saved: " + entry.get())

    entry.bind("<Return>", save)

    def record(event):
        with (directory / "desktop-keys.jsonl").open("a") as target:
            target.write(json.dumps({"keysym": event.keysym, "state": event.state, "keycode": event.keycode}) + "\n")

    entry.bind("<KeyPress>", record, add=True)
    entry.bind("<Button-1>", lambda _event: (directory / "desktop-clicked").touch())
    window.update()
    window.lift()
    window.focus_force()
    entry.focus_set()

    def ready():
        (directory / "desktop-ready.json").write_text(
            json.dumps(
                {
                    "x": entry.winfo_rootx() + 50,
                    "y": entry.winfo_rooty() + entry.winfo_height() // 2,
                }
            )
        )

    window.after(500, ready)
    window.mainloop()


if __name__ == "__main__":
    main(Path(sys.argv[1]))
