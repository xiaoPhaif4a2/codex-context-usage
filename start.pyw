"""Double-click launcher for the Windows badge, without a console window."""

import os
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)

try:
    from context_indicator.__main__ import main
    main()
except Exception as error:
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("上下文指示器启动失败", str(error))
    root.destroy()
