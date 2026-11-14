"""The bot file: a readable YAML list of steps.

    name: alta-empleados
    start_url: https://intranet.example.com/empleados/nuevo
    settings:
      timeout: 20            # seconds to wait for each element
      date_format: "%d/%m/%Y"
      files_dir: null        # where uploads are looked up (default: data file folder)
      on_dialog: accept      # accept | dismiss  (alert/confirm pop-ups)
      settle_timeout: 8      # max seconds to wait for the page to calm down
      busy_selectors: []     # extra CSS selectors of "loading" indicators
    steps:
      - action: click
        target:
          desc: botón "Nuevo"
          find:
            - role: button
              name: Nuevo
            - css: '#btn-nuevo'
      - action: fill
        target: {...}
        value: "{{Nombre}}"
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from webtaskbot import template

ACTIONS: Dict[str, Dict[str, Any]] = {
    # action: required fields
    "goto": {"required": ["url"]},
    "click": {"required": ["target"]},
    "dblclick": {"required": ["target"]},
    "hover": {"required": ["target"]},
    "fill": {"required": ["target", "value"]},
    "type": {"required": ["target", "value"]},
    "select": {"required": ["target"]},
    "check": {"required": ["target"]},
    "uncheck": {"required": ["target"]},
    "upload": {"required": ["target", "files"]},
    "press": {"required": ["key"]},
    "wait_for": {"required": []},
    "assert_text": {"required": ["target", "text"]},
    "extract": {"required": ["target", "column"]},
    "download": {"required": ["target", "save_as"]},
    "back": {"required": []},
    "reload": {"required": []},
    "sleep": {"required": ["seconds"]},
}

DEFAULT_SETTINGS: Dict[str, Any] = {
    "timeout": 20,
    "date_format": template.DEFAULT_DATE_FORMAT,
    "files_dir": None,
    "on_dialog": "accept",
    "settle_timeout": 8,
    "busy_selectors": [],
}


class BotError(Exception):
    pass


@dataclass
class Bot:
    name: str
    start_url: str
    steps: List[Dict[str, Any]]
    settings: Dict[str, Any] = field(default_factory=dict)
    path: Optional[Path] = None

    def setting(self, key: str) -> Any:
        return self.settings.get(key, DEFAULT_SETTINGS.get(key))

    # ------------------------------------------------------------------ io
    @classmethod
    def load(cls, path: Path) -> "Bot":
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise BotError(f"{path}: YAML inválido: {exc}")
        bot = cls(
            name=str(data.get("name") or path.parent.name),
            start_url=str(data.get("start_url") or ""),
            steps=list(data.get("steps") or []),
            settings=dict(data.get("settings") or {}),
            path=path,
        )
        bot.validate()
        return bot

    def save(self, path: Optional[Path] = None) -> Path:
        path = path or self.path
        if path is None:
            raise BotError("no hay ruta para guardar el bot")
        path.parent.mkdir(parents=True, exist_ok=True)
        settings = {k: self.settings.get(k, v) for k, v in DEFAULT_SETTINGS.items()}
        settings.update({k: v for k, v in self.settings.items() if k not in settings})
        doc = {
            "name": self.name,
            "start_url": self.start_url,
            "settings": settings,
            "steps": self.steps,
        }
        text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)
        path.write_text(text, encoding="utf-8")
        self.path = path
        return path

    # ------------------------------------------------------------ checks
    def validate(self) -> None:
        problems = []
        for i, step in enumerate(self.steps, 1):
            if not isinstance(step, dict):
                problems.append(f"paso {i}: debe ser un diccionario")
                continue
            action = step.get("action")
            if action not in ACTIONS:
                problems.append(f"paso {i}: acción desconocida '{action}'")
                continue
            for req in ACTIONS[action]["required"]:
                if step.get(req) in (None, "", []) and not (req == "value" and step.get(req) == ""):
                    problems.append(f"paso {i} ({action}): falta '{req}'")
            if action == "select" and not (step.get("value") or step.get("label")):
                problems.append(f"paso {i} (select): falta 'value' o 'label'")
            if action == "wait_for" and not (step.get("target") or step.get("text") or step.get("url")):
                problems.append(f"paso {i} (wait_for): indica 'target', 'text' o 'url'")
            tgt = step.get("target")
            if tgt is not None and not (isinstance(tgt, dict) and tgt.get("find")):
                problems.append(f"paso {i}: 'target' necesita una lista 'find'")
        if problems:
            raise BotError("el bot tiene errores:\n  " + "\n  ".join(problems))

    def text_fields(self) -> List[str]:
        """Every string in the steps that may contain placeholders."""
        out: List[str] = []
        for step in self.steps:
            for key in ("value", "label", "text", "url", "save_as"):
                v = step.get(key)
                if isinstance(v, str):
                    out.append(v)
                elif isinstance(v, list):
                    out.extend(str(x) for x in v)
            files = step.get("files")
            if isinstance(files, list):
                out.extend(str(f) for f in files)
            elif isinstance(files, str):
                out.append(files)
            tgt = step.get("target") or {}
            for cand in tgt.get("find") or []:
                out.extend(v for v in cand.values() if isinstance(v, str) and "{{" in v)
        return out

    def required_columns(self) -> List[str]:
        cols: List[str] = []
        for text in self.text_fields():
            for c in template.referenced_columns(text):
                if c not in cols:
                    cols.append(c)
        return cols

    def required_secrets(self) -> List[str]:
        out: List[str] = []
        for text in self.text_fields():
            for s in template.referenced_secrets(text):
                if s not in out:
                    out.append(s)
        return out

    def extract_columns(self) -> List[str]:
        return [s["column"] for s in self.steps if s.get("action") == "extract"]


def describe_step(step: Dict[str, Any]) -> str:
    """One readable line per step, for `wtb show` and run logs."""
    a = step.get("action")
    tgt = (step.get("target") or {}).get("desc", "")
    if a == "goto":
        return f"ir a {step.get('url')}"
    if a in ("fill", "type"):
        return f"escribir {step.get('value')!r} en {tgt}"
    if a == "select":
        return f"elegir {step.get('label') or step.get('value')!r} en {tgt}"
    if a in ("check", "uncheck"):
        return f"{'marcar' if a == 'check' else 'desmarcar'} {tgt}"
    if a == "upload":
        return f"subir {step.get('files')} en {tgt}"
    if a == "press":
        return f"pulsar {step.get('key')}" + (f" en {tgt}" if tgt else "")
    if a == "wait_for":
        what = tgt or step.get("text") or step.get("url")
        return f"esperar {what}" + (" (hasta que desaparezca)" if step.get("state") == "hidden" else "")
    if a == "assert_text":
        return f"verificar que {tgt} contiene {step.get('text')!r}"
    if a == "extract":
        return f"guardar texto de {tgt} en columna '{step.get('column')}'"
    if a == "download":
        return f"descargar desde {tgt} a {step.get('save_as')!r}"
    if a == "sleep":
        return f"esperar {step.get('seconds')} s"
    if a in ("click", "dblclick", "hover"):
        verb = {"click": "clic en", "dblclick": "doble clic en", "hover": "pasar el ratón por"}[a]
        return f"{verb} {tgt}"
    return str(a)
