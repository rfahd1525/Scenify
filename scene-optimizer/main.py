#!/usr/bin/env python3
"""
Scenify — Desktop Application
A Python tkinter app that uses headless Blender for all 3D operations.
"""

import json
import os
import subprocess
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import Optional, Dict, Callable

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ASSETS_DIR = os.path.join(_SCRIPT_DIR, "..")

sys.path.insert(0, _SCRIPT_DIR)

from blender_backend import BlenderBackend, BlenderResult

try:
    from PIL import Image, ImageTk, ImageDraw
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


# ── Theme ───────────────────────────────────────────────────
C = {
    "bg":            "#0e0f15",
    "surface":       "#151821",
    "surface_hi":    "#1d212d",
    "surface_lo":    "#0b0c11",
    "border":        "#262b38",
    "border_hi":     "#363c4d",
    "text":          "#e7eaf3",
    "text_muted":    "#8b91a5",
    "text_subtle":   "#5b6174",
    "primary":       "#7c8cff",
    "primary_hi":    "#94a2ff",
    "primary_lo":    "#5f6fe5",
    "success":       "#5dd4a3",
    "warning":       "#f0b95a",
    "error":         "#ff6b8a",
    "accent_soft":   "#2a2f47",
    "scrollbar":     "#2a3040",
}

FONT_UI   = ("SF Pro Display", 12)
FONT_SM   = ("SF Pro Display", 11)
FONT_XS   = ("SF Pro Display", 10)
FONT_BOLD = ("SF Pro Display", 12, "bold")
FONT_TITLE = ("SF Pro Display", 20, "bold")
FONT_SECTION = ("SF Pro Display", 13, "bold")
FONT_STAT = ("SF Pro Rounded", 28, "bold")
FONT_MONO = ("SF Mono", 11)


# ─── Custom widgets ─────────────────────────────────────────

class FlatButton(tk.Frame):
    """A flat, modern button built from a Frame + Label with hover/press states."""

    def __init__(self, parent, text, command=None, *, variant="primary",
                  icon="", padx=18, pady=10, **kwargs):
        palette = {
            "primary": (C["primary"],    C["primary_hi"], "#ffffff"),
            "accent":  (C["primary_lo"], C["primary"],    "#ffffff"),
            "ghost":   (C["surface_hi"], C["border_hi"],  C["text"]),
            "soft":    (C["surface"],    C["surface_hi"], C["text_muted"]),
        }[variant]
        self._bg, self._bg_hover, self._fg = palette
        self._disabled_bg = C["surface_hi"]
        self._disabled_fg = C["text_muted"]
        self._command = command
        self._enabled = True

        super().__init__(parent, bg=self._bg, cursor="hand2",
                         highlightthickness=1,
                         highlightbackground=C["border"],
                         bd=0, **kwargs)

        display = f"{icon}  {text}" if icon else text
        self._label = tk.Label(self, text=display, fg=self._fg, bg=self._bg,
                                font=FONT_BOLD, padx=padx, pady=pady,
                                cursor="hand2")
        self._label.pack(fill="both", expand=True)

        for w in (self, self._label):
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
            w.bind("<Button-1>", self._on_press)
            w.bind("<ButtonRelease-1>", self._on_release)

    def _on_enter(self, _):
        if self._enabled:
            self.configure(bg=self._bg_hover)
            self._label.configure(bg=self._bg_hover)

    def _on_leave(self, _):
        if self._enabled:
            self.configure(bg=self._bg)
            self._label.configure(bg=self._bg)

    def _on_press(self, _):
        if self._enabled:
            self.configure(bg=self._bg)
            self._label.configure(bg=self._bg)

    def _on_release(self, _):
        if self._enabled:
            self.configure(bg=self._bg_hover)
            self._label.configure(bg=self._bg_hover)
            if self._command:
                self._command()

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        if enabled:
            self.configure(bg=self._bg, cursor="hand2",
                           highlightbackground=C["border"])
            self._label.configure(bg=self._bg, fg=self._fg, cursor="hand2")
        else:
            self.configure(bg=self._disabled_bg, cursor="arrow",
                           highlightbackground=C["border_hi"])
            self._label.configure(bg=self._disabled_bg, fg=self._disabled_fg,
                                   cursor="arrow")

    def set_text(self, text, icon=""):
        self._label.configure(text=f"{icon}  {text}" if icon else text)


class Card(tk.Frame):
    """A bordered container with a header."""
    def __init__(self, parent, title: Optional[str] = None, subtitle: str = "",
                 **kwargs):
        super().__init__(parent, bg=C["surface"],
                         highlightbackground=C["border"],
                         highlightthickness=1, bd=0, **kwargs)
        self._subtitle = None
        if title is not None:
            header = tk.Frame(self, bg=C["surface"])
            header.pack(fill="x", padx=18, pady=(14, 4))
            tk.Label(header, text=title, bg=C["surface"], fg=C["text"],
                      font=FONT_SECTION).pack(side="left")
            self._subtitle = tk.Label(header, text=subtitle, bg=C["surface"],
                                       fg=C["text_muted"], font=FONT_XS)
            self._subtitle.pack(side="right")

    def set_subtitle(self, text: str):
        if self._subtitle is not None:
            self._subtitle.configure(text=text)


class StatCard(tk.Frame):
    def __init__(self, parent, label: str, *, accent: str = C["primary"]):
        super().__init__(parent, bg=C["surface"],
                         highlightbackground=C["border"], highlightthickness=1)
        self._accent = accent
        self._value_label = tk.Label(self, text="—", bg=C["surface"],
                                      fg=C["text_muted"], font=FONT_STAT)
        self._value_label.pack(pady=(18, 0))
        tk.Label(self, text=label, bg=C["surface"], fg=C["text_muted"],
                  font=FONT_XS).pack(pady=(4, 16))

    def set_value(self, text: str, highlight: bool = False):
        self._value_label.configure(text=text,
                                     fg=self._accent if highlight else C["text"])


def _bind_click_all(widget, handler):
    """Bind <Button-1> to a widget and every descendant so there are no dead zones."""
    widget.bind("<Button-1>", handler)
    for child in widget.winfo_children():
        _bind_click_all(child, handler)


def _draw_pill(canvas, x1, y1, x2, y2, fill, outline=None):
    """Draw a horizontal rounded-pill shape on a Canvas."""
    outline = outline or fill
    radius = (y2 - y1) / 2
    # Two end caps + center rectangle, all with the same fill/outline so the
    # seams disappear.
    canvas.create_oval(x1, y1, x1 + 2 * radius, y2,
                        fill=fill, outline=outline)
    canvas.create_oval(x2 - 2 * radius, y1, x2, y2,
                        fill=fill, outline=outline)
    canvas.create_rectangle(x1 + radius, y1, x2 - radius, y2,
                             fill=fill, outline=fill)


class RadioRow(tk.Frame):
    """Horizontal radio-button group styled for the dark theme."""
    def __init__(self, parent, var: tk.StringVar, options):
        super().__init__(parent, bg=C["surface"])
        self._var = var
        self._buttons = []
        for i, (value, label, hint) in enumerate(options):
            btn = tk.Frame(self, bg=C["surface_hi"],
                           highlightbackground=C["border"],
                           highlightthickness=1, cursor="hand2")
            btn.pack(side="left", fill="both", expand=True,
                      padx=(0 if i == 0 else 8, 0))

            inner = tk.Frame(btn, bg=C["surface_hi"], cursor="hand2")
            inner.pack(fill="both", expand=True, padx=14, pady=10)

            # Radio indicator — a ring that fills with the accent when selected.
            indicator = tk.Canvas(inner, width=20, height=20,
                                   bg=C["surface_hi"], highlightthickness=0,
                                   cursor="hand2")
            indicator.pack(side="left", padx=(0, 10), anchor="n", pady=(2, 0))

            text_col = tk.Frame(inner, bg=C["surface_hi"], cursor="hand2")
            text_col.pack(side="left", fill="both", expand=True)

            title = tk.Label(text_col, text=label, bg=C["surface_hi"],
                              fg=C["text"], font=FONT_BOLD,
                              anchor="w", cursor="hand2")
            title.pack(fill="x")

            if hint:
                tk.Label(text_col, text=hint, bg=C["surface_hi"],
                          fg=C["text_muted"], font=FONT_XS,
                          anchor="w", justify="left",
                          wraplength=240, cursor="hand2").pack(
                              fill="x", pady=(2, 0))

            handler = lambda _e, v=value: self._select(v)
            _bind_click_all(btn, handler)

            self._buttons.append({
                "value": value,
                "btn": btn,
                "inner": inner,
                "title": title,
                "indicator": indicator,
                "text_col": text_col,
            })
        self._refresh()

    def _select(self, value):
        self._var.set(value)
        self._refresh()

    def _refresh(self):
        current = self._var.get()
        for b in self._buttons:
            selected = b["value"] == current
            bg = C["accent_soft"] if selected else C["surface_hi"]
            fg_title = C["primary_hi"] if selected else C["text"]
            border = C["primary"] if selected else C["border"]

            b["btn"].configure(bg=bg, highlightbackground=border)
            b["inner"].configure(bg=bg)
            b["text_col"].configure(bg=bg)
            b["title"].configure(bg=bg, fg=fg_title)
            b["indicator"].configure(bg=bg)
            for child in b["text_col"].winfo_children():
                if child is not b["title"]:
                    child.configure(bg=bg)

            # Draw the radio dot on the indicator canvas.
            ind = b["indicator"]
            ind.delete("all")
            ring_color = C["primary"] if selected else C["border_hi"]
            ind.create_oval(1, 1, 19, 19, outline=ring_color, width=2)
            if selected:
                ind.create_oval(6, 6, 14, 14,
                                 fill=C["primary"], outline=C["primary"])


class CheckboxRow(tk.Frame):
    """Row with a label + hint and a rounded-pill toggle on the right."""

    TRACK_W = 52
    TRACK_H = 28
    KNOB_PAD = 3

    def __init__(self, parent, var: tk.BooleanVar, label: str, hint: str = ""):
        super().__init__(parent, bg=C["surface"], cursor="hand2")
        self._var = var

        left = tk.Frame(self, bg=C["surface"], cursor="hand2")
        left.pack(side="left", fill="both", expand=True)
        tk.Label(left, text=label, bg=C["surface"], fg=C["text"],
                  font=FONT_BOLD, anchor="w", cursor="hand2").pack(fill="x")
        if hint:
            tk.Label(left, text=hint, bg=C["surface"], fg=C["text_muted"],
                      font=FONT_XS, anchor="w", justify="left",
                      wraplength=320, cursor="hand2").pack(
                          fill="x", pady=(2, 0))

        self._box = tk.Canvas(self, width=self.TRACK_W, height=self.TRACK_H,
                               bg=C["surface"], highlightthickness=0,
                               cursor="hand2")
        self._box.pack(side="right", padx=(12, 0))
        self._draw()

        _bind_click_all(self, self._toggle)

    def _toggle(self, _=None):
        self._var.set(not self._var.get())
        self._draw()

    def _draw(self):
        c = self._box
        c.delete("all")
        on = self._var.get()
        track = C["primary"] if on else C["border_hi"]
        _draw_pill(c, 0, 0, self.TRACK_W, self.TRACK_H, fill=track)

        knob_size = self.TRACK_H - 2 * self.KNOB_PAD
        if on:
            x = self.TRACK_W - self.KNOB_PAD - knob_size
        else:
            x = self.KNOB_PAD
        y = self.KNOB_PAD
        c.create_oval(x, y, x + knob_size, y + knob_size,
                       fill="#ffffff", outline="#ffffff")


class ScrollableFrame(tk.Frame):
    """Vertically scrollable container; scrollbar appears only when content overflows."""

    def __init__(self, parent, bg=C["bg"], **kwargs):
        super().__init__(parent, bg=bg, **kwargs)
        self._bg = bg

        self._canvas = tk.Canvas(self, bg=bg, highlightthickness=0)
        self._scrollbar = ttk.Scrollbar(self, orient="vertical",
                                         command=self._canvas.yview,
                                         style="Modern.Vertical.TScrollbar")
        self.inner = tk.Frame(self._canvas, bg=bg)
        self._win_id = self._canvas.create_window((0, 0), window=self.inner,
                                                    anchor="nw")

        self._canvas.configure(yscrollcommand=self._on_yscroll)
        self.inner.bind("<Configure>", lambda _e: self._update_region())
        self._canvas.bind("<Configure>", self._update_width)

        self.bind("<Enter>", lambda _e: self._canvas.bind_all(
            "<MouseWheel>", self._on_mousewheel))
        self.bind("<Leave>", lambda _e: self._canvas.unbind_all("<MouseWheel>"))

        self._canvas.pack(side="left", fill="both", expand=True)

    def _on_yscroll(self, lo, hi):
        if float(lo) <= 0.0 and float(hi) >= 1.0:
            self._scrollbar.pack_forget()
        else:
            if not self._scrollbar.winfo_ismapped():
                self._scrollbar.pack(side="right", fill="y")
        self._scrollbar.set(lo, hi)

    def _update_region(self):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _update_width(self, event):
        self._canvas.itemconfig(self._win_id, width=event.width)

    def _on_mousewheel(self, event):
        self._canvas.yview_scroll(int(-1 * event.delta), "units")


def _apply_macos_icon_rounding(img: "Image.Image") -> "Image.Image":
    """Apply a macOS-style rounded rectangle mask to an icon image."""
    img = img.convert("RGBA")
    w, h = img.size
    radius = int(min(w, h) * 0.225)
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    result = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    result.paste(img, mask=mask)
    return result


# ─── App ────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("Scenify")
        self.configure(bg=C["bg"])
        self.update_idletasks()
        screen_h = self.winfo_screenheight()
        win_h = min(840, screen_h - 120)
        self.geometry(f"1280x{win_h}")
        self.minsize(1100, min(720, win_h))

        self.scene_file: Optional[str] = None
        self.manifest: Optional[Dict] = None
        self.last_extraction: Optional[Dict] = None
        self.preview_image = None
        self.backend = BlenderBackend()
        self._is_busy = False

        self.export_mode = tk.StringVar(value="single")
        self.center_pivots = tk.BooleanVar(value=True)
        self._app_icon = None
        self._logo_image = None

        if HAS_PIL:
            try:
                icon_path = os.path.join(_ASSETS_DIR, "AppIcon.png")
                icon_img = _apply_macos_icon_rounding(Image.open(icon_path))
                self._app_icon = ImageTk.PhotoImage(icon_img)
                self.iconphoto(True, self._app_icon)
            except Exception:
                pass

        if not self.backend.is_available:
            self.after(500, self._show_blender_setup)

        self._build_ui()

    # ── UI construction ─────────────────────────────────────

    def _build_ui(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        self._style_ttk(style)

        root = tk.Frame(self, bg=C["bg"])
        root.pack(fill="both", expand=True)

        self._build_header(root)
        self._build_status_bar(root)   # side="bottom"
        self._build_actions(root)      # side="bottom", above status bar

        body = tk.Frame(root, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=22, pady=(6, 6))

        left_outer = tk.Frame(body, bg=C["bg"])
        left_outer.pack(side="left", fill="both", expand=True, padx=(0, 10))

        right = tk.Frame(body, bg=C["bg"], width=400)
        right.pack(side="right", fill="both")
        right.pack_propagate(False)

        # Preview is outside the scroll area so it never disappears
        self._build_preview(left_outer)

        left_scroll = ScrollableFrame(left_outer, bg=C["bg"])
        left_scroll.pack(fill="both", expand=True)
        left = left_scroll.inner

        self._build_stats(left)
        self._build_export_options(left)
        self._build_asset_panel(right)

    def _style_ttk(self, style: ttk.Style):
        style.configure("Treeview",
                         background=C["surface_lo"],
                         foreground=C["text"],
                         fieldbackground=C["surface_lo"],
                         font=FONT_SM,
                         bordercolor=C["border"],
                         lightcolor=C["border"], darkcolor=C["border"],
                         rowheight=30)
        style.configure("Treeview.Heading",
                         background=C["surface"],
                         foreground=C["text_muted"],
                         font=(FONT_SM[0], FONT_SM[1], "bold"),
                         relief="flat")
        style.map("Treeview",
                   background=[("selected", C["accent_soft"])],
                   foreground=[("selected", C["primary_hi"])])
        style.map("Treeview.Heading",
                   background=[("active", C["surface_hi"])])

        style.configure("Modern.Vertical.TScrollbar",
                         background=C["scrollbar"],
                         troughcolor=C["surface_lo"],
                         bordercolor=C["surface_lo"],
                         arrowcolor=C["text_muted"])

        style.configure("Modern.Horizontal.TProgressbar",
                         troughcolor=C["surface_lo"],
                         background=C["primary"],
                         bordercolor=C["surface_lo"],
                         lightcolor=C["primary"],
                         darkcolor=C["primary"])

        style.configure("Modern.TCombobox",
                         fieldbackground=C["surface_lo"],
                         background=C["surface_lo"],
                         foreground=C["text"],
                         arrowcolor=C["text_muted"],
                         bordercolor=C["border"],
                         lightcolor=C["border"], darkcolor=C["border"])

    def _build_header(self, parent):
        header = tk.Frame(parent, bg=C["bg"])
        header.pack(fill="x", padx=22, pady=(16, 8))

        brand = tk.Frame(header, bg=C["bg"])
        brand.pack(side="left")

        if HAS_PIL and self._logo_image is None:
            try:
                logo_path = os.path.join(_ASSETS_DIR, "TransparentIcon.png")
                logo_img = Image.open(logo_path).resize((40, 40), Image.Resampling.LANCZOS)
                self._logo_image = ImageTk.PhotoImage(logo_img)
            except Exception:
                pass
        if self._logo_image:
            logo = tk.Label(brand, image=self._logo_image, bg=C["bg"])
        else:
            logo = tk.Label(brand, text="⬡", bg=C["bg"], fg=C["primary"],
                            font=("SF Pro Display", 26, "bold"))
        logo.pack(side="left", padx=(0, 10))

        titles = tk.Frame(brand, bg=C["bg"])
        titles.pack(side="left")
        tk.Label(titles, text="Scenify", bg=C["bg"],
                  fg=C["text"], font=FONT_TITLE).pack(anchor="w")
        tk.Label(titles, text="Deduplicate, extract, and rebuild scenes in Roblox",
                  bg=C["bg"], fg=C["text_muted"], font=FONT_XS).pack(anchor="w")

        actions = tk.Frame(header, bg=C["bg"])
        actions.pack(side="right")

        self.btn_settings = FlatButton(actions, "Settings", icon="⚙",
                                        variant="ghost",
                                        command=self._show_settings)
        self.btn_settings.pack(side="right", padx=(8, 0))

        self.btn_open = FlatButton(actions, "Open Scene", icon="📁",
                                    variant="primary", command=self._open_file)
        self.btn_open.pack(side="right")

    def _build_preview(self, parent):
        self._preview_card = Card(parent, title="Scene Preview",
                                    subtitle="No scene loaded")
        self._preview_card.pack(fill="x", pady=(0, 10))

        self.preview_canvas = tk.Canvas(self._preview_card,
                                          bg=C["surface_lo"],
                                          highlightthickness=0, height=280)
        self.preview_canvas.pack(fill="x", padx=18, pady=(2, 16))
        self.preview_canvas.bind("<Configure>", lambda _e: self._draw_empty_preview())
        self.after(50, self._draw_empty_preview)

    def _build_stats(self, parent):
        row = tk.Frame(parent, bg=C["bg"])
        row.pack(fill="x", pady=(0, 10))

        self.stat_instances = StatCard(row, "Total Instances")
        self.stat_instances.pack(side="left", fill="both", expand=True,
                                  padx=(0, 8))
        self.stat_unique = StatCard(row, "Unique Assets",
                                     accent=C["primary_hi"])
        self.stat_unique.pack(side="left", fill="both", expand=True,
                               padx=(0, 8))
        self.stat_opt = StatCard(row, "Optimization",
                                  accent=C["success"])
        self.stat_opt.pack(side="left", fill="both", expand=True)

    def _build_export_options(self, parent):
        card = Card(parent, title="Export Options")
        card.pack(fill="x", pady=(0, 10))

        body = tk.Frame(card, bg=C["surface"])
        body.pack(fill="x", padx=18, pady=(2, 14))

        RadioRow(body, self.export_mode, [
            ("single",     "Single combined GLB",
             "One file containing every asset. Import once into Roblox Studio. (Recommended)"),
            ("individual", "Individual files",
             "One GLB per asset. Slower to import; useful for modular uploads."),
        ]).pack(fill="x", pady=(0, 10))

        CheckboxRow(body, self.center_pivots,
                     "Center pivots on bounding box",
                     "Aligns every asset's origin to its geometric center so "
                     "reconstruction in Roblox stays consistent across the scene.").pack(fill="x")

    def _build_actions(self, parent):
        bar = tk.Frame(parent, bg=C["bg"],
                       highlightbackground=C["border"], highlightthickness=1)
        bar.pack(fill="x", side="bottom", padx=22, pady=(0, 8))

        self.btn_extract = FlatButton(bar, "Export Assets", icon="▶",
                                       variant="primary",
                                       command=self._extract_assets,
                                       pady=14)
        self.btn_extract.pack(side="left", fill="x", expand=True, padx=(0, 6),
                               pady=10)
        self.btn_extract.set_enabled(False)

        self.btn_lua = FlatButton(bar, "Generate Lua", icon="📝",
                                    variant="ghost",
                                    command=self._generate_lua,
                                    pady=14)
        self.btn_lua.pack(side="left", fill="x", expand=True, padx=(6, 0),
                           pady=10)
        self.btn_lua.set_enabled(False)

    def _build_asset_panel(self, parent):
        card = Card(parent, title="Unique Assets", subtitle="")
        card.pack(fill="both", expand=True)
        self._asset_card = card

        controls = tk.Frame(card, bg=C["surface"])
        controls.pack(fill="x", padx=18, pady=(0, 6))

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", self._filter_assets)

        search = tk.Entry(controls, textvariable=self.search_var,
                           bg=C["surface_lo"], fg=C["text"],
                           insertbackground=C["text"],
                           font=FONT_SM, relief="flat", bd=0,
                           highlightthickness=1,
                           highlightbackground=C["border"],
                           highlightcolor=C["primary"])
        search.pack(fill="x", ipady=7, ipadx=10, pady=(2, 8))
        # Placeholder-style hint
        search.insert(0, "")

        self.category_var = tk.StringVar(value="All")
        cat_combo = ttk.Combobox(controls, textvariable=self.category_var,
                                   values=["All"], state="readonly",
                                   style="Modern.TCombobox", font=FONT_SM)
        cat_combo.pack(fill="x")
        cat_combo.current(0)
        cat_combo.bind("<<ComboboxSelected>>", lambda _e: self._filter_assets())
        self.category_menu = cat_combo

        tree_wrap = tk.Frame(card, bg=C["surface"])
        tree_wrap.pack(fill="both", expand=True, padx=18, pady=(10, 16))

        self.asset_tree = ttk.Treeview(
            tree_wrap,
            columns=("count", "category", "verts"),
            show="tree headings",
            height=20,
        )

        scroll = ttk.Scrollbar(tree_wrap, orient="vertical",
                                command=self.asset_tree.yview,
                                style="Modern.Vertical.TScrollbar")
        self.asset_tree.configure(yscrollcommand=scroll.set)

        self.asset_tree.heading("#0", text="Asset", anchor="w")
        self.asset_tree.column("#0", width=190, anchor="w")
        self.asset_tree.heading("count", text="Count", anchor="center")
        self.asset_tree.column("count", width=60, anchor="center")
        self.asset_tree.heading("category", text="Category", anchor="w")
        self.asset_tree.column("category", width=100, anchor="w")
        self.asset_tree.heading("verts", text="Verts", anchor="e")
        self.asset_tree.column("verts", width=70, anchor="e")

        self.asset_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _build_status_bar(self, parent):
        bar = tk.Frame(parent, bg=C["surface_lo"],
                        highlightbackground=C["border"], highlightthickness=1)
        bar.pack(fill="x", side="bottom")

        self.progress = ttk.Progressbar(bar, mode="indeterminate",
                                          style="Modern.Horizontal.TProgressbar",
                                          length=200)
        self.progress.pack(side="left", padx=(12, 8), pady=8)
        self.progress.pack_forget()

        self.status_label = tk.Label(bar, text="Ready — open a scene to begin",
                                       bg=C["surface_lo"], fg=C["text_muted"],
                                       font=FONT_SM)
        self.status_label.pack(side="left", padx=12, pady=8)

        blender_text = f"Blender {'detected' if self.backend.is_available else 'not found'}"
        blender_color = C["success"] if self.backend.is_available else C["error"]
        self.blender_label = tk.Label(bar, text=f"● {blender_text}",
                                        bg=C["surface_lo"], fg=blender_color,
                                        font=FONT_XS)
        self.blender_label.pack(side="right", padx=12, pady=8)

    # ── Preview rendering ───────────────────────────────────

    def _draw_empty_preview(self):
        canvas = self.preview_canvas
        canvas.delete("all")
        w = canvas.winfo_width() or 600
        h = canvas.winfo_height() or 320
        if w <= 1 or h <= 1:
            return
        canvas.create_rectangle(0, 0, w, h, fill=C["surface_lo"],
                                 outline=C["surface_lo"])
        canvas.create_text(w // 2, h // 2 - 8, text="⬡",
                            fill=C["border_hi"], font=("SF Pro Display", 48))
        canvas.create_text(w // 2, h // 2 + 34,
                            text="Open a scene file to see a preview",
                            fill=C["text_subtle"], font=FONT_SM)

    def _display_preview(self, image_path: str):
        if not HAS_PIL or not os.path.exists(image_path):
            return
        canvas = self.preview_canvas
        canvas.delete("all")
        canvas.update_idletasks()
        w = canvas.winfo_width() or 600
        h = canvas.winfo_height() or 320
        try:
            img = Image.open(image_path)
            ratio = min(w / img.width, h / img.height)
            img = img.resize((int(img.width * ratio), int(img.height * ratio)),
                              Image.Resampling.LANCZOS)
            self.preview_image = ImageTk.PhotoImage(img)
            canvas.create_image(w // 2, h // 2, image=self.preview_image,
                                 anchor="center")
        except Exception as e:
            canvas.create_text(w // 2, h // 2, text=f"Preview failed: {e}",
                                fill=C["error"], font=FONT_SM)

    # ── Actions ─────────────────────────────────────────────

    def _open_file(self):
        if self._is_busy:
            return
        filetypes = [
            ("3D Scene Files", "*.blend *.fbx *.obj *.gltf *.glb *.dae *.3ds *.stl *.ply *.abc *.usd *.usda *.usdc *.usdz"),
            ("Blender", "*.blend"),
            ("FBX", "*.fbx"),
            ("OBJ", "*.obj"),
            ("GLTF/GLB", "*.gltf *.glb"),
            ("Collada", "*.dae"),
            ("All Files", "*.*"),
        ]
        filepath = filedialog.askopenfilename(title="Open Scene File",
                                                filetypes=filetypes)
        if not filepath:
            return

        import shutil, tempfile
        tmp_dir = os.path.join(tempfile.gettempdir(), "scene_optimizer")
        os.makedirs(tmp_dir, exist_ok=True)
        tmp_path = os.path.join(tmp_dir, os.path.basename(filepath))

        self._set_status(f"Copying {os.path.basename(filepath)} for Blender access...")
        try:
            shutil.copy2(filepath, tmp_path)
            self.scene_file = tmp_path
        except Exception:
            self.scene_file = filepath

        self.title(f"Scenify — {os.path.basename(filepath)}")
        self._preview_card.set_subtitle(os.path.basename(filepath))
        self.last_extraction = None
        self._set_status(f"Opening {os.path.basename(filepath)}...")
        self._start_analysis()

    def _start_analysis(self):
        self._set_busy(True)
        self._set_status("Analyzing scene with Blender (this may take a few minutes)...")

        def on_progress(msg):
            self.after(0, lambda: self._set_status(msg))

        def on_done(result: BlenderResult):
            self.after(0, lambda: self._on_analysis_done(result))

        self.backend.run_async("analyze_scene",
                                {"input_file": self.scene_file,
                                 "progress_callback": on_progress},
                                done_callback=on_done)

    def _on_analysis_done(self, result: BlenderResult):
        if not result.success:
            self._set_busy(False)
            err = result.error or "Unknown error"
            if result.stderr and result.stderr.strip():
                err += f"\n\nBlender output:\n{result.stderr[:500]}"
            self._set_status(f"Analysis failed: {result.error}")
            messagebox.showerror("Analysis Failed", err)
            return

        self.manifest = result.data
        self._populate_results()
        self._set_busy(False)
        self.btn_extract.set_enabled(True)
        self.btn_lua.set_enabled(True)

        unique = self.manifest['uniqueAssets']
        total = self.manifest['totalInstances']
        ratio = self.manifest['optimizationRatio']
        self._set_status(f"Ready — {unique} unique assets, {total} instances "
                          f"({ratio}% optimization). Rendering preview...")

        def on_preview_done(res: BlenderResult):
            def _update():
                if res.success and res.output_path:
                    self._display_preview(res.output_path)
                    self._set_status(f"Ready — {unique} unique assets, {total} instances "
                                      f"({ratio}% optimization).")
            self.after(0, _update)

        self.backend.run_async("render_preview",
                                {"input_file": self.scene_file,
                                 "progress_callback": lambda _m: None},
                                done_callback=on_preview_done)

    def _populate_results(self):
        if not self.manifest:
            return
        data = self.manifest
        total = data.get("totalInstances", 0)
        unique = data.get("uniqueAssets", 0)
        ratio = data.get("optimizationRatio", 0)

        self.stat_instances.set_value(f"{total:,}", highlight=True)
        self.stat_unique.set_value(f"{unique:,}", highlight=True)
        self.stat_opt.set_value(f"{ratio}%", highlight=True)
        self._asset_card.set_subtitle(f"{unique} assets")

        self._all_assets = data.get("assets", [])

        categories = sorted({a.get("category", "Uncategorized")
                              for a in self._all_assets})
        self.category_menu.config(values=["All"] + categories)

        self._insert_assets(self._all_assets)

    def _insert_assets(self, assets):
        self.asset_tree.delete(*self.asset_tree.get_children())
        for asset in assets:
            name = asset.get("baseName", "Unknown")
            count = asset.get("instanceCount", 0)
            cat = asset.get("category", "—")
            verts = asset.get("vertexCount", 0)
            verts_str = f"{verts:,}" if verts else "—"
            self.asset_tree.insert("", "end", text=name,
                                    values=(f"×{count}", cat, verts_str))

    def _filter_assets(self, *_):
        if not hasattr(self, '_all_assets'):
            return
        search = self.search_var.get().lower()
        category = self.category_var.get()
        filtered = []
        for a in self._all_assets:
            if search and search not in a.get("baseName", "").lower():
                continue
            if category != "All" and a.get("category") != category:
                continue
            filtered.append(a)
        self._insert_assets(filtered)

    def _extract_assets(self):
        if self._is_busy or not self.manifest:
            return

        manifest_path = os.path.join(self.backend.output_dir, "scene_manifest.json")
        os.makedirs(self.backend.output_dir, exist_ok=True)
        with open(manifest_path, 'w') as f:
            json.dump(self.manifest, f)

        mode = self.export_mode.get()
        pivots = self.center_pivots.get()
        mode_label = "single combined GLB" if mode == "single" else "individual GLB files"
        pivot_label = "centered pivots" if pivots else "original pivots"

        self._set_busy(True)
        self._set_status(f"Extracting as {mode_label} ({pivot_label})...")

        def on_progress(msg):
            self.after(0, lambda: self._set_status(msg))

        def on_done(result: BlenderResult):
            self.after(0, lambda: self._on_extraction_done(result))

        self.backend.run_async("extract_assets",
                                {"input_file": self.scene_file,
                                 "progress_callback": on_progress,
                                 "mode": mode,
                                 "center_pivots": pivots},
                                done_callback=on_done)

    def _on_extraction_done(self, result: BlenderResult):
        self._set_busy(False)
        if not result.success:
            self._set_status(f"Extraction failed: {result.error}")
            messagebox.showerror("Extraction Failed", result.error or "Unknown error")
            return

        data = result.data or {}
        self.last_extraction = data
        exported = data.get("exported", 0)
        total = data.get("totalAssets", 0)
        failed = data.get("failed", 0)
        mode = data.get("mode", "single")
        combined = data.get("combinedFile")

        self._set_status(f"Extraction complete — {exported}/{total} assets exported "
                          f"({mode} mode)")

        if mode == "single" and combined:
            msg = (f"Combined export ready:\n{combined}\n\n"
                    f"{exported} assets inside one GLB. Import it into Roblox Studio "
                    f"and place the resulting Model under \"SceneAssets\" in "
                    f"ServerStorage or ReplicatedStorage.")
        else:
            msg = (f"Extracted {exported} GLB files to:\n{result.output_path}")
        if failed > 0:
            msg += f"\n\n{failed} asset(s) failed to export."

        messagebox.showinfo("Extraction Complete", msg)

        if result.output_path and os.path.isdir(result.output_path):
            subprocess.Popen(["open", result.output_path])

    def _generate_lua(self):
        if not self.manifest:
            return

        from lua_generator import generate_lua_script, generate_split_output
        from scene_analyzer import SceneAnalysis, UniqueAsset, AssetInstance

        analysis = SceneAnalysis()
        for asset_data in self.manifest.get("assets", []):
            base_name = asset_data["baseName"]
            unique_asset = UniqueAsset(base_name=base_name)
            unique_asset.category = asset_data.get("category", "Uncategorized")
            bbox = asset_data.get("localBboxCenter")
            if bbox and len(bbox) == 3:
                unique_asset.local_bbox_center = (bbox[0], bbox[1], bbox[2])
            for inst in asset_data.get("instances", []):
                unique_asset.instances.append(AssetInstance(
                    instance_name=inst.get("name", base_name),
                    world_position=tuple(inst.get("position", [0, 0, 0])),
                    world_rotation=tuple(inst.get("rotation", [0, 0, 0])),
                    world_scale=tuple(inst.get("scale", [1, 1, 1])),
                ))
            analysis.assets[base_name] = unique_asset

        analysis.total_instances = self.manifest.get("totalInstances", 0)
        analysis.unique_assets = self.manifest.get("uniqueAssets", 0)
        analysis.optimization_ratio = self.manifest.get("optimizationRatio", 0)

        scene_name = os.path.splitext(os.path.basename(self.scene_file or "Scene"))[0]

        # Single-file Lua (legacy; still written for reference).
        script = generate_lua_script(
            analysis,
            scene_name=scene_name,
            export_results=self.last_extraction,
        )
        legacy_path = os.path.join(self.backend.output_dir,
                                    f"{scene_name}_reconstruct.lua")
        with open(legacy_path, "w") as f:
            f.write(script)

        # Split output: tiny runner + rbxmx data module.
        runner_lua, data_rbxmx = generate_split_output(
            analysis,
            scene_name=scene_name,
            export_results=self.last_extraction,
        )
        runner_path = os.path.join(self.backend.output_dir,
                                    f"{scene_name}_runner.lua")
        rbxmx_path = os.path.join(self.backend.output_dir, "scene_data.rbxmx")
        with open(runner_path, "w") as f:
            f.write(runner_lua)
        with open(rbxmx_path, "w") as f:
            f.write(data_rbxmx)

        self._set_status(
            f"Saved runner → {runner_path}  |  data → {rbxmx_path}"
        )
        subprocess.Popen(["open", self.backend.output_dir])

    # ── Settings ────────────────────────────────────────────

    def _show_settings(self):
        dialog = tk.Toplevel(self)
        dialog.title("Settings")
        dialog.geometry("540x280")
        dialog.configure(bg=C["bg"])
        dialog.transient(self)
        dialog.grab_set()

        tk.Label(dialog, text="Settings", bg=C["bg"], fg=C["text"],
                  font=FONT_TITLE).pack(anchor="w", padx=22, pady=(18, 4))
        tk.Label(dialog, text="Configure Blender and output paths.",
                  bg=C["bg"], fg=C["text_muted"], font=FONT_XS).pack(
                  anchor="w", padx=22, pady=(0, 14))

        def labeled_entry(label: str, initial: str, browse: Optional[Callable] = None):
            frame = tk.Frame(dialog, bg=C["bg"])
            frame.pack(fill="x", padx=22, pady=6)
            tk.Label(frame, text=label, bg=C["bg"], fg=C["text_muted"],
                      font=FONT_XS).pack(anchor="w")
            row = tk.Frame(frame, bg=C["bg"])
            row.pack(fill="x", pady=4)
            var = tk.StringVar(value=initial)
            entry = tk.Entry(row, textvariable=var, bg=C["surface_lo"],
                              fg=C["text"], insertbackground=C["text"],
                              font=FONT_MONO, relief="flat", bd=0,
                              highlightthickness=1,
                              highlightbackground=C["border"],
                              highlightcolor=C["primary"])
            entry.pack(side="left", fill="x", expand=True, ipady=8, ipadx=10)
            if browse:
                def _browse():
                    path = filedialog.askopenfilename()
                    if path:
                        var.set(path)
                FlatButton(row, "Browse", variant="ghost", padx=14, pady=6,
                            command=_browse).pack(side="right", padx=(6, 0))
            return var

        blender_var = labeled_entry("Blender Executable",
                                      self.backend.blender_path or "",
                                      browse=True)
        output_var = labeled_entry("Output Directory",
                                     self.backend.output_dir)

        btn_row = tk.Frame(dialog, bg=C["bg"])
        btn_row.pack(fill="x", padx=22, pady=(14, 18))

        def save_settings():
            if blender_var.get().strip():
                self.backend.blender_path = blender_var.get().strip()
            if output_var.get().strip():
                self.backend.output_dir = output_var.get().strip()
                os.makedirs(self.backend.output_dir, exist_ok=True)

            ok = self.backend.is_available
            self.blender_label.configure(
                text=f"● Blender {'detected' if ok else 'not found'}",
                fg=C["success"] if ok else C["error"])
            dialog.destroy()

        FlatButton(btn_row, "Save", variant="primary",
                    command=save_settings).pack(side="right")
        FlatButton(btn_row, "Cancel", variant="ghost",
                    command=dialog.destroy).pack(side="right", padx=(0, 8))

    def _show_blender_setup(self):
        messagebox.showwarning(
            "Blender Not Found",
            "Blender is required for scene analysis.\n\n"
            "Install Blender from blender.org, or use Settings (⚙) "
            "to point to its executable."
        )

    # ── Helpers ─────────────────────────────────────────────

    def _set_busy(self, busy: bool):
        self._is_busy = busy
        if busy:
            self.progress.pack(side="right", padx=12, pady=8)
            self.progress.start(12)
            self.btn_open.set_enabled(False)
            self.btn_extract.set_enabled(False)
            self.btn_lua.set_enabled(False)
        else:
            self.progress.stop()
            self.progress.pack_forget()
            self.btn_open.set_enabled(True)
            if self.manifest:
                self.btn_extract.set_enabled(True)
                self.btn_lua.set_enabled(True)

    def _set_status(self, msg: str):
        self.status_label.configure(text=msg)


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
