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
    python object_editor.py [fichier.json]

    from object_editor import edit_object
    nouveau = edit_object({"a": 1})   # None si l'utilisateur annule
"""

from __future__ import annotations

import ast
import copy
import json
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

    def add_item(self, obj: Any, path: Path, key: str | None, raw: str) -> None:
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

    def add_item(self, obj: Any, path: Path, key: str | None, raw: str) -> None:
        container = self._resolve(obj, path)
        value = parse_literal(raw)
        if isinstance(container, dict):
            if not key:
                raise ValueError("Une clé est requise.")
            parsed_key = parse_literal(key)
            if parsed_key in container:
                raise KeyError(f"La clé {parsed_key!r} existe déjà.")
            container[parsed_key] = value
        else:
            container.append(value)

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

        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.tree.bind("<Double-1>", lambda _e: self._on_edit())
        self.tree.bind("<Return>", lambda _e: self._on_edit())
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
    def refresh(self, expand_all: bool = False) -> None:
        """Reconstruit l'arbre en conservant les nœuds ouverts."""
        expanded = {self._nodes[i].path for i in self._walk("") if self.tree.item(i, "open")}
        self.tree.delete(*self.tree.get_children())
        self._nodes.clear()
        self._populate("", (), expanded, expand_all)

    def _populate(self, parent_iid: str, path: Path, expanded: set[Path], expand_all: bool) -> None:
        for node in self.adapter.children(self.obj, path):
            iid = self.tree.insert(
                parent_iid,
                "end",
                text=" " + node.label,
                image=self._icon(node.type_name),
                values=(node.display, node.type_name),
                tags=self._tags(node),
                open=expand_all or node.path in expanded,
            )
            self._nodes[iid] = node
            if node.is_container:
                self._populate(iid, node.path, expanded, expand_all)

    def _icon(self, type_name: str) -> tk.PhotoImage:
        """Pastille carrée de la couleur du type (créée une seule fois)."""
        if type_name not in self._icons:
            color = self.TYPE_COLORS.get(type_name, self.DEFAULT_TYPE_COLOR)
            icon = tk.PhotoImage(width=12, height=12)
            icon.put(color, to=(2, 2, 11, 11))
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

    # -- mutation avec historique ----------------------------------------
    def _mutate(self, action: Callable[[], None]) -> None:
        snapshot = copy.deepcopy(self.obj)
        try:
            action()
        except Exception as exc:  # noqa: BLE001 (erreur affichée à l'utilisateur)
            self.obj = snapshot
            messagebox.showerror("Erreur", str(exc), parent=self)
        else:
            self._undo.append(snapshot)
        self.refresh()

    # -- actions ----------------------------------------------------------
    def _on_edit(self) -> None:
        node = self._selected()
        if node is None or not node.editable:
            return
        raw = simpledialog.askstring(
            "Modifier la valeur",
            f"{node.label} ({node.type_name})",
            initialvalue=node.edit_text,
            parent=self,
        )
        if raw is not None:
            self._mutate(lambda: self.adapter.set_value(self.obj, node.path, raw))

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
        }
    result = edit_object(initial)
    print("Annulé." if result is None else json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(sys.argv)