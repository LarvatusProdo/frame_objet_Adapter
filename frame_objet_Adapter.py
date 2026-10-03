"""Éditeur graphique d'objets Python basé sur tkinter.

Architecture
------------
- ``Node`` : description neutre d'une ligne de l'arbre (aucune dépendance GUI).
- ``ObjectAdapter`` : contrat à implémenter pour chaque type d'objet éditable.
- ``DictAdapter`` : implémentation pour ``dict`` (avec ``dict`` et ``list`` imbriqués).
- ``AdapterRegistry`` : choisit automatiquement l'adaptateur selon le type de l'objet.
- ``ObjectEditorApp`` : fenêtre tkinter, indépendante du type édité.

Pour supporter un nouveau type (ex : ``xarray.Dataset``), il suffit d'écrire
un adaptateur et de le déclarer avec ``@registry.register``. La GUI ne change pas.

Usage
-----
    python frame_objet_Adapter.py [fichier.json]

    from frame_objet_Adapter import edit_object
    nouveau = edit_object({"a": 1})   # None si l'utilisateur annule
"""

from __future__ import annotations

import ast
import copy
import json
import os
import re
import sys
import tkinter as tk
import tkinter.font as tkfont
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable

Path = tuple[Any, ...]  # chemin d'accès à un élément, ex : ("db", "ports", 0)


# ---------------------------------------------------------------------------
# Modèle neutre
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Node:
    """Une ligne de l'arbre, indépendante de tkinter."""

    path: Path
    label: str
    display: str  # texte affiché dans la colonne « Valeur »
    type_name: str
    edit_text: str  # texte proposé lors de l'édition
    is_container: bool
    editable: bool
    renamable: bool = False  # la clé peut-elle être modifiée ?
    key_edit_text: str = ""  # texte proposé lors du renommage de la clé
    path_kind: str | None = None  # "dir" / "file" si la valeur ressemble à un chemin


# Début typique d'un chemin : « C:\ », « C:/ », « \\serveur », « / », « ~/ », « ./ », « ../ ».
_PATH_START = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/|~[\\/]?|\.{1,2}[\\/])")


def guess_path_kind(value: Any) -> str | None:
    """Retourne ``"dir"``, ``"file"`` ou None selon que ``value`` ressemble à un chemin.

    Un chemin existant est classé d'après le disque. Sinon, on se fie à sa forme :
    avec une extension c'est un fichier, sans extension un dossier. Une chaîne
    relative sans séparateur (« python ») n'est jamais considérée comme un chemin.
    """
    if not isinstance(value, str) or not value.strip() or "\n" in value:
        return None
    has_separator = "/" in value or "\\" in value
    if not (_PATH_START.match(value) or has_separator):
        return None
    expanded = os.path.expanduser(value)
    if os.path.isdir(expanded):
        return "dir"
    if os.path.isfile(expanded):
        return "file"
    if not _PATH_START.match(value):  # « km/h », « a/b »… : pas assez fiable
        return None
    return "file" if os.path.splitext(value)[1] else "dir"


def parse_literal(text: str) -> Any:
    """Convertit un texte saisi en valeur Python.

    ``42`` -> int, ``3.5`` -> float, ``True`` -> bool, ``None`` -> None,
    ``[1, 2]`` -> list, ``'abc'`` -> str. Un texte non interprétable reste une
    chaîne. Pour forcer une chaîne numérique, saisir ``'42'`` avec les guillemets.
    """
    try:
        return ast.literal_eval(text.strip())
    except (ValueError, SyntaxError):
        return text


# ---------------------------------------------------------------------------
# Contrat des adaptateurs
# ---------------------------------------------------------------------------
class ObjectAdapter(ABC):
    """Interface entre un type d'objet et l'éditeur graphique."""

    file_types: list[tuple[str, str]] = [("Tous les fichiers", "*.*")]

    @classmethod
    @abstractmethod
    def supports(cls, obj: Any) -> bool:
        """Retourne True si cet adaptateur sait gérer ``obj``."""

    @abstractmethod
    def children(self, obj: Any, path: Path) -> list[Node]:
        """Liste les enfants directs du nœud situé à ``path``."""

    @abstractmethod
    def set_value(self, obj: Any, path: Path, raw: str) -> None:
        """Modifie en place la valeur située à ``path``."""

    def can_add(self, obj: Any, path: Path) -> bool:
        return False

    def requires_key(self, obj: Any, path: Path) -> bool:
        return False

    def rename_key(self, obj: Any, path: Path, raw: str) -> Path:
        """Renomme la clé située à ``path`` et retourne le nouveau chemin."""
        raise NotImplementedError("Renommage non supporté pour ce type.")

    def add_item(self, obj: Any, path: Path, key: str | None, raw: str) -> Path:
        """Ajoute un élément dans le conteneur ``path`` et retourne son chemin."""
        raise NotImplementedError("Ajout non supporté pour ce type.")

    def delete_item(self, obj: Any, path: Path) -> None:
        raise NotImplementedError("Suppression non supportée pour ce type.")

    def read(self, filename: str) -> Any:
        raise NotImplementedError("Lecture de fichier non supportée.")

    def write(self, obj: Any, filename: str) -> None:
        raise NotImplementedError("Écriture de fichier non supportée.")


class AdapterRegistry:
    """Associe un objet à l'adaptateur adapté (le dernier enregistré gagne)."""

    def __init__(self) -> None:
        self._adapters: list[type[ObjectAdapter]] = []

    def register(self, adapter_cls: type[ObjectAdapter]) -> type[ObjectAdapter]:
        self._adapters.append(adapter_cls)
        return adapter_cls

    def resolve(self, obj: Any) -> ObjectAdapter:
        for adapter_cls in reversed(self._adapters):
            if adapter_cls.supports(obj):
                return adapter_cls()
        raise TypeError(f"Aucun adaptateur pour le type {type(obj).__name__}.")


registry = AdapterRegistry()


# ---------------------------------------------------------------------------
# Adaptateur dict
# ---------------------------------------------------------------------------
@registry.register
class DictAdapter(ObjectAdapter):
    """Édition d'un ``dict`` (imbrications de ``dict`` et ``list`` incluses)."""

    file_types = [("JSON", "*.json"), ("Tous les fichiers", "*.*")]

    @classmethod
    def supports(cls, obj: Any) -> bool:
        return isinstance(obj, dict)

    # -- lecture ----------------------------------------------------------
    @staticmethod
    def _resolve(obj: Any, path: Path) -> Any:
        current = obj
        for key in path:
            current = current[key]
        return current

    @staticmethod
    def _items(container: Any) -> list[tuple[Any, Any]]:
        if isinstance(container, dict):
            return list(container.items())
        return list(enumerate(container))

    def children(self, obj: Any, path: Path) -> list[Node]:
        container = self._resolve(obj, path)
        in_dict = isinstance(container, dict)
        nodes = []
        for key, value in self._items(container):
            is_container = isinstance(value, (dict, list))
            display = f"({len(value)} élément(s))" if is_container else repr(value)
            nodes.append(
                Node(
                    path=path + (key,),
                    label=str(key),
                    display=display,
                    type_name=type(value).__name__,
                    edit_text=repr(value),
                    is_container=is_container,
                    editable=not is_container,
                    renamable=in_dict,
                    key_edit_text=key if isinstance(key, str) else repr(key),
                    path_kind=guess_path_kind(value),
                )
            )
        return nodes

    # -- écriture ---------------------------------------------------------
    def set_value(self, obj: Any, path: Path, raw: str) -> None:
        if not path:
            raise ValueError("Le nœud racine ne peut pas être remplacé.")
        self._resolve(obj, path[:-1])[path[-1]] = parse_literal(raw)

    def can_add(self, obj: Any, path: Path) -> bool:
        return isinstance(self._resolve(obj, path), (dict, list))

    def requires_key(self, obj: Any, path: Path) -> bool:
        return isinstance(self._resolve(obj, path), dict)

    def rename_key(self, obj: Any, path: Path, raw: str) -> Path:
        """Renomme une clé de dict en conservant l'ordre des éléments.

        Une clé ``str`` reste une ``str`` (le texte saisi est pris tel quel) ;
        une clé d'un autre type (int, tuple…) est interprétée par ``parse_literal``.
        """
        if not path:
            raise ValueError("Le nœud racine ne peut pas être renommé.")
        parent = self._resolve(obj, path[:-1])
        if not isinstance(parent, dict):
            raise TypeError("Seules les clés d'un dict peuvent être renommées.")
        old_key = path[-1]
        new_key = raw if isinstance(old_key, str) else parse_literal(raw)
        if new_key == "":
            raise ValueError("La clé ne peut pas être vide.")
        try:
            hash(new_key)
        except TypeError:
            raise TypeError(f"{new_key!r} ne peut pas servir de clé (non hachable).") from None
        if new_key == old_key:
            return path
        if new_key in parent:
            raise ValueError(f"La clé {new_key!r} existe déjà.")
        items = list(parent.items())
        parent.clear()
        for key, value in items:
            parent[new_key if key == old_key else key] = value
        return path[:-1] + (new_key,)

    def add_item(self, obj: Any, path: Path, key: str | None, raw: str) -> Path:
        container = self._resolve(obj, path)
        value = parse_literal(raw)
        if isinstance(container, dict):
            if not key:
                raise ValueError("Une clé est requise.")
            parsed_key = parse_literal(key)
            if parsed_key in container:
                raise KeyError(f"La clé {parsed_key!r} existe déjà.")
            container[parsed_key] = value
            return path + (parsed_key,)
        container.append(value)
        return path + (len(container) - 1,)

    def delete_item(self, obj: Any, path: Path) -> None:
        if not path:
            raise ValueError("Le nœud racine ne peut pas être supprimé.")
        del self._resolve(obj, path[:-1])[path[-1]]

    # -- fichiers ---------------------------------------------------------
    def read(self, filename: str) -> dict:
        with open(filename, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("Le fichier JSON doit contenir un objet (dict).")
        return data

    def write(self, obj: dict, filename: str) -> None:
        with open(filename, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Interface graphique
# ---------------------------------------------------------------------------
KEY_COLUMN = "#0"
VALUE_COLUMN = "#1"


@dataclass
class _InlineEditor:
    """Champ de saisie superposé à une cellule du Treeview."""

    entry: ttk.Entry
    iid: str
    column: str  # KEY_COLUMN ou VALUE_COLUMN
    node: Node


class ObjectEditorApp(tk.Tk):
    """Fenêtre d'édition générique, pilotée par un ``ObjectAdapter``."""

    UNDO_LIMIT = 50

    # Couleur du texte (et de la pastille) selon le type de la valeur.
    TYPE_COLORS: dict[str, str] = {
        "dict": "#1f5fa8",
        "list": "#2e7d32",
        "tuple": "#00796b",
        "str": "#b03a2e",
        "int": "#b35c00",
        "float": "#8e6a00",
        "bool": "#6a3fa0",
        "NoneType": "#808080",
    }
    DEFAULT_TYPE_COLOR = "#333333"
    # Fond des lignes conteneurs (affichées en gras).
    CONTAINER_BACKGROUNDS: dict[str, str] = {
        "dict": "#e6effa",
        "list": "#e8f5e9",
        "tuple": "#e0f2f1",
    }
    DEFAULT_CONTAINER_BACKGROUND = "#f0f0f0"
    SELECTION_BACKGROUND = "#3a6ea5"
    ICON_SIZE = 12
    PATH_KIND_LABELS = {"dir": "dossier", "file": "fichier"}

    def __init__(
        self,
        obj: Any,
        adapter_registry: AdapterRegistry = registry,
        title: str = "Éditeur d'objet",
    ) -> None:
        super().__init__()
        self.title(title)
        self.geometry("760x500")
        self.minsize(520, 320)

        self._registry = adapter_registry
        self.obj = copy.deepcopy(obj)  # on ne modifie jamais l'original
        self.adapter = adapter_registry.resolve(self.obj)
        self.result: Any | None = None

        self._undo: deque[Any] = deque(maxlen=self.UNDO_LIMIT)
        self._nodes: dict[str, Node] = {}
        self._icons: dict[str, tk.PhotoImage] = {}  # références à conserver (sinon GC)
        self._styled_tags: set[str] = set()
        self._editor: _InlineEditor | None = None
        self._path_buttons: dict[str, ttk.Button] = {}  # iid -> bouton « parcourir »

        self._build_menu()
        self._build_tree()
        self._build_buttons()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.refresh(expand_all=True)

    # -- construction -----------------------------------------------------
    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Ouvrir…", command=self._on_open, accelerator="Ctrl+O")
        file_menu.add_command(label="Enregistrer sous…", command=self._on_save, accelerator="Ctrl+S")
        file_menu.add_separator()
        file_menu.add_command(label="Valider et fermer", command=self._on_validate)
        file_menu.add_command(label="Annuler et fermer", command=self._on_cancel)
        menubar.add_cascade(label="Fichier", menu=file_menu)
        self.config(menu=menubar)
        self.bind("<Control-o>", lambda _e: self._on_open())
        self.bind("<Control-s>", lambda _e: self._on_save())
        self.bind("<Control-z>", lambda _e: self._on_undo())

    def _build_style(self) -> None:
        style = ttk.Style(self)
        default_font = tkfont.nametofont("TkDefaultFont")
        self._bold_font = default_font.copy()
        self._bold_font.configure(weight="bold")
        style.configure("Treeview", rowheight=int(default_font.metrics("linespace") * 1.6))
        style.configure("Treeview.Heading", font=self._bold_font)
        style.configure("Path.TButton", padding=0)
        # Garde la ligne sélectionnée lisible malgré les couleurs des tags.
        style.map(
            "Treeview",
            background=[("selected", self.SELECTION_BACKGROUND)],
            foreground=[("selected", "white")],
        )

    def _build_tree(self) -> None:
        self._build_style()
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        self.tree = ttk.Treeview(frame, columns=("value", "type"), selectmode="browse")
        self.tree.heading("#0", text="Clé")
        self.tree.heading("value", text="Valeur")
        self.tree.heading("type", text="Type")
        self.tree.column("#0", width=220, stretch=False)
        self.tree.column("value", width=380)
        self.tree.column("type", width=90, stretch=False)

        self._scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=self._on_tree_scroll)
        self.tree.pack(side="left", fill="both", expand=True)
        self._scrollbar.pack(side="right", fill="y")

        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-1>", lambda _e: self._finish_edit(commit=True), add=True)
        self.tree.bind("<Configure>", lambda _e: self._place_overlays(), add=True)
        # Ouverture/fermeture d'un nœud, redimensionnement d'une colonne : l'arbre
        # n'est à jour qu'après le traitement de l'événement, d'où after_idle.
        for sequence in ("<<TreeviewOpen>>", "<<TreeviewClose>>", "<B1-Motion>", "<ButtonRelease-1>"):
            self.tree.bind(sequence, lambda _e: self.after_idle(self._place_overlays), add=True)
        self.tree.bind("<Return>", lambda _e: self._edit_selected(VALUE_COLUMN))
        self.tree.bind("<F2>", lambda _e: self._edit_selected(KEY_COLUMN))
        self.tree.bind("<Tab>", lambda _e: self._tab_from_tree(+1))
        self.tree.bind("<Shift-Tab>", lambda _e: self._tab_from_tree(-1))
        self.tree.bind("<Delete>", lambda _e: self._on_delete())

    def _build_buttons(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(4, 8))
        ttk.Button(bar, text="Ajouter", command=self._on_add).pack(side="left")
        ttk.Button(bar, text="Supprimer", command=self._on_delete).pack(side="left", padx=4)
        ttk.Button(bar, text="Annuler l'action", command=self._on_undo).pack(side="left")
        ttk.Button(bar, text="Valider", command=self._on_validate).pack(side="right")
        ttk.Button(bar, text="Annuler", command=self._on_cancel).pack(side="right", padx=4)

    # -- affichage --------------------------------------------------------
    def refresh(
        self,
        expand_all: bool = False,
        select: Path | None = None,
        moved: tuple[Path, Path] | None = None,
    ) -> None:
        """Reconstruit l'arbre en conservant les nœuds ouverts et la sélection.

        ``select`` : chemin à sélectionner (par défaut, la sélection courante).
        ``moved`` : (ancien, nouveau) chemin d'un nœud renommé, pour que lui et
        ses descendants restent ouverts.
        """
        self._finish_edit(commit=False)
        current = self._selected()
        if select is None and current is not None:
            select = current.path
        expanded = {self._nodes[i].path for i in self._walk("") if self.tree.item(i, "open")}
        if moved is not None:
            old, new = moved
            expanded = {new + p[len(old):] if p[: len(old)] == old else p for p in expanded}
        for button in self._path_buttons.values():
            button.destroy()
        self._path_buttons.clear()
        self.tree.delete(*self.tree.get_children())
        self._nodes.clear()
        self._populate("", (), expanded, expand_all)
        iid = self._iid_for(select) if select is not None else None
        if iid is not None:
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.see(iid)
        self.after_idle(self._place_overlays)

    def _populate(self, parent_iid: str, path: Path, expanded: set[Path], expand_all: bool) -> None:
        for node in self.adapter.children(self.obj, path):
            type_label = node.type_name
            if node.path_kind:
                type_label += f" ({self.PATH_KIND_LABELS[node.path_kind]})"
            iid = self.tree.insert(
                parent_iid,
                "end",
                text=" " + node.label,
                image=self._icon(node.type_name),
                values=(node.display, type_label),
                tags=self._tags(node),
                open=expand_all or node.path in expanded,
            )
            self._nodes[iid] = node
            if node.path_kind and node.editable:
                self._path_buttons[iid] = ttk.Button(
                    self.tree,
                    image=self._path_icon(node.path_kind),
                    style="Path.TButton",
                    takefocus=False,
                    command=lambda n=node: self._browse_path(n),
                )
            if node.is_container:
                self._populate(iid, node.path, expanded, expand_all)

    def _path_icon(self, kind: str) -> tk.PhotoImage:
        """Icône dessinée du bouton « parcourir » : dossier ou feuille."""
        name = f"path:{kind}"
        if name not in self._icons:
            icon = tk.PhotoImage(width=16, height=14)
            if kind == "dir":
                icon.put("#c98a00", to=(1, 1, 7, 4))  # onglet
                icon.put("#c98a00", to=(1, 3, 15, 13))  # contour
                icon.put("#f2c14e", to=(2, 4, 14, 12))  # corps
            else:
                icon.put("#7a7a7a", to=(3, 0, 13, 14))  # contour
                icon.put("#ffffff", to=(4, 1, 12, 13))  # page
                for y in (4, 7, 10):
                    icon.put("#9aa7b8", to=(6, y, 11, y + 1))  # lignes de texte
            self._icons[name] = icon
        return self._icons[name]

    def _icon(self, type_name: str) -> tk.PhotoImage:
        """Pastille carrée de la couleur du type (créée une seule fois)."""
        if type_name not in self._icons:
            color = self.TYPE_COLORS.get(type_name, self.DEFAULT_TYPE_COLOR)
            size = self.ICON_SIZE
            icon = tk.PhotoImage(width=size, height=size)
            icon.put(color, to=(2, 2, size - 1, size - 1))
            self._icons[type_name] = icon
        return self._icons[type_name]

    def _tags(self, node: Node) -> tuple[str, ...]:
        """Tags de style d'une ligne, configurés à la première utilisation.

        Le tag ``type:`` ne fixe que la couleur du texte et le tag ``container:``
        que le fond et la police : aucun conflit de priorité entre les deux.
        """
        tags = [f"type:{node.type_name}"]
        if node.is_container:
            tags.append(f"container:{node.type_name}")
        for tag in tags:
            if tag in self._styled_tags:
                continue
            kind, type_name = tag.split(":", 1)
            if kind == "type":
                color = self.TYPE_COLORS.get(type_name, self.DEFAULT_TYPE_COLOR)
                self.tree.tag_configure(tag, foreground=color)
            else:
                background = self.CONTAINER_BACKGROUNDS.get(
                    type_name, self.DEFAULT_CONTAINER_BACKGROUND
                )
                self.tree.tag_configure(tag, background=background, font=self._bold_font)
            self._styled_tags.add(tag)
        return tuple(tags)

    def _walk(self, parent_iid: str):
        for iid in self.tree.get_children(parent_iid):
            yield iid
            yield from self._walk(iid)

    def _selected(self) -> Node | None:
        selection = self.tree.selection()
        return self._nodes.get(selection[0]) if selection else None

    def _iid_for(self, path: Path) -> str | None:
        return next((iid for iid, node in self._nodes.items() if node.path == path), None)

    # -- édition en place -------------------------------------------------
    def _on_double_click(self, event: tk.Event) -> str | None:
        iid = self.tree.identify_row(event.y)
        if not iid or self.tree.identify_region(event.x, event.y) not in ("tree", "cell"):
            return None
        column = KEY_COLUMN if self.tree.identify_column(event.x) == KEY_COLUMN else VALUE_COLUMN
        # "break" empêche le double-clic d'ouvrir/fermer le nœud en plus.
        return "break" if self._start_edit(iid, column) else None

    def _edit_selected(self, column: str) -> str:
        selection = self.tree.selection()
        if selection:
            self._start_edit(selection[0], column)
        return "break"

    def _start_edit(self, iid: str, column: str, text: str | None = None) -> bool:
        """Ouvre un champ de saisie sur la cellule ; False si non éditable."""
        node = self._nodes.get(iid)
        if node is None:
            return False
        if self._editor is not None:
            # Valider la saisie en cours reconstruit l'arbre : les iid changent.
            self._finish_edit(commit=True)
            if self._editor is not None:  # saisie refusée, rouverte pour correction
                return False
            iid = self._iid_for(node.path)
            node = self._nodes.get(iid) if iid is not None else None
            if node is None:
                return False
        if column == KEY_COLUMN and node.renamable:
            initial = node.key_edit_text
        elif column == VALUE_COLUMN and node.editable:
            initial = node.edit_text
        else:
            return False

        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self.tree.see(iid)
        entry = ttk.Entry(self.tree)
        entry.insert(0, initial if text is None else text)
        entry.select_range(0, "end")
        entry.icursor("end")
        entry.bind("<Return>", lambda _e: self._close_edit(commit=True))
        entry.bind("<KP_Enter>", lambda _e: self._close_edit(commit=True))
        entry.bind("<Escape>", lambda _e: self._close_edit(commit=False))
        entry.bind("<Tab>", lambda _e: self._move_edit(+1))
        entry.bind("<Shift-Tab>", lambda _e: self._move_edit(-1))
        entry.bind("<Down>", lambda _e: self._move_edit(+1, same_column=True))
        entry.bind("<Up>", lambda _e: self._move_edit(-1, same_column=True))
        entry.bind("<FocusOut>", lambda _e: self.after_idle(self._on_editor_focus_out))
        self._editor = _InlineEditor(entry, iid, column, node)
        self.update_idletasks()  # bbox n'est fiable qu'une fois l'arbre affiché
        self._place_overlays()
        entry.focus_set()
        return True

    def _place_overlays(self) -> None:
        """(Re)positionne les widgets superposés à l'arbre : boutons « parcourir »
        et champ de saisie (après défilement, redimensionnement, ouverture…)."""
        try:
            self.tree.winfo_exists()
        except tk.TclError:  # appel différé arrivé après la fermeture de la fenêtre
            return
        for iid, button in self._path_buttons.items():
            bbox = self.tree.bbox(iid, VALUE_COLUMN)
            if not bbox:  # ligne masquée ou hors de la zone visible
                button.place_forget()
                continue
            x, y, width, height = bbox
            size = height - 2  # bouton carré
            button.place(x=x + width - size - 1, y=y + 1, width=size, height=size)

        editor = self._editor
        if editor is None:
            return
        bbox = self.tree.bbox(editor.iid, editor.column)
        if not bbox:  # cellule hors de la zone visible
            editor.entry.place_forget()
            return
        x, y, width, height = bbox
        if editor.column == KEY_COLUMN:
            # bbox tient déjà compte de la profondeur ; on saute l'indicateur et la pastille.
            offset = self._indent() + self.ICON_SIZE
            x, width = x + offset, max(width - offset, 40)
        elif editor.iid in self._path_buttons:
            width = max(width - (height - 2) - 2, 40)  # laisse le bouton accessible
        editor.entry.place(x=x, y=y, width=width, height=height)

    def _browse_path(self, node: Node) -> None:
        """Choisit un dossier ou un fichier pour la valeur ``node``.

        Si la cellule est en cours d'édition, le chemin choisi remplace le texte
        du champ (à valider ensuite) ; sinon la valeur est modifiée directement.
        """
        editor = self._editor
        editing = (
            editor is not None and editor.node.path == node.path and editor.column == VALUE_COLUMN
        )
        current = parse_literal(editor.entry.get() if editing else node.edit_text)
        current = current if isinstance(current, str) else ""
        initial = os.path.expanduser(current)
        initial_dir = initial if os.path.isdir(initial) else os.path.dirname(initial)
        if node.path_kind == "dir":
            chosen = filedialog.askdirectory(
                parent=self, initialdir=initial_dir or None, mustexist=False
            )
        else:
            chosen = filedialog.askopenfilename(
                parent=self,
                initialdir=initial_dir or None,
                initialfile=os.path.basename(initial) or None,
            )
        if not chosen:
            return
        if "\\" in current and "/" not in current:  # conserve le style de séparateur
            chosen = chosen.replace("/", "\\")
        text = repr(chosen)
        editor = self._editor  # le champ a pu être fermé pendant le dialogue
        if editing and editor is not None and editor.node.path == node.path:
            editor.entry.delete(0, "end")
            editor.entry.insert(0, text)
            editor.entry.focus_set()
        else:
            self._mutate(lambda: self.adapter.set_value(self.obj, node.path, text))

    def _indent(self) -> int:
        try:
            return int(ttk.Style(self).lookup("Treeview", "indent") or 20)
        except (ValueError, tk.TclError):
            return 20

    def _on_tree_scroll(self, first: str, last: str) -> None:
        self._scrollbar.set(first, last)
        self._place_overlays()

    def _on_editor_focus_out(self) -> None:
        editor = self._editor
        if editor is None:
            return
        try:
            focused = self.focus_get()
        except KeyError:  # widget interne de Tk (menu déroulant…)
            focused = None
        # Fenêtre inactive (Alt+Tab…) : on garde la saisie en cours.
        if focused is not None and focused is not editor.entry:
            self._finish_edit(commit=True)

    def _finish_edit(self, commit: bool) -> Path | None:
        """Ferme le champ de saisie, en appliquant ou non la valeur saisie.

        Retourne le chemin du nœud édité (nouveau chemin s'il a été renommé),
        ou None si aucun champ n'était ouvert ou si la saisie a été refusée.
        """
        editor, self._editor = self._editor, None
        if editor is None:
            return None
        raw = editor.entry.get()
        editor.entry.destroy()
        self.tree.focus_set()
        node = editor.node
        initial = node.key_edit_text if editor.column == KEY_COLUMN else node.edit_text
        if not commit or raw == initial:
            return node.path
        if editor.column == KEY_COLUMN:
            ok = self._mutate(
                lambda: self.adapter.rename_key(self.obj, node.path, raw), origin=node.path
            )
        else:
            ok = self._mutate(lambda: self.adapter.set_value(self.obj, node.path, raw))
        if ok:
            selected = self._selected()  # _mutate sélectionne le nœud (éventuellement renommé)
            return selected.path if selected is not None else node.path
        # Saisie refusée : on rouvre le champ pour correction.
        iid = self._iid_for(node.path)
        if iid is not None:
            self._start_edit(iid, editor.column, text=raw)
        return None

    def _close_edit(self, commit: bool) -> str:
        """Gestionnaire clavier (Entrée / Échap) : ferme le champ sans propager l'événement."""
        self._finish_edit(commit)
        return "break"

    # -- navigation clavier entre cellules -------------------------------
    def _walk_visible(self, parent_iid: str):
        """Lignes affichées, dans l'ordre (les enfants des nœuds fermés sont ignorés)."""
        for iid in self.tree.get_children(parent_iid):
            yield iid
            if self.tree.item(iid, "open"):
                yield from self._walk_visible(iid)

    def _editable_cells(self) -> list[tuple[str, str]]:
        """Cellules éditables visibles, dans l'ordre de lecture : (iid, colonne)."""
        cells = []
        for iid in self._walk_visible(""):
            node = self._nodes[iid]
            if node.renamable:
                cells.append((iid, KEY_COLUMN))
            if node.editable:
                cells.append((iid, VALUE_COLUMN))
        return cells

    def _move_edit(self, step: int, same_column: bool = False) -> str:
        """Valide la saisie puis ouvre la cellule voisine.

        ``step`` : +1 (suivante) ou -1 (précédente). Avec ``same_column``, on
        reste dans la même colonne (flèches haut/bas) et on s'arrête aux bords ;
        sinon on parcourt toutes les cellules (Tab) en revenant au début.
        """
        editor = self._editor
        if editor is None:
            return "break"
        cells = self._editable_cells()
        index = cells.index((editor.iid, editor.column))
        if same_column:
            candidates = [
                cell for cell in (cells[index + step :] if step > 0 else cells[:index][::-1])
                if cell[1] == editor.column
            ]
            target = candidates[0] if candidates else None
        else:
            target = cells[(index + step) % len(cells)]
        if target is None:
            return "break"
        # Les iid changent si la saisie modifie l'objet : on repère la cible par chemin.
        target_path, target_column = self._nodes[target[0]].path, target[1]
        old_path = editor.node.path
        new_path = self._finish_edit(commit=True)
        if new_path is None:  # saisie refusée, le champ est rouvert
            return "break"
        if target_path[: len(old_path)] == old_path:  # cible dans le nœud renommé
            target_path = new_path + target_path[len(old_path) :]
        iid = self._iid_for(target_path)
        if iid is not None:
            self._start_edit(iid, target_column)
        return "break"

    def _tab_from_tree(self, step: int) -> str:
        """Tab depuis l'arbre : ouvre la première (ou dernière) cellule de la ligne."""
        cells = self._editable_cells()
        if not cells:
            return "break"
        selection = self.tree.selection()
        row_cells = [cell for cell in cells if selection and cell[0] == selection[0]]
        if row_cells:
            target = row_cells[0] if step > 0 else row_cells[-1]
        else:
            target = cells[0] if step > 0 else cells[-1]
        self._start_edit(*target)
        return "break"

    # -- mutation avec historique ----------------------------------------
    def _mutate(self, action: Callable[[], Any], origin: Path | None = None) -> bool:
        """Applique ``action`` avec possibilité d'annulation ; False en cas d'erreur.

        Si ``action`` retourne un chemin, celui-ci est sélectionné après mise à
        jour ; avec ``origin``, il est considéré comme le nouveau chemin du nœud
        ``origin`` (renommage).
        """
        snapshot = copy.deepcopy(self.obj)
        try:
            new_path = action()
        except Exception as exc:  # noqa: BLE001 (erreur affichée à l'utilisateur)
            self.obj = snapshot
            self.refresh()
            messagebox.showerror("Erreur", str(exc), parent=self)
            return False
        self._undo.append(snapshot)
        if not isinstance(new_path, tuple):
            new_path = None
        moved = (origin, new_path) if origin is not None and new_path is not None else None
        self.refresh(select=new_path, moved=moved)
        return True

    # -- actions ----------------------------------------------------------

    def _on_add(self) -> None:
        node = self._selected()
        target: Path = () if node is None else (node.path if node.is_container else node.path[:-1])
        if not self.adapter.can_add(self.obj, target):
            messagebox.showinfo("Ajout impossible", "Sélectionnez un conteneur.", parent=self)
            return
        key = None
        if self.adapter.requires_key(self.obj, target):
            key = simpledialog.askstring("Nouvelle clé", "Nom de la clé :", parent=self)
            if not key:
                return
        raw = simpledialog.askstring(
            "Nouvelle valeur", "Valeur (ex : 42, 'texte', [1, 2]) :", parent=self
        )
        if raw is not None:
            self._mutate(lambda: self.adapter.add_item(self.obj, target, key, raw))

    def _on_delete(self) -> None:
        node = self._selected()
        if node is not None:
            self._mutate(lambda: self.adapter.delete_item(self.obj, node.path))

    def _on_undo(self) -> None:
        if self._undo:
            self.obj = self._undo.pop()
            self.refresh()

    def _on_open(self) -> None:
        filename = filedialog.askopenfilename(filetypes=self.adapter.file_types, parent=self)
        if not filename:
            return
        try:
            new_obj = self.adapter.read(filename)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Erreur de lecture", str(exc), parent=self)
            return
        self._undo.append(copy.deepcopy(self.obj))
        self.obj = new_obj
        self.adapter = self._registry.resolve(self.obj)
        self.refresh(expand_all=True)

    def _on_save(self) -> None:
        filename = filedialog.asksaveasfilename(
            filetypes=self.adapter.file_types, defaultextension=".json", parent=self
        )
        if not filename:
            return
        try:
            self.adapter.write(self.obj, filename)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Erreur d'écriture", str(exc), parent=self)

    def _on_validate(self) -> None:
        self.result = self.obj
        self.destroy()

    def _on_cancel(self) -> None:
        self.result = None
        self.destroy()


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------
def edit_object(obj: Any, title: str = "Éditeur d'objet") -> Any | None:
    """Ouvre l'éditeur et retourne l'objet modifié (None si annulation)."""
    app = ObjectEditorApp(obj, title=title)
    app.mainloop()
    return app.result


def main(argv: list[str]) -> None:
    if len(argv) > 1:
        initial = DictAdapter().read(argv[1])
    else:
        initial = {
            "nom": "Projet",
            "version": 1.0,
            "actif": True,
            "tags": ["python", "tkinter"],
            "base": {"hote": "localhost", "port": 5432, "timeout": None},
            "paths": ["C:/Users", "D:/"],
        }
    result = edit_object(initial)
    print("Annulé." if result is None else json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(sys.argv)