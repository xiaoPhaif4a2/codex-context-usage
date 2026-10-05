"""Double-click launcher for the Windows badge, without a console window."""

import os
import sys
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)

try:
    from context_indicator.__main__ import main
    main()
except Exception as error:
    if "--managed-follower" in sys.argv:
        from datetime import datetime, timezone
        try:
            with (Path(__file__).resolve().parent / ".context-indicator-follower.log").open("a", encoding="utf-8") as stream:
                stream.write(f"{datetime.now(timezone.utc).isoformat()} launcher failed: {type(error).__name__}: {error}\n")
        except OSError:
            pass
        raise SystemExit(1)
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("上下文指示器启动失败", str(error))
    root.destroy()
