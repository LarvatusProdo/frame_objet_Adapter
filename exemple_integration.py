"""Exemple d'intégration de l'éditeur dans une application tkinter existante.

Petit gestionnaire de profils de configuration :

- à gauche, la liste des profils (widget de l'application hôte) ;
- à droite, un ``ObjectEditor`` intégré qui édite le profil sélectionné :
  « Valider » enregistre les modifications dans le profil, « Annuler » les abandonne ;
- le bouton « Éditer dans une fenêtre… » montre l'autre mode d'intégration :
  ``edit_object(..., parent=...)`` ouvre l'éditeur en boîte de dialogue modale.

    python exemple_integration.py
"""

from __future__ import annotations

import copy
import json
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from typing import Any

from frame_objet_Adapter import ObjectEditor, edit_object

PROFILS = {
    "développement": {
        "base": {"hote": "localhost", "port": 5432, "timeout": None},
        "debug": True,
        "dossier_logs": "C:/Users",
    },
    "production": {
        "base": {"hote": "db.exemple.fr", "port": 5432, "timeout": 30},
        "debug": False,
        "dossier_logs": "D:/",
        "miroirs": ["srv1", "srv2"],
    },
}


class GestionnaireProfils(tk.Tk):
    def __init__(self, profils: dict[str, dict]) -> None:
        super().__init__()
        self.title("Gestionnaire de profils")
        self.geometry("980x520")
        self.profils = copy.deepcopy(profils)
        self.courant: str | None = None
        self.modifie = False

        self._construire_menu()

        panneau = ttk.PanedWindow(self, orient="horizontal")
        panneau.pack(fill="both", expand=True, padx=8, pady=8)

        # -- partie propre à l'application hôte ---------------------------
        gauche = ttk.Frame(panneau)
        ttk.Label(gauche, text="Profils").pack(anchor="w")
        self.liste = tk.Listbox(gauche, exportselection=False, width=22)
        self.liste.pack(fill="both", expand=True, pady=4)
        self.liste.bind("<<ListboxSelect>>", self._on_selection)
        ttk.Button(gauche, text="Nouveau profil", command=self._nouveau).pack(fill="x")
        ttk.Button(gauche, text="Éditer dans une fenêtre…", command=self._editer_fenetre).pack(
            fill="x", pady=(4, 0)
        )
        panneau.add(gauche, weight=0)

        # -- l'éditeur, intégré comme n'importe quel widget ----------------
        self.editeur = ObjectEditor(
            panneau, {}, on_validate=self._appliquer, on_cancel=self._retablir
        )
        self.editeur.bind("<<ObjectChanged>>", self._on_modification)
        self.editeur.bind_shortcuts(self)  # Ctrl+O / Ctrl+S / Ctrl+Z sur toute la fenêtre
        panneau.add(self.editeur, weight=1)

        self.statut = ttk.Label(self, relief="sunken", anchor="w", padding=(6, 2))
        self.statut.pack(fill="x", side="bottom")

        self._remplir_liste()
        self._selectionner(next(iter(self.profils)))
        self.protocol("WM_DELETE_WINDOW", self._quitter)

    def _construire_menu(self) -> None:
        barre = tk.Menu(self)
        fichier = tk.Menu(barre, tearoff=False)
        fichier.add_command(label="Afficher les profils (console)", command=self._afficher)
        fichier.add_separator()
        fichier.add_command(label="Quitter", command=self._quitter)
        barre.add_cascade(label="Fichier", menu=fichier)
        # Les actions de l'éditeur s'intègrent au menu de l'application hôte.
        edition = tk.Menu(barre, tearoff=False)
        edition.add_command(label="Annuler", accelerator="Ctrl+Z", command=lambda: self.editeur.undo())
        edition.add_command(label="Importer dans le profil…", accelerator="Ctrl+O",
                            command=lambda: self.editeur.open_file())
        edition.add_command(label="Exporter le profil…", accelerator="Ctrl+S",
                            command=lambda: self.editeur.save_file())
        barre.add_cascade(label="Édition", menu=edition)
        self.config(menu=barre)

    # -- liste des profils --------------------------------------------------
    def _remplir_liste(self) -> None:
        self.liste.delete(0, "end")
        for nom in self.profils:
            self.liste.insert("end", nom)

    def _selectionner(self, nom: str) -> None:
        index = list(self.profils).index(nom)
        self.liste.selection_clear(0, "end")
        self.liste.selection_set(index)
        self.liste.see(index)
        self.courant = nom
        self.editeur.set_object(self.profils[nom])
        self._set_modifie(False)

    def _on_selection(self, _event: tk.Event) -> None:
        selection = self.liste.curselection()
        if not selection:
            return
        nom = self.liste.get(selection[0])
        if nom == self.courant or not self._confirmer_abandon():
            if self.courant is not None:  # on revient sur le profil courant
                self.liste.selection_clear(0, "end")
                self.liste.selection_set(list(self.profils).index(self.courant))
            return
        self._selectionner(nom)

    def _nouveau(self) -> None:
        nom = simpledialog.askstring("Nouveau profil", "Nom du profil :", parent=self)
        if not nom or not self._confirmer_abandon():
            return
        self.profils.setdefault(nom, {})
        self._remplir_liste()
        self._selectionner(nom)

    # -- liaison avec l'éditeur -------------------------------------------
    def _on_modification(self, _event: tk.Event) -> None:
        self._set_modifie(True)

    def _appliquer(self, obj: Any) -> None:
        """Callback « Valider » de l'éditeur."""
        if self.courant is not None:
            self.profils[self.courant] = copy.deepcopy(obj)
            self._set_modifie(False)

    def _retablir(self) -> None:
        """Callback « Annuler » de l'éditeur."""
        if self.courant is not None:
            self._selectionner(self.courant)

    def _set_modifie(self, modifie: bool) -> None:
        self.modifie = modifie
        etat = "modifications non validées" if modifie else "à jour"
        self.statut.config(text=f"Profil « {self.courant} » : {etat}")

    def _confirmer_abandon(self) -> bool:
        if not self.modifie:
            return True
        reponse = messagebox.askyesnocancel(
            "Modifications en cours",
            f"Valider les modifications du profil « {self.courant} » ?",
            parent=self,
        )
        if reponse is None:
            return False
        if reponse:
            self.editeur.validate()
            return not self.modifie  # False si une saisie a été refusée
        return True

    # -- deuxième mode : boîte de dialogue modale -----------------------------
    def _editer_fenetre(self) -> None:
        if self.courant is None or not self._confirmer_abandon():
            return
        resultat = edit_object(
            self.profils[self.courant], title=f"Profil « {self.courant} »", parent=self
        )
        if resultat is not None:
            self.profils[self.courant] = resultat
        self._selectionner(self.courant)

    # -- divers -------------------------------------------------------------
    def _afficher(self) -> None:
        print(json.dumps(self.profils, ensure_ascii=False, indent=2))

    def _quitter(self) -> None:
        if self._confirmer_abandon():
            self.destroy()


if __name__ == "__main__":
    GestionnaireProfils(PROFILS).mainloop()
